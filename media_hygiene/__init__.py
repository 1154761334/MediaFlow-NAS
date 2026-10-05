# -*- coding: utf-8 -*-
"""
Media Automation v2.1: Media Hygiene Package
提供智能片头拼接广告检测、OCR 文本提取、远端大模型审查、无损流裁剪与版本回滚管理
"""

from .ad_detector import AdDetector, DetectionResult
from .ocr_engine import OCREngine, OCRResult
from .ai_reviewer import AIReviewer, ReviewDecision
from .cleaner import MediaCleaner, CleanResult
from .history import HygieneHistory, CleanRecord
from .pipeline import process_video_hygiene

__all__ = [
    "AdDetector",
    "DetectionResult",
    "OCREngine",
    "OCRResult",
    "AIReviewer",
    "ReviewDecision",
    "MediaCleaner",
    "CleanResult",
    "HygieneHistory",
    "CleanRecord",
    "process_video_hygiene"
]
