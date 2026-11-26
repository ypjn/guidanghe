"""把一个整理空间选定的分类包应用到预览计划。

学习包保持原来的课程规则。其他包一次只用自己的标准名：
已保存的分类直接沿用，没保存过的才询问模型。路径由程序填写。
按类型整理不使用这本手册。
"""

from __future__ import annotations

import json
import os
import sys

from ai_fallback import AI_FILE_LIMIT, AiStop, default_transport
from handbook_session import (
    SCHOOL_PACK,
    Decision,
    list_packs,
    open_pack_session,
    parse_handbook,
)
from planner import MODE_BY_TYPE, assign_unique_targets

_HANDBOOK_NAME = "2026-11-23-classification-handbook.md"
_PACK_VERSION = 1


def handbook_file() -> str:
    """源码树或打包后的手册。找不到就返回空字符串。"""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        candidates.append(os.path.join(base, "handbooks", _HANDBOOK_NAME))
        candidates.append(os.path.join(os.path.dirname(sys.executable), "handbooks", _HANDBOOK_NAME))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "handbooks", _HANDBOOK_NAME))
    candidates.append(os.path.join(
        os.path.dirname(here), "docs", "handbooks", _HANDBOOK_NAME))
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def list_pack_choices() -> list[str]:
    path = handbook_file()
    if not path:
        return []
    try:
        return list_packs(parse_handbook(path))
    except (OSError, ValueError):
        return []


def _pack_file(root: str) -> str:
    return os.path.join(os.path.abspath(root), ".organizer", "handbook_pack.json")


def load_pack_choice(root: str) -> str:
    """这个文件夹上次选的包。没有记录时用学习包。"""
    path = _pack_file(root)
    if not os.path.isfile(path):
        return SCHOOL_PACK
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return SCHOOL_PACK
    if not isinstance(payload, dict):
        return SCHOOL_PACK
    pack = str(payload.get("pack") or "").strip()
    if pack == SCHOOL_PACK:
        return SCHOOL_PACK
    if pack in list_pack_choices():
        return pack
    return SCHOOL_PACK


def save_pack_choice(root: str, pack: str) -> None:
    chosen = SCHOOL_PACK if (pack or "").strip() in ("", SCHOOL_PACK) else pack.strip()
    if chosen != SCHOOL_PACK and chosen not in list_pack_choices():
        raise ValueError(f"没有「{chosen}」这个分类包")
    path = _pack_file(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"version": _PACK_VERSION, "pack": chosen}, handle, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def _session_path(root: str, pack: str) -> str:
    return os.path.join(
        os.path.abspath(root), ".organizer", "handbook", f"{pack}.json")


def _parent_dir(rel_path: str) -> str:
    return os.path.dirname((rel_path or "").replace("\\", "/")).strip("/")


def _body(item) -> str:
    parts = []
    content = (getattr(item, "content_text", "") or "").strip()
    ocr = (getattr(item, "ocr_text", "") or "").strip()
    if content:
        parts.append(content)
    if ocr:
        parts.append(ocr)
    return "\n".join(parts)


def _hold(item, reason: str) -> None:
    item.target_dir_rel = _parent_dir(item.file.rel_path)
    item.direction = "待定"
    item.category = ""
    item.course = ""
    item.semester = ""
    item.inner_path = ""
    item.rule_key = "handbook"
    item.selected = False
    item.ambiguity = None
    item.rule_applied = reason
    item.decision_evidence = reason
    item.content_source = "分类手册"


def _place(item, pack: str, decision: Decision) -> None:
    target = (decision.path or "").strip("/")
    item.target_dir_rel = target
    item.direction = pack
    item.category = decision.category
    item.course = decision.category
    item.semester = ""
    item.inner_path = ""
    item.rule_key = "handbook"
    item.ambiguity = None
    item.skipped = False
    item.content_source = "分类手册"
    item.decision_evidence = decision.evidence
    how = "沿用已保存的分类" if decision.reused else "本次分类"
    text = f"分类手册·{pack}/{decision.category}（{how}）：{decision.evidence}"
    if _parent_dir(item.file.rel_path) == target:
        item.selected = False
        item.rule_applied = text + "；已在自有文件夹中，默认不移动"
    else:
        item.selected = True
        item.rule_applied = text


