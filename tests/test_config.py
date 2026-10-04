import unittest
import os
from pathlib import Path
from core.config import load_config, find_config_path, DEFAULT_CONFIG

class TestConfigLoading(unittest.TestCase):
    def test_default_config_structure(self):
        cfg = load_config()
        self.assertIn("paths", cfg)
        self.assertIn("scheduler", cfg)
        self.assertIn("stall", cfg)
        self.assertIn("storage", cfg)
        self.assertIn("xunlei", cfg)
        self.assertEqual(cfg["scheduler"]["target_active"], 32)
        self.assertEqual(cfg["storage"]["stop_free_gb"], 120.0)

    def test_find_config_path_exists(self):
        p = find_config_path()
        self.assertTrue(p.exists(), f"Configuration path should exist: {p}")

if __name__ == "__main__":
    unittest.main()
