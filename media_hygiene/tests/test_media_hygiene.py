# -*- coding: utf-8 -*-
import unittest
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from media_hygiene.ad_detector import AdDetector, DetectionResult
from media_hygiene.ocr_engine import OCREngine, OCRResult
from media_hygiene.ai_reviewer import AIReviewer, ReviewDecision
from media_hygiene.history import HygieneHistory, CleanRecord
from media_hygiene.pipeline import process_video_hygiene


class TestMediaHygiene(unittest.TestCase):
    def setUp(self):
        self.detector = AdDetector()
        self.ocr = OCREngine()

    def test_01_template_loading(self):
        """测试工业级时间戳指纹模板加载"""
        self.assertGreater(len(self.detector.signatures), 0)
        sig_ids = [s["id"] for s in self.detector.signatures]
        self.assertIn("leyu_uu_standard_75s", sig_ids)
        self.assertIn("uu_qr_long_103s", sig_ids)
        self.assertIn("91_hth_kaiyun_71s", sig_ids)

    def test_02_ocr_scoring_commercial_ad(self):
        """测试 OCR 正向广告特征计分"""
        sample_ad_text = "乐鱼体育 赞助 官方直营 200151.com 送豪礼 最新地址"
        matched_ads, matched_off, score, is_off, is_comm = self.ocr.evaluate_text(sample_ad_text)
        self.assertGreaterEqual(score, 75)
        self.assertTrue(is_comm)
        self.assertFalse(is_off)
        self.assertIn("体育", matched_ads)
        self.assertIn("赞助", matched_ads)

    def test_03_ocr_scoring_official_intro_blocking(self):
        """测试官方版权/法律警告词强阻断一票否决"""
        sample_official = "ご注意 当作品は完全なるフィクションです。18歳未満の方は視聴できません。法律により無断複製禁止。"
        matched_ads, matched_off, score, is_off, is_comm = self.ocr.evaluate_text(sample_official)
        self.assertEqual(score, 0)
        self.assertTrue(is_off)
        self.assertFalse(is_comm)
        self.assertIn("ご注意", matched_off)
        self.assertIn("18歳未満", matched_off)

    def test_04_ai_reviewer_offline_fallback(self):
        """测试 AI 审核器在无远程响应时的确定性离线降级"""
        reviewer = AIReviewer(api_key="fake-test-key", proxy_url=None)
        
        # 针对包含官方词汇的场景，应强制拒绝切除
        dec_off = reviewer.review("TEST-001", 15.0, [], ocr_text="ご注意 18歳未満", rule_reason="疑似切换")
        self.assertFalse(dec_off.safe_to_cut)
        self.assertTrue(dec_off.is_official_intro)

        # 针对明确命中乐鱼模板的场景，离线降级应安全放行
        dec_ad = reviewer.review("TEST-002", 75.7, [], ocr_text="乐鱼体育", rule_reason="命中乐鱼体育模板")
        self.assertTrue(dec_ad.safe_to_cut)
        self.assertTrue(dec_ad.is_advertisement)

    def test_05_history_db_lifecycle(self):
        """测试审计历史数据库的新增、查询、回滚标记与修剪"""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp_db:
            db_p = Path(tmp_db.name)
            hist = HygieneHistory(db_path=db_p)
            
            # 记录一次清洗
            rec_id = hist.record_clean(
                avid="ABC-123",
                file_path="/tmp/test/ABC-123.mp4",
                trash_path="/tmp/trash/ABC-123.mp4",
                orig_dur=7200.0,
                clean_dur=7125.0,
                cut_sec=75.0,
                confidence=98,
                reason="Unit test"
            )
            self.assertGreater(rec_id, 0)

            # 查询有效记录
            latest = hist.get_latest_by_avid("ABC-123")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.cut_seconds, 75.0)
            self.assertEqual(latest.status, "active")

            # 标记回滚
            hist.mark_rolled_back(rec_id)
            latest_after = hist.get_latest_by_avid("ABC-123")
            self.assertIsNone(latest_after)

    def test_06_real_sample_dry_run(self):
        """对 NAS 上真实存在的样本执行非破坏性 Dry-Run 校验"""
        sun_path = "/volume2/video/avnook/#整理完成/#未知女优/[SUN-055-C] 濡れ透け露出 Mカップ爆乳美女と汁だく野外交尾/SUN-055-C.mp4"
        if os.path.exists(sun_path):
            res = process_video_hygiene(sun_path, dry_run=True)
            self.assertEqual(res["avid"], "SUN-055")
            self.assertEqual(res["status"], "CLEANED_DRY_RUN")
            self.assertAlmostEqual(res["cut_seconds"], 75.72, delta=1.5)
            self.assertGreaterEqual(res["confidence"], 85)


if __name__ == "__main__":
    unittest.main()
