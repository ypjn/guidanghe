"""分类仍是待定，或图片识别失败时，再问一次 DeepSeek。

接口和模型是固定的，不能在界面里改。默认关闭，也不内置密钥。
只有用户自己打开并填写密钥之后才会发请求。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from classification_config import compile_config, default_config
from planner import (
    DEFAULT_UNCLASSIFIED, MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER,
    MODE_BY_TYPE, OrganizePlan, _assign_unique_names, _build_target_dir,
    _build_target_dir_v2,
)
from scheme_store import app_data_dir

AI_FILE_LIMIT = 40
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".webp": "image/webp"}
_MAX_IMAGE_BYTES = 1_500_000
_SETTINGS_NAME = "ai.json"


class AiStop(Exception):
    """接口不可用，停止继续询问。已经得到的结果保留。"""


@dataclass
class AiSettings:
    base_url: str = DEEPSEEK_BASE
    model: str = DEEPSEEK_MODEL
    api_key: str = ""
    enabled: bool = False

    def ready(self) -> bool:
        return bool(self.api_key.strip())


def _settings_path() -> str:
    return os.path.join(app_data_dir(), _SETTINGS_NAME)


def load_settings() -> AiSettings:
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return AiSettings()
    if not isinstance(data, dict):
        return AiSettings()
    key = str(data.get("api_key") or "").strip()
    enabled = bool(data.get("enabled")) and bool(key)
    return AiSettings(api_key=key, enabled=enabled)


def save_settings(settings: AiSettings) -> None:
    directory = app_data_dir()
    os.makedirs(directory, exist_ok=True)
    path = _settings_path()
    payload = json.dumps({
        "base_url": DEEPSEEK_BASE,
        "model": DEEPSEEK_MODEL,
        "api_key": settings.api_key,
        "enabled": bool(settings.enabled),
    }, ensure_ascii=False, indent=2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def needs_ai(item, mode: str) -> bool:
    """待定，或图片识别失败时，才问 DeepSeek。"""
    if getattr(item, "is_manual_overridden", False):
        return False
    if mode == MODE_BY_TYPE:
        return False
    ocr_failed = bool((getattr(item, "ocr_error", "") or "").strip())
    if mode == MODE_BY_DIRECTION_SEMESTER:
        return (item.direction or "") == "待定" or ocr_failed
    if mode == MODE_BY_COURSE_THEN_TYPE:
        return (item.course or "") == DEFAULT_UNCLASSIFIED or ocr_failed
    return ocr_failed


def allowed_pairs(config=None) -> list[tuple[str, str]]:
    """AI 只能从现有规则里的方向和类别中选，不能新造目录。"""
    compiled = compile_config(config or default_config())
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(direction: str, category: str) -> None:
        direction = (direction or "").strip()
        category = (category or "").strip()
        if not direction or not category or direction == "待定":
            return
        key = (direction, category)
        if key in seen:
            return
        seen.add(key)
        pairs.append(key)

    for rule in list(compiled.keyword_unmatched) + list(compiled.keyword_subdir):
        add(rule.direction, rule.target)
    for rule in compiled.long_term:
        add(rule.direction, rule.category)
    order = {"课程": 0, "校园事务": 1, "竞赛": 2, "项目": 3}
    pairs.sort(key=lambda pair: (order.get(pair[0], 9), pair[1]))
    return pairs


def course_pairs(rules) -> list[tuple[str, str]]:
    pairs = []
    seen = set()
    for rule in rules or []:
        name = (rule.course_name or "").strip()
        if name and name not in seen:
            seen.add(name)
            pairs.append(("课程", name))
    return pairs


def parse_choice(text: str, allowed: set[tuple[str, str]]):
    """解析模型输出。空选择或清单外的类别返回 None。"""
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    direction = str(data.get("direction") or "").strip()
    category = str(data.get("category") or data.get("course") or "").strip()
    if not direction or not category:
        return None
    if (direction, category) not in allowed:
        return None
    return direction, category


def _evidence(item) -> str:
    if (getattr(item, "ocr_error", "") or "").strip():
        return "图片识别失败"
    if (getattr(item, "ocr_text", "") or "").strip():
        return "图片文字没有命中规则"
    return "规则没有命中"


def _apply_choice(item, mode: str, direction: str, category: str) -> None:
    evidence = _evidence(item)
    item.rule_key = "ai"
    if mode == MODE_BY_COURSE_THEN_TYPE:
        item.course = category
        item.rule_applied = f"AI（{evidence}）→ {category}"
        item.target_dir_rel = _build_target_dir(mode, category, item.file_type)
        return
    item.direction = direction
    item.category = category
    item.course = category or direction
    item.inner_path = ""
    item.rule_applied = f"AI（{evidence}）→ {direction}/{category}"
    item.target_dir_rel = _build_target_dir_v2(
        direction, item.semester or "", category, item.file_type)


def _rebuild_names(plan: OrganizePlan) -> None:
    done = []
    for item in plan.items:
        item.target_filename = item.file.filename
        item.conflict_with_existing = False
        item.target_rel_path = os.path.join(item.target_dir_rel, item.file.filename)
        updated = _assign_unique_names(done, plan.root, item)
        done.append(updated)
    plan.items[:] = done


def _user_text(item) -> str:
    ocr = (getattr(item, "ocr_text", "") or "").strip()
    if len(ocr) > 1200:
        ocr = ocr[:1200] + "…"
    lines = [
        f"文件名：{item.file.filename}",
        f"相对路径：{item.file.rel_path}",
        f"类型：{item.file.file_type}",
    ]
    if ocr:
        lines.append("图片文字：\n" + ocr)
    elif (getattr(item, "ocr_error", "") or "").strip():
        lines.append("图片识别失败，没有可用文字。")
    else:
        lines.append("没有图片文字。")
    return "\n".join(lines)


def _messages(item, pairs, with_image: bool):
    listing = "\n".join(f"{direction}/{category}" for direction, category in pairs)
    system = (
        "你是文件归档助手。只能从清单里选一个分类。"
        "无法判断时返回 {\"direction\":\"\",\"category\":\"\"}。"
        "只输出 JSON，不要解释。格式："
        "{\"direction\":\"课程\",\"category\":\"高等数学\"}"
    )
    user = _user_text(item) + "\n\n可选分类：\n" + listing
    if not with_image:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    part = _image_part(item)
    content = [{"type": "text", "text": user}]
    if part is not None:
        content.append(part)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _image_part(item):
    """OCR 没有文字时，附上较小的位图。读不了就只发文字。"""
    if (getattr(item, "ocr_text", "") or "").strip():
        return None
    filename = item.file.filename
    ext = os.path.splitext(filename)[1].lower()
    mime = _IMAGE_EXTS.get(ext)
    if mime is None:
        return None
    if item.file.size <= 0 or item.file.size > _MAX_IMAGE_BYTES:
        return None
    path = item.file.abs_path
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_IMAGE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_IMAGE_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def default_transport(settings: AiSettings, messages) -> str:
    url = settings.base_url.strip().rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": settings.model.strip(),
        "temperature": 0,
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if settings.api_key.strip():
        request.add_header("Authorization", "Bearer " + settings.api_key.strip())
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiStop("AI 接口拒绝了密钥。") from exc
        if exc.code == 400:
            raise AiStop("AI 接口不接受这次请求。",) from exc
        raise AiStop(f"AI 接口返回 {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise AiStop(f"连不上 AI 接口（{settings.base_url.strip()}）。") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise AiStop("AI 接口没有返回可解析的结果。") from exc
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise AiStop("AI 接口没有返回文本。") from exc


def _ask(settings, item, pairs, transport) -> str:
    # deepseek-chat 只收文字。识别失败时带上失败说明，不附图片。
    return transport(settings, _messages(item, pairs, with_image=False))


def apply_ai_fallback(plan: OrganizePlan, settings: AiSettings,
                      cancel_event=None, progress_cb=None,
                      transport=None, config=None) -> str:
    """就地补充分类。返回给状态栏的一句说明。"""
    if plan.mode == MODE_BY_TYPE:
        return "按类型整理不使用 AI。"
    if settings is None or not settings.enabled or not settings.ready():
        return "AI 未开启，或还没有 DeepSeek 密钥。"
    if plan.mode == MODE_BY_COURSE_THEN_TYPE:
        pairs = course_pairs(plan.rules)
    else:
        pairs = allowed_pairs(config)
    if not pairs:
        return "没有可供 AI 选择的现有分类。"
    allowed = set(pairs)
    pending = [item for item in plan.items if needs_ai(item, plan.mode)]
    if not pending:
        return "规则已经覆盖全部文件，没有询问 AI。"
    ask = transport or default_transport
    limited = pending[:AI_FILE_LIMIT]
    changed = 0
    skipped = 0
    stopped = ""
    for index, item in enumerate(limited, start=1):
        if cancel_event is not None and cancel_event.is_set():
            stopped = "AI 询问已取消。"
            break
        if progress_cb is not None:
            progress_cb(index, len(limited), f"询问 AI {item.file.filename}")
        try:
            content = _ask(settings, item, pairs, ask)
        except AiStop as exc:
            stopped = str(exc)
            break
        choice = parse_choice(content, allowed)
        if choice is None:
            skipped += 1
            continue
        _apply_choice(item, plan.mode, choice[0], choice[1])
        changed += 1
    if changed:
        _rebuild_names(plan)
    note = f"AI 补充分类 {changed} 个"
    if skipped:
        note += f"，{skipped} 个仍无法判断"
    left = len(pending) - len(limited)
    if left > 0 and not stopped:
        note += f"，另有 {left} 个未询问"
    if stopped:
        note += "。" + stopped
    else:
        note += "。"
    plan.ai_note = note
    return note
