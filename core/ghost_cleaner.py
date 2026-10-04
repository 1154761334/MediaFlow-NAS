#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迅雷高速下载池幽灵空目录安全修剪工具 (8 重安全门槛防线)
严格保障：
1. 必须是 download_root 直接子目录且非软链
2. 必须真正为空 (0-entry，绝不 unlink 任何文件)
3. 必须非系统保留或隐藏目录
4. 迅雷活跃任务双向保护 (不属于 running/pending 任务名及其去 (1) 原始名)
5. 数据库活跃任务保护 (不属于 queue.db 中 status='active' 的条目)
6. 进程内核句柄未锁定
7. 年龄保护 (mtime 必须超过 30 分钟静止期)
8. 先 Dry-Run 分类审计，确认后方可安全 rmdir()
"""

import os
import sys
import time
import re
import sqlite3
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from core.xunlei_client import XunleiAdapter
    from core.mover import get_xunlei_open_files, SYSTEM_IGNORE_DIRS
    from core.config import load_config
except ImportError:
    from xunlei_client import XunleiAdapter
    from mover import get_xunlei_open_files, SYSTEM_IGNORE_DIRS
    from config import load_config

import yaml

cfg = load_config()
DOWNLOAD_ROOT = Path(cfg["paths"].get("ssd_download", "/volume1/迅雷/下载"))
DB_PATH = Path(cfg["paths"].get("db_path", "./data/queue.db"))

def audit_and_prune_ghost_dirs(dry_run=True, grace_period_sec=1800):
    if not DOWNLOAD_ROOT.exists():
        print(f"错误: 下载目录不存在: {DOWNLOAD_ROOT}")
        return

    now = time.time()
    cfg_live = load_config()
    adapter = XunleiAdapter(cfg_live["xunlei"])

    # 1. 收集迅雷活跃任务保护名单 (全量 + 去版本号原名)
    ok, xunlei_tasks = adapter.list_tasks(limit=200)
    protected_xunlei_names = set()
    if ok:
        for t in xunlei_tasks:
            if t.get("phase") in ("PHASE_TYPE_RUNNING", "PHASE_TYPE_PENDING", "PHASE_TYPE_PAUSED"):
                name = t.get("name", "")
                if name:
                    protected_xunlei_names.add(name)
                    # 去除 (1), (2) 后缀的原名保护
                    base_name = re.sub(r'\(\d+\)$', '', name).strip()
                    if base_name:
                        protected_xunlei_names.add(base_name)

    # 2. 收集数据库活跃任务保护名单
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT title, avid FROM tasks WHERE status = 'active'")
    protected_db_names = set()
    for row in cur.fetchall():
        if row[0]:
            protected_db_names.add(row[0])
            protected_db_names.add(re.sub(r'\(\d+\)$', '', row[0]).strip())
        if row[1]:
            protected_db_names.add(row[1])
    conn.close()

    # 3. 收集迅雷内核打开的文件句柄
    xunlei_fds = get_xunlei_open_files()

    print(f"=== 幽灵空目录八重门槛安全审计开始 ===")
    print(f"模式: {'【DRY-RUN 试运行】' if dry_run else '【正式安全修剪】'}")
    print(f"受保护活跃任务名数: Xunlei={len(protected_xunlei_names)}, DB={len(protected_db_names)}, 句柄={len(xunlei_fds)}")
    print(f"安全静止期门槛: >= {grace_period_sec // 60} 分钟")

    candidates = []
    protected_active = []
    protected_recent = []
    protected_system = []
    non_empty = []

    for entry in DOWNLOAD_ROOT.iterdir():
        # 门槛 1: 必须是目录且非符号链接
        if not entry.is_dir() or entry.is_symlink():
            continue

        # 门槛 2: 非系统保留
        if entry.name in SYSTEM_IGNORE_DIRS or entry.name.startswith("."):
            protected_system.append(entry.name)
            continue

        # 门槛 3: 检查是否真正 0-entry
        try:
            sub_items = list(entry.iterdir())
        except Exception as e:
            print(f"无法读取目录 {entry.name}: {e}")
            continue

        if len(sub_items) > 0:
            non_empty.append(entry.name)
            continue

        # 门槛 4: 迅雷任务活跃保护
        entry_base = re.sub(r'\(\d+\)$', '', entry.name).strip()
        if (entry.name in protected_xunlei_names) or (entry_base in protected_xunlei_names):
            protected_active.append((entry.name, "迅雷活跃任务"))
            continue

        # 门槛 5: 数据库活跃任务保护
        if (entry.name in protected_db_names) or (entry_base in protected_db_names):
            protected_active.append((entry.name, "DB active 任务"))
            continue

        # 门槛 6: 句柄锁定保护
        norm_p = os.path.normpath(str(entry.resolve()))
        if norm_p in xunlei_fds:
            protected_active.append((entry.name, "进程句柄占用"))
            continue

        # 门槛 7: 年龄保护 (静止期 >= 30分钟)
        try:
            st = entry.stat()
            age_sec = now - st.st_mtime
            if age_sec < grace_period_sec:
                protected_recent.append((entry.name, int(age_sec)))
                continue
        except Exception:
            continue

        # 通过全部 8 重检查，确认为可修剪候选
        candidates.append(entry)

    print("--------------------------------------------------")
    print(f"审计统计结果:")
    print(f"  ● 非空任务目录 (正常保留):      {len(non_empty)} 个")
    print(f"  ● 系统/保留目录 (绝对保护):      {len(protected_system)} 个")
    print(f"  ● 活跃任务保护 (活跃锁定):      {len(protected_active)} 个")
    print(f"  ● 新建静止期保护 (< 30分钟):     {len(protected_recent)} 个")
    print(f"  ★ 符合安全回收纯空目录 (Candidate): {len(candidates)} 个")
    print("--------------------------------------------------")

    if dry_run:
        print("【试运行完成】当前未做任何删除操作。")
        if candidates:
            print("样例候选目录 (前 5 个):")
            for c in candidates[:5]:
                print(f"  - {c.name}")
        return len(candidates)

    # 正式执行安全 rmdir
    pruned_cnt = 0
    err_cnt = 0
    for c in candidates:
        try:
            c.rmdir()
            pruned_cnt += 1
        except Exception as e:
            err_cnt += 1

    print(f"【修剪完成】成功安全 rmdir() 回收纯空目录: {pruned_cnt} 个 (失败: {err_cnt} 个)")
    return pruned_cnt

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="幽灵空目录安全修剪")
    parser.add_argument("--execute", action="store_true", help="确认正式执行删除 (默认仅 dry-run)")
    parser.add_argument("--grace-sec", type=int, default=1800, help="静止等待秒数 (默认 1800s / 30min)")
    args = parser.parse_args()

    audit_and_prune_ghost_dirs(dry_run=not args.execute, grace_period_sec=args.grace_sec)
