#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Automation v2.0 — 本地媒体资产数据库与索引引擎 (Media Inventory Engine)

核心职能:
1. 一次性构建 / 可重建的 SQLite 媒体资产索引数据库 (/volume1/docker/media-automation/data/media_inventory.db)
2. 真实事实源永远为 /volume2/video/**/#整理完成 物理磁盘目录
3. 调用 /opt/bin/ffprobe 极速提取 codec, 分辨率, 码率, 时长与字幕变体
4. 增量单片更新接口 (update_single_video)，杜绝周期性全库盲扫
5. 历史 HEVC 深度质量审计导出 (reports/legacy_hevc_audit.csv)
"""

import os
import sys
import time
import json
import sqlite3
import subprocess
import argparse
import csv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from gate.media_quality import extract_canonical_avid, parse_quality_tags, RESOLUTION_RANKS
except ImportError:
    from media_quality import extract_canonical_avid, parse_quality_tags, RESOLUTION_RANKS

import shutil
FFPROBE_BIN = shutil.which("ffprobe") or ("/opt/bin/ffprobe" if Path("/opt/bin/ffprobe").is_file() else "ffprobe")

try:
    from core.config import load_config
    _cfg = load_config()
    _inv_db = _cfg["paths"].get("inventory_db", "./data/media_inventory.db")
    INVENTORY_DB = Path(_inv_db) if Path(_inv_db).is_absolute() else (REPO_ROOT / _inv_db).resolve()
    DATA_DIR = INVENTORY_DB.parent
    _archive = _cfg["paths"].get("hdd_archive")
    ARCHIVE_ROOTS = [Path(_archive)] if _archive else [Path("/volume2/video/avnook/#整理完成")]
except Exception:
    DATA_DIR = REPO_ROOT / "data"
    INVENTORY_DB = DATA_DIR / "media_inventory.db"
    ARCHIVE_ROOTS = [Path("/volume2/video/avnook/#整理完成")]

REPORTS_DIR = REPO_ROOT / "reports"

def init_inventory_db(db_path: Path = INVENTORY_DB) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_files (
                path TEXT PRIMARY KEY,
                avid TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                duration_sec REAL,
                width INTEGER,
                height INTEGER,
                video_codec TEXT,
                video_bitrate INTEGER,
                has_subtitle INTEGER DEFAULT 0,
                variant TEXT DEFAULT 'NORMAL',
                indexed_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_files_avid ON media_files(avid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_files_codec ON media_files(video_codec)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_files_variant ON media_files(variant)")
    return conn

def probe_video_file(file_path: Path) -> Dict[str, Any]:
    """
    极速调用 ffprobe 提取视频编码与规格
    参数仅请求流关键信息与格式时长码率，避免解包整个视频
    """
    res = {
        "width": 0,
        "height": 0,
        "codec": "H264",
        "duration": 0.0,
        "bitrate": 0
    }
    if not os.path.exists(FFPROBE_BIN):
        return res

    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height",
        "-show_entries", "format=duration,bit_rate",
        "-of", "json",
        str(file_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            streams = data.get("streams", [])
            if streams:
                st = streams[0]
                res["width"] = int(st.get("width", 0) or 0)
                res["height"] = int(st.get("height", 0) or 0)
                res["codec"] = str(st.get("codec_name", "h264")).lower()

            fmt = data.get("format", {})
            if fmt:
                try:
                    res["duration"] = float(fmt.get("duration", 0) or 0.0)
                except Exception:
                    pass
                try:
                    res["bitrate"] = int(fmt.get("bit_rate", 0) or 0)
                except Exception:
                    pass
    except Exception:
        pass

    return res

def inspect_file_record(fpath: Path) -> Optional[Dict[str, Any]]:
    """提取单个视频文件的完整资产记录"""
    try:
        st = fpath.stat()
        sz = st.st_size
        if sz < 150 * 1024 * 1024:  # 忽略 <150MB 样片与非正片
            return None
        mtime = int(st.st_mtime)

        # 番号解析: 优先自身文件名，其次所在父级目录名
        avid = extract_canonical_avid(fpath.name)
        if not avid:
            avid = extract_canonical_avid(fpath.parent.name)
        if not avid:
            return None

        # 视频规格探测
        probe = probe_video_file(fpath)
        qtags = parse_quality_tags(fpath.name, width=probe["width"], height=probe["height"], codec=probe["codec"])

        return {
            "path": str(fpath.resolve()),
            "avid": avid,
            "size_bytes": sz,
            "mtime": mtime,
            "duration_sec": probe["duration"],
            "width": probe["width"],
            "height": probe["height"],
            "video_codec": qtags["codec"],
            "video_bitrate": probe["bitrate"],
            "has_subtitle": 1 if qtags["is_sub"] else 0,
            "variant": qtags["variant"],
            "indexed_at": int(time.time())
        }
    except Exception:
        return None

def build_full_inventory(workers: int = 8, db_path: Path = INVENTORY_DB) -> Dict[str, Any]:
    """
    完整扫描所有归档目录，多线程极速提取资产并入库
    """
    conn = init_inventory_db(db_path)
    print(f"=== Media Automation v2.0 资产库全量建库 ===")
    print(f"目标数据库: {db_path}")
    print(f"并发工作线程: {workers}")

    # 1. 发现候选文件
    t0 = time.time()
    candidates = []
    for aroot in ARCHIVE_ROOTS:
        if not aroot.exists():
            continue
        print(f"正在收集归档目录: {aroot.name} ...")
        for root, dirs, files in os.walk(aroot):
            for f in files:
                if f.startswith("._") or f.startswith("."):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in {".mp4", ".mkv", ".avi", ".ts", ".wmv"}:
                    candidates.append(Path(root) / f)

    t_scan = time.time() - t0
    total_found = len(candidates)
    print(f"发现视频候选文件总数: {total_found} 个 (文件树遍历耗时 {t_scan:.1f}s)")
    if total_found == 0:
        return {}

    # 查重与断点续建: 自动跳过已有 path
    existing_paths = set(r[0] for r in conn.execute("SELECT path FROM media_files").fetchall())
    print(f"当前已有索引记录数: {len(existing_paths)} 个 (支持断点续建，自动跳过)")
    pending_candidates = [fp for fp in candidates if str(fp.resolve()) not in existing_paths]
    total_files = len(pending_candidates)
    print(f"本次待探测与索引文件数: {total_files} 个")

    if total_files == 0:
        print("所有文件均已完成索引，无需探测！")
        conn.close()
        return get_inventory_stats(db_path)

    # 2. 多线程并发探测 + 增量分批持久化 (每 200 条即时 commit，杜绝超时/中断丢进度)
    print(f"开始多线程 ffprobe 探测与规格分析 (每 200 条增量刷盘)...")
    batch_records = []
    total_newly_inserted = 0
    processed = 0
    t_probe_start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(inspect_file_record, fp): fp for fp in pending_candidates}
        for fut in as_completed(futures):
            processed += 1
            rec = fut.result()
            if rec:
                batch_records.append(rec)

            if len(batch_records) >= 200:
                with conn:
                    conn.executemany("""
                        INSERT OR REPLACE INTO media_files (
                            path, avid, size_bytes, mtime, duration_sec, width, height,
                            video_codec, video_bitrate, has_subtitle, variant, indexed_at
                        ) VALUES (
                            :path, :avid, :size_bytes, :mtime, :duration_sec, :width, :height,
                            :video_codec, :video_bitrate, :has_subtitle, :variant, :indexed_at
                        )
                    """, batch_records)
                total_newly_inserted += len(batch_records)
                batch_records.clear()

            if processed % 500 == 0 or processed == total_files:
                elapsed = time.time() - t_probe_start
                rate = processed / max(0.1, elapsed)
                print(f"  进度: [{processed}/{total_files}] ({processed*100//total_files}%) | 累计刷盘: {total_newly_inserted} | 速率: {rate:.1f} 文件/秒", flush=True)

    # 3. 提交末尾残留批次
    if batch_records:
        with conn:
            conn.executemany("""
                INSERT OR REPLACE INTO media_files (
                    path, avid, size_bytes, mtime, duration_sec, width, height,
                    video_codec, video_bitrate, has_subtitle, variant, indexed_at
                ) VALUES (
                    :path, :avid, :size_bytes, :mtime, :duration_sec, :width, :height,
                    :video_codec, :video_bitrate, :has_subtitle, :variant, :indexed_at
                )
            """, batch_records)
        total_newly_inserted += len(batch_records)
        batch_records.clear()

    conn.close()
    t_total = time.time() - t0
    print(f"★ 资产数据库构建完毕！本次新增入库记录: {total_newly_inserted} 条 (总耗时: {t_total:.1f}s)")
    return get_inventory_stats(db_path)

def update_single_video(video_path: Path, db_path: Path = INVENTORY_DB) -> bool:
    """
    单片增量更新接口 (日常调度使用，耗时 < 50ms)
    """
    if not video_path.exists() or not video_path.is_file():
        return False
    rec = inspect_file_record(video_path)
    if not rec:
        return False

    conn = init_inventory_db(db_path)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO media_files (
                path, avid, size_bytes, mtime, duration_sec, width, height,
                video_codec, video_bitrate, has_subtitle, variant, indexed_at
            ) VALUES (
                :path, :avid, :size_bytes, :mtime, :duration_sec, :width, :height,
                :video_codec, :video_bitrate, :has_subtitle, :variant, :indexed_at
            )
        """, rec)
    conn.close()
    return True

