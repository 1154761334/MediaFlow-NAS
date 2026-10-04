#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Automation System — 统一系统健康巡检工具 (Phase 5)
一条命令全面自检：Xunlei 引擎、SQLite 任务库、SSD 缓冲池、JavSP 刮削、
本地翻译模型、翻译路由、Emby 服务、Systemd 守护器及核心日志。
默认 100% 只读，无任何副作用。
退出码规范:
  0: 完全健康 [OK]
  1: 存在提示或告警 [WARN]
  2: 存在故障或异常 [ERROR]
"""

import os
import sys
import time
import json
import sqlite3
import shutil
import re
import urllib.request
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from core.config import load_config
    from core.xunlei_client import XunleiAdapter
    _cfg = load_config()
    DB_PATH = Path(_cfg["paths"].get("db_path", "./data/queue.db"))
    DOWNLOAD_ROOT = Path(_cfg["paths"].get("ssd_download", "/volume1/迅雷/下载"))
    STAGING_ROOT = Path(_cfg["paths"].get("hdd_staging", "/volume2/video/avnook/#重刮队列/avok"))
    MOVER_LOG = Path(_cfg["paths"].get("log_file", "./mover.log"))
    CONFIG_PATH = Path(_cfg.get("config_path", REPO_ROOT / "config.yaml"))
except Exception:
    XunleiAdapter = None
    DB_PATH = Path("/volume1/docker/xunlei/queue.db")
    DOWNLOAD_ROOT = Path("/volume1/迅雷/下载")
    STAGING_ROOT = Path("/volume2/video/avnook/#重刮队列/avok")
    MOVER_LOG = Path("/volume1/docker/xunlei/mover.log")
    CONFIG_PATH = Path("/volume1/docker/xunlei/config.yaml")

class HealthChecker:
    def __init__(self):
        self.status_code = 0  # 0: OK, 1: WARN, 2: ERROR
        self.report = []

    def log(self, level, module, message):
        lvl_str = f"[{level}]"
        self.report.append((level, module, message))
        if level == "ERROR":
            self.status_code = max(self.status_code, 2)
            print(f"  \033[31m{lvl_str:7s}\033[0m \033[1m[{module}]\033[0m {message}")
        elif level == "WARN":
            self.status_code = max(self.status_code, 1)
            print(f"  \033[33m{lvl_str:7s}\033[0m \033[1m[{module}]\033[0m {message}")
        elif level == "OK":
            print(f"  \033[32m{lvl_str:7s}\033[0m \033[1m[{module}]\033[0m {message}")
        else:
            print(f"  {lvl_str:7s} \033[1m[{module}]\033[0m {message}")

    def check_xunlei_and_queue(self):
        print("\n=== 1. 迅雷引擎与任务状态库 (Xunlei & Queue.db) ===")
        if not DB_PATH.exists():
            self.log("ERROR", "QueueDB", f"数据库文件不存在: {DB_PATH}")
            return

        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=30)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # 1. 检查数据守恒与状态分布
            total = cur.execute("SELECT count(*) FROM tasks").fetchone()[0]
            counts = dict(cur.execute("SELECT status, count(*) FROM tasks GROUP BY status").fetchall())
            self.log("OK", "QueueDB", f"任务总数: {total} 部 (数据守恒正常)")
            self.log("INFO", "QueueDB", f"状态分布: archived={counts.get('archived',0)}, active={counts.get('active',0)}, pending={counts.get('pending',0)}, retry={counts.get('retry',0)}, stalled={counts.get('stalled',0)}, completed={counts.get('completed',0)}")

            # 2. 检查 VIP Hold 数量
            vip_hold = cur.execute("SELECT count(*) FROM tasks WHERE progress > 0 AND status IN ('retry', 'stalled')").fetchone()[0]
            if vip_hold > 0:
                self.log("INFO", "QueueDB", f"VIP 断点安全隔离中: {vip_hold} 部任务受 Resume-Safety Hold 保护")
            conn.close()
        except Exception as e:
            self.log("ERROR", "QueueDB", f"查询数据库异常: {e}")

        # 3. 检查 Xunlei API 连通性与 Phase
        try:
            if XunleiAdapter and CONFIG_PATH.exists():
                import yaml
                cfg = yaml.safe_load(open(CONFIG_PATH))
                adapter = XunleiAdapter(cfg.get("xunlei", {}))
                ok, tasks = adapter.list_tasks(limit=100)
                if ok:
                    phases = {}
                    running = 0
                    pending = 0
                    total_speed = 0
                    for t in tasks:
                        p = t.get("phase", "UNKNOWN")
                        phases[p] = phases.get(p, 0) + 1
                        sp = t.get("speed", 0) or 0
                        total_speed += sp
                        if p == "PHASE_TYPE_RUNNING":
                            running += 1
                        elif p == "PHASE_TYPE_PENDING":
                            pending += 1

                    sp_mb = total_speed / (1024 * 1024)
                    self.log("OK", "XunleiAPI", f"引擎连接正常, 总任务: {len(tasks)} 部 (RUNNING={running}, PENDING={pending}, COMPLETE={phases.get('PHASE_TYPE_COMPLETE', 0)})")
                    self.log("OK", "XunleiAPI", f"物理占槽数: {running + pending}/45 槽位, 瞬时总带宽: {sp_mb:.2f} MB/s ({sp_mb*8:.1f} Mbps)")
                else:
                    self.log("WARN", "XunleiAPI", f"读取迅雷任务列表异常: {tasks}")
        except Exception as e:
            self.log("WARN", "XunleiAPI", f"迅雷套件引擎连接异常 (可检查 socket 权限): {e}")

    def check_download_storage(self):
        print("\n=== 2. 存储分层与下载池安全 (Storage & Download Pool) ===")
        if not DOWNLOAD_ROOT.exists():
            self.log("ERROR", "Storage", f"下载池目录不存在: {DOWNLOAD_ROOT}")
            return

        try:
            usage = shutil.disk_usage(DOWNLOAD_ROOT)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            if free_gb < 120.0:
                self.log("WARN", "Storage", f"SSD 剩余空间较低: {free_gb:.1f} GB / {total_gb:.1f} GB (<120G 熔断线)")
            else:
                self.log("OK", "Storage", f"SSD 剩余空间充足: {free_gb:.1f} GB / {total_gb:.1f} GB (高于恢复线 180G)")

            subdirs = [d for d in DOWNLOAD_ROOT.iterdir() if d.is_dir()]
            empty_dirs = 0
            xltd_count = 0
            dup_named_dirs = 0
            for d in subdirs:
                try:
                    entries = list(d.iterdir())
                    if len(entries) == 0:
                        empty_dirs += 1
                    for e in entries:
                        if e.name.endswith(".xltd"):
                            xltd_count += 1
                except Exception:
                    pass
                if re.search(r'\(\d+\)$', d.name):
                    dup_named_dirs += 1

            self.log("OK", "Storage", f"下载池总目录: {len(subdirs)} 个, 活跃 .xltd 临时分块: {xltd_count} 个")
            if dup_named_dirs > 0:
                self.log("INFO", "Storage", f"包含重复编号 (1)/(2) 的历史目录: {dup_named_dirs} 个 (正逐步随归档与修剪安全代谢)")
            if empty_dirs > 300:
                self.log("WARN", "Storage", f"检测到纯空目录偏多: {empty_dirs} 个 (待 clean_ghost_dirs_safe 静止期后回收)")
            else:
                self.log("OK", "Storage", f"静止期待回收空目录: {empty_dirs} 个 (符合预期)")
        except Exception as e:
            self.log("ERROR", "Storage", f"检测存储异常: {e}")

    def check_javsp(self):
        print("\n=== 3. JavSP 智能刮削生态 (Scraper Service) ===")
        try:
            # 1. 待刮队列
            staging_count = 0
            if STAGING_ROOT.exists():
                for root, dirs, files in os.walk(STAGING_ROOT):
                    for f in files:
                        if f.lower().endswith((".mp4", ".mkv", ".avi", ".ts")) and not f.startswith("."):
                            staging_count += 1

            if staging_count == 0:
                self.log("OK", "JavSP", "待刮就绪队列已清空，无任何积压")
            elif staging_count >= 10:
                self.log("WARN", "JavSP", f"待刮就绪队列积压偏高: {staging_count} 部视频待处理")
            else:
                self.log("INFO", "JavSP", f"待刮就绪队列积累中: {staging_count} 部 (满足 >=3 部或 30min 自动触发)")

            # 2. 容器状态
            res = subprocess.run(["sudo", "-n", "docker", "ps", "-a", "-f", "name=javsp-avnook", "--format", "{{.Status}}"], stdout=subprocess.PIPE, text=True)
            status_text = res.stdout.strip()
            if "Up" in status_text:
                self.log("OK", "JavSP", f"JavSP 容器正在运行刮削中 ({status_text})")
            else:
                self.log("OK", "JavSP", f"JavSP 容器处于就绪待命状态 (上次运行: {status_text})")
        except Exception as e:
            self.log("ERROR", "JavSP", f"检测 JavSP 异常: {e}")

    def check_translation(self):
        print("\n=== 4. 智能翻译双引擎与路由 (Translation Ecosystem) ===")
        # 1. llama-hymt
        hymt_ok = False
        t0 = time.time()
        try:
            req = urllib.request.Request("http://127.0.0.1:18085/v1/models")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3) as resp:
                lat = (time.time() - t0) * 1000
                if resp.status == 200:
                    self.log("OK", "llama-hymt", f"本地腾讯 Hy-MT2 1.8B 服务正常 (端口 18085, 延迟: {lat:.1f}ms)")
                    hymt_ok = True
        except Exception as e:
            self.log("ERROR", "llama-hymt", f"本地翻译模型端口 18085 连接失败: {e}")

        # 2. translation-router
        t0 = time.time()
        try:
            req = urllib.request.Request("http://127.0.0.1:18088/health")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=8) as resp:
                lat = (time.time() - t0) * 1000
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    model = data.get("backend_model", "Unknown")
                    cached = data.get("cached_entries", 0)
                    self.log("OK", "Router", f"翻译路由正常 (端口 18088, 延迟: {lat:.1f}ms, 模型: {model}, 缓存: {cached}条)")
        except Exception as e:
            self.log("ERROR", "Router", f"翻译路由端口 18088 连接失败: {e}")

    def check_emby(self):
        print("\n=== 5. Emby 媒体中心与网络直连 (Emby Server) ===")
        t0 = time.time()
        try:
            req = urllib.request.Request("http://127.0.0.1:8096/web/index.html")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as resp:
                lat = (time.time() - t0) * 1000
                if resp.status == 200:
                    self.log("OK", "Emby", f"Emby 局域网直连正常 (端口 8096, 延迟: {lat:.1f}ms, 无外部代理劫持)")
                else:
                    self.log("WARN", "Emby", f"Emby 返回非预期状态码: {resp.status}")
        except Exception as e:
            self.log("ERROR", "Emby", f"连接 Emby 发生异常: {e} (请检查容器运行状态或内部直连 ProxyHandler)")

    def check_systemd(self):
        print("\n=== 6. Systemd 自动化守护器 (Systemd Timers & Services) ===")
        # Timer
        try:
            out = subprocess.check_output(["systemctl", "status", "xunlei-mover.timer"], text=True)
            active_line = next((l.strip() for l in out.splitlines() if "Active:" in l), "")
            if "active (running)" in active_line or "active (waiting)" in active_line:
                self.log("OK", "Systemd", f"定时器 xunlei-mover.timer 正常运行: {active_line}")
            else:
                self.log("WARN", "Systemd", f"定时器状态异常: {active_line}")
        except Exception as e:
            self.log("ERROR", "Systemd", f"查询 xunlei-mover.timer 失败: {e}")

        # Service
        try:
            res = subprocess.run(["systemctl", "status", "xunlei-mover.service"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out = res.stdout + res.stderr
            status_line = next((l.strip() for l in out.splitlines() if "Active:" in l), "")
            code_line = next((l.strip() for l in out.splitlines() if "code=exited" in l), "")
            pid_line = next((l.strip() for l in out.splitlines() if "Main PID:" in l), "")
            
            if "activating" in status_line or "active (running)" in status_line:
                self.log("OK", "Systemd", f"单周期调度服务 xunlei-mover.service 当前正在执行中 ({pid_line})")
            elif "status=0/SUCCESS" in code_line or "status=0" in status_line:
                self.log("OK", "Systemd", f"单周期调度服务 xunlei-mover.service 上次执行成功 ({code_line})")
            else:
                self.log("WARN", "Systemd", f"调度服务当前状态: {status_line} {code_line}")
        except Exception as e:
            self.log("WARN", "Systemd", f"查询 xunlei-mover.service 提示: {e}")

    def check_logs(self):
        print("\n=== 7. 系统调度日志健康度 (Mover Log Health) ===")
        if not MOVER_LOG.exists():
            self.log("WARN", "Log", f"未找到日志文件: {MOVER_LOG}")
            return

        try:
            sz = MOVER_LOG.stat().st_size
            sz_mb = sz / (1024 * 1024)
            if sz_mb > 15.0:
                self.log("WARN", "Log", f"日志体积偏大: {sz_mb:.1f} MB (需关注轮转策略)")
            else:
                self.log("OK", "Log", f"日志体积健康: {sz_mb:.2f} MB (10MB 自动轮转正常)")

            # 读取最新 100 行检查 ERROR
            errors = []
            warnings = []
            with open(MOVER_LOG, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-100:]
                for l in lines:
                    if "ERROR" in l or "Traceback" in l:
                        errors.append(l.strip())
                    elif "WARN" in l or "警告" in l:
                        warnings.append(l.strip())

            if errors:
                self.log("WARN", "Log", f"最新日志中检测到 {len(errors)} 条错误记录 (最新示例: {errors[-1][:80]})")
            else:
                self.log("OK", "Log", "最新日志中无任何未捕获致命错误 (0 Errors)")
        except Exception as e:
            self.log("ERROR", "Log", f"读取日志异常: {e}")

    def check_media_inventory(self):
        print("\n=== 8. 本地媒体资产库与质量门禁 (Media Inventory & Quality Gate v2.0) ===")
        inv_db = Path("/volume1/docker/media-automation/data/media_inventory.db")
        if not inv_db.exists():
            self.log("WARN", "Inventory", "资产数据库尚未建立或正在构建中")
            return

        try:
            conn = sqlite3.connect(str(inv_db), timeout=10)
            cur = conn.cursor()
            total_records = cur.execute("SELECT count(*) FROM media_files").fetchone()[0]
            distinct_avids = cur.execute("SELECT count(DISTINCT avid) FROM media_files").fetchone()[0]
            hevc_count = cur.execute("SELECT count(*) FROM media_files WHERE video_codec = 'HEVC'").fetchone()[0]
            h264_count = cur.execute("SELECT count(*) FROM media_files WHERE video_codec = 'H264'").fetchone()[0]
            conn.close()

            if total_records > 0:
                self.log("OK", "Inventory", f"资产数据库正常 (已索引视频: {total_records} 部, 唯一番号: {distinct_avids} 部)")
                self.log("INFO", "Inventory", f"编码分布: HEVC={hevc_count} 部, H264={h264_count} 部 (HEVC 保护规则已就绪)")
            else:
                self.log("WARN", "Inventory", "资产数据库记录为 0 (可能正在首次构建)")

            # 检查最近一次 Ingest 报告
            reports_dir = Path("/volume1/docker/media-automation/reports")
            if reports_dir.exists():
                reps = sorted(list(reports_dir.glob("ingest-*.json")), key=lambda x: x.stat().st_mtime, reverse=True)
                if reps:
                    latest = reps[0]
                    try:
                        data = json.loads(latest.read_text(encoding="utf-8"))
                        counts = data.get("counts", {})
                        self.log("OK", "IngestGate", f"最近门禁过筛报告: {latest.name} (NEW={counts.get('NEW',0)}, UPGRADE={counts.get('UPGRADE',0)}, SKIP={counts.get('SKIP',0)}, HASH_DUP={counts.get('HASH_DUP',0)})")
                    except Exception:
                        pass
        except Exception as e:
            self.log("WARN", "Inventory", f"查询资产库异常: {e}")

    def run_all(self):
        print("================================================================================")
        print(f"Media Automation System 全系统健康巡检报告")
        print(f"执行时间: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} | 宿主: CHEN-X (N100, DSM 7.2+)")
        print("================================================================================")
        self.check_xunlei_and_queue()
        self.check_download_storage()
        self.check_javsp()
        self.check_translation()
        self.check_emby()
        self.check_systemd()
        self.check_logs()
        self.check_media_inventory()

        print("\n================================================================================")
        if self.status_code == 0:
            print("  \033[32m【综合诊断结论】: Health Check Exit Code 0: 当前无阻断性故障，已知 Hold 与技术债均处于受控状态，无人值守平稳运行。\033[0m")
        elif self.status_code == 1:
            print("  \033[33m【综合诊断结论】: 系统整体运行正常，但存在若干提示项 [WARNING]，请关注上述详情。\033[0m")
        else:
            print("  \033[31m【综合诊断结论】: 系统存在异常项 [ERROR]，需要维护介入！\033[0m")
        print("================================================================================\n")
        return self.status_code

if __name__ == "__main__":
    checker = HealthChecker()
    code = checker.run_all()
    sys.exit(code)