def apply_handbook_pack(plan, pack: str, settings=None, *,
                        cancel_event=None, progress_cb=None,
                        transport=None, limit: int = AI_FILE_LIMIT) -> str:
    """按选定的包改写计划。学习包和按类型整理直接返回，不改条目。"""
    chosen = (pack or "").strip()
    if plan.mode == MODE_BY_TYPE:
        return "按类型整理不使用分类手册。"
    if not chosen or chosen == SCHOOL_PACK:
        return ""
    book = handbook_file()
    if not book:
        for item in plan.items:
            if item.is_manual_overridden or item.user_resolved:
                continue
            _hold(item, "找不到分类手册，保持原位。")
        assign_unique_targets(plan.items, plan.root)
        note = "找不到分类手册，这些文件保持原位。"
        plan.ai_note = note
        return note

    try:
        session = open_pack_session(_session_path(plan.root, chosen), book, chosen)
    except (OSError, ValueError) as exc:
        note = f"「{chosen}」包没有用上：{exc}"
        plan.ai_note = note
        return note

    ask_new = bool(
        settings is not None and settings.enabled and settings.ready())
    sender = transport or default_transport
    asked = 0
    reused = 0
    moved = 0
    held = 0
    stopped = ""

    class _Stopped(Exception):
        pass

    def ask(messages):
        if cancel_event is not None and cancel_event.is_set():
            raise _Stopped()
        return sender(settings, messages)

    pending_new = []
    for item in plan.items:
        if item.is_manual_overridden or item.user_resolved:
            continue
        key = item.file.rel_path
        saved = session.decisions.get(key)
        if saved is None:
            pending_new.append(item)
            continue
        decision = Decision(
            saved.filename, saved.action, saved.category,
            saved.path, saved.evidence, True,
        )
        reused += 1
        if decision.action == "move" and decision.path:
            _place(item, chosen, decision)
            moved += 1
        else:
            reason = decision.evidence or "已保存的分类是跳过，保持原位。"
            _hold(item, reason)
            held += 1

    for index, item in enumerate(pending_new, start=1):
        if cancel_event is not None and cancel_event.is_set():
            stopped = "分类询问已取消。"
            break
        if not ask_new:
            _hold(item, f"「{chosen}」包还没有这份文件的分类，保持原位。打开 AI 后才会询问。")
            held += 1
            continue
        if asked >= limit:
            _hold(item, f"「{chosen}」包这一轮只询问前 {limit} 个新文件，其余保持原位。")
            held += 1
            continue
        if progress_cb is not None:
            progress_cb(index, len(pending_new), f"按{chosen}包分类 {item.file.filename}")
        try:
            decision = session.classify(item.file.rel_path, _body(item), ask)
        except _Stopped:
            stopped = "分类询问已取消。"
            break
        except AiStop as exc:
            stopped = str(exc)
            _hold(item, f"没能问到分类，保持原位。{exc}")
            held += 1
            break
        asked += 1
        if decision.action == "move" and decision.path:
            _place(item, chosen, decision)
            moved += 1
        else:
            reason = decision.evidence or "没有对上这个包的标准名，保持原位。"
            _hold(item, reason)
            held += 1

    assign_unique_targets(plan.items, plan.root)
    note = f"「{chosen}」包分类 {moved} 个"
    if reused:
        note += f"（其中 {reused} 个沿用已保存结果）"
    if asked:
        note += f"，新询问 {asked} 个"
    if held:
        note += f"，{held} 个保持原位"
    if stopped:
        note += "。" + stopped
    else:
        note += "。"
    plan.ai_note = note
    return note
