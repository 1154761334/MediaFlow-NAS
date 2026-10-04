#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Automation v2.0R — 下载前智能过筛门禁 (Pre-download Media Gate Correctness Hardened)

核心职能:
1. 输入磁力链接文本清单 (如 javbus-magnets.txt)
2. 默认 DRY RUN 试运行，严防误操作；只有显式带 --commit 时才写入 queue.db
3. 严格基于 media_inventory.db 真实多版本资产列表 (list[profile]) 与 queue.db 进行比对
4. 严格执行 BTIH 规范化 (normalize_btih，支持 32位 Base32 与 40位 Hex 统一)
5. 缺失资产库时 Fail Closed 拒绝盲跑，彻底杜绝 Ingest 阶段对机械盘的盲目递归扫描
6. 判定结果五分流:
   - HASH_DUP: BTIH 哈希已存在 -> 剔除
   - SKIP: 本地已有同等或更好版本 (包括保护现有 HEVC) -> 剔除，节约带宽与存储
   - REVIEW: 缺乏明确提升凭证或番号无法解析 -> 默认不下载
   - NEW: 本地完全未收录 -> 放行导入
   - UPGRADE: 本地已有但属于明确 4K/无码/中字重大升级 -> 放行导入
7. 输出审计报告: /volume1/docker/media-automation/reports/ingest-YYYYMMDD-HHMM.json
"""

import os
import sys
import time
import json
import sqlite3
import argparse
import urllib.parse
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

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
        evaluate_quality_decision_multi,
        normalize_btih,
        RESOLUTION_RANKS
    )
except ImportError:
    from media_quality import (
        extract_canonical_avid,
        parse_quality_tags,
        evaluate_quality_decision_multi,
        normalize_btih,
        RESOLUTION_RANKS
    )

try:
    from core.config import load_config
    _cfg = load_config()
    _inv_db = _cfg["paths"].get("inventory_db", "./data/media_inventory.db")
    INVENTORY_DB = Path(_inv_db) if Path(_inv_db).is_absolute() else (REPO_ROOT / _inv_db).resolve()
    _q_db = _cfg["paths"].get("db_path", "./data/queue.db")
    QUEUE_DB = Path(_q_db) if Path(_q_db).is_absolute() else (REPO_ROOT / _q_db).resolve()
except Exception:
    INVENTORY_DB = REPO_ROOT / "data" / "media_inventory.db"
    QUEUE_DB = Path("/volume1/docker/xunlei/queue.db")

REPORTS_DIR = REPO_ROOT / "reports"

def load_inventory_avid_profiles(db_path: Path = INVENTORY_DB) -> Dict[str, List[Dict[str, Any]]]:
    """
    加载资产库中每个番号的真实存在视频版本列表 List[Dict[str, Any]]
    彻底废除虚构合并超级版本，保留每个实际文件的物理属性
    """
    if not db_path.exists():
        print(f"错误: 媒体资产库不存在 ({db_path})！请先运行 python3 media_inventory.py --build 构建索引。")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT avid, width, height, video_codec, has_subtitle, variant, size_bytes
        FROM media_files
    """).fetchall()
    conn.close()

    if not rows:
        print(f"错误: 媒体资产库 ({db_path}) 记录为空！请先运行 python3 media_inventory.py --build。")
        sys.exit(1)

    profiles: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        avid = r["avid"]
        w = r["width"] or 0
        h = r["height"] or 0
        codec = r["video_codec"] or "H264"
        sub = bool(r["has_subtitle"])
        var = r["variant"] or "NORMAL"
        unc = "UNC" in var

        res = "SD"
        if w >= 5000 or h >= 2800:
            res = "6K"
        elif w >= 3800 or h >= 2100:
            res = "4K"
        elif w >= 1800 or h >= 1000:
            res = "1080P"
        elif w >= 1200 or h >= 700:
            res = "720P"

        rank = RESOLUTION_RANKS.get(res, 1)

        prof = {
            "resolution": res,
            "rank": rank,
            "is_unc": unc,
            "is_sub": sub,
            "codec": codec,
            "variant": var,
            "size_bytes": r["size_bytes"]
        }
        profiles.setdefault(avid, []).append(prof)

    return profiles

