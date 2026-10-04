#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迅雷全自动自适应流水线主控调度器 (Xunlei Pipeline Orchestrator) — Phase 2B-1
具备：
1. 单实例进程锁 (fcntl.flock)，彻底消除并发冲突
2. 零批量状态污染的 Eligibility 动态准入调度
3. 优先级 + 并发准入上限 (Admission Ceiling) + 空闲槽位回填
4. 高价值断点任务识别与 Resume-Safety Hold 安全隔离保护
5. 迅雷 API 注入前防重查验与 DB 一致性对账补偿
6. 严格闭环生命周期状态机与单片叶子 Emby 增量感知
"""

import os
import sys
import time
import json
import sqlite3
import shutil
import subprocess
import yaml
import re
import urllib.request
import fcntl
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from gate.media_quality import (
        extract_canonical_avid,
        parse_quality_tags,
        evaluate_quality_decision,
        evaluate_quality_decision_multi,
        normalize_btih
    )
    from gate.media_inventory import probe_video_file, update_single_video
except ImportError:
    try:
        from media_quality import (
            extract_canonical_avid,
            parse_quality_tags,
            evaluate_quality_decision,
            evaluate_quality_decision_multi,
            normalize_btih
        )
        from media_inventory import probe_video_file, update_single_video
    except Exception:
        pass

try:
    from core.xunlei_client import XunleiAdapter
    from core.config import load_config as get_config, find_config_path
except ImportError:
    from xunlei_client import XunleiAdapter
    from config import load_config as get_config, find_config_path

_raw_cfg = get_config()
CONFIG_PATH = find_config_path()
_db = _raw_cfg["paths"].get("db_path", "./data/queue.db")
DB_PATH = Path(_db) if Path(_db).is_absolute() else (REPO_ROOT / _db).resolve()
DATA_DIR = DB_PATH.parent
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "orchestrator_state.json"
LOCK_FILE = DATA_DIR / "orchestrator.lock"
TELEMETRY_FILE = DATA_DIR / "scheduler_telemetry.jsonl"

def record_telemetry(event_type, details):
    """
    Phase 4A: 轻量级遥测事件追加写入 (JSONL 格式, 零 DB Schema 修改, 严格无敏感机密)
    """
    try:
        record = {
            "timestamp": int(time.time()),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type,
            **details
        }
        with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

class SingleInstanceLock:
    """
    进程级别单实例文件锁，防止 systemd timer 与手工 CLI 调度并发运行造成竞争
    """
    def __init__(self, lock_path=LOCK_FILE):
        self.lock_path = lock_path
        self.fp = None

    def __enter__(self):
        try:
            self.fp = open(self.lock_path, "w")
            fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.write(str(os.getpid()))
            self.fp.flush()
            return self
        except (BlockingIOError, IOError):
            print("提示: 另一个调度器进程正在运行中，当前进程自动退出以避免并发冲突。")
            sys.exit(0)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fp:
            try:
                fcntl.flock(self.fp, fcntl.LOCK_UN)
                self.fp.close()
            except Exception:
                pass

def load_config():
    return get_config(str(CONFIG_PATH))

def load_internal_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"circuit_broken": False, "last_scrape_at": 0}

def save_internal_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def get_disk_free_gb(path_str):
    try:
        p = Path(path_str)
        if not p.exists():
            return 999.0
        usage = shutil.disk_usage(p)
        return usage.free / (1024 ** 3)
    except Exception:
        return 0.0

def classify_tier(task_row):
    """
    动态计算任务的分层调度优先级 (零 Schema 侵入，依据现有 progress / retry_count / status)
    """
    rc = task_row["retry_count"] or 0
    prog = task_row["progress"] or 0
    st = task_row["status"]
    
    if rc == 0 and st == "pending":
        return 1, "Tier-Fresh"   # 全新种子
    elif prog > 0:
        return 2, "Tier-VIP"     # 高价值有进度断点任务
    elif rc == 1:
        return 3, "Tier-Hot"     # 初次失败黄金重试期
    elif rc == 2:
        return 4, "Tier-Warm"    # 二次重试温区
    elif rc == 3:
        return 5, "Tier-Cold"    # 三次重试冷区
    else:
        return 6, "Tier-Frozen"  # 深度冷冻区 (rc >= 4 或 status == 'stalled')

def triage_and_clean_staging(staging_path_str, archive_path_str, conn):
    """
    在唤醒 JavSP 前对待刮队列进行智能分流与预清洗：
    1. 规范化特殊番号前缀 (如 420ERK-111 -> ERK-111, CPZ69-015 -> CPZ-69015)
    2. 提取番号并在已整理库中比对：
       - 若库内已存在：
         * 若为无码/泄露版 (_UNC, -UC) 或画质显著升级 (体积 >= 现存 1.3 倍)：重命名为多版本直移入库 (Emby 自动折叠)
         * 若为相同冗余 (体积相差 <= 20% 或更小)：安全清理暂存
         * 更新 DB 为 archived
       - 若库内无：放行给 JavSP 刮削
    3. 清理待刮目录下残留的空文件夹
    """
    staging_dir = Path(staging_path_str)
    archive_dir = Path(archive_path_str)
    if not staging_dir.exists() or not archive_dir.exists():
        return

    for entry in list(staging_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        # 规范化特殊番号前缀
        new_name = re.sub(r"^420([A-Za-z]+-\d+)", r"\1", entry.name)
        new_name = re.sub(r"^CPZ69-(\d+)", r"CPZ-69\1", new_name)
        if new_name != entry.name:
            target_new = staging_dir / new_name
            if not target_new.exists():
                try:
                    entry.rename(target_new)
                    entry = target_new
                except Exception:
                    pass

        # 寻找子目录内的主要视频文件
        video_files = []
        for root, dirs, files in os.walk(entry):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in {".mp4", ".mkv", ".avi", ".wmv", ".ts"}:
                    fp = Path(root) / f
                    try:
                        sz = fp.stat().st_size
                        if sz > 200 * 1024 * 1024:
                            video_files.append((fp, sz))
                    except Exception:
                        pass

        if not video_files:
            try:
                shutil.rmtree(entry)
            except Exception:
                pass
            continue

        main_vid, main_sz = max(video_files, key=lambda x: x[1])
        clean_stem = main_vid.stem.split("@")[-1]
        m = re.search(r"([A-Za-z0-9]+-[0-9]+)", clean_stem)
        if not m:
            m = re.search(r"([A-Za-z0-9]+-[0-9]+)", entry.name)
        if not m:
            continue

        avid = m.group(1).upper()
        matches = list(archive_dir.glob(f"*/*{avid}*"))
        if matches:
            exist_folder = matches[0]
            exist_vids = []
            for ef in exist_folder.iterdir():
                if ef.is_file() and ef.suffix.lower() in {".mp4", ".mkv", ".avi", ".ts"}:
                    try:
                        exist_vids.append((ef, ef.stat().st_size))
                    except Exception:
                        pass

            # Media Automation v2.0R 统一质量决策引擎 (多真实版本对比，绝不按体积推定升级)
            new_prof = {}
            exist_profs = []
            try:
                probe_new = probe_video_file(main_vid)
                new_prof = parse_quality_tags(main_vid.name, width=probe_new.get("width", 0), height=probe_new.get("height", 0), codec=probe_new.get("codec", ""))
                for ef, _ in exist_vids:
                    probe_e = probe_video_file(ef)
                    exist_profs.append(parse_quality_tags(ef.name, width=probe_e.get("width", 0), height=probe_e.get("height", 0), codec=probe_e.get("codec", "")))
            except Exception:
                pass

            if new_prof and exist_profs:
                decision, reason = evaluate_quality_decision_multi(exist_profs, new_prof)
            else:
                # ffprobe 失败或信息不足: 严格禁止按体积判断升级，默认作为多版本安全并存 (REVIEW)
                decision = "REVIEW"
                reason = "ffprobe 探测异常或信息不足，绝不按体积推定升级，作为多版本安全并存"

            is_upgrade = (decision == "UPGRADE")
            is_skip = (decision == "SKIP")

            cur = conn.cursor()
            now_ts = int(time.time())

            # 状态更新辅助闭环: 严格限定只更新 status = 'completed' 的唯一匹配任务，严禁触碰 pending/active/retry！
            def mark_completed_task_archived():
                cur.execute("SELECT infohash FROM tasks WHERE avid = ? AND status = 'completed'", (avid,))
                matches = cur.fetchall()
                if not matches:
                    cur.execute("SELECT infohash FROM tasks WHERE title LIKE ? AND status = 'completed'", (f"%{avid}%",))
                    matches = cur.fetchall()

                if len(matches) == 1:
                    target_hash = matches[0][0]
                    cur.execute(
                        "UPDATE tasks SET status = 'archived', archived_at = ? WHERE infohash = ? AND status = 'completed'",
                        (now_ts, target_hash)
                    )
                elif len(matches) > 1:
                    print(f"警告: 番号 [{avid}] 匹配到多条 completed 任务 ({len(matches)}条)，拒绝批量修改，跳过自动写回")

            if is_upgrade:
                if new_prof.get("resolution") in ("4K", "6K"):
                    tag = new_prof["resolution"]
                elif new_prof.get("is_unc"):
                    tag = "UNC"
                elif new_prof.get("is_sub"):
                    tag = "SUB"
                else:
                    tag = "HQ"
                dst_vid_name = f"{avid} - {tag}{main_vid.suffix.lower()}"
                dst_vid_path = exist_folder / dst_vid_name
                try:
                    shutil.move(str(main_vid), str(dst_vid_path))
                    os.chmod(str(dst_vid_path), 0o777)
                    shutil.rmtree(entry, ignore_errors=True)
                    print(f"★ 智能多版本入库: [{avid}] -> {exist_folder.name}/{dst_vid_name} ({reason})")
                    mark_completed_task_archived()
                    conn.commit()
                    try:
                        update_single_video(dst_vid_path)
                    except Exception:
                        pass
                    notify_emby_paths_updated([str(exist_folder)])
                except Exception as e:
                    print(f"多版本移动异常: {e}")
            elif is_skip:
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    print(f"○ 清理重复冗余下载: [{avid}] (已有完全匹配/同等副本在 {exist_folder.name}, {reason})")
                    mark_completed_task_archived()
                    conn.commit()
                except Exception as e:
                    print(f"清理冗余异常: {e}")
            else:
                # REVIEW: 存在取舍冲突，作为多版本并存
                dst_vid_name = f"{avid} - ALT{main_vid.suffix.lower()}"
                dst_vid_path = exist_folder / dst_vid_name
                try:
                    shutil.move(str(main_vid), str(dst_vid_path))
                    os.chmod(str(dst_vid_path), 0o777)
                    shutil.rmtree(entry, ignore_errors=True)
                    print(f"▲ 多版本共存入库: [{avid}] -> {exist_folder.name}/{dst_vid_name} ({reason})")
                    mark_completed_task_archived()
                    conn.commit()
                    try:
                        update_single_video(dst_vid_path)
                    except Exception:
                        pass
                    notify_emby_paths_updated([str(exist_folder)])
                except Exception as e:
                    print(f"多版本移动异常: {e}")

def count_staging_movies(staging_path_str):
    p = Path(staging_path_str)
    if not p.exists():
        return 0
    cnt = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in {".mp4", ".mkv", ".wmv", ".avi", ".ts", ".iso"} and not f.startswith("."):
                fpath = os.path.join(root, f)
                try:
                    if os.path.getsize(fpath) > 232 * 1024 * 1024:
                        cnt += 1
                except Exception:
                    pass
    return cnt

def notify_emby_paths_updated(paths):
    """
    归档入库后主动通过 API 通知 Emby 进行精准局部增量刷新
    1. 强制使用 ProxyHandler({}) 隔离系统代理，防止 502 Bad Gateway
    2. 严格限定仅接受叶子影片目录，绝不传递顶层根目录，防止 Emby 全库重扫风暴
    3. 批量汇聚调用官方标准 POST /emby/Library/Media/Updated (9ms 极速定向刷新)
    4. 异常时安全记录 warning，绝不连锁触发全局 /Library/Refresh
    """
    if not paths:
        return
    emby_url = os.environ.get("EMBY_URL", "http://127.0.0.1:8096")
    emby_key = os.environ.get("EMBY_API_KEY", "0edfd9830aab49d7a51aa367f36c39fa")

    emby_paths = []
    for p in paths:
        sp = str(p)
        # 严格防御性检查: 过滤顶层库根目录，严禁触发全局重扫
        if sp.rstrip("/").endswith("#整理完成") or sp.rstrip("/").endswith("video") or sp.rstrip("/").endswith("avnook") or sp.rstrip("/").endswith("avok"):
            continue
        if sp.startswith("/volume2/video/"):
            emby_paths.append(sp.replace("/volume2/video/", "/video/", 1))

    if not emby_paths:
        return

    payload = {
        "Updates": [{"Path": p, "UpdateType": "Created"} for p in set(emby_paths)]
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"{emby_url}/emby/Library/Media/Updated?api_key={emby_key}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with opener.open(req, timeout=8) as resp:
            if resp.status in (200, 204):
                print(f"⚡ [Emby 即时感知] 成功精准上报 {len(set(emby_paths))} 部影片增量至 Emby。")
                return
    except Exception as e:
        print(f"Emby 局部通知提示 (非致命): {e}")

def is_javsp_running():
    try:
        res = subprocess.run(
            ["sudo", "-n", "docker", "ps", "-q", "-f", "name=javsp-avnook"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        return bool(res.stdout.strip())
    except Exception:
        return False

KNOWN_RELEASE_PREFIXES = {'420', '546', '702', '360', '326', '328', '858', '865', '857', '393', '348', '458', '476', '107', '112'}
AVID_PATTERN = re.compile(r'^[A-Z0-9]+-\d+$', re.IGNORECASE)

def extract_canonical_avid(raw_str: str) -> str:
    """
    严格从目录名或番号字符串中提取规范番号 (Canonical AVID):
    1. 剥离版本与画质后缀: -U, -UC, -C, _UNC, -4K, -CD1, -HD, -FHD
    2. 前置处理特殊复合前缀标准化 (如 CPZ69-015 -> CPZ-69015, 420ERK-111 -> ERK-111)
    3. 校验白名单前缀剥离: 仅当以 KNOWN_RELEASE_PREFIXES 开头且剥离后仍符合标准番号结构才剥离
    4. 统一大写与中划线
    """
    if not raw_str:
        return ""
    clean = raw_str.strip().upper().replace("_", "-")
    clean = re.sub(r'([A-Z0-9]+)-\s+(\d+)', r'\1-\2', clean)
    clean = re.sub(r'-(U|UC|C|4K|CD\d+|HD|FHD|UNC)$', '', clean)
    clean = re.sub(r'-[U|C]+$', '', clean)

    # 前置特殊复合前缀标准化
    clean = re.sub(r'CPZ69-(\d+)', r'CPZ-69\1', clean)
    clean = re.sub(r'^420([A-Z]+-\d+)', r'\1', clean)

    m = re.search(r'([A-Z0-9]+-\d+)', clean)
    if not m:
        return ""
    cand = m.group(1)
    for pfx in KNOWN_RELEASE_PREFIXES:
        if cand.startswith(pfx):
            sub_cand = cand[len(pfx):]
            if AVID_PATTERN.match(sub_cand):
                return sub_cand
    return cand

def sync_archived_with_library(conn, archive_path_str=None):
    """
    扫描实际 #整理完成 目录，采用白名单与规范番号严格对账
    性能与安全保障:
    1. 零 I/O 短路保护: 若 DB 中无 status = 'completed' 的任务，直接 0ms 返回，杜绝机械盘盲扫
    2. 精准叶子通知: 仅将对账匹配到的具体单片叶子目录上报给 Emby (9ms 极速响应)，杜绝全库大扫库
    3. 优先仅检索当前写入池 /volume2/video/avnook/#整理完成
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM tasks WHERE status = 'completed'")
    if cur.fetchone()[0] == 0:
        return 0

    archive_roots = [
        Path("/volume2/video/avnook/#整理完成"),
    ]
    if archive_path_str:
        custom_p = Path(archive_path_str)
        if custom_p not in archive_roots and custom_p.exists():
            archive_roots.append(custom_p)

    library_avid_to_folder = {}
    for aroot in archive_roots:
        if not aroot.exists():
            continue
        for root, dirs, files in os.walk(aroot):
            for d in dirs:
                m = re.search(r'\[([a-zA-Z0-9_-]+)\]', d)
                if m:
                    folder_path = os.path.join(root, d)
                    c = extract_canonical_avid(m.group(1))
                    raw_id = m.group(1).upper().replace('_', '-')
                    if c:
                        library_avid_to_folder[c] = folder_path
                    library_avid_to_folder[raw_id] = folder_path

    cur.execute("SELECT infohash, avid, title FROM tasks WHERE status = 'completed'")
    rows = cur.fetchall()
    now = int(time.time())
    updated_cnt = 0
    matched_leaf_folders = set()

    for r in rows:
        avid = r["avid"] or ""
        title = r["title"] or ""
        c1 = extract_canonical_avid(avid) if avid else ""
        c2 = extract_canonical_avid(title) if title else ""

        hits = set()
        matched_folder = None
        if avid and avid in library_avid_to_folder:
            hits.add(avid)
            matched_folder = library_avid_to_folder[avid]
        if avid and avid.upper() in library_avid_to_folder:
            hits.add(avid.upper())
            matched_folder = library_avid_to_folder[avid.upper()]
        if c1 and c1 in library_avid_to_folder:
            hits.add(c1)
            matched_folder = library_avid_to_folder[c1]
        if c2 and c2 in library_avid_to_folder:
            hits.add(c2)
            matched_folder = library_avid_to_folder[c2]

        base_set = {extract_canonical_avid(x) for x in hits} if hits else set()
        if len(base_set) == 1:
            cur.execute(
                "UPDATE tasks SET status = 'archived', archived_at = ? WHERE infohash = ? AND status = 'completed'",
                (now, r["infohash"])
            )
            updated_cnt += 1
            if matched_folder:
                matched_leaf_folders.add(matched_folder)
        elif len(base_set) > 1:
            print(f"警告: 条目存在歧义匹配，跳过自动归档: {avid} / {title} -> {hits}")

    if updated_cnt > 0:
        conn.commit()
        if matched_leaf_folders:
            for mf in matched_leaf_folders:
                try:
                    for vfile in Path(mf).iterdir():
                        if vfile.is_file() and vfile.suffix.lower() in ('.mp4', '.mkv', '.avi', '.ts', '.wmv'):
                            update_single_video(vfile)
                except Exception:
                    pass
            notify_emby_paths_updated(list(matched_leaf_folders))

    return updated_cnt

