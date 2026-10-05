# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 视频片头广告与特征切点检测器 (Rule & Template Ad Detector)
职责：
1. 使用 ffmpeg 提取片头 120 秒内的场景硬切点 (scene change timestamps)
2. 探测切点临近区间的静音隙缝 (silencedetect) 与黑屏转场 (blackdetect)
3. 匹配工业级时间戳指纹模板库 (ad_signatures.json)
4. 输出候选切点、风险评分与关键抽帧路径
"""

import os
import sys
import json
import subprocess
import re
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

FFMPEG_BIN = "/opt/bin/ffmpeg" if os.path.exists("/opt/bin/ffmpeg") else "ffmpeg"
FFPROBE_BIN = "/opt/bin/ffprobe" if os.path.exists("/opt/bin/ffprobe") else "ffprobe"

TEMPLATES_PATH = Path(__file__).resolve().parent / "templates" / "ad_signatures.json"


@dataclass
class DetectionResult:
    is_ad: bool
    confidence: int  # 0 - 100
    candidate_cut_point: float  # 建议切除时间点 (秒)
    signature_id: Optional[str] = None
    signature_name: Optional[str] = None
    scene_timestamps: List[float] = field(default_factory=list)
    has_silence_or_black: bool = False
    extracted_frames: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_ad": self.is_ad,
            "confidence": self.confidence,
            "candidate_cut_point": round(self.candidate_cut_point, 3),
            "signature_id": self.signature_id,
            "signature_name": self.signature_name,
            "scene_count": len(self.scene_timestamps),
            "has_silence_or_black": self.has_silence_or_black,
            "extracted_frames_count": len(self.extracted_frames),
            "reason": self.reason
        }


class AdDetector:
    def __init__(self, templates_file: Optional[Path] = None):
        self.templates_file = templates_file or TEMPLATES_PATH
        self.signatures = self._load_signatures()

    def _load_signatures(self) -> List[Dict[str, Any]]:
        if not self.templates_file.exists():
            return []
        try:
            with open(self.templates_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("signatures", [])
        except Exception:
            return []

    def get_video_info(self, video_path: str) -> Optional[Dict[str, Any]]:
        """获取视频基础元数据（时长、码率、分辨率等）"""
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration,size,bit_rate",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "json", video_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode != 0:
                return None
            data = json.loads(res.stdout)
            duration = float(data.get("format", {}).get("duration", 0))
            streams = data.get("streams", [])
            vid_stream = next((s for s in streams if "width" in s and s.get("width")), {})
            return {
                "duration": duration,
                "width": vid_stream.get("width", 0),
                "height": vid_stream.get("height", 0),
                "codec": vid_stream.get("codec_name", "")
            }
        except Exception:
            return None

    def scan_scenes(self, video_path: str, scan_window_sec: float = 120.0, scene_threshold: float = 0.35) -> List[float]:
        """提取片头 scan_window_sec 秒内的场景硬切点时间戳"""
        cmd = [
            FFMPEG_BIN, "-ss", "0", "-t", str(scan_window_sec),
            "-i", video_path,
            "-vf", f"select=gt(scene\\,{scene_threshold}),showinfo",
            "-f", "null", "-"
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
            raw_pts = re.findall(r"pts_time:([0-9\.]+)", res.stderr)
            scenes = [round(float(p), 3) for p in raw_pts]
            return sorted(list(set(scenes)))
        except Exception:
            return []

    def check_silence_or_black(self, video_path: str, center_sec: float, span_sec: float = 6.0) -> bool:
        """检查切点临近区间是否存在纯静音或黑屏转场"""
        start = max(0.0, center_sec - span_sec / 2)
        cmd_black = [
            FFMPEG_BIN, "-ss", str(start), "-t", str(span_sec),
            "-i", video_path,
            "-vf", "blackdetect=d=0.08:pic_th=0.98:pix_th=0.10",
            "-f", "null", "-"
        ]
        try:
            res_b = subprocess.run(cmd_black, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10)
            if "black_start:" in res_b.stderr:
                return True
        except Exception:
            pass

        cmd_audio = [
            FFMPEG_BIN, "-ss", str(start), "-t", str(span_sec),
            "-i", video_path,
            "-af", "silencedetect=noise=-30dB:d=0.3",
            "-f", "null", "-"
        ]
        try:
            res_a = subprocess.run(cmd_audio, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10)
            if "silence_start:" in res_a.stderr:
                return True
        except Exception:
            pass

        return False

    def extract_preview_frames(self, video_path: str, timestamps: List[float], output_dir: str) -> List[str]:
        """按时间戳提取画面关键帧"""
        os.makedirs(output_dir, exist_ok=True)
        extracted = []
        for i, ts in enumerate(timestamps):
            out_file = os.path.join(output_dir, f"frame_{i:02d}_{ts:.2f}s.jpg")
            cmd = [
                FFMPEG_BIN, "-ss", str(ts),
                "-i", video_path,
                "-vframes", "1", "-q:v", "3",
                out_file, "-y"
            ]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if os.path.exists(out_file) and os.path.getsize(out_file) > 1024:
                    extracted.append(out_file)
            except Exception:
                continue
        return extracted

    def detect(self, video_path: str, temp_frame_dir: Optional[str] = None) -> DetectionResult:
        """端到端综合检测流程"""
        info = self.get_video_info(video_path)
        if not info or info["duration"] < 1200:  # 低于 20 分钟的视频不作为常规长片清洗，防止误切预告/花絮
            return DetectionResult(
                is_ad=False, confidence=0, candidate_cut_point=0.0,
                reason="视频时长过短（低于20分钟）或读取元数据失败，安全忽略"
            )

        scenes = self.scan_scenes(video_path, scan_window_sec=120.0)

        # 1. 尝试匹配工业级模板库
        for sig in self.signatures:
            target_scenes = sig.get("scene_timestamps", [])
            min_matches = sig.get("min_matched_scenes", 5)
            matched_count = 0
            for ts in target_scenes:
                if any(abs(s - ts) <= 0.08 for s in scenes):
                    matched_count += 1

            if matched_count >= min_matches:
                exp_cut = sig.get("expected_cut_point", 0.0)
                window = sig.get("cut_point_window", [exp_cut - 2.0, exp_cut + 2.0])
                # 在窗口范围内搜寻最接近的场景切点
                cand_cuts = [s for s in scenes if window[0] <= s <= window[1]]
                best_cut = cand_cuts[0] if cand_cuts else exp_cut

                has_gap = self.check_silence_or_black(video_path, best_cut)
                
                # 提取验证帧（切点前、中、后）
                frames = []
                if temp_frame_dir:
                    sample_points = [2.0, 10.0, max(0.0, best_cut - 1.5), min(info["duration"], best_cut + 2.0)]
                    frames = self.extract_preview_frames(video_path, sample_points, temp_frame_dir)

                return DetectionResult(
                    is_ad=True,
                    confidence=sig.get("confidence", 95),
                    candidate_cut_point=best_cut,
                    signature_id=sig.get("id"),
                    signature_name=sig.get("name"),
                    scene_timestamps=scenes,
                    has_silence_or_black=has_gap,
                    extracted_frames=frames,
                    reason=f"命中已知广告指纹模板 [{sig.get('name')}] (匹配切点: {matched_count}/{len(target_scenes)})"
                )

        # 2. 启发式统计规则初筛（未命中模板但前30秒有高密场景切换）
        first_30s_scenes = [s for s in scenes if s <= 30.0]
        if len(first_30s_scenes) >= 6:
            # 寻找片头 40s ~ 115s 之间的长场景跳变点作为疑似切点
            potential_cuts = [s for s in scenes if 45.0 <= s <= 110.0]
            if potential_cuts:
                cand_cut = potential_cuts[-1]
                has_gap = self.check_silence_or_black(video_path, cand_cut)
                frames = []
                if temp_frame_dir:
                    sample_points = [2.0, 10.0, cand_cut - 1.0, cand_cut + 2.0]
                    frames = self.extract_preview_frames(video_path, sample_points, temp_frame_dir)

                return DetectionResult(
                    is_ad=True,
                    confidence=70,  # 规则初筛嫌疑分，待后续 OCR 与 AI 验证
                    candidate_cut_point=cand_cut,
                    signature_id="heuristic_high_density",
                    signature_name="高频场景切换可疑广告",
                    scene_timestamps=scenes,
                    has_silence_or_black=has_gap,
                    extracted_frames=frames,
                    reason=f"前30秒检测到 {len(first_30s_scenes)} 次高频镜头切换，疑似商业拼盘广告"
                )

        return DetectionResult(
            is_ad=False,
            confidence=0,
            candidate_cut_point=0.0,
            scene_timestamps=scenes,
            reason="未检测到已知广告指纹或高密场景切换，属于普通正片"
        )
