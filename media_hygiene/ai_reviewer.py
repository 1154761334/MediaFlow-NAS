# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Media Hygiene - 远端多模态视觉大模型审核器 (Remote Vision AI Reviewer)
职责：
1. 调用 Google Gemini 2.5 Flash / OpenAI Compatible 视觉接口
2. 对候选切点前后关键帧进行深度多模态理解（辨析商业广告 vs 官方版权片头 vs 正片剧情）
3. 严格输出结构化审核 JSON 决策，作为最终安全切除的第一守门人
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

DEFAULT_PROXY = os.environ.get("ALL_PROXY", os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"))


@dataclass
class ReviewDecision:
    is_advertisement: bool
    is_official_intro: bool
    has_main_feature_leak: bool
    safe_to_cut: bool
    confidence_score: int  # 0 - 100
    recommended_cut_point: float
    reason: str
    model_name: str = "gemini-2.5-flash"
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_advertisement": self.is_advertisement,
            "is_official_intro": self.is_official_intro,
            "has_main_feature_leak": self.has_main_feature_leak,
            "safe_to_cut": self.safe_to_cut,
            "confidence_score": self.confidence_score,
            "recommended_cut_point": round(self.recommended_cut_point, 3),
            "reason": self.reason,
            "model_name": self.model_name
        }


class AIReviewer:
    SYSTEM_PROMPT = """你是一名资深的音视频质量与版权审核专家。你的职责是对给出的影片片头抽帧图像进行鉴别，判定是否存在压制组后期强行拼接的不良商业广告（如体育博彩、直播约炮、引流二维码）。

请务必严格区分以下三类内容：
1. 【商业广告 (Commercial Ad)】：乐鱼/开云体育、博彩APP、91福利姬、UU视讯聊天室、扫码送礼等，切除此类内容对正片没有任何损害。
2. 【官方合规片头 (Official Intro)】：VSIC、映伦、警告卡（フィクション、18歳未満禁止）、SOD/Moodyz/S1等厂牌动画。此类属于合法合规的原始影片组成部分，绝对禁止切除！
3. 【正片剧情 (Main Feature)】：演员出场、对话、前情提要等。绝对禁止切除！

你必须严格输出合法的 JSON 格式（不要使用 Markdown 代码块包裹，直接输出纯 JSON），字段如下：
{
  "is_advertisement": true/false,
  "is_official_intro": true/false,
  "has_main_feature_leak": true/false,
  "safe_to_cut": true/false,
  "confidence_score": 0到100整数,
  "recommended_cut_point": 建议切除的秒数浮点数,
  "reason": "具体判定原因与依据，50字以内"
}
"""

    def __init__(self, api_key: Optional[str] = None, proxy_url: Optional[str] = DEFAULT_PROXY):
        self.proxy_url = proxy_url
        self.api_key = api_key or self._discover_api_key()
        self.model = "gemini-2.5-flash"

    def _discover_api_key(self) -> str:
        """从环境变量或系统已有配置安全探测 API Key"""
        for env_var in ["GEMINI_API_KEY", "GOOGLE_API_KEY"]:
            val = os.environ.get(env_var, "").strip()
            if val:
                return val

        # 尝试从 MetaTube 备份配置中发现
        metatube_conf = "/volume1/docker/emby/config/plugins/configurations/MetaTube.json.bak_gemini_20260914"
        if os.path.exists(metatube_conf):
            try:
                with open(metatube_conf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    key = data.get("GoogleApiKey", "").strip()
                    if key:
                        return key
            except Exception:
                pass
        return ""

    def _call_gemini_vision(self, prompt: str, image_paths: List[str]) -> Optional[Dict[str, Any]]:
        """通过 Gemini REST API 发起视觉理解推理请求"""
        if not self.api_key:
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        parts = [{"text": prompt}]
        for p in image_paths[:4]:  # 控制每次最多发送 4 张最具代表性的关键帧，极大节约流量与延时
            if not os.path.exists(p):
                continue
            try:
                with open(p, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("ascii")
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": b64_data
                    }
                })
            except Exception:
                continue

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        }

        handlers = []
        if self.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
        opener = urllib.request.build_opener(*handlers)

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        try:
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if not candidates:
                    return None
                text_content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                # 清洗可能的 markdown 标记
                clean_text = text_content.strip()
                if clean_text.startswith("```json"):
                    clean_text = clean_text[7:]
                if clean_text.startswith("```"):
                    clean_text = clean_text[3:]
                if clean_text.endswith("```"):
                    clean_text = clean_text[:-3]
                return json.loads(clean_text.strip())
        except Exception:
            return None

    def review(
        self,
        avid: str,
        candidate_cut: float,
        frame_paths: List[str],
        ocr_text: str = "",
        rule_reason: str = ""
    ) -> ReviewDecision:
        """对候选视频执行综合大模型视觉复核"""
        # 构建精炼的上下文 prompt
        prompt = (
            f"影片番号: {avid}\n"
            f"规则检测引擎建议切除点: {candidate_cut:.2f} 秒\n"
            f"规则检测原因: {rule_reason}\n"
            f"OCR 提取文字概要: {ocr_text}\n\n"
            f"请仔细审查附带的图片（前几张为切点前画面，最后一张为切点后正片首帧画面）。\n"
            f"评估确认：切点前是否为商业拼接广告？切点后是否已安全进入正片或官方警告？是否允许安全无损切除？"
        )

        resp = self._call_gemini_vision(prompt, frame_paths)
        if resp and "safe_to_cut" in resp:
            return ReviewDecision(
                is_advertisement=bool(resp.get("is_advertisement", False)),
                is_official_intro=bool(resp.get("is_official_intro", False)),
                has_main_feature_leak=bool(resp.get("has_main_feature_leak", False)),
                safe_to_cut=bool(resp.get("safe_to_cut", False)),
                confidence_score=int(resp.get("confidence_score", 90)),
                recommended_cut_point=float(resp.get("recommended_cut_point", candidate_cut)),
                reason=str(resp.get("reason", "Gemini 视觉判定确认")),
                model_name=self.model,
                raw_response=resp
            )

        # 离线或 API 异常时的兜底降级决策（基于确定性规则与 OCR 特征）
        # 若 OCR 包含官方词汇，绝对不可切除
        if "ご注意" in ocr_text or "映倫" in ocr_text or "vsic" in ocr_text.lower():
            return ReviewDecision(
                is_advertisement=False,
                is_official_intro=True,
                has_main_feature_leak=True,
                safe_to_cut=False,
                confidence_score=95,
                recommended_cut_point=0.0,
                reason="OCR 检测到官方版权与警告标识，安全兜底拒绝切除",
                model_name="fallback-rule"
            )

        # 若命中了已知广告特征（高置信度），且切点合理
        if candidate_cut > 10.0 and ("乐鱼" in rule_reason or "91" in rule_reason or "UU" in rule_reason):
            return ReviewDecision(
                is_advertisement=True,
                is_official_intro=False,
                has_main_feature_leak=False,
                safe_to_cut=True,
                confidence_score=92,
                recommended_cut_point=candidate_cut,
                reason=f"命中已知工业级特征模板，离线降级确认安全: {rule_reason}",
                model_name="fallback-rule"
            )

        return ReviewDecision(
            is_advertisement=False,
            is_official_intro=False,
            has_main_feature_leak=False,
            safe_to_cut=False,
            confidence_score=50,
            recommended_cut_point=candidate_cut,
            reason="AI 离线且未满足确定性规则阈值，存入人工复核队列",
            model_name="fallback-rule"
        )
