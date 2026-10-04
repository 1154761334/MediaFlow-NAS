#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaFlow-NAS 生产环境在线热灾备与滚动修剪工具 (Hot Backup Engine)

核心职能:
1. 采用 SQLite 原生 backup() API 导出一致性内存/磁盘热快照，绝不锁表，绝不损坏 WAL
2. 备份目标:
   - 任务流转数据库: queue.db
   - 本地媒体资产数据库: media_inventory.db
   - 全局核心配置文件: config.yaml
3. 自动打包压缩为 .tar.gz 归档文件
4. 滚动修剪: 自动维护最近 N 天 (默认 7 天) 的历史快照，过期自动清除，杜绝存储膨胀
"""

import os
import sys
import time
import tarfile
import sqlite3
import argparse
import shutil
from pathlib import Path
from typing import List, Dict, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from core.config import load_config, find_config_path
except ImportError:
    load_config = lambda: {}
    find_config_path = lambda: Path("config.yaml")

def perform_hot_backup(backup_root: Path = None, keep_days: int = 7) -> bool:
    cfg = load_config()
    cfg_file = find_config_path()

    if not backup_root:
        backup_root = REPO_ROOT / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)

    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    temp_dir = backup_root / f"tmp_{timestamp_str}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    archive_filename = f"mediaflow_backup_{timestamp_str}.tar.gz"
    archive_path = backup_root / archive_filename

    print("=" * 70)
    print("           MediaFlow-NAS 生产数据库与配置在线热备份")
    print("=" * 70)
    print(f"执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"目标归档: {archive_path.name}")
    print("-" * 70)

    success = True
    try:
        # 1. 在线热备份 queue.db
        q_db_path = Path(cfg.get("paths", {}).get("db_path", "./data/queue.db"))
        if not q_db_path.is_absolute():
            q_db_path = (REPO_ROOT / q_db_path).resolve()
        
        if q_db_path.exists():
            dest_q = temp_dir / "queue.db"
            print(f"• 正在执行 queue.db 在线热快照导出 ({q_db_path})...")
            src_conn = sqlite3.connect(str(q_db_path), timeout=30)
            dst_conn = sqlite3.connect(str(dest_q))
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            print(f"  \033[32m[OK]\033[0m queue.db 快照完成 (体积: {dest_q.stat().st_size / (1024*1024):.2f} MB)")
        else:
            print(f"  \033[33m[WARN]\033[0m queue.db 不存在，跳过此文件。")

        # 2. 在线热备份 media_inventory.db
        inv_db_path = Path(cfg.get("paths", {}).get("inventory_db", "./data/media_inventory.db"))
        if not inv_db_path.is_absolute():
            inv_db_path = (REPO_ROOT / inv_db_path).resolve()

        if inv_db_path.exists():
            dest_inv = temp_dir / "media_inventory.db"
            print(f"• 正在执行 media_inventory.db 在线热快照导出 ({inv_db_path})...")
            src_conn = sqlite3.connect(str(inv_db_path), timeout=30)
            dst_conn = sqlite3.connect(str(dest_inv))
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            print(f"  \033[32m[OK]\033[0m media_inventory.db 快照完成 (体积: {dest_inv.stat().st_size / (1024*1024):.2f} MB)")
        else:
            print(f"  \033[33m[WARN]\033[0m media_inventory.db 不存在，跳过此文件。")

        # 3. 复制配置文件
        if cfg_file.exists():
            dest_cfg = temp_dir / "config.yaml"
            shutil.copy2(cfg_file, dest_cfg)
            print(f"  \033[32m[OK]\033[0m 配置文件已收录 (config.yaml)")

        # 4. 打包为 tar.gz
        print(f"• 正在执行 Gzip 紧凑压缩打包...")
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in temp_dir.iterdir():
                tar.add(item, arcname=item.name)
        
        comp_size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"★ 备份归档打包成功: {archive_path.name} (压缩后体积: {comp_size_mb:.2f} MB)")

    except Exception as e:
        print(f"\033[31m[ERROR] 备份执行失败:\033[0m {e}")
        success = False
    finally:
        # 清理临时工作目录
        shutil.rmtree(temp_dir, ignore_errors=True)

    # 5. 自动滚动修剪历史旧备份
    if success and keep_days > 0:
        prune_old_backups(backup_root, keep_days)

    print("=" * 70)
    return success

def prune_old_backups(backup_root: Path, keep_days: int):
    now = time.time()
    cutoff_sec = keep_days * 86400
    pruned = 0
    total = 0
    for f in backup_root.glob("mediaflow_backup_*.tar.gz"):
        total += 1
        age = now - f.stat().st_mtime
        if age > cutoff_sec:
            try:
                f.unlink()
                pruned += 1
            except Exception:
                pass
    if pruned > 0:
        print(f"• 滚动修剪: 已清除 {pruned} 个超过 {keep_days} 天的历史旧备份快照。")
    print(f"• 当前有效灾备快照总数: {total - pruned} 份 (保留窗口: {keep_days} 天)")

def list_backups(backup_root: Path = None):
    if not backup_root:
        backup_root = REPO_ROOT / "backup"
    backups = sorted(list(backup_root.glob("mediaflow_backup_*.tar.gz")), reverse=True)
    print("=" * 70)
    print(f"现有媒体自动化灾备快照清单 ({len(backups)} 份)")
    print("=" * 70)
    if not backups:
        print("暂无任何备份文件。")
        return
    for b in backups:
        st = b.stat()
        mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        sz_mb = st.st_size / (1024 * 1024)
        print(f"  * {b.name:<38} | {sz_mb:>6.2f} MB | 创建于: {mtime_str}")
    print("=" * 70)

def main():
    parser = argparse.ArgumentParser(description="MediaFlow-NAS 在线热灾备与滚动修剪工具")
    parser.add_argument("--run", action="store_true", help="立即执行一次全量在线热备份并修剪过期快照")
    parser.add_argument("--list", action="store_true", help="列出当前所有有效灾备快照")
    parser.add_argument("--keep-days", type=int, default=7, help="快照保留天数 (默认 7 天)")
    parser.add_argument("--output-dir", help="自定义备份存储路径")
    args = parser.parse_args()

    out_p = Path(args.output_dir) if args.output_dir else None

    if args.list:
        list_backups(out_p)
    else:
        succ = perform_hot_backup(out_p, keep_days=args.keep_days)
        sys.exit(0 if succ else 1)

if __name__ == "__main__":
    main()
