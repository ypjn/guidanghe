"""AI 厂商识别与设置持久化。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="archivebox_ai_detect_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from ai_fallback import (  # noqa: E402
    DEEPSEEK_BASE, DEEPSEEK_MODEL, AiSettings,
    detect_from_paste, load_settings, save_settings,
)


class AiProviderDetectTests(unittest.TestCase):
    def test_detect_deepseek_key_defaults(self):
        detected = detect_from_paste("sk-deepseekdemo1234567890")
        self.assertEqual(detected.provider_id, "deepseek")
        self.assertEqual(detected.base_url, DEEPSEEK_BASE)
        self.assertEqual(detected.model, DEEPSEEK_MODEL)
        self.assertTrue(detected.api_key.startswith("sk-"))

    def test_detect_openrouter_prefix(self):
        detected = detect_from_paste("sk-or-v1-abcdef0123456789")
        self.assertEqual(detected.provider_id, "openrouter")
        self.assertIn("openrouter.ai", detected.base_url)

    def test_detect_curl_script_moonshot(self):
        script = """
        curl https://api.moonshot.cn/v1/chat/completions \\
          -H "Authorization: Bearer sk-moonshotdemo123456" \\
          -d '{"model":"moonshot-v1-8k","messages":[]}'
        """
        detected = detect_from_paste(script)
        self.assertEqual(detected.provider_id, "moonshot")
        self.assertEqual(detected.api_key, "sk-moonshotdemo123456")
        self.assertEqual(detected.model, "moonshot-v1-8k")
        self.assertTrue(detected.base_url.startswith("https://api.moonshot.cn"))

    def test_detect_dashscope_url(self):
        detected = detect_from_paste(
            "https://dashscope.aliyuncs.com/compatible-mode/v1\nsk-qwendemo123456789")
        self.assertEqual(detected.provider_id, "dashscope")
        self.assertIn("dashscope", detected.base_url)

    def test_save_load_keeps_custom_endpoint(self):
        settings = AiSettings(
            provider_id="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-openaidemo123456",
            enabled=True,
            setup_dismissed=True,
        )
        save_settings(settings)
        loaded = load_settings()
        self.assertEqual(loaded.provider_id, "openai")
        self.assertEqual(loaded.base_url, "https://api.openai.com/v1")
        self.assertEqual(loaded.model, "gpt-4o-mini")
        self.assertEqual(loaded.api_key, "sk-openaidemo123456")
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.setup_dismissed)
        path = os.path.join(os.environ["ARCHIVEBOX_APP_DIR"], "ai.json")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["provider_id"], "openai")


if __name__ == "__main__":
    unittest.main()
