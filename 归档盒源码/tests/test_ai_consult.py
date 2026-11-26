"""AI 整理顾问：结构扫描、提示词、会话库与多轮对话。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ARCHIVEBOX_APP_DIR"] = tempfile.mkdtemp(prefix="archivebox_ai_consult_")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from ai_consult import (  # noqa: E402
    build_followup_messages, build_initial_messages,
    build_preview_plan_from_consult, start_consult_session, summarize_space,
)
from ai_consult_store import list_providers, get_session  # noqa: E402
from ai_fallback import AiSettings  # noqa: E402
from ai_prompts import (  # noqa: E402
    CLASSIFY_SYSTEM_PROMPT, CONSULT_PLAN_SYSTEM_PROMPT, CONSULT_SYSTEM_PROMPT,
)
from consult_chat_format import render_transcript  # noqa: E402
from ui_theme import AMBER_BG  # noqa: E402


class AiConsultTests(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.path.dirname(SRC), ".flow_tmp")
        os.makedirs(base, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="consult_space_", dir=base)
        os.makedirs(os.path.join(self.root, "大二上", "高数"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "下载"), exist_ok=True)
        with open(os.path.join(self.root, "杂乱笔记.pdf"), "w", encoding="utf-8") as f:
            f.write("demo")
        with open(os.path.join(self.root, "大二上", "高数", "作业1.docx"),
                  "w", encoding="utf-8") as f:
            f.write("demo")
        with open(os.path.join(self.root, "下载", "shot.png"),
                  "w", encoding="utf-8") as f:
            f.write("x")

    def test_prompts_exist(self):
        self.assertIn("归档助手", CLASSIFY_SYSTEM_PROMPT)
        self.assertIn("整理顾问", CONSULT_SYSTEM_PROMPT)
        self.assertIn("不声称已经移动", CONSULT_SYSTEM_PROMPT)
        self.assertIn("## 需要你确认", CONSULT_SYSTEM_PROMPT)
        self.assertIn("## 现状判断", CONSULT_SYSTEM_PROMPT)
        self.assertIn("只输出一个 JSON 对象", CONSULT_PLAN_SYSTEM_PROMPT)

    def test_build_preview_plan_from_consult(self):
        snap = summarize_space(self.root)
        settings = AiSettings(
            api_key="sk-test", enabled=True, provider_id="deepseek",
            base_url="https://api.deepseek.com/v1", model="deepseek-chat")

        def transport(_settings, messages):
            self.assertEqual(messages[0]["content"], CONSULT_PLAN_SYSTEM_PROMPT)
            self.assertIn("杂乱笔记.pdf", messages[1]["content"])
            return (
                '{"summary":"收拢根目录散落文件",'
                '"moves":['
                '{"file":"杂乱笔记.pdf","target_dir":"_已整理/课程/大二上/高等数学/文档",'
                '"direction":"课程","semester":"大二上","category":"高等数学",'
                '"evidence":"根目录散落","selected":true},'
                '{"file":"不存在.xyz","target_dir":"_已整理/待定",'
                '"direction":"待定","evidence":"应被拒绝","selected":true}'
                "]}"
            )

        result = build_preview_plan_from_consult(
            settings, snap, None, goal="按学期", transport=transport)
        self.assertEqual(len(result.plan.items), 1)
        item = result.plan.items[0]
        self.assertTrue(item.selected)
        self.assertEqual(item.file.filename, "杂乱笔记.pdf")
        self.assertIn("_已整理", item.target_dir_rel)
        self.assertTrue(any("不在候选清单" in x for x in result.rejected))
        self.assertIsNotNone(result.scan)

    def test_summarize_space(self):
        snap = summarize_space(self.root)
        self.assertGreaterEqual(snap.file_count, 3)
        self.assertIn("大二上", snap.text)
        self.assertIn("文件类型统计", snap.text)
        self.assertIn(self.root, snap.root)

    def test_provider_catalog_db(self):
        rows = list_providers()
        ids = {row["id"] for row in rows}
        self.assertIn("deepseek", ids)
        self.assertIn("openai", ids)
        self.assertIn("dashscope", ids)

    def test_consult_session_with_fake_transport(self):
        snap = summarize_space(self.root)
        messages_seen = []

        def transport(_settings, messages):
            messages_seen.append(messages)
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("整理顾问", messages[0]["content"])
            self.assertIn("大二上", messages[1]["content"])
            return "1. 现状判断\n根目录有散落文件，建议先归入学期目录。"

        settings = AiSettings(
            api_key="sk-test", enabled=True, provider_id="deepseek",
            base_url="https://api.deepseek.com/v1", model="deepseek-chat")
        session, reply = start_consult_session(
            settings, snap, goal="按学期整理", transport=transport)
        self.assertIn("现状判断", reply)
        self.assertEqual(len(messages_seen), 1)
        loaded = get_session(session.id)
        self.assertIsNotNone(loaded)
        self.assertGreaterEqual(len(loaded.messages), 3)
        self.assertEqual(loaded.messages[-1].role, "assistant")

        follow = build_followup_messages(
            [{"role": m.role, "content": m.content} for m in loaded.messages],
            "项目目录不要动")
        self.assertEqual(follow[-1]["role"], "user")
        self.assertIn("项目目录不要动", follow[-1]["content"])

    def test_initial_message_template(self):
        snap = summarize_space(self.root)
        messages = build_initial_messages(snap, goal="只收拾下载区")
        self.assertEqual(messages[0]["content"], CONSULT_SYSTEM_PROMPT)
        self.assertIn("只收拾下载区", messages[1]["content"])


class ConsultChatFormatTests(unittest.TestCase):
    def test_user_bubble_is_right_aligned_and_escaped(self):
        rendered = render_transcript([("user", "<script>alert(1)</script>")])
        self.assertIn('width="28%"', rendered)
        self.assertIn('width="72%"', rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn(">我</p>", rendered)

    def test_thinking_row_is_separate(self):
        rendered = render_transcript([("thinking", "AI 思考中…")])
        self.assertIn("AI 思考中…", rendered)
        self.assertIn(">AI</td>", rendered)

    def test_assistant_sections_highlight_questions(self):
        text = (
            "## 现状判断\n根目录散落文件多。\n\n"
            "## 建议怎么收\n- `下载/`：先收拢\n\n"
            "## 先做哪几步\n1. 可直接整理：收下载区\n\n"
            "## 需要你确认\n- 项目目录要不要动？\n\n"
            "## 这轮改了什么\n这是第一轮建议。\n"
        )
        rendered = render_transcript([("assistant", text)])
        self.assertIn("顾问", rendered)
        self.assertIn("需要你确认", rendered)
        self.assertLess(rendered.find("需要你确认"), rendered.find("现状判断"))
        self.assertIn(AMBER_BG, rendered)
        self.assertNotIn("##", rendered)
        self.assertNotIn("`下载/`", rendered)
        self.assertIn("下载/", rendered)
        self.assertIn("这是第一轮建议", rendered)

    def test_legacy_numbered_advice_splits(self):
        text = (
            "1. 现状判断\n根目录有散落文件。\n"
            "2. 建议目标结构（在现有基础上增量调整，不推倒）\n"
            "– `mathorcup/`：先别动\n"
            "4. 需要确认\n项目目录是否保留？\n"
        )
        rendered = render_transcript([("assistant", text)])
        self.assertIn(AMBER_BG, rendered)
        self.assertIn("在现有基础上增量调整，不推倒", rendered)
        self.assertIn("mathorcup/", rendered)
        self.assertNotIn("`mathorcup/`", rendered)
        self.assertNotIn("1. 现状判断", rendered)
        self.assertIn("项目目录是否保留？", rendered)


if __name__ == "__main__":
    unittest.main()