def get_inventory_stats(db_path: Path = INVENTORY_DB) -> Dict[str, Any]:
    """输出资产统计报告"""
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    total_files = cur.execute("SELECT count(*) FROM media_files").fetchone()[0]
    distinct_avids = cur.execute("SELECT count(DISTINCT avid) FROM media_files").fetchone()[0]
    total_size_gb = (cur.execute("SELECT sum(size_bytes) FROM media_files").fetchone()[0] or 0) / (1024**3)

    codecs = dict(cur.execute("SELECT video_codec, count(*) FROM media_files GROUP BY video_codec").fetchall())
    resolutions = {}
    for r in cur.execute("SELECT width, height FROM media_files").fetchall():
        w, h = r[0] or 0, r[1] or 0
        if w >= 3800 or h >= 2100:
            resolutions["4K"] = resolutions.get("4K", 0) + 1
        elif w >= 1800 or h >= 1000:
            resolutions["1080P"] = resolutions.get("1080P", 0) + 1
        elif w >= 1200 or h >= 700:
            resolutions["720P"] = resolutions.get("720P", 0) + 1
        else:
            resolutions["SD"] = resolutions.get("SD", 0) + 1

    variants = dict(cur.execute("SELECT variant, count(*) FROM media_files GROUP BY variant ORDER BY count(*) DESC LIMIT 8").fetchall())
    conn.close()

    return {
        "total_files": total_files,
        "distinct_avids": distinct_avids,
        "total_size_tb": total_size_gb / 1024,
        "codecs": codecs,
        "resolutions": resolutions,
        "variants": variants
    }

