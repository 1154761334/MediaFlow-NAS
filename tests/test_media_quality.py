#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Automation v2.0R — 质量规则与边界回归测试套件 (11 项核心测试)
"""

import unittest
import sys
import base64
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from gate.media_quality import (
        extract_canonical_avid,
        parse_quality_tags,
        evaluate_quality_decision,
        evaluate_quality_decision_multi,
        normalize_btih
    )
except ImportError:
    from media_quality import (
        extract_canonical_avid,
        parse_quality_tags,
        evaluate_quality_decision,
        evaluate_quality_decision_multi,
        normalize_btih
    )

class TestMediaQualityRegression(unittest.TestCase):
    # Test 1: local SD + candidate UNKNOWN -> REVIEW (P0 UNKNOWN Bug 验证)
    def test_01_local_sd_vs_candidate_unknown(self):
        local = {"resolution": "SD", "is_unc": False, "is_sub": False, "codec": "H264"}
        cand = {"resolution": "UNKNOWN", "is_unc": False, "is_sub": False, "codec": "H264"}
        dec, _ = evaluate_quality_decision(local, cand)
        self.assertEqual(dec, "REVIEW", "local SD vs candidate UNKNOWN 必须返回 REVIEW，绝不能因为 Rank 判定为 UPGRADE")

    # Test 2: local 1080P + candidate UNKNOWN -> REVIEW
    def test_02_local_1080p_vs_candidate_unknown(self):
        local = {"resolution": "1080P", "is_unc": False, "is_sub": False, "codec": "H264"}
        cand = {"resolution": "UNKNOWN", "is_unc": False, "is_sub": False, "codec": "H264"}
        dec, _ = evaluate_quality_decision(local, cand)
        self.assertEqual(dec, "REVIEW")

    # Test 3: local [4K censored, 1080P UNC] vs candidate 4K UNC -> UPGRADE (多版本非虚构合并验证)
    def test_03_local_multi_versions_vs_candidate_4k_unc(self):
        locals_list = [
            {"resolution": "4K", "is_unc": False, "is_sub": False, "codec": "H264", "variant": "4K"},
            {"resolution": "1080P", "is_unc": True, "is_sub": True, "codec": "HEVC", "variant": "UNC+SUB"}
        ]
        cand = {"resolution": "4K", "is_unc": True, "is_sub": False, "codec": "H264", "variant": "UNC+4K"}
        dec, reason = evaluate_quality_decision_multi(locals_list, cand)
        self.assertEqual(dec, "UPGRADE", f"真实 4K UNC 相比本地各真实版本为显著升级，不得被虚构超级版本误杀: {reason}")

    # Test 4: local [4K UNC] vs candidate 4K UNC -> SKIP
    def test_04_local_4k_unc_vs_candidate_4k_unc(self):
        locals_list = [
            {"resolution": "4K", "is_unc": True, "is_sub": False, "codec": "HEVC", "variant": "UNC+4K"}
        ]
        cand = {"resolution": "4K", "is_unc": True, "is_sub": False, "codec": "H264", "variant": "UNC+4K"}
        dec, _ = evaluate_quality_decision_multi(locals_list, cand)
        self.assertEqual(dec, "SKIP")

    # Test 5: local 1080P HEVC SUB vs candidate 1080P H264 SUB -> SKIP (HEVC 资产保护法)
    def test_05_hevc_protection(self):
        local = {"resolution": "1080P", "is_unc": False, "is_sub": True, "codec": "HEVC"}
        cand = {"resolution": "1080P", "is_unc": False, "is_sub": True, "codec": "H264"}
        dec, reason = evaluate_quality_decision(local, cand)
        self.assertEqual(dec, "SKIP")
        self.assertIn("HEVC", reason)

    # Test 6: local 1080P HEVC vs candidate 4K H264 -> UPGRADE (像素升维优先)
    def test_06_resolution_upgrade_over_hevc(self):
        local = {"resolution": "1080P", "is_unc": False, "is_sub": True, "codec": "HEVC"}
        cand = {"resolution": "4K", "is_unc": False, "is_sub": True, "codec": "H264"}
        dec, reason = evaluate_quality_decision(local, cand)
        self.assertEqual(dec, "UPGRADE")
        self.assertIn("分辨率", reason)

    # Test 7: ffprobe exception / 无法获取参数 -> 必须 REVIEW，绝不能 size-based UPGRADE
    def test_07_ffprobe_failure_fallback_review(self):
        local = {"resolution": "1080P", "is_unc": False, "is_sub": False, "codec": "H264"}
        # 异常或空 profile
        cand_empty = {}
        dec, reason = evaluate_quality_decision(local, cand_empty)
        self.assertEqual(dec, "REVIEW")
        self.assertNotIn("体积", reason)

    # Test 8: same BTIH in hex and base32 -> normalize_btih identical
    def test_08_btih_hex_base32_normalization(self):
        # 40-char hex
        hex_btih = "76a38fb7568225c64fcdce868ce0bb0a6cd20793"
        # 对应 32-char base32
        b32_btih = base64.b32encode(bytes.fromhex(hex_btih)).decode("ascii").lower()
        self.assertEqual(normalize_btih(hex_btih), hex_btih)
        self.assertEqual(normalize_btih(b32_btih), hex_btih)

    # Test 9: AVID 提取规范化与特殊格式
    def test_09_canonical_avid_special_cases(self):
        self.assertEqual(extract_canonical_avid("CPZ69-015 高清"), "CPZ-69015")
        self.assertEqual(extract_canonical_avid("420ERK-111 高清"), "ERK-111")
        self.assertEqual(extract_canonical_avid("MVSD- 593- C 高清 字幕"), "MVSD-593")

    # Test 10: actual 6K ffprobe 参数 -> 6K
    def test_10_actual_6k_detection(self):
        # 5760 x 2880
        p = parse_quality_tags("test.mp4", width=5760, height=2880, codec="hevc")
        self.assertEqual(p["resolution"], "6K")
        self.assertEqual(p["rank"], 4)

    # Test 11: local 4K censored vs candidate 1080P UNC -> UPGRADE (内容升级多版本并存，但旧 4K 绝不能被删)
    def test_11_4k_censored_vs_1080p_unc(self):
        local = {"resolution": "4K", "is_unc": False, "is_sub": False, "codec": "HEVC", "variant": "4K"}
        cand = {"resolution": "1080P", "is_unc": True, "is_sub": False, "codec": "H264", "variant": "UNC"}
        dec, reason = evaluate_quality_decision(local, cand)
        # 它是无码内容重大升级
        self.assertEqual(dec, "UPGRADE")
        self.assertIn("无码", reason)

if __name__ == "__main__":
    unittest.main()
