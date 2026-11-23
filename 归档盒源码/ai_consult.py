"""AI 整理顾问：先扫结构，再多轮对话调整管理建议。

真正移动文件仍走主界面预览与确认；这里只产出可读建议与会话记录。
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ai_fallback import AiSettings, AiStop, chat_completion
from ai_prompts import (
    CONSULT_FIRST_USER_TEMPLATE,
    CONSULT_FOLLOWUP_HINT,
    CONSULT_PLAN_SYSTEM_PROMPT,
    CONSULT_PLAN_USER_TEMPLATE,
    CONSULT_SYSTEM_PROMPT,
)
from ai_consult_store import (
    ConsultMessage,
    ConsultSession,
    append_message,
    create_session,
)
from planner import (
    MODE_BY_DIRECTION_SEMESTER,
    OrganizePlan,
    PlanItem,
    TARGET_ROOT,
    _build_target_dir_v2,
    assign_unique_targets,
)
from scanner import (
    EXT_TYPE_MAP, SKIP_DIRS, WINDOWS_SYSTEM_DIRS, scan_directory,
)
from utils import sha256_file

_MAX_DEPTH = 4
_MAX_DIRS = 80
_MAX_TOP_FILES = 40
_MAX_SAMPLES_PER_TYPE = 6
_CONSULT_TIMEOUT = 90.0
_MAX_PLAN_MOVES = 40
_MAX_CANDIDATES = 120
_ALLOWED_DIRECTIONS = {"课程", "竞赛", "项目", "校园事务", "待定"}


@dataclass
class SpaceSnapshot:
    root: str
    text: str
    dir_count: int = 0
    file_count: int = 0
    type_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _skip_dir_name(name: str) -> bool:
    lowered = name.lower()
    if name in SKIP_DIRS or lowered in SKIP_DIRS:
        return True
    if lowered in WINDOWS_SYSTEM_DIRS:
        return True
    if name.startswith("."):
        return True
    return False


def summarize_space(root: str, *, cancel_event=None,
                    progress_cb: Callable[[str], None] | None = None
                    ) -> SpaceSnapshot:
    """本地快速扫描目录骨架，生成给模型看的摘要（不上传整盘文件）。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"整理空间不存在：{root}")

    type_counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    top_files: list[str] = []
    dir_lines: list[str] = []
    dir_count = 0
    file_count = 0
    notes: list[str] = []

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if progress_cb:
        progress_cb("正在扫描目录结构…")

    root_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if cancelled():
            notes.append("扫描被取消，摘要可能不完整。")
            break
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(
            name for name in dirnames if not _skip_dir_name(name))
        if depth > _MAX_DEPTH:
            dirnames[:] = []
            continue
        if rel_dir != "." and dir_count < _MAX_DIRS:
            dir_count += 1
            indent = "  " * max(0, depth - 1)
            child_dirs = len(dirnames)
            child_files = sum(1 for name in filenames if not name.startswith("."))
            dir_lines.append(
                f"{indent}{os.path.basename(dirpath) or rel_dir}/"
                f"  ({child_dirs} 子目录, {child_files} 文件)")
        elif rel_dir != "." and dir_count >= _MAX_DIRS:
            dirnames[:] = []
            continue

        for name in filenames:
            if name.startswith("."):
                continue
            file_count += 1
            ext = os.path.splitext(name)[1].lower()
            kind = EXT_TYPE_MAP.get(ext, "其他")
            type_counts[kind] += 1
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            if rel_dir == "." and len(top_files) < _MAX_TOP_FILES:
                top_files.append(rel)
            bucket = samples.setdefault(kind, [])
            if len(bucket) < _MAX_SAMPLES_PER_TYPE:
                bucket.append(rel.replace("\\", "/"))
        if progress_cb and file_count and file_count % 400 == 0:
            progress_cb(f"已看到 {file_count} 个文件…")

    if dir_count >= _MAX_DIRS:
        notes.append(f"目录较多，摘要只列出前 {_MAX_DIRS} 个文件夹。")
    if file_count == 0:
        notes.append("这个空间里几乎没有可见文件。")

    lines = [
        f"根目录文件约 {file_count} 个；列出的文件夹 {dir_count} 个。",
        "文件类型统计：" + (
            "、".join(f"{k} {v}" for k, v in type_counts.most_common())
            or "无"),
        "",
        "目录骨架：",
    ]
    lines.extend(dir_lines or ["  （没有可列的子目录）"])
    if top_files:
        lines.append("")
        lines.append("根目录散落文件（抽样）：")
        lines.extend(f"  - {name}" for name in top_files[:_MAX_TOP_FILES])
    if samples:
        lines.append("")
        lines.append("各类型抽样路径：")
        for kind, paths in samples.items():
            lines.append(f"  [{kind}] " + "；".join(paths))
    if notes:
        lines.append("")
        lines.append("备注：" + "；".join(notes))

    return SpaceSnapshot(
        root=root,
        text="\n".join(lines),
        dir_count=dir_count,
        file_count=file_count,
        type_counts=dict(type_counts),
        notes=notes,
    )