def parse_magnet_entry(magnet_str: str) -> Dict[str, Any]:
    """解析单条磁力链接并执行 BTIH 规范化"""
    m_btih = re.search(r"btih:([a-zA-Z0-9]+)", magnet_str, re.I)
    raw_btih = m_btih.group(1) if m_btih else ""
    btih = normalize_btih(raw_btih)

    m_dn = re.search(r"dn=([^&]+)", magnet_str)
    dn = urllib.parse.unquote(m_dn.group(1)) if m_dn else f"Task_{btih[:8]}"
    avid = extract_canonical_avid(dn)
    qtags = parse_quality_tags(dn)

    return {
        "magnet": magnet_str,
        "raw_btih": raw_btih,
        "btih": btih,
        "dn": dn,
        "avid": avid,
        "candidate_profile": qtags
    }

def run_ingest_gate(input_file: Path, commit: bool = False, custom_report: Optional[Path] = None) -> Dict[str, Any]:
    if not input_file.exists():
        print(f"错误: 输入文件不存在: {input_file}")
        sys.exit(1)

    lines = [l.strip() for l in input_file.read_text(encoding="utf-8").splitlines() if l.strip().startswith("magnet:")]
    print("=" * 70)
    print("        Media Automation v2.0R — Pre-download Media Gate")
    print("=" * 70)
    print(f"输入源文件: {input_file.name}")
    print(f"有效磁力链接数: {len(lines)} 条")
    print(f"运行模式: {'【COMMIT 正式导入 queue.db】' if commit else '【DRY-RUN 试运行评估 (默认只读)】'}")

    # 1. 查询 queue.db 已有 BTIH (规范化为 40位 Hex)
    db_btih = set()
    if QUEUE_DB.exists():
        qconn = sqlite3.connect(str(QUEUE_DB))
        for r in qconn.execute("SELECT infohash FROM tasks").fetchall():
            norm_h = normalize_btih(r[0])
            if norm_h:
                db_btih.add(norm_h)
        qconn.close()

    # 2. 查询资产库真实多版本列表 (Fail Closed 机制，零 HDD 盲扫)
    inventory_profiles = load_inventory_avid_profiles(INVENTORY_DB)
    print(f"已加载本地媒体资产库索引番号: {len(inventory_profiles)} 部")
    print("-" * 70)

    results = {
        "HASH_DUP": [],
        "NEW": [],
        "UPGRADE": [],
        "SKIP": [],
        "REVIEW": []
    }

    for idx, line in enumerate(lines, 1):
        item = parse_magnet_entry(line)
        btih = item["btih"]
        avid = item["avid"]
        cand_prof = item["candidate_profile"]

        if not btih:
            results["REVIEW"].append({
                "item": item,
                "reason": "缺少有效或无法解码的 BTIH 哈希"
            })
            continue

        if btih in db_btih:
            results["HASH_DUP"].append({
                "item": item,
                "reason": "BTIH 在 queue.db 中已存在相同任务 (40位规范化匹配)"
            })
            continue

        if not avid:
            results["REVIEW"].append({
                "item": item,
                "reason": f"无法提取标准规范番号 (原始名称: {item['dn'][:35]})"
            })
            continue

        # 查询资产库真实列表多版本仲裁
        local_profs = inventory_profiles.get(avid)
        decision, reason = evaluate_quality_decision_multi(local_profs, cand_prof)

        results[decision].append({
            "item": item,
            "decision": decision,
            "reason": reason,
            "local_profiles": local_profs,
            "candidate_profile": cand_prof
        })

    # 输出统计报告
    hash_dup_cnt = len(results["HASH_DUP"])
    new_cnt = len(results["NEW"])
    upgrade_cnt = len(results["UPGRADE"])
    skip_cnt = len(results["SKIP"])
    review_cnt = len(results["REVIEW"])
    will_import_cnt = new_cnt + upgrade_cnt
    will_not_import_cnt = hash_dup_cnt + skip_cnt + review_cnt
    definite_redundant = hash_dup_cnt + skip_cnt

    print(f"总计评估任务:            {len(lines):4d} 部")
    print(f"  ● HASH_DUP (哈希重复):    {hash_dup_cnt:4d} 部 (精确 BTIH 过滤)")
    print(f"  ★ NEW (纯新影片):         {new_cnt:4d} 部 (放行下载)")
    print(f"  ★ UPGRADE (高规格升级):   {upgrade_cnt:4d} 部 (放行下载)")
    print(f"  ○ SKIP (同质冗余/保护):   {skip_cnt:4d} 部 (拦截，节约带宽)")
    print(f"  ▲ REVIEW (信息不足/待查): {review_cnt:4d} 部 (暂缓，默认不下载)")
    print("-" * 70)
    print(f"拟准入下载总数 (NEW+UPGRADE): {will_import_cnt:4d} 部 (待写入 queue.db)")
    print(f"未准入总数 (拦截与暂缓):        {will_not_import_cnt:4d} 部 (其中 {definite_redundant} 部明确重复/同质冗余已剔除，{review_cnt} 部因证据不足暂缓，有效避免潜在的大量无效流量)")
    print("=" * 70)

    # 打印典型明细样本
    if results["UPGRADE"]:
        print("\n【高价值规格升级任务样本】:")
        for u in results["UPGRADE"][:6]:
            it = u["item"]
            print(f"  * [{it['avid']:<10}] {it['dn'][:38]:<38} -> {u['reason']}")

    if results["NEW"]:
        print("\n【纯新片放行任务样本】:")
        for n in results["NEW"][:4]:
            it = n["item"]
            print(f"  * [{it['avid']:<10}] {it['dn'][:38]}")

    if results["SKIP"]:
        print("\n【同质冗余拦截任务样本 (节约带宽)】:")
        for s in results["SKIP"][:4]:
            it = s["item"]
            print(f"  * [{it['avid']:<10}] {it['dn'][:38]:<38} -> {s['reason']}")

    # 导出 JSON 报告
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_ts = time.strftime("%Y%m%d-%H%M%S")
    report_file = custom_report or (REPORTS_DIR / f"ingest-{report_ts}.json")

    report_payload = {
        "timestamp": report_ts,
        "input_file": str(input_file),
        "total_input": len(lines),
        "will_import": will_import_cnt,
        "will_not_import": will_not_import_cnt,
        "counts": {
            "HASH_DUP": hash_dup_cnt,
            "NEW": new_cnt,
            "UPGRADE": upgrade_cnt,
            "SKIP": skip_cnt,
            "REVIEW": review_cnt
        },
        "details": results
    }
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, ensure_ascii=False, indent=2)
    print(f"\n结构化审计报告已保存至: {report_file}")

    # 若为 commit 模式，安全持久化至 queue.db
    if commit:
        to_import = results["NEW"] + results["UPGRADE"]
        if not to_import:
            print("无可准入任务需要导入。")
            return report_payload

        now = int(time.time())
        qconn = sqlite3.connect(str(QUEUE_DB), timeout=30)
        imported = 0
        with qconn:
            for entry in to_import:
                it = entry["item"]
                try:
                    cur = qconn.execute("""
                        INSERT OR IGNORE INTO tasks (
                            infohash, magnet, title, avid, status, progress, size, speed,
                            retry_count, next_retry_at, created_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, 0, 0, 0, 0, ?)
                    """, (it["btih"], it["magnet"], it["dn"], it["avid"], now))
                    if cur.rowcount == 1:
                        imported += 1
                except Exception as e:
                    print(f"写入 queue.db 失败 [{it['avid']}]: {e}")
        qconn.close()
        print(f"\n★ [COMMIT 完成] 真实写入 queue.db 成功: {imported} 个高价值任务 (状态: pending, retry_count: 0)！")
        print("v1.0 调度器将在下一调度周期以 Tier-Fresh 最高优先级抢先拉取下载。")
    else:
        print("\n【提示】当前为只读 Dry-Run 模式，queue.db 未做任何修改。")
        print(f"若确认以上准入决策无误，请执行: python3 media_ingest.py {input_file} --commit")

    return report_payload

def main():
    parser = argparse.ArgumentParser(description="Media Automation v2.0R — 磁力智能预筛门禁")
    parser.add_argument("magnet_file", help="磁力链接文本文件路径")
    parser.add_argument("--commit", action="store_true", help="确认正式将 NEW 和 UPGRADE 任务写入 queue.db")
    parser.add_argument("--report", help="指定审计报告保存路径")
    args = parser.parse_args()

    rep_path = Path(args.report) if args.report else None
    run_ingest_gate(Path(args.magnet_file), commit=args.commit, custom_report=rep_path)

if __name__ == "__main__":
    main()