def export_legacy_hevc_audit(db_path: Path = INVENTORY_DB, csv_path: Path = REPORTS_DIR / "legacy_hevc_audit.csv"):
    """
    审计全库历史重压的 HEVC 影片，导出低于标准码率的候选清单
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        print("错误: 数据库尚未建立。")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 检索所有 HEVC 影片
    rows = cur.execute("""
        SELECT avid, path, size_bytes, width, height, video_bitrate, duration_sec, variant
        FROM media_files
        WHERE video_codec = 'HEVC'
    """).fetchall()

    audit_records = []
    for r in rows:
        sz_mb = r["size_bytes"] / (1024 * 1024)
        br_kbps = r["video_bitrate"] / 1000 if r["video_bitrate"] else 0
        w, h = r["width"] or 0, r["height"] or 0
        is_1080p = (w >= 1800 or h >= 1000)

        # 标记极低体积或极低码率的候选升级项
        is_low_quality_candidate = False
        reason = "正常 HEVC"

        if is_1080p and sz_mb < 1200:
            is_low_quality_candidate = True
            reason = f"1080P 体积极小 ({sz_mb:.0f}MB < 1.2GB)"
        elif is_1080p and br_kbps > 0 and br_kbps < 1500:
            is_low_quality_candidate = True
            reason = f"1080P 码率过低 ({br_kbps:.0f}kbps < 1500kbps)"

        audit_records.append({
            "avid": r["avid"],
            "resolution": f"{w}x{h}",
            "size_mb": round(sz_mb, 1),
            "bitrate_kbps": round(br_kbps, 0),
            "variant": r["variant"],
            "candidate_for_upgrade": "YES" if is_low_quality_candidate else "NO",
            "reason": reason,
            "path": r["path"]
        })

    conn.close()

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "avid", "resolution", "size_mb", "bitrate_kbps", "variant",
            "candidate_for_upgrade", "reason", "path"
        ])
        writer.writeheader()
        writer.writerows(audit_records)

    cand_cnt = sum(1 for a in audit_records if a["candidate_for_upgrade"] == "YES")
    print(f"★ 历史 HEVC 审计完成！全量 HEVC: {len(audit_records)} 部，其中低码率潜力升级候选: {cand_cnt} 部")
    print(f"详细报告已导出至: {csv_path}")

def find_replacement_candidates(db_path: Path = INVENTORY_DB):
    """
    检索同番号多版本中，已存在严格更优版本的旧文件候选 (仅展示，严格人工审核)
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 查有多文件的番号
    avids = [r[0] for r in cur.execute("SELECT avid FROM media_files GROUP BY avid HAVING count(*) > 1").fetchall()]
    print(f"=== 多版本影片替换候选分析 (共 {len(avids)} 个番号拥有多个副本) ===")
    print("安全准则: 仅列出候选，默认不自动删除任何旧片！")

    candidates_to_prune = []
    for a in avids:
        files = cur.execute("SELECT path, size_bytes, width, height, video_codec, variant FROM media_files WHERE avid = ?", (a,)).fetchall()
        # 寻找严格降级文件 (例如同目录下有 4K/UNC/中字，且另一个文件为纯普通画质)
        has_4k = any(f["variant"] and "4K" in f["variant"] for f in files)
        has_unc = any(f["variant"] and "UNC" in f["variant"] for f in files)
        for f in files:
            v = f["variant"] or "NORMAL"
            w, h = f["width"] or 0, f["height"] or 0
            if has_4k and "4K" not in v and (w < 3800 and h < 2100):
                candidates_to_prune.append({
                    "avid": a,
                    "path": f["path"],
                    "size_mb": f["size_bytes"] / (1024*1024),
                    "reason": "同目录下已有 4K 高分辨率完整版本"
                })

    conn.close()
    print(f"可回收旧版本冗余文件数: {len(candidates_to_prune)} 个")
    total_reclaimable_gb = sum(c["size_mb"] for c in candidates_to_prune) / 1024
    print(f"预计若人工确认清理可释放空间: {total_reclaimable_gb:.1f} GB")
    for c in candidates_to_prune[:8]:
        print(f"  - [{c['avid']}] {Path(c['path']).name} ({c['size_mb']:.0f}MB) -> {c['reason']}")

def main():
    parser = argparse.ArgumentParser(description="Media Automation v2.0 资产库管理")
    parser.add_argument("--build", action="store_true", help="构建/更新全量资产数据库")
    parser.add_argument("--rebuild", action="store_true", help="清空并彻底重新构建资产库")
    parser.add_argument("--stats", action="store_true", help="查看资产库总体统计指标")
    parser.add_argument("--audit-hevc", action="store_true", help="导出历史 HEVC 编码质量审计报告")
    parser.add_argument("--replacement-candidates", action="store_true", help="查看多版本旧片可替换候选 (只读)")
    parser.add_argument("--workers", type=int, default=8, help="探测并发线程数 (默认: 8)")
    args = parser.parse_args()

    if args.rebuild:
        if INVENTORY_DB.exists():
            INVENTORY_DB.unlink()
        build_full_inventory(workers=args.workers)
        export_legacy_hevc_audit()
    elif args.build:
        build_full_inventory(workers=args.workers)
        export_legacy_hevc_audit()
    elif args.stats:
        stats = get_inventory_stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    elif args.audit_hevc:
        export_legacy_hevc_audit()
    elif args.replacement_candidates:
        find_replacement_candidates()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