def _rules_hint(root: str) -> str:
    try:
        from classification_config import load_config
        config, source = load_config(root)
        semester = len(getattr(config, "semester_rules", []) or [])
        keyword = len(getattr(config, "keyword_rules", []) or [])
        long_term = len(getattr(config, "long_term_rules", []) or [])
        return (
            f"配置来源 {source}；学期规则 {semester} 条，"
            f"关键词规则 {keyword} 条，长期规则 {long_term} 条。"
            "建议优先沿用现有学期/课程目录。")
    except Exception:
        return "未能读取本空间分类配置，请按常见课程资料结构给出保守建议。"


def build_initial_messages(snapshot: SpaceSnapshot, goal: str = "") -> list[dict]:
    user = CONSULT_FIRST_USER_TEMPLATE.format(
        root=snapshot.root,
        snapshot=snapshot.text,
        rules_hint=_rules_hint(snapshot.root),
        goal=(goal or "（用户还没说具体目标，请先做通用诊断并提问）").strip(),
    )
    return [
        {"role": "system", "content": CONSULT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_followup_messages(history: list[dict], user_text: str) -> list[dict]:
    messages = list(history)
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": CONSULT_SYSTEM_PROMPT}] + messages
    messages.append({
        "role": "user",
        "content": f"{CONSULT_FOLLOWUP_HINT}\n\n用户说：\n{user_text.strip()}",
    })
    return messages


def run_consult_turn(settings: AiSettings, messages: list[dict], *,
                     temperature: float = 0.2) -> str:
    return chat_completion(
        settings, messages, temperature=temperature, timeout=_CONSULT_TIMEOUT)


def start_consult_session(
        settings: AiSettings, snapshot: SpaceSnapshot, *,
        goal: str = "",
        transport: Callable[[AiSettings, list[dict]], str] | None = None,
        cancel_event=None) -> tuple[ConsultSession, str]:
    """扫描后的第一轮：写入会话并返回顾问回复。"""
    if not settings.ready():
        raise AiStop("还没有配置可用的 AI 接口密钥。")
    messages = build_initial_messages(snapshot, goal=goal)
    reply = _call(settings, messages, transport, cancel_event)
    stored = [
        ConsultMessage(role="system", content=CONSULT_SYSTEM_PROMPT),
        ConsultMessage(role="user", content=messages[1]["content"]),
        ConsultMessage(role="assistant", content=reply),
    ]
    session = create_session(
        root=snapshot.root,
        title="整理顾问",
        provider_id=settings.provider_id,
        model=settings.model,
        snapshot_text=snapshot.text,
        messages=stored,
    )
    return session, reply


def continue_consult_session(
        settings: AiSettings, session: ConsultSession, user_text: str, *,
        transport: Callable[[AiSettings, list[dict]], str] | None = None,
        cancel_event=None) -> tuple[ConsultSession, str]:
    history = [{"role": m.role, "content": m.content} for m in session.messages
               if m.role in ("system", "user", "assistant")]
    messages = build_followup_messages(history, user_text)
    reply = _call(settings, messages, transport, cancel_event)
    append_message(session.id, "user", messages[-1]["content"])
    append_message(session.id, "assistant", reply)
    from ai_consult_store import get_session
    refreshed = get_session(session.id)
    assert refreshed is not None
    return refreshed, reply


def _call(settings, messages, transport, cancel_event) -> str:
    ask = transport or (lambda s, m: run_consult_turn(s, m))
    if cancel_event is None:
        return ask(settings, messages)

    box: dict = {}

    def run():
        try:
            box["text"] = ask(settings, messages)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="archivebox-ai-consult")
    worker.start()
    while worker.is_alive():
        if cancel_event.is_set():
            raise AiStop("已取消本次 AI 请求。")
        worker.join(0.2)
    if "error" in box:
        raise box["error"]
    return str(box.get("text", ""))


def _norm_rel(path: str) -> str:
    return os.path.normcase(os.path.normpath(
        (path or "").replace("\\", os.sep).replace("/", os.sep)))


def _advice_text(session: ConsultSession | None, fallback: str = "") -> str:
    if session is None:
        return fallback or "（还没有顾问对话，仅按结构摘要生成保守方案）"
    parts = []
    for message in session.messages:
        if message.role == "system":
            continue
        who = "用户" if message.role == "user" else "顾问"
        # 首轮 user 含完整摘要，截断避免重复塞爆上下文
        body = message.content
        if message.role == "user" and len(body) > 1200:
            body = body[:1200] + "…"
        parts.append(f"[{who}]\n{body}")
    return "\n\n".join(parts[-6:]) if parts else (fallback or "")


def _candidate_files(root: str, protect_dirs=None, cancel_event=None):
    scan = scan_directory(
        root, protect_dirs=protect_dirs or [], cancel_event=cancel_event)
    files = []
    for entry in scan.files:
        rel = entry.rel_path.replace("\\", "/")
        top = rel.split("/", 1)[0]
        if top in (TARGET_ROOT, ".organizer", ".archivebox-backup"):
            continue
        files.append(entry)
        if len(files) >= _MAX_CANDIDATES:
            break
    return scan, files


def _safe_target_dir(raw: str, direction: str, semester: str,
                     category: str, file_type: str) -> str:
    text = (raw or "").strip().replace("\\", "/").strip("/")
    if text:
        if os.path.isabs(text) or text.startswith("..") or "/../" in f"/{text}/":
            raise ValueError("目标路径非法")
        parts = [part for part in text.split("/") if part and part != "."]
        if not parts:
            raise ValueError("目标路径为空")
        return os.path.join(*parts)
    return _build_target_dir_v2(direction, semester, category, file_type)


def parse_plan_payload(text: str) -> dict:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise AiStop("模型没有返回 JSON 方案。")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AiStop("模型返回的方案 JSON 无法解析。") from exc
    if not isinstance(data, dict):
        raise AiStop("模型返回的方案不是对象。")
    moves = data.get("moves")
    if moves is None:
        moves = []
    if not isinstance(moves, list):
        raise AiStop("方案里的 moves 不是列表。")
    data["moves"] = moves
    data["summary"] = str(data.get("summary") or "").strip()
    return data


@dataclass
class ConsultPlanResult:
    plan: OrganizePlan
    scan: object
    summary: str
    rejected: list[str] = field(default_factory=list)


def build_preview_plan_from_consult(
        settings: AiSettings,
        snapshot: SpaceSnapshot,
        session: ConsultSession | None, *,
        goal: str = "",
        protect_dirs=None,
        transport: Callable[[AiSettings, list[dict]], str] | None = None,
        cancel_event=None) -> ConsultPlanResult:
    """把顾问对话收成可勾选 OrganizePlan；校验失败的条目进入 rejected。"""
    if not settings.ready():
        raise AiStop("还没有配置可用的 AI 接口密钥。")
    scan, candidates = _candidate_files(
        snapshot.root, protect_dirs=protect_dirs, cancel_event=cancel_event)
    if not candidates:
        raise AiStop("这个空间里没有可纳入方案的文件。")
    by_rel = {_norm_rel(entry.rel_path): entry for entry in candidates}
    candidate_lines = "\n".join(
        f"- {entry.rel_path.replace(chr(92), '/')} ({entry.file_type})"
        for entry in candidates)
    messages = [
        {"role": "system", "content": CONSULT_PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": CONSULT_PLAN_USER_TEMPLATE.format(
            root=snapshot.root,
            snapshot=snapshot.text,
            candidates=candidate_lines,
            advice=_advice_text(session),
            goal=(goal or "（未填写，按顾问对话生成保守方案）").strip(),
        )},
    ]
    raw = _call(settings, messages, transport, cancel_event)
    payload = parse_plan_payload(raw)
    items: list[PlanItem] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for row in payload["moves"][:_MAX_PLAN_MOVES]:
        if not isinstance(row, dict):
            rejected.append("忽略非对象条目")
            continue
        rel = str(row.get("file") or row.get("rel_path") or "").strip()
        key = _norm_rel(rel)
        if not key or key in seen:
            rejected.append(f"重复或空路径：{rel or '（空）'}")
            continue
        entry = by_rel.get(key)
        if entry is None:
            rejected.append(f"不在候选清单：{rel}")
            continue
        direction = str(row.get("direction") or "待定").strip() or "待定"
        if direction not in _ALLOWED_DIRECTIONS:
            rejected.append(f"{rel}：方向「{direction}」不受支持")
            continue
        semester = str(row.get("semester") or "").strip()
        category = str(row.get("category") or "").strip()
        evidence = str(row.get("evidence") or row.get("reason") or "").strip()
        selected = bool(row.get("selected", True))
        try:
            target_dir = _safe_target_dir(
                str(row.get("target_dir") or row.get("target_dir_rel") or ""),
                direction, semester, category, entry.file_type)
        except ValueError as exc:
            rejected.append(f"{rel}：{exc}")
            continue
        seen.add(key)
        items.append(PlanItem(
            file=entry,
            course=category or direction,
            file_type=entry.file_type,
            rule_applied=f"AI 顾问方案：{evidence or '对话收束'}",
            target_dir_rel=target_dir,
            target_filename=entry.filename,
            target_rel_path=os.path.join(target_dir, entry.filename),
            direction=direction,
            semester=semester or None,
            category=category or None,
            rule_key="consult:plan",
            content_source="AI",
            decision_evidence=evidence,
            selected=selected,
            source_sha256=sha256_file(entry.abs_path) or "",
        ))
    if not items:
        detail = "；".join(rejected[:5]) if rejected else "模型没有给出可校验的移动项"
        raise AiStop(f"没有生成可勾选的预览项。{detail}")
    assign_unique_targets(items, snapshot.root)
    plan = OrganizePlan(
        root=os.path.realpath(os.path.abspath(snapshot.root)),
        mode=MODE_BY_DIRECTION_SEMESTER,
        rules=[],
        items=items,
        duplicates_computed=False,
    )
    summary = payload.get("summary") or f"AI 顾问方案：{len(items)} 个可勾选文件"
    if rejected:
        summary += f"；另有 {len(rejected)} 条未通过校验"
    plan.ai_note = summary
    return ConsultPlanResult(
        plan=plan, scan=scan, summary=summary, rejected=rejected)

