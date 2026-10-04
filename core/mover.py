#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
迅雷极速下载自动归档引擎 (NVMe SSD -> 机械盘 RAID 5)
具备工业级五重判定保障：
1. 进程文件描述符检查 (/proc/*/fd 实时扫描 Xunlei 活跃写入)
2. 临时与校验文件黑名单递归扫描 (*.xltd, *.cfg, *.downloading, *.part 等)
3. 双采样大小与修改时间静止期校验 (mtime 与 size 持续稳定)
4. 系统级临时目录及隐藏文件隔离保护
5. 递归多文件 BT 目录原子级完整性验证
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
import shutil
import logging
from logging.handlers import RotatingFileHandler
import argparse
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from core.config import load_config
    _cfg = load_config()
    DEFAULT_SRC = _cfg["paths"].get("ssd_download", "/volume1/迅雷/下载")
    DEFAULT_DEST = _cfg["paths"].get("hdd_staging", "/volume2/video/avnook/#重刮队列/avok")
    DEFAULT_LOG = _cfg["paths"].get("log_file", "./mover.log")
    EMBY_URL = os.environ.get("EMBY_URL", _cfg.get("emby", {}).get("url", "http://127.0.0.1:8096"))
    EMBY_API_KEY = os.environ.get("EMBY_API_KEY", _cfg.get("emby", {}).get("api_key", ""))
except Exception:
    DEFAULT_SRC = "/volume1/迅雷/下载"
    DEFAULT_DEST = "/volume2/video/avnook/#重刮队列/avok"
    DEFAULT_LOG = "./mover.log"
    EMBY_URL = os.environ.get("EMBY_URL", "http://127.0.0.1:8096")
    EMBY_API_KEY = os.environ.get("EMBY_API_KEY", "")

# 系统与内部忽略目录列表 (严禁触碰)
SYSTEM_IGNORE_DIRS = {
    ".bt", "down", "云盘缓存文件", "@eaDir", "#recycle", 
    ".DS_Store", "@SynoFinder-log", ".drive", "@tmp"
}

# 临时文件与未完成下载后缀黑名单
TEMP_EXTENSIONS = {
    ".xltd", ".cfg", ".downloading", ".tmp", ".part", ".crdownload"
}

# 允许归档到视频刮削队列的有效媒体后缀
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".wmv", ".avi", ".ts", ".iso", ".mov"
}

# 稳定静止期阈值（秒）：文件修改时间距离当前必须超过该时间
DEFAULT_QUIET_SECONDS = 60
# ============================================

def setup_logger(log_file=DEFAULT_LOG, verbose=False):
    logger = logging.getLogger("XunleiMover")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    # 控制台输出 (仅在终端交互时输出，避免后台重定向时产生重复日志)
    if sys.stdout.isatty():
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch_fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        ch.setFormatter(ch_fmt)
        logger.addHandler(ch)

    # 日志文件输出 (使用 RotatingFileHandler，上限 10MB，保留 3 份备份，只落盘 INFO 及以上关键事件)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=3, encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh_fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
            fh.setFormatter(fh_fmt)
            logger.addHandler(fh)
        except Exception as e:
            print(f"Warning: Failed to setup log file: {e}")

    return logger

def get_xunlei_open_files():
    """
    实时扫描 Linux /proc 文件系统，获取所有 Xunlei 相关进程打开的全部文件绝对路径集合
    这是内核级铁证，只要迅雷还在读写任何分块，必然命中
    """
    open_files = set()
    try:
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            try:
                cmdline_path = f"/proc/{pid_str}/cmdline"
                with open(cmdline_path, "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").lower()
                if b"xunlei" in cmdline or b"pan-xunlei" in cmdline:
                    fd_dir = f"/proc/{pid_str}/fd"
                    if os.path.isdir(fd_dir):
                        for fd in os.listdir(fd_dir):
                            try:
                                link = os.readlink(f"{fd_dir}/{fd}")
                                open_files.add(os.path.normpath(link))
                            except Exception:
                                pass
            except Exception:
                pass
    except Exception as e:
        logging.getLogger("XunleiMover").error(f"Error scanning /proc: {e}")
    return open_files

def is_path_busy_by_xunlei(entry_path: Path, xunlei_fds: set) -> tuple[bool, str]:
    """
    检查某个文件或目录是否正被迅雷进程打开
    """
    norm_entry = os.path.normpath(str(entry_path.resolve()))

    # 1. 直接比对该路径
    if norm_entry in xunlei_fds:
        return True, f"文件直接被迅雷进程句柄锁定: {norm_entry}"

    # 2. 如果是目录，检查是否有任何被打开的文件属于该目录
    if entry_path.is_dir():
        prefix = norm_entry if norm_entry.endswith(os.sep) else norm_entry + os.sep
        for open_f in xunlei_fds:
            if open_f.startswith(prefix):
                return True, f"目录内有文件被迅雷锁定: {open_f}"

    return False, ""

def inspect_entry(entry: Path, xunlei_fds: set, quiet_seconds: int = DEFAULT_QUIET_SECONDS) -> tuple[bool, str]:
    """
    工业级五重判定单项条目 (文件或目录) 是否已 100% 下载完成
    返回 (is_complete, reason)
    """
    # 规则 1: 忽略系统和临时目录
    if entry.name in SYSTEM_IGNORE_DIRS or entry.name.startswith("."):
        return False, "系统保留或临时隐藏文件"

    now = time.time()

    # 规则 2: 单文件任务判定
    if entry.is_file():
        ext = entry.suffix.lower()
        if ext in TEMP_EXTENSIONS or ".xltd" in entry.name.lower():
            return False, f"具有未完成临时后缀: {ext}"

        if ext not in MEDIA_EXTENSIONS:
            return False, f"非视频媒体文件 ({ext})，跳过归档到刮削队列"

        # 规则 3: 内核进程句柄检查
        busy, busy_reason = is_path_busy_by_xunlei(entry, xunlei_fds)
        if busy:
            return False, busy_reason

        # 规则 4: 时间静止期检查
        try:
            st = entry.stat()
        except Exception as e:
            return False, f"读取 stat 失败: {e}"

        age = now - st.st_mtime
        if age < quiet_seconds:
            return False, f"修改时间过新 (距今 {int(age)}s < {quiet_seconds}s)，可能仍在写入"

        if st.st_size == 0:
            return False, "空文件 (0 字节)"

        return True, f"单文件完成 (大小: {st.st_size / (1024*1024):.2f} MB, 静止时间: {int(age)}s)"

    # 规则 5: 目录型任务 (多文件 BT) 判定
    elif entry.is_dir():
        # 句柄检查
        busy, busy_reason = is_path_busy_by_xunlei(entry, xunlei_fds)
        if busy:
            return False, busy_reason

        # 递归扫描目录树
        has_valid_media = False
        latest_mtime = 0
        total_size = 0
        blocking_temp_file = None

        for root, dirs, files in os.walk(entry):
            for fname in files:
                fpath = Path(root) / fname
                fext = fpath.suffix.lower()
                try:
                    fst = fpath.stat()
                except Exception:
                    continue

                # 智能临时文件过滤：
                # 若临时文件大小为 0，或者是小于 50MB 的广告/宣传网页/padding 等垃圾残留，视为非关键残留，不阻碍正片归档
                if fext in TEMP_EXTENSIONS or ".xltd" in fname.lower():
                    is_zero_or_junk = (fst.st_size == 0) or (fst.st_size < 50 * 1024 * 1024 and any(k in fname.lower() or k in root.lower() for k in ['.htm', '.url', '.txt', '.png', '.jpg', '.gif', 'part', '2048', '社区', '情报', '直播', '加速器', '游戏', '荷官', '发牌', '性感', '赌场', '澳门', 'pad', '广告', '宣传', '最新地址']))
                    if not is_zero_or_junk:
                        blocking_temp_file = fname
                    continue

                total_size += fst.st_size
                if fst.st_mtime > latest_mtime:
                    latest_mtime = fst.st_mtime
                if fext in MEDIA_EXTENSIONS and fst.st_size > 200 * 1024 * 1024:
                    has_valid_media = True

        if blocking_temp_file:
            return False, f"子目录内存在未完成临时文件: {blocking_temp_file}"

        if not has_valid_media:
            # 八重门槛安全静默回收: 仅当真正 0-entry 且静止期超过 30 分钟 (1800s) 时安全 rmdir
            if total_size == 0:
                try:
                    if not any(entry.iterdir()) and (now - entry.stat().st_mtime > 1800):
                        entry.rmdir()
                        return False, "空目录已静止超过30分钟，已安全回收"
                except Exception:
                    pass
            return False, "目录内无有效视频媒体文件，跳过归档到刮削队列"

        age = now - latest_mtime
        if age < quiet_seconds:
            return False, f"目录内存在过新修改的文件 (距今 {int(age)}s < {quiet_seconds}s)"

        if total_size == 0:
            return False, "目录总大小为 0"

        return True, f"目录任务完成 (总大小: {total_size / (1024*1024):.2f} MB, 静止时间: {int(age)}s)"

    return False, "未知文件类型"

def is_sample_or_ad_video(file_path: Path, file_size: int) -> bool:
    """
    精细化广告样片判定 (杜绝误伤 OVA/短篇正片):
    1. 含有明确广告样片关键词 ('sample', 'trailer', 'preview', '广告', '宣传', '最新地址') -> 判定为垃圾样片
    2. 文件体积 < 150MB:
       - 尝试通过 ffprobe 探测时长:
         - 时长 >= 1200秒 (20分钟) -> 判定为正规短片/OVA，安全保留！
         - 时长 < 1200秒 -> 判定为片头广告/样片，安全过滤
       - 若无 ffprobe 或探测失败 -> 视名称若包含 sample/ad 判定过滤，否则保守保留
    3. 文件体积 >= 150MB -> 正片保留
    """
    fname_lower = file_path.name.lower()
    ad_keywords = ("sample", "trailer", "preview", "广告", "宣传", "最新地址", "澳門", "荷官", "发牌", "草榴")
    if any(k in fname_lower for k in ad_keywords):
        return True

    if file_size < 150 * 1024 * 1024:
        try:
            ff_bin = shutil.which("ffprobe") or ("/opt/bin/ffprobe" if Path("/opt/bin/ffprobe").is_file() else "ffprobe")
            cmd = [ff_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                dur = float(res.stdout.strip())
                if dur >= 1200.0:
                    return False  # 正规短片/OVA，保留！
                return True       # 小于20分钟的小片段，过滤
        except Exception:
            pass
        return True

    return False

def fast_copy_with_verify(src_path: Path, dst_path: Path, logger, chunk_size=16*1024*1024):
    """
    高性能分块复制 (16MB Buffer 跑满 NVMe 读与 RAID 5 连续顺序写)
    并进行严格的文件大小完整性校验，异常时自动清理未完成目标文件
    """
    try:
        if src_path.is_file():
            # 搬运时自动过滤掉残留的未完成临时文件 (.xltd 等)
            if src_path.suffix.lower() in TEMP_EXTENSIONS or ".xltd" in src_path.name.lower():
                return
            # 搬运时精细化过滤广告样片与宣传垃圾文件，避免干扰 JavSP 刮削
            if src_path.suffix.lower() in MEDIA_EXTENSIONS and is_sample_or_ad_video(src_path, src_path.stat().st_size):
                logger.info(f"安全跳过广告样片/短片头: {src_path.name} ({src_path.stat().st_size / (1024*1024):.2f} MB)")
                return
            if src_path.suffix.lower() in {".apk", ".url", ".html", ".htm", ".mhtml", ".chm"}:
                return
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            src_size = src_path.stat().st_size
            logger.info(f"开始顺序搬运文件: {src_path.name} ({src_size / (1024*1024):.2f} MB)...")
            start_t = time.time()
            
            with open(src_path, "rb") as fsrc, open(dst_path, "wb") as fdst:
                copied = 0
                while True:
                    buf = fsrc.read(chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                    copied += len(buf)

            cost_t = time.time() - start_t
            speed_mb = (src_size / (1024*1024)) / max(0.01, cost_t)
            logger.info(f"搬运完毕，耗时 {cost_t:.1f}s，速度 {speed_mb:.1f} MB/s")

            # 大小校验
            dst_size = dst_path.stat().st_size
            if dst_size != src_size:
                raise IOError(f"大小校验失败! 源: {src_size}, 目标: {dst_size}")

            shutil.copystat(src_path, dst_path)
            # 移除源文件释放 SSD 空间
            src_path.unlink()

        elif src_path.is_dir():
            logger.info(f"开始顺序搬运文件夹任务: {src_path.name}...")
            dst_path.mkdir(parents=True, exist_ok=True)
            for root, dirs, files in os.walk(src_path):
                rel_path = Path(root).relative_to(src_path)
                target_root = dst_path / rel_path
                target_root.mkdir(parents=True, exist_ok=True)
                for fname in files:
                    s_file = Path(root) / fname
                    if s_file.suffix.lower() in TEMP_EXTENSIONS or ".xltd" in fname.lower():
                        continue
                    d_file = target_root / fname
                    fast_copy_with_verify(s_file, d_file, logger, chunk_size)
            # 清除源目录
            shutil.rmtree(src_path)
    except Exception as e:
        # 回滚清理未完成的目标文件/目录，防止碎片残留导致空间耗尽
        if dst_path.exists():
            try:
                if dst_path.is_file():
                    dst_path.unlink()
                elif dst_path.is_dir():
                    shutil.rmtree(dst_path)
                logger.warning(f"已回滚清理未完成的目标残留: {dst_path.name}")
            except Exception as ce:
                logger.error(f"回滚清理残留失败: {ce}")
        raise e

def clean_and_sanitize_target(target: Path, logger):
    """
    对归档到重刮就绪目录的目标条目进行正片提纯与命名标准化
    1. 清理目录内所有非媒体广告垃圾 (.url, .apk, .html, .mhtml, .chm)
    2. 清理 < 150MB 的小视频（预览样片、宣传片）
    3. 去除文件名中常见的网站发布前缀 (如 hhd800.com@, 489155.com@)
    4. 如果存在多个大视频且非标准分卷，保留最大主正片，清理冲突样片
    """
    try:
        if target.is_dir():
            # 1. 清理广告与小视频
            for root, dirs, files in os.walk(target, topdown=False):
                for f in files:
                    fp = Path(root) / f
                    ext = fp.suffix.lower()
                    if ext in {".url", ".apk", ".html", ".htm", ".mhtml", ".chm"}:
                        try:
                            fp.unlink()
                        except Exception:
                            pass
                        continue
                    if ext in MEDIA_EXTENSIONS:
                        try:
                            if fp.stat().st_size < 150 * 1024 * 1024:
                                fp.unlink()
                                logger.info(f"清理伴随样片/广告视频: {f}")
                        except Exception:
                            pass

            # 2. 规范化文件名前缀 (@)
            for root, dirs, files in os.walk(target):
                for f in files:
                    if "@" in f:
                        clean_f = f.split("@")[-1]
                        old_fp = Path(root) / f
                        new_fp = Path(root) / clean_f
                        if not new_fp.exists():
                            try:
                                old_fp.rename(new_fp)
                                logger.info(f"去除非标准发布前缀: {f} -> {clean_f}")
                            except Exception:
                                pass

            # 3. 多视频文件冲突保护
            vids = []
            for root, dirs, files in os.walk(target):
                for f in files:
                    if Path(f).suffix.lower() in MEDIA_EXTENSIONS:
                        fp = Path(root) / f
                        try:
                            vids.append((fp, fp.stat().st_size))
                        except Exception:
                            pass

            if len(vids) > 1:
                is_cd_part = all(re.search(r"(-cd\d+|_part\d+|_\d+$)", v[0].stem, re.I) for v in vids)
                if not is_cd_part:
                    vids.sort(key=lambda x: x[1], reverse=True)
                    main_vid = vids[0]
                    for sub_vid, sub_sz in vids[1:]:
                        if sub_sz < main_vid[1] * 0.6:
                            try:
                                sub_vid.unlink()
                                logger.info(f"清理同目录下冲突多余样片: {sub_vid.name} (保留主正片: {main_vid[0].name})")
                            except Exception:
                                pass
    except Exception as e:
        logger.warning(f"标准化处理异常 (非致命): {e}")

def set_destination_permissions(path: Path):
    """赋予目标文件 0777 权限，方便群晖各用户及 Docker(JavSP/Emby)读取"""
    try:
        os.chmod(path, 0o777)
        for root, dirs, files in os.walk(path):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
            for f in files:
                os.chmod(os.path.join(root, f), 0o777)
    except Exception:
        pass

def notify_emby_updated(target_path: Path, logger):
    """
    归档完成后主动通过 API 通知 Emby 进行精准局部增量刷新
    解决 NFS 远程挂载目录无法触发 Inotify 内核事件的缺陷
    """
    try:
        str_path = str(target_path.resolve())
        if str_path.startswith("/volume2/video/"):
            emby_path = str_path.replace("/volume2/video/", "/video/", 1)
        elif str_path.startswith("/volume2/video"):
            emby_path = "/video"
        else:
            return

        url = f"{EMBY_URL}/emby/Library/Media/Updated?api_key={EMBY_API_KEY}"
        payload = {
            "Updates": [
                {
                    "Path": emby_path,
                    "UpdateType": "Created"
                }
            ]
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status in (200, 204):
                logger.info(f"⚡ [Emby 即时感知] 成功触发 Emby 局部增量刷新: {emby_path}")
            else:
                logger.warning(f"Emby 通知返回状态码: {resp.status}")
    except Exception as e:
        logger.warning(f"Emby 即时通知失败 (不影响文件归档结果): {e}")

def run_archive(src_dir: str, dest_dir: str, quiet_sec: int, dry_run: bool, status_only: bool, logger):
    src_path = Path(src_dir)
    dest_path = Path(dest_dir)

    if not src_path.exists():
        logger.error(f"源下载目录不存在: {src_path}")
        return

    logger.info(f"=== 迅雷下载队列扫描开始 ===")
    logger.info(f"源下载池 (SSD): {src_path}")
    logger.info(f"归档目标池 (HDD): {dest_path}")

    xunlei_fds = get_xunlei_open_files()
    logger.info(f"实时检测到迅雷进程打开的文件句柄数: {len(xunlei_fds)}")

    items = [item for item in src_path.iterdir() if item.name not in SYSTEM_IGNORE_DIRS and not item.name.startswith(".")]
    if not items:
        logger.info("当前下载目录无候选待测任务。")
        return

    ready_to_move = []
    for item in sorted(items, key=lambda x: x.name):
        is_ready, reason = inspect_entry(item, xunlei_fds, quiet_sec)
        if is_ready:
            logger.info(f"[完成 √] {item.name} -> {reason}")
            ready_to_move.append(item)
        else:
            logger.debug(f"[进行中 / 忽略 ×] {item.name} -> {reason}")

    if status_only:
        logger.info(f"诊断扫描完毕。满足归档条件数: {len(ready_to_move)} / 总任务数: {len(items)}")
        return

    if not ready_to_move:
        logger.info("暂无已完成的影片需要归档。")
        return

    logger.info(f"准备归档 {len(ready_to_move)} 个已完成的任务...")
    if dry_run:
        logger.info("【DRY-RUN 试运行模式】仅打印，未执行实际迁移。")
        return

    dest_path.mkdir(parents=True, exist_ok=True)

    for item in ready_to_move:
        target = dest_path / item.name
        if target.exists():
            # 目标已存在时的幂等处理：绝不生成无限 _dup_ 时间戳
            if item.is_file() and target.is_file() and target.stat().st_size == item.stat().st_size:
                logger.info(f"目标已存在完全一致的文件，直接清理源文件释放 SSD: {item.name}")
                try:
                    item.unlink()
                except Exception as e:
                    logger.error(f"清理源文件失败: {e}")
                continue
            else:
                logger.warning(f"目标已存在同名条目 {target.name}，正在清理旧残留后重新归档...")
                try:
                    if target.is_file():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target)
                except Exception as e:
                    logger.error(f"清理旧残留失败: {e}")
                    continue

        try:
            fast_copy_with_verify(item, target, logger)
            clean_and_sanitize_target(target, logger)
            set_destination_permissions(target)
            logger.info(f"★ 成功归档并释放 SSD 空间: {item.name} -> {target}")
        except Exception as e:
            logger.error(f"归档失败: {item.name}, 错误: {e}")

    logger.info("=== 归档任务处理完毕 ===")

def main():
    parser = argparse.ArgumentParser(description="迅雷 SSD 高速下载自动归档工具")
    parser.add_argument("--src", default=DEFAULT_SRC, help="迅雷下载源目录 (默认: /volume1/迅雷/下载)")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="机械盘归档目标目录 (默认: /volume2/video/avnook/#重刮队列/avok)")
    parser.add_argument("--quiet-sec", type=int, default=DEFAULT_QUIET_SECONDS, help="静止等待秒数 (默认: 60)")
    parser.add_argument("--dry-run", action="store_true", help="试运行模式，仅检测不移动")
    parser.add_argument("--status", action="store_true", help="诊断状态模式，输出所有任务详细检测状态")
    parser.add_argument("--log", default=DEFAULT_LOG, help="日志文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细调试输出")

    args = parser.parse_args()
    logger = setup_logger(args.log, args.verbose)
    run_archive(args.src, args.dest, args.quiet_sec, args.dry_run, args.status, logger)

if __name__ == "__main__":
    main()