def show_dashboard():
    cfg = load_config()
    conn = get_db_connection()
    cur = conn.cursor()
    sync_archived_with_library(conn, cfg["paths"]["hdd_archive"])

    cur.execute("SELECT status, count(*) as cnt FROM tasks GROUP BY status")
    stat_map = {row["status"]: row["cnt"] for row in cur.fetchall()}
    total = cur.execute("SELECT count(*) FROM tasks").fetchone()[0]

    ssd_free = get_disk_free_gb(cfg["paths"]["ssd_download"])
    hdd_free = get_disk_free_gb(cfg["paths"]["hdd_staging"])
    staging_cnt = count_staging_movies(cfg["paths"]["hdd_staging"])
    javsp_active = is_javsp_running()
    state = load_internal_state()

    print("==================================================================")
    print("        NAS 迅雷无人值守持续吞吐流水线监控控制台 (Phase 2B-1)")
    print("==================================================================")
    print(f"【任务池全景统计】(总任务库: {total} 部)")
    print(f"  ● 待下载任务 (pending):   {stat_map.get('pending', 0)} 部")
    print(f"  ▶ 迅雷活跃中 (active):    {stat_map.get('active', 0)} 部")
    print(f"  ⏳ 退避重试中 (retry):     {stat_map.get('retry', 0)} 部")
    print(f"  × 长期冷区池 (stalled):   {stat_map.get('stalled', 0)} 部")
    print(f"  √ 迅雷已完成 (completed): {stat_map.get('completed', 0)} 部")
    print(f"  ★ 已入库整理 (archived):  {stat_map.get('archived', 0)} 部")

    archived_cnt = stat_map.get('archived', 0)
    pct = (archived_cnt / total * 100) if total else 0
    print(f"  ➜ 最终媒体库入库进度:      {archived_cnt} / {total} ({pct:.1f}%)")
    print("------------------------------------------------------------------")
    print("【存储水位与迟滞熔断状态】")
    stop_line = cfg["storage"]["stop_free_gb"]
    resume_line = cfg["storage"]["resume_free_gb"]
    cb_status = "【熔断中：暂停新任务】" if state.get("circuit_broken") else "正常 (允许注水)"
    print(f"  - SSD 高速池 (/volume1):  剩余 {ssd_free:.1f} GB (停止线: {stop_line}G, 恢复线: {resume_line}G)")
    print(f"  - 熔断器当前状态:          {cb_status}")
    print(f"  - 机械盘大仓库 (/volume2): 剩余 {hdd_free:.1f} GB")
    print("------------------------------------------------------------------")
    print("【刮削流水线状态】")
    print(f"  - 重刮就绪队列待刮影片:    {staging_cnt} 部 (触发阈值: >= {cfg['scrape']['batch_count_trigger']} 部)")
    print(f"  - JavSP 刮削容器运行状态:  {'正在刮削中...' if javsp_active else '就绪待命'}")
    print("==================================================================")
    conn.close()

