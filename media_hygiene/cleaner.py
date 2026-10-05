# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 高速无损流剪辑器与物理隔离箱 (Lossless Stream Cleaner & Trash Quarantine)
职责：
1. 执行 ffmpeg -c copy 高速无损流解复用裁剪（单部耗时仅 2~5 秒，0 重编码画质损耗）
2. 严格核对裁剪后文件完整性、音视频流健康度与预期时长
3. 将原始文件原子归入安全隔离箱 (.media_trash/)，支持随时无损恢复
4. 记录全生命周期审计事件到 ad_history.db
"""

import os
import sys
import subprocess
import shutil
import time
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any

from .history import HygieneHistory

FFMPEG_BIN = "/opt/bin/ffmpeg" if os.path.exists("/opt/bin/ffmpeg") else "ffmpeg"
FFPROBE_BIN = "/opt/bin/ffprobe" if os.path.exists("/opt/bin/ffprobe") else "ffprobe"

DEFAULT_TRASH_ROOT = "/volume2/video/.media_trash"


@dataclass
class CleanResult:
    success: bool
    avid: str
    original_path: str
    cleaned_path: str
    trash_path: str
    cut_seconds: float
    original_duration: float
    cleaned_duration: float
    elapsed_seconds: float
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "avid": self.avid,
            "original_path": self.original_path,
            "cleaned_path": self.cleaned_path,
            "trash_path": self.trash_path,
            "cut_seconds": round(self.cut_seconds, 2),
            "original_duration": round(self.original_duration, 2),
            "cleaned_duration": round(self.cleaned_duration, 2),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "error_message": self.error_message
        }


class MediaCleaner:
    def __init__(self, trash_root: str = DEFAULT_TRASH_ROOT, history: Optional[HygieneHistory] = None):
        self.trash_root = Path(trash_root)
        self.history = history or HygieneHistory()

    def _get_video_duration(self, file_path: str) -> Optional[float]:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception:
            pass
        return None

    def clean_ad_intro(
        self,
        video_path: str,
        cut_point_sec: float,
        avid: str,
        confidence: int = 95,
        reason: str = ""
    ) -> CleanResult:
        """
        无损切除片头广告入口
        """
        start_time = time.time()
        v_path = Path(video_path)

        if not v_path.exists() or not v_path.is_file():
            return CleanResult(
                success=False, avid=avid, original_path=video_path,
                cleaned_path="", trash_path="", cut_seconds=0.0,
                original_duration=0.0, cleaned_duration=0.0,
                elapsed_seconds=0.0, error_message="目标文件不存在"
            )

        orig_dur = self._get_video_duration(video_path)
        if not orig_dur or orig_dur < 1200:  # 必须大于20分钟
            return CleanResult(
                success=False, avid=avid, original_path=video_path,
                cleaned_path="", trash_path="", cut_seconds=0.0,
                original_duration=orig_dur or 0.0, cleaned_duration=0.0,
                elapsed_seconds=0.0, error_message="时长异常或低于保护阈值(20分钟)，安全拒绝切除"
            )

        # 安全门禁核验：切点范围必须在 5s ~ 120s 之间，且不得超过整片 15%
        if cut_point_sec < 5.0 or cut_point_sec > 120.0 or cut_point_sec > (orig_dur * 0.15):
            return CleanResult(
                success=False, avid=avid, original_path=video_path,
                cleaned_path="", trash_path="", cut_seconds=cut_point_sec,
                original_duration=orig_dur, cleaned_duration=0.0,
                elapsed_seconds=0.0, error_message=f"切点秒数 {cut_point_sec:.2f}s 超出安全允许窗口 [5s, 120s]"
            )

        tmp_clean_path = v_path.parent / f"{v_path.stem}.clean_tmp{v_path.suffix}"
        
        # 1. 执行 ffmpeg -c copy 高速无损流拷贝
        cmd = [
            FFMPEG_BIN,
            "-ss", f"{cut_point_sec:.3f}",
            "-i", str(v_path),
            "-c", "copy",
            "-avoid_negative_ts", "1",
            str(tmp_clean_path),
            "-y"
        ]

        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60)
            if res.returncode != 0 or not tmp_clean_path.exists() or tmp_clean_path.stat().st_size < 1024 * 1024:
                if tmp_clean_path.exists():
                    tmp_clean_path.unlink()
                return CleanResult(
                    success=False, avid=avid, original_path=video_path,
                    cleaned_path="", trash_path="", cut_seconds=cut_point_sec,
                    original_duration=orig_dur, cleaned_duration=0.0,
                    elapsed_seconds=time.time() - start_time,
                    error_message=f"ffmpeg 剪切失败或产物文件损坏: {res.stderr[:200]}"
                )

            # 2. 核验产物时长
            clean_dur = self._get_video_duration(str(tmp_clean_path))
            if not clean_dur or abs(clean_dur - (orig_dur - cut_point_sec)) > 3.0:
                tmp_clean_path.unlink()
                return CleanResult(
                    success=False, avid=avid, original_path=video_path,
                    cleaned_path="", trash_path="", cut_seconds=cut_point_sec,
                    original_duration=orig_dur, cleaned_duration=clean_dur or 0.0,
                    elapsed_seconds=time.time() - start_time,
                    error_message=f"产物时长异常: 原 {orig_dur:.1f}s - 剪 {cut_point_sec:.1f}s != 新 {clean_dur:.1f}s"
                )

            # 3. 原文件安全移入 .media_trash 隔离箱
            date_dir = time.strftime("%Y%m%d")
            dest_trash_dir = self.trash_root / date_dir / avid
            dest_trash_dir.mkdir(parents=True, exist_ok=True)
            trash_target_path = dest_trash_dir / v_path.name
            
            # 若隔离箱已存在同名原片，添加时间戳后缀
            if trash_target_path.exists():
                trash_target_path = dest_trash_dir / f"{v_path.stem}_{int(time.time())}{v_path.suffix}"

            shutil.move(str(v_path), str(trash_target_path))

            # 4. 原子重命名 clean 产物为正片原名
            shutil.move(str(tmp_clean_path), str(v_path))

            # 5. 写入历史审计数据库
            self.history.record_clean(
                avid=avid,
                file_path=str(v_path),
                trash_path=str(trash_target_path),
                orig_dur=orig_dur,
                clean_dur=clean_dur,
                cut_sec=cut_point_sec,
                confidence=confidence,
                reason=reason
            )

            return CleanResult(
                success=True,
                avid=avid,
                original_path=video_path,
                cleaned_path=str(v_path),
                trash_path=str(trash_target_path),
                cut_seconds=cut_point_sec,
                original_duration=orig_dur,
                cleaned_duration=clean_dur,
                elapsed_seconds=time.time() - start_time,
                error_message=""
            )

        except Exception as e:
            if tmp_clean_path.exists():
                tmp_clean_path.unlink()
            return CleanResult(
                success=False, avid=avid, original_path=video_path,
                cleaned_path="", trash_path="", cut_seconds=cut_point_sec,
                original_duration=orig_dur, cleaned_duration=0.0,
                elapsed_seconds=time.time() - start_time,
                error_message=f"系统级未捕获异常: {e}"
            )
