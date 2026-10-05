# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 智能媒体卫生总装调度流水线 (Hygiene Master Pipeline)
职责：
串联 规则检测 -> OCR边缘提取 -> AI大模型仲裁 -> 无损流清洗 -> 审计落库 全链路
"""

import os
import sys
import re
import tempfile
import shutil
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any

from .ad_detector import AdDetector, DetectionResult
from .ocr_engine import OCREngine, OCRResult
from .ai_reviewer import AIReviewer, ReviewDecision
from .cleaner import MediaCleaner, CleanResult
from .history import HygieneHistory

logger = logging.getLogger("media_hygiene")


def extract_avid_from_filename(filename: str) -> str:
    """从文件名解析标准番号，如 SUN-055-C.mp4 -> SUN-055"""
    m = re.search(r"([A-Za-z0-9]+-[0-9]+)", filename)
    if m:
        return m.group(1).upper()
    return Path(filename).stem.upper()


def process_video_hygiene(
    video_path: str,
    dry_run: bool = False,
    min_confidence: int = 85,
    trash_root: str = "/volume2/video/.media_trash"
) -> Dict[str, Any]:
    """
    单部影片媒体卫生综合处理入口
    """
    v_path = Path(video_path)
    avid = extract_avid_from_filename(v_path.name)
    result_report = {
        "avid": avid,
        "video_path": str(video_path),
        "status": "PASS",  # PASS, CLEANED, REVIEW_PENDING, SKIPPED
        "action_taken": "NONE",
        "cut_seconds": 0.0,
        "confidence": 0,
        "reason": ""
    }

    if not v_path.exists() or not v_path.is_file():
        result_report["status"] = "SKIPPED"
        result_report["reason"] = "目标文件不存在"
        return result_report

    # 1. 创建专用抽帧临时目录
    temp_dir = tempfile.mkdtemp(prefix=f"hygiene_{avid}_")
    try:
        detector = AdDetector()
        det_res = detector.detect(str(v_path), temp_frame_dir=temp_dir)

        if not det_res.is_ad:
            result_report["status"] = "PASS"
            result_report["reason"] = det_res.reason
            return result_report

        # 2. 提取帧文字 OCR
        ocr_engine = OCREngine()
        ocr_res = ocr_engine.process_frames(det_res.extracted_frames)

        # 官方版权词一票否决
        if ocr_res.is_official_intro:
            result_report["status"] = "PASS"
            result_report["reason"] = f"检出官方版权/警告标识 ({','.join(ocr_res.matched_official_keywords)})，安全保留"
            return result_report

        # 3. 远端大模型审查 (AI Reviewer)
        reviewer = AIReviewer()
        ocr_summary = " | ".join(filter(None, ocr_res.frame_texts.values()))
        review_dec = reviewer.review(
            avid=avid,
            candidate_cut=det_res.candidate_cut_point,
            frame_paths=det_res.extracted_frames,
            ocr_text=ocr_summary,
            rule_reason=det_res.reason
        )

        result_report["confidence"] = review_dec.confidence_score
        result_report["cut_seconds"] = review_dec.recommended_cut_point

        if not review_dec.safe_to_cut or review_dec.confidence_score < min_confidence:
            result_report["status"] = "REVIEW_PENDING"
            result_report["reason"] = f"未达安全切除置信度或存疑: {review_dec.reason}"
            return result_report

        # 4. 执行清洗动作
        if dry_run:
            result_report["status"] = "CLEANED_DRY_RUN"
            result_report["action_taken"] = "WOULD_CUT"
            result_report["reason"] = f"[DryRun 模拟] {review_dec.reason}"
            return result_report

        cleaner = MediaCleaner(trash_root=trash_root)
        clean_res = cleaner.clean_ad_intro(
            video_path=str(v_path),
            cut_point_sec=review_dec.recommended_cut_point,
            avid=avid,
            confidence=review_dec.confidence_score,
            reason=f"[{review_dec.model_name}] {review_dec.reason}"
        )

        if clean_res.success:
            result_report["status"] = "CLEANED"
            result_report["action_taken"] = "LOSSLESS_CUT"
            result_report["reason"] = f"成功切除片头 {clean_res.cut_seconds:.1f}s 广告，耗时 {clean_res.elapsed_seconds:.1f}s"
        else:
            result_report["status"] = "FAILED"
            result_report["reason"] = f"切除执行中断: {clean_res.error_message}"

        return result_report

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