def run_schedule_cycle(dry_run=False, max_add_override=None):
    """
    单轮自适应调度核心逻辑 (Phase 2B-1 Eligibility 升级版)：
    1. 进程级单实例锁保护
    2. SSD 双阈值迟滞熔断检查
    3. 同步迅雷任务状态到 DB (更新 progress, speed)
    4. 死种/占坑让位与阶梯退避处理
    5. 基于 Eligibility 动态准入调度 (P1~P6 优先级 + 并发 Ceiling + 空闲回填)
    6. 搬运完成文件并聚合唤醒 JavSP 刮削与对账
    """
    with SingleInstanceLock():
        now = int(time.time())
        cfg = load_config()
        state = load_internal_state()
        conn = get_db_connection()
        cur = conn.cursor()

        tag = "[DRY-RUN 试运行] " if dry_run else ""
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {tag}开始新一轮自适应调度巡检...")

        # 0. 准备自适应巡检 (可重建式自愈对账在步骤 2 随引擎扫描自动执行)

        # 1. 迟滞双阈值熔断控制
        ssd_free = get_disk_free_gb(cfg["paths"]["ssd_download"])
        stop_gb = cfg["storage"]["stop_free_gb"]
        resume_gb = cfg["storage"]["resume_free_gb"]

        if state.get("circuit_broken"):
            if ssd_free >= resume_gb:
                state["circuit_broken"] = False
                print(f"SSD 空间已释放至 {ssd_free:.1f} GB (>= {resume_gb} GB)，解除熔断，恢复注水。")
            else:
                print(f"SSD 处于熔断保护中 (当前 {ssd_free:.1f} GB < 恢复线 {resume_gb} GB)，本轮禁止添加新任务。")
        else:
            if ssd_free < stop_gb:
                state["circuit_broken"] = True
                print(f"警告: SSD 空间降至 {ssd_free:.1f} GB (< 停止线 {stop_gb} GB)，触发熔断，停止新任务注水！")

        if not dry_run:
            save_internal_state(state)

        # 2. 调用 XunleiAdapter 同步任务
        adapter = XunleiAdapter({
            "sock_path": cfg["xunlei"]["sock_path"],
            "info_file": cfg["xunlei"]["info_file"],
            "pan_auth_token": cfg["xunlei"].get("pan_auth_token", "")
        })

        ok, xunlei_tasks = adapter.list_tasks(limit=200)
        xunlei_active_count = 0
        running_tasks = []
        pending_tasks = []
        total_speed_bytes = 0
        xt_map = {}

        if ok:
            desired_runners = cfg.get("xunlei", {}).get("runner_count", 16)
            if desired_runners and not dry_run:
                adapter.set_runner_count(desired_runners)

            archived_in_xunlei_tids = set()
            for xt in xunlei_tasks:
                tid = xt["id"]
                phase = xt["phase"]
                progress = xt["progress"]
                speed = xt["speed"]
                xt_map[tid] = xt

                if phase == "PHASE_TYPE_RUNNING":
                    running_tasks.append(xt)
                    total_speed_bytes += speed
                    xunlei_active_count += 1
                elif phase == "PHASE_TYPE_PENDING":
                    pending_tasks.append(xt)
                    xunlei_active_count += 1

                # 权威身份提取: 从迅雷返回的 url 中解析 BTIH (40位十六进制)，严禁以 title 为唯一主键
                xt_url = xt.get("url", "")
                m_btih = re.search(r'xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', xt_url, re.IGNORECASE)
                xt_hash = m_btih.group(1).lower() if m_btih else None

                # 优先以 infohash 权威匹配，次选 xunlei_task_id
                if xt_hash:
                    cur.execute("SELECT infohash, progress, last_progress_at, first_active_at, retry_count, status, title FROM tasks WHERE infohash = ?", (xt_hash,))
                else:
                    cur.execute("SELECT infohash, progress, last_progress_at, first_active_at, retry_count, status, title FROM tasks WHERE xunlei_task_id = ?", (tid,))
                row = cur.fetchone()

                if row and not dry_run:
                    h = row["infohash"]
                    # 【无状态自愈对账 (Reconstructible Reconciliation)】:
                    # 若任务在迅雷中处于运行态 (RUNNING/PENDING)，而 DB 状态仍为 pending/retry/stalled (例如上次写库异常或崩溃)，
                    # 无需依赖任何内存或临时文件，直接在此处自愈校准为 active！
                    if row["status"] in ("pending", "retry", "stalled") and phase in ("PHASE_TYPE_RUNNING", "PHASE_TYPE_PENDING"):
                        print(f"⚡ [自愈对账] 发现迅雷引擎已存在在跑任务 [{row['title']}] (ID: {tid}, BTIH: {h})，但 DB 为 {row['status']}，自动校准为 active！")
                        cur.execute("UPDATE tasks SET status = 'active', xunlei_task_id = ?, first_active_at = COALESCE(first_active_at, ?), last_progress_at = COALESCE(last_progress_at, ?) WHERE infohash = ?", (tid, now, now, h))
                    record_telemetry("task_reconciled", {"infohash": h, "title": row["title"], "task_id": tid, "previous_status": row["status"]})

                    last_prog = row["progress"]
                    last_prog_at = row["last_progress_at"] or now
                    first_act_at = row["first_active_at"] or now

                    if progress > last_prog:
                        last_prog_at = now

                    if phase == "PHASE_TYPE_COMPLETE":
                        # 防震荡铁律: 若任务已归档入库 (archived)，绝不重新拉回到 completed，且登记其迅雷任务 ID 待 GC 清理
                        if row["status"] == "archived":
                            archived_in_xunlei_tids.add(tid)
                        elif row["status"] != "completed":
                            cur.execute("UPDATE tasks SET status = 'completed', progress = 100, completed_at = COALESCE(completed_at, ?), speed = 0 WHERE infohash = ?", (now, h))
                            record_telemetry("task_completed", {"infohash": h, "title": xt["name"], "task_id": tid})
                    else:
                        cur.execute("UPDATE tasks SET progress = ?, speed = ?, last_progress_at = ?, first_active_at = ? WHERE infohash = ?", (progress, speed, last_prog_at, first_act_at, h))
            if not dry_run:
                conn.commit()
            total_speed_mb = total_speed_bytes / (1024 * 1024)
            print(f"成功连接迅雷引擎: 运行中 {len(running_tasks)} 个, 排队中 {len(pending_tasks)} 个, 瞬时总带宽: {total_speed_mb:.2f} MB/s ({total_speed_mb*8:.1f} Mbps)")
        else:
            print(f"注意: 无法通过接口读取迅雷列表: {xunlei_tasks}")
            cur.execute("SELECT count(*) FROM tasks WHERE status = 'active'")
            xunlei_active_count = cur.fetchone()[0]
            total_speed_mb = 0.0

        # 3. 死种/停滞任务检测与分级让位机制
        stall_hours = cfg["stall"]["no_progress_hours"]
        stall_sec = int(stall_hours * 3600)
        meta_timeout_sec = int(cfg["stall"].get("metadata_timeout_minutes", 15) * 60)

        cur.execute("SELECT infohash, title, xunlei_task_id, retry_count, first_active_at, last_progress_at, progress FROM tasks WHERE status = 'active'")
        active_db_rows = cur.fetchall()

        if not dry_run:
            for r in active_db_rows:
                act_time = r["first_active_at"] or now
                last_mv = r["last_progress_at"] or act_time
                tid = r["xunlei_task_id"]
                xt_info = xt_map.get(tid, {})
                file_size = xt_info.get("file_size", 1)
                phase = xt_info.get("phase", "")

                is_meta_stuck = (r["progress"] == 0 and file_size == 0 and phase == "PHASE_TYPE_PENDING")
                need_evict = False
                evict_reason = ""

                # 规则 A: 元数据解析快速熔断
                if is_meta_stuck and (now - act_time) > meta_timeout_sec:
                    need_evict = True
                    evict_reason = f"获取元数据超时 ({meta_timeout_sec//60} min 无做种节点)"
                # 规则 B: 普通长时间 0 速度让位
                elif (now - last_mv) > stall_sec and (now - act_time) > stall_sec:
                    need_evict = True
                    evict_reason = f"持续 {stall_hours}h 进度无推进且 0 速度"

                if need_evict:
                    rc = r["retry_count"] + 1
                    if rc == 1:
                        delay_h = cfg["stall"]["retry_hours"][0]
                    elif rc == 2:
                        delay_h = cfg["stall"]["retry_hours"][1]
                    elif rc == 3:
                        delay_h = cfg["stall"]["retry_hours"][2]
                    else:
                        delay_h = cfg["stall"]["cold_retry_days"] * 24

                    next_at = now + int(delay_h * 3600)
                    new_status = "retry" if rc <= 3 else "stalled"
                    print(f"★ 让位退避: [{r['title']}] -> {evict_reason}，移入 {new_status} (下次重试: {delay_h}h 后)")

                    if tid:
                        adapter.delete_task(tid)

                    cur.execute("UPDATE tasks SET status = ?, retry_count = ?, next_retry_at = ?, speed = 0 WHERE infohash = ?", (new_status, rc, next_at, r["infohash"]))
                    record_telemetry("task_evicted", {"infohash": r["infohash"], "title": r["title"], "retry_count": rc, "progress": r["progress"] or 0, "reason": evict_reason, "delay_hours": delay_h, "new_status": new_status})
                    conn.commit()

        # 4. 统计到期 Eligible 规模 (零批量 UPDATE，仅作状态监控)
        cur.execute("SELECT count(*) FROM tasks WHERE status IN ('retry', 'stalled') AND next_retry_at <= ?", (now,))
        due_count = cur.fetchone()[0]
        print(f"调度候选池检测: 当前到期可参与调度任务数: {due_count} 部 (按 Eligibility 动态准入，不提前刷库)")

        # 5. 统计当前在跑任务的真实 Tier 分布 (用于 Admission Ceiling 决策)
        cur.execute("SELECT infohash, title, avid, status, progress, retry_count FROM tasks WHERE status = 'active'")
        current_actives = cur.fetchall()
        active_tier_counts = {
            "Tier-Fresh": 0, "Tier-VIP": 0, "Tier-Hot": 0,
            "Tier-Warm": 0, "Tier-Cold": 0, "Tier-Frozen": 0
        }
        for at in current_actives:
            _, tname = classify_tier(at)
            active_tier_counts[tname] = active_tier_counts.get(tname, 0) + 1

        # 生产力水位与注水配额决策
        low_water = cfg["scheduler"]["low_watermark"]
        target_active = cfg["scheduler"]["target_active"]
        max_add = cfg["scheduler"]["add_per_cycle"]
        if max_add_override:
            max_add = min(max_add, max_add_override)
        delay_s = cfg["scheduler"]["feed_delay_seconds"]
        max_physical = cfg["scheduler"].get("max_physical_active", 45)
        bandwidth_target = cfg["scheduler"].get("bandwidth_target_mb", 50.0)
        slow_threshold_bytes = cfg["scheduler"].get("slow_speed_threshold_kb", 50) * 1024

        productive_slots = 0.0
        for t in running_tasks:
            sp = t.get("speed", 0)
            if sp >= 500 * 1024:
                productive_slots += 1.0
            elif sp >= slow_threshold_bytes:
                productive_slots += 0.5
            else:
                productive_slots += 0.1

        physical_active = len(running_tasks) + len(pending_tasks)
        print(f"生产力水位评估: 物理总任务数 {physical_active}/{max_physical}, 有效生产力槽位 {productive_slots:.1f}/{target_active} (警戒线: {low_water}), 当前总带宽: {total_speed_mb:.2f} MB/s (目标: {bandwidth_target} MB/s)")

        need_feed = (physical_active < max_physical) and (productive_slots < low_water or total_speed_mb < bandwidth_target)

        # 准入上限 (Admission Ceiling) 与单轮注入限制 (Cycle Cap)
        CAPS = {
            "Tier-Fresh": 45, "Tier-VIP": 15, "Tier-Hot": 25,
            "Tier-Warm": 10, "Tier-Cold": 3, "Tier-Frozen": 1
        }
        CYCLE_CAPS = {
            "Tier-Fresh": 8, "Tier-VIP": 4, "Tier-Hot": 5,
            "Tier-Warm": 2, "Tier-Cold": 1, "Tier-Frozen": 1
        }

        if need_feed and not state.get("circuit_broken"):
            needed = min(target_active - int(productive_slots), max_add)
            needed = max(1, needed)
            needed = min(needed, max_physical - physical_active)

            # 查询全部 Eligible 任务 (pending + 到期 retry/stalled)
            cur.execute("""
                SELECT infohash, magnet, title, avid, status, progress, retry_count, next_retry_at, first_active_at, last_progress_at
                FROM tasks
                WHERE status = 'pending'
                   OR (status IN ('retry', 'stalled') AND next_retry_at <= ?)
            """, (now,))
            eligible_tasks = cur.fetchall()

            tier_buckets = {
                "Tier-Fresh": [], "Tier-VIP": [], "Tier-Hot": [],
                "Tier-Warm": [], "Tier-Cold": [], "Tier-Frozen": []
            }
            for et in eligible_tasks:
                _, tname = classify_tier(et)
                tier_buckets[tname].append(et)

            candidates = []
            traces = []

            for tname in ["Tier-Fresh", "Tier-VIP", "Tier-Hot", "Tier-Warm", "Tier-Cold", "Tier-Frozen"]:
                if len(candidates) >= needed:
                    break

                current_in_engine = active_tier_counts.get(tname, 0)
                ceiling = CAPS[tname]
                cycle_cap = CYCLE_CAPS[tname]

                # 准入上限判定：若在跑任务已达 Ceiling，严格停止新增该 Tier
                available_slots = max(0, ceiling - current_in_engine)
                allow_in_cycle = min(cycle_cap, available_slots, needed - len(candidates))

                bucket = tier_buckets[tname]

                # VIP 优先排序但暂时实行 Resume-Safety Hold 保护
                if tname == "Tier-VIP":
                    bucket.sort(key=lambda x: x["progress"] or 0, reverse=True)
                    for p in bucket:
                        print(f"  [Resume-Safety Hold] 发现高价值断点任务 [{p['title']}] (进度: {p['progress']}%)，处于安全保护期，暂缓自动调度。")
                    continue  # 不自动注入，等待断点目录验证专项后开放

                if tname == "Tier-Fresh":
                    bucket.sort(key=lambda x: x["rowid"] if "rowid" in x.keys() else 0)
                else:
                    bucket.sort(key=lambda x: (x["retry_count"] or 0, x["next_retry_at"] or 0))

                picked = bucket[:allow_in_cycle]
                for p in picked:
                    candidates.append(p)
                    traces.append((tname, p, f"在跑={current_in_engine}/{ceiling} (Ceiling限制), 本轮限额={allow_in_cycle}"))

            if candidates:
                action_str = "试运行拟挑选" if dry_run else "正在注入"
                print(f"自适应准入决策：{action_str} {len(candidates)} 个任务 (目标需求 {needed} 个，允许空闲回填，不强塞死种)...")
                for idx, (tname, c, reason) in enumerate(traces, 1):
                    print(f"  [{idx}/{len(candidates)}] {tname:11s} | rc={c['retry_count']} | prog={c['progress']:2d}% | 原状态={c['status']:7s} | {c['title'][:35]} | ({reason})")

                if not dry_run:
                    for idx, c in enumerate(candidates, 1):
                        # 权威防重查验: 以 infohash (BTIH) 为唯一权威匹配键，严禁仅按 title 匹配
                        already_in_xunlei = False
                        existing_tid = None
                        for xt in xunlei_tasks:
                            xt_url = xt.get("url", "")
                            m_h = re.search(r'xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', xt_url, re.IGNORECASE)
                            if m_h and m_h.group(1).lower() == c["infohash"].lower():
                                already_in_xunlei = True
                                existing_tid = xt["id"]
                                break

                        if already_in_xunlei and existing_tid:
                            print(f"  [防重对账] 迅雷引擎中已存在同 BTIH 任务 [{c['title']}] (ID: {existing_tid}, BTIH: {c['infohash']})，直接补录 DB 为 active！")
                            cur.execute("UPDATE tasks SET status = 'active', xunlei_task_id = ?, first_active_at = COALESCE(first_active_at, ?), last_progress_at = COALESCE(last_progress_at, ?) WHERE infohash = ?", (existing_tid, now, now, c["infohash"]))
                            conn.commit()
                            continue

                        succ, tid, msg = adapter.add_magnet(c["magnet"], name=c["title"])
                        if succ:
                            print(f"  [{idx}/{len(candidates)}] 成功注入: {c['title']} (Task ID: {tid})")
                            try:
                                cur.execute("UPDATE tasks SET status = 'active', xunlei_task_id = ?, first_active_at = ?, last_progress_at = ? WHERE infohash = ?", (tid, now, now, c["infohash"]))
                                record_telemetry("task_injected", {"infohash": c["infohash"], "title": c["title"], "avid": c["avid"] or "", "tier": tname, "retry_count": c["retry_count"] or 0, "progress": c["progress"] or 0, "task_id": tid})
                                conn.commit()
                            except Exception as db_err:
                                print(f"  [DB写异常] 迅雷任务已创建 (Task ID: {tid}) 但数据库更新失败: {db_err} (将在下一周期入口基于 BTIH 自动无损修复)")
                        else:
                            print(f"  [{idx}/{len(candidates)}] 注入失败 ({c['title']}): {msg}")
                            if "checkAuth" in str(msg) or "token" in str(msg).lower():
                                print(f"  [XunleiAdapter] 鉴权异常已自动尝试本地续期: {msg}")
                                break
                        if idx < len(candidates):
                            time.sleep(delay_s)
            else:
                print("本轮准入评估结束：在当前 Ceiling 约束下，无更高优先级候选任务，允许槽位部分空闲。")
        else:
            if state.get("circuit_broken"):
                print("当前空间熔断中，跳过注水。")
            else:
                print("当前带宽充足或已达最大物理并发上限，暂不增注。")

        # 5.5 迅雷任务列表垃圾回收 (仅清理已入库 archived、retry、stalled 记录)
        if not dry_run:
            cur.execute("SELECT xunlei_task_id FROM tasks WHERE status IN ('archived', 'retry', 'stalled') AND xunlei_task_id IS NOT NULL AND xunlei_task_id != '' LIMIT 50")
            cleanup_tids = set(r["xunlei_task_id"] for r in cur.fetchall())
            cleanup_tids.update(archived_in_xunlei_tids)
            cleanup_list = list(cleanup_tids)
            if cleanup_list:
                succ, _ = adapter.delete_task(cleanup_list)
                if succ:
                    placeholders = ",".join(["?"] * len(cleanup_list))
                    cur.execute(f"UPDATE tasks SET xunlei_task_id = '' WHERE xunlei_task_id IN ({placeholders})", cleanup_list)
                    conn.commit()
                    print(f"已清理迅雷任务列表中 {len(cleanup_list)} 个已归档/让位任务的旧记录。")

        # 6. 安全搬移已完成文件 (SSD -> HDD)
        if not dry_run:
            print("执行已完成影片搬运检测 (SSD -> HDD)...")
            try:
                subprocess.run(
                    ["/usr/bin/python3", str(BASE_DIR / "xunlei_auto_mover.py")],
                    check=True
                )
            except Exception as e:
                print(f"搬运脚本执行报错: {e}")

        # 6.5 待刮就绪队列智能去重分流与预清洗
        if not dry_run:
            try:
                triage_and_clean_staging(cfg["paths"]["hdd_staging"], cfg["paths"]["hdd_archive"], conn)
            except Exception as te:
                print(f"待刮分流预处理异常: {te}")

        # 7. 刮削聚合触发
        if not dry_run:
            staging_cnt = count_staging_movies(cfg["paths"]["hdd_staging"])
            batch_trigger = cfg["scrape"]["batch_count_trigger"]
            max_wait_sec = cfg["scrape"]["max_wait_minutes"] * 60
            time_since_scrape = now - state.get("last_scrape_at", 0)

            if staging_cnt > 0:
                should_scrape = (staging_cnt >= batch_trigger) or (time_since_scrape >= max_wait_sec)
                if should_scrape:
                    if not is_javsp_running():
                        print(f"满足刮削聚合条件 (待处理: {staging_cnt} 部, 距上次刮削: {time_since_scrape//60} min)，正在唤醒 JavSP 刮削容器...")
                        try:
                            subprocess.run(["sudo", "-n", "docker", "start", cfg["scrape"]["docker_container"]], check=True)
                            state["last_scrape_at"] = now
                            save_internal_state(state)
                            print("JavSP 刮削容器启动成功。")
                        except Exception as e:
                            print(f"启动 JavSP 容器失败: {e}")
                    else:
                        print(f"JavSP 刮削容器正在运行中 (待刮: {staging_cnt} 部)...")
                else:
                    print(f"重刮队列积累中 (当前 {staging_cnt} 部 < 触发线 {batch_trigger} 部，且距上次 {time_since_scrape//60}min < {cfg['scrape']['max_wait_minutes']}min)，暂不打扰 Docker。")
            else:
                print("重刮队列暂无新文件。")

        # 8. 同步整理完成库
        if not dry_run:
            sync_archived_with_library(conn, cfg["paths"]["hdd_archive"])

        conn.close()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {tag}巡检调度轮询结束。")

