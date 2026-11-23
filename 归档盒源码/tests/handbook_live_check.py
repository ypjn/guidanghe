"""用已配置的模型真实走一遍手册，并把记录留在临时目录。

不写用户的整理空间，不打印密钥。
"""
from __future__ import annotations

import json
import os
import re
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from ai_fallback import chat_completion, load_settings  # noqa: E402
from handbook_session import HandbookSession  # noqa: E402

HANDBOOK = os.path.join(
    os.path.dirname(SRC), "docs", "handbooks",
    "2026-11-23-classification-handbook.md",
)
STORE = "/tmp/guidanghe-handbook-live/session.json"
CASES = (
    ("2026-03-14 京东 发票.pdf", "", "move", "发票", "_已整理/财务/发票/2026/"),
    ("滴滴出行电子发票.pdf", "", "move", "发票", "_已整理/财务/发票/"),
    ("2026 张三 体检报告.pdf", "", "move", "体检", "_已整理/健康/体检/张三/2026/"),
    ("2026 家财险保单.pdf", "", "move", "房屋保险", "_已整理/住家/房屋保险/2026/"),
    ("2026-04-18 酒店预订单.pdf", "", "move", "出行", "_已整理/出行/2026-04-18/"),
    ("高等数学 作业.pdf", "", "skip", "", ""),
)
SECOND_FILE = "洗衣机 说明书.pdf"
SECOND_EXPECT = ("move", "说明书", "_已整理/购物/说明书/")


def _safe(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_\-\.]{6,}", "sk-…", text)


def _ask(settings):
    def ask(messages):
        return chat_completion(settings, messages, temperature=0, timeout=90)
    return ask


def main() -> int:
    settings = load_settings()
    print(
        f"provider={settings.provider_id} model={settings.model} "
        f"enabled={settings.enabled} key_len={len(settings.api_key.strip())}"
    )
    if not settings.ready():
        print("没有可用的模型密钥，真实调用没有进行。")
        return 2
    if os.path.exists(STORE):
        os.remove(STORE)
    first = HandbookSession.create(STORE, HANDBOOK)
    rows = []
    for filename, body, action, category, path in CASES:
        try:
            decision = first.classify(filename, body, _ask(settings))
        except Exception as exc:  # noqa: BLE001 — 界面要看到失败原因，不能带出密钥
            print(f"FAIL\t{filename}\t{_safe(str(exc))}")
            return 1
        matched = (
            decision.action == action
            and decision.category == category
            and decision.path == path
        )
        rows.append({
            "file": filename,
            "action": decision.action,
            "category": decision.category,
            "path": decision.path,
            "evidence": decision.evidence,
            "matched": matched,
        })
        print(
            f"{'OK' if matched else 'DIFF'}\t{filename}\t"
            f"{decision.action}\t{decision.category}\t{decision.path}"
        )

    def forbidden(_messages):
        raise RuntimeError("第二次打开仍去调用了模型")

    second = HandbookSession.load(STORE)
    replay = second.classify(CASES[0][0], "", forbidden)
    replay_ok = (
        replay.reused and replay.category == "发票"
        and replay.path == "_已整理/财务/发票/2026/"
    )
    print(f"{'OK' if replay_ok else 'FAIL'}\treopen\t{replay.category}\t{replay.path}")
    try:
        fresh = second.classify(SECOND_FILE, "", _ask(settings))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL\t{SECOND_FILE}\t{_safe(str(exc))}")
        return 1
    fresh_ok = (
        not fresh.reused
        and fresh.action == SECOND_EXPECT[0]
        and fresh.category == SECOND_EXPECT[1]
        and fresh.path == SECOND_EXPECT[2]
    )
    print(
        f"{'OK' if fresh_ok else 'DIFF'}\tnext\t{SECOND_FILE}\t"
        f"{fresh.action}\t{fresh.category}\t{fresh.path}"
    )
    third = HandbookSession.load(STORE)
    stuck = third.classify(SECOND_FILE, "", forbidden)
    stuck_ok = stuck.reused and stuck.path == fresh.path and stuck.category == fresh.category
    print(f"{'OK' if stuck_ok else 'FAIL'}\treopen-next\t{stuck.category}\t{stuck.path}")
    report = {
        "rules": len(second.rules),
        "replay_ok": replay_ok,
        "fresh_ok": fresh_ok,
        "stuck_ok": stuck_ok,
        "rows": rows,
    }
    report_path = os.path.join(os.path.dirname(STORE), "result.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"saved={STORE}")
    return 0 if replay_ok and stuck_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
