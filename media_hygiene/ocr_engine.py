# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 轻量 OCR 提取与敏感特征评分引擎 (Edge OCR & Keyword Scoring)
职责：
1. 对抽帧图片进行文字识别（支持本地 GLM-OCR / 容器化端点，兼容离线降级）
2. 构建双向特征词典（正向商业广告词 vs 官方版权保护词）
3. 计算广告置信度分值，为后续 AI 审核与规则决策提供关键结构化特征
"""

import os
import sys
import re
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any


@dataclass
class OCRResult:
    frame_texts: Dict[str, str] = field(default_factory=dict)
    matched_ad_keywords: List[str] = field(default_factory=list)
    matched_official_keywords: List[str] = field(default_factory=list)
    ad_text_score: int = 0  # 0 - 100
    is_official_intro: bool = False
    is_commercial_ad: bool = False
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched_ad_keywords": self.matched_ad_keywords,
            "matched_official_keywords": self.matched_official_keywords,
            "ad_text_score": self.ad_text_score,
            "is_official_intro": self.is_official_intro,
            "is_commercial_ad": self.is_commercial_ad,
            "extracted_text_summary": " | ".join(filter(None, self.frame_texts.values()))[:200]
        }


class OCREngine:
    # 商业博彩/引流高频特征词
    AD_KEYWORDS = [
        "体育", "赞助", "开云", "乐鱼", "官方直营", "官方自营", "约炮", "美女荷官",
        "聊天室", "福利姬", "充值", "扫码", "最新地址", "最新情报", "精彩继续",
        "裸聊", "成人快手", "成人抖音", "台湾uu", "永久地址", "防屏蔽", "送豪礼",
        ".com", ".vip", ".cc", ".xyz", ".me", ".shop", ".ag", "hth", "91"
    ]

    # 日本官方厂牌/版权警告词（强安全护栏，命中代表绝对合规，严禁误删）
    OFFICIAL_KEYWORDS = [
        "ご注意", "フィクション", "18歳未満", "無断", "貸出", "上映", "複製",
        "法律", "禁止", "撮影", "出演", "映倫", "vsic", "ビジュアルソフト",
        "ippa", "moodyz", "s1", "sod", "ideapocket", "prestige", "soft on demand"
    ]

    def __init__(self, endpoint_url: Optional[str] = None):
        """
        :param endpoint_url: 本地 GLM-OCR 接口，如 http://127.0.0.1:18090/v1/ocr
        """
        self.endpoint_url = endpoint_url or os.environ.get("GLM_OCR_URL", "")

    def extract_text_from_image(self, image_path: str) -> str:
        """从单张图像提取文字。若未配置远程端点或脱网，返回空字符串，依赖大模型直接看图"""
        if not self.endpoint_url or not os.path.exists(image_path):
            return ""

        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            import base64
            payload = json.dumps({"image_b64": base64.b64encode(img_bytes).decode("ascii")}).encode("utf-8")
            req = urllib.request.Request(self.endpoint_url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("text", "")
        except Exception:
            return ""

    def evaluate_text(self, text: str) -> Tuple[List[str], List[str], int, bool, bool]:
        """对一段文本进行双向敏感词打分"""
        text_lower = text.lower()
        matched_ads = []
        matched_official = []

        for kw in self.AD_KEYWORDS:
            if kw.lower() in text_lower:
                matched_ads.append(kw)

        for kw in self.OFFICIAL_KEYWORDS:
            if kw.lower() in text_lower:
                matched_official.append(kw)

        # 官方版权词具有强一票否决权
        is_official = len(matched_official) >= 1
        
        # 计算广告分：每个独立广告词提供 25 分，封顶 100 分
        score = min(100, len(matched_ads) * 25)
        if is_official:
            score = 0  # 官方片头一票清零广告嫌疑

        is_commercial = (score >= 50) and not is_official
        return matched_ads, matched_official, score, is_official, is_commercial

    def process_frames(self, image_paths: List[str]) -> OCRResult:
        """批量处理抽帧图片并生成汇总报告"""
        frame_texts = {}
        all_text_blocks = []

        for img in image_paths:
            txt = self.extract_text_from_image(img)
            frame_texts[os.path.basename(img)] = txt
            if txt:
                all_text_blocks.append(txt)

        combined_text = " \n ".join(all_text_blocks)
        matched_ads, matched_official, score, is_official, is_commercial = self.evaluate_text(combined_text)

        return OCRResult(
            frame_texts=frame_texts,
            matched_ad_keywords=matched_ads,
            matched_official_keywords=matched_official,
            ad_text_score=score,
            is_official_intro=is_official,
            is_commercial_ad=is_commercial
        )