def set_token_command(new_token):
    cfg = load_config()
    cfg["xunlei"]["pan_auth_token"] = new_token.strip()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    print("成功更新 config.yaml 中的 pan_auth_token！")

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("用法:")
        print("  python3 xunlei_orchestrator.py --status                 # 查看流水线全景控制台")
        print("  python3 xunlei_orchestrator.py --cycle                  # 手动执行一轮自适应调度")
        print("  python3 xunlei_orchestrator.py --cycle --limit <N>      # Canary 限制单轮最多注水 N 个任务")
        print("  python3 xunlei_orchestrator.py --dry-run-schedule       # 纯只读试运行准入决策对比")
        print("  python3 xunlei_orchestrator.py --set-token <token>      # 更新迅雷 pan-auth token")
        return

    cmd = sys.argv[1]
    if cmd == "--status":
        show_dashboard()
    elif cmd == "--cycle":
        limit = None
        if len(sys.argv) > 3 and sys.argv[2] == "--limit":
            try:
                limit = int(sys.argv[3])
            except ValueError:
                pass
        run_schedule_cycle(dry_run=False, max_add_override=limit)
    elif cmd == "--dry-run-schedule":
        limit = None
        if len(sys.argv) > 3 and sys.argv[2] == "--limit":
            try:
                limit = int(sys.argv[3])
            except ValueError:
                pass
        run_schedule_cycle(dry_run=True, max_add_override=limit)
    elif cmd == "--set-token":
        if len(sys.argv) > 2:
            set_token_command(sys.argv[2])
        else:
            print("错误: 请提供 token 字符串")
    else:
        print(f"未知指令: {cmd}")

if __name__ == "__main__":
    main()
