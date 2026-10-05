# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 审计数据库与一键回滚元数据管理 (Audit & Rollback Store)
职责：
1. 使用 SQLite WAL 记录每一次清洗事件与元数据
2. 维护安全隔离箱 (.media_trash/) 物理文件映射
3. 提供 CLI 审计查询与一键恢复被剪辑原片能力
4. 周期性滚动清理超过保留期 (默认 7 天) 的垃圾文件
"""

import os
import sys
import sqlite3
import time
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ad_history.db"


@dataclass
class CleanRecord:
    id: int
    avid: str
    file_path: str
    trash_path: str
    original_duration: float
    cleaned_duration: float
    cut_seconds: float
    confidence: int
    reason: str
    status: str
    created_at: str


class HygieneHistory:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS cleaned_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                avid TEXT NOT NULL,
                file_path TEXT NOT NULL,
                trash_path TEXT NOT NULL,
                original_duration REAL,
                cleaned_duration REAL,
                cut_seconds REAL,
                confidence INTEGER,
                reason TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_avid ON cleaned_records(avid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_created ON cleaned_records(created_at);")

    def record_clean(
        self,
        avid: str,
        file_path: str,
        trash_path: str,
        orig_dur: float,
        clean_dur: float,
        cut_sec: float,
        confidence: int,
        reason: str
    ) -> int:
        """记录一次成功的清洗事件"""
        with self._get_conn() as conn:
            cur = conn.execute("""
            INSERT INTO cleaned_records (
                avid, file_path, trash_path, original_duration,
                cleaned_duration, cut_seconds, confidence, reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active');
            """, (avid, file_path, trash_path, orig_dur, clean_dur, cut_sec, confidence, reason))
            return cur.lastrowid

    def get_latest_by_avid(self, avid: str) -> Optional[CleanRecord]:
        """获取指定番号最近一条有效的清洗记录"""
        with self._get_conn() as conn:
            cur = conn.execute("""
            SELECT * FROM cleaned_records
            WHERE avid = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1;
            """, (avid,))
            row = cur.fetchone()
            if not row:
                return None
            return CleanRecord(
                id=row["id"],
                avid=row["avid"],
                file_path=row["file_path"],
                trash_path=row["trash_path"],
                original_duration=row["original_duration"],
                cleaned_duration=row["cleaned_duration"],
                cut_seconds=row["cut_seconds"],
                confidence=row["confidence"],
                reason=row["reason"],
                status=row["status"],
                created_at=row["created_at"]
            )

    def mark_rolled_back(self, record_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE cleaned_records SET status = 'rolled_back' WHERE id = ?;", (record_id,))

    def list_recent(self, limit: int = 20) -> List[CleanRecord]:
        """查询最近清洗记录"""
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM cleaned_records ORDER BY id DESC LIMIT ?;", (limit,))
            records = []
            for row in cur.fetchall():
                records.append(CleanRecord(
                    id=row["id"],
                    avid=row["avid"],
                    file_path=row["file_path"],
                    trash_path=row["trash_path"],
                    original_duration=row["original_duration"],
                    cleaned_duration=row["cleaned_duration"],
                    cut_seconds=row["cut_seconds"],
                    confidence=row["confidence"],
                    reason=row["reason"],
                    status=row["status"],
                    created_at=row["created_at"]
                ))
            return records

    def prune_trash(self, retention_days: int = 7) -> int:
        """清理超过保留天数的物理暂存原片，释放磁盘空间"""
        cutoff_sec = time.time() - (retention_days * 86400)
        pruned_count = 0
        with self._get_conn() as conn:
            cur = conn.execute("""
            SELECT id, trash_path FROM cleaned_records
            WHERE status = 'active' AND strftime('%s', created_at) < ?;
            """, (str(int(cutoff_sec)),))
            for row in cur.fetchall():
                trash_p = row["trash_path"]
                if os.path.exists(trash_p):
                    try:
                        os.remove(trash_p)
                        pruned_count += 1
                    except Exception:
                        pass
                conn.execute("UPDATE cleaned_records SET status = 'purged' WHERE id = ?;", (row["id"],))
        return pruned_count


def cli_main():
    import argparse
    parser = argparse.ArgumentParser(description="Media Hygiene Audit & Rollback CLI")
    parser.add_argument("--list", action="store_true", help="列出最近的清洗记录")
    parser.add_argument("--rollback", type=str, metavar="AVID", help="一键还原指定番号的原片")
    parser.add_argument("--prune", type=int, metavar="DAYS", default=0, help="清理超过指定天数的暂存垃圾原片")
    args = parser.parse_args()

    hist = HygieneHistory()

    if args.list:
        records = hist.list_recent(25)
        print(f"=== 最近清洗审计记录 (总计 {len(records)} 条) ===")
        for r in records:
            print(f"[{r.id:03d}] {r.avid:12s} | 剪除: {r.cut_seconds:5.1f}s | 置信度: {r.confidence:2d}% | 状态: {r.status:11s} | 时间: {r.created_at}")
        return

    if args.rollback:
        avid = args.rollback.strip().upper()
        rec = hist.get_latest_by_avid(avid)
        if not rec:
            print(f"[-] 未找到番号 {avid} 的有效清洗记录，无法回滚。")
            sys.exit(1)
        if not os.path.exists(rec.trash_path):
            print(f"[-] 错误：暂存原片已丢失或被修剪: {rec.trash_path}")
            sys.exit(1)

        print(f"[*] 正在回滚 {avid}: 将 {rec.trash_path} 还原至 {rec.file_path}...")
        try:
            # 备份当前 clean 文件（防止覆盖破坏）
            if os.path.exists(rec.file_path):
                os.remove(rec.file_path)
            shutil.move(rec.trash_path, rec.file_path)
            hist.mark_rolled_back(rec.id)
            print(f"[+] 成功回滚 {avid}！原片已完整恢复至原始目录。")
        except Exception as e:
            print(f"[-] 回滚执行失败: {e}")
            sys.exit(1)
        return

    if args.prune > 0:
        pruned = hist.prune_trash(args.prune)
        print(f"[+] 成功清理 {pruned} 个超过 {args.prune} 天的历史隔离原片。")
        return

    parser.print_help()


if __name__ == "__main__":
    cli_main()
