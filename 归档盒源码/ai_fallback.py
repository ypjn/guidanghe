"""分类仍是待定，或图片识别失败时，再问一次外部 AI。

默认关闭，也不内置密钥。用户粘贴自己的密钥或接口脚本后，
程序识别常见 OpenAI 兼容厂商，填好地址与默认模型，再发请求。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ai_prompts import CLASSIFY_SYSTEM_PROMPT
from classification_config import compile_config, default_config
from planner import (
    DEFAULT_UNCLASSIFIED, MODE_BY_COURSE_THEN_TYPE, MODE_BY_DIRECTION_SEMESTER,
    MODE_BY_TYPE, OrganizePlan, _assign_unique_names, _build_target_dir,
    _build_target_dir_v2, assign_unique_targets,
)
from scheme_store import app_data_dir

AI_FILE_LIMIT = 40
# 阶段 1：正文片段也会参与模型判断，但只发送很短的一段。
AI_BODY_LIMIT = 1200
# 太短的「依据」等于没有依据，不能进入可执行批次。
MIN_EVIDENCE_CHARS = 4
# S4 / misc F-15：请求里不要把几百条分类清单全塞进 prompt
_MAX_PROMPT_PAIRS = 80
# 已询问过的文件结果缓存（重新预览时复用，不重复花钱）
_RESULT_CACHE_LIMIT = 512
_result_cache: dict[tuple, tuple] = {}
_result_cache_order: list[tuple] = []
_result_cache_lock = threading.Lock()
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
_REQUEST_TIMEOUT = 20
_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".webp": "image/webp"}
_MAX_IMAGE_BYTES = 1_500_000
_SETTINGS_NAME = "ai.json"
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BEARER_RE = re.compile(
    r"(?:Authorization\s*[:=]\s*Bearer\s+|api[_-]?key\s*[:=]\s*[\"']?|"
    r"sk-(?:or|ant)-|[A-Za-z0-9_\-]{0,12}sk-)([A-Za-z0-9_\-\.]{8,})",
    re.IGNORECASE)
_MODEL_RE = re.compile(
    r"[\"']?model[\"']?\s*[:=]\s*[\"']([^\"'\s,]+)[\"']?",
    re.IGNORECASE)


class AiStop(Exception):
    """接口不可用，停止继续询问。已经得到的结果保留。"""


class _AiCancelled(Exception):
    """用户在等待当前请求时取消。"""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str
    model: str
    host_needles: tuple[str, ...] = ()
    key_prefixes: tuple[str, ...] = ()


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "deepseek", "DeepSeek", DEEPSEEK_BASE, DEEPSEEK_MODEL,
        host_needles=("api.deepseek.com", "deepseek.com"),
    ),
    ProviderSpec(
        "openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini",
        host_needles=("api.openai.com",),
    ),
    ProviderSpec(
        "moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1",
        "moonshot-v1-8k",
        host_needles=("moonshot.cn", "moonshot.ai"),
    ),
    ProviderSpec(
        "dashscope", "通义千问",
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
        host_needles=("dashscope.aliyuncs.com", "dashscope"),
    ),
    ProviderSpec(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-flash",
        host_needles=("bigmodel.cn",),
    ),
    ProviderSpec(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
        host_needles=("siliconflow.cn",),
    ),
    ProviderSpec(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
        "openai/gpt-4o-mini",
        host_needles=("openrouter.ai",),
        key_prefixes=("sk-or-",),
    ),
    ProviderSpec(
        "groq", "Groq", "https://api.groq.com/openai/v1",
        "llama-3.1-8b-instant",
        host_needles=("api.groq.com", "groq.com"),
    ),
    ProviderSpec(
        "ollama", "Ollama 本地", "http://127.0.0.1:11434/v1", "llama3.2",
        host_needles=("127.0.0.1:11434", "localhost:11434"),
    ),
    ProviderSpec(
        "custom", "自定义 OpenAI 兼容", "", "",
    ),
)
_PROVIDER_BY_ID = {spec.id: spec for spec in PROVIDERS}


@dataclass
class AiSettings:
    base_url: str = DEEPSEEK_BASE
    model: str = DEEPSEEK_MODEL
    api_key: str = ""
    enabled: bool = False
    provider_id: str = "deepseek"
    setup_dismissed: bool = False

    def ready(self) -> bool:
        return bool(self.api_key.strip() and self.base_url.strip()
                    and self.model.strip())

    def provider(self) -> ProviderSpec:
        return _PROVIDER_BY_ID.get(self.provider_id) or _PROVIDER_BY_ID["custom"]

    def provider_label(self) -> str:
        if self.provider_id == "custom" and self.base_url.strip():
            host = urllib.parse.urlparse(self.base_url.strip()).netloc or "自定义"
            return f"自定义（{host}）"
        return self.provider().name

    def status_text(self) -> str:
        if not self.api_key.strip():
            return "AI 未配置"
        label = self.provider_label()
        if self.enabled:
            return f"AI 已开启 · {label}"
        return f"AI 已配置 · {label}（补充分类关闭）"


@dataclass
class DetectedAiConfig:
    provider_id: str = "deepseek"
    provider_name: str = "DeepSeek"
    base_url: str = DEEPSEEK_BASE
    model: str = DEEPSEEK_MODEL
    api_key: str = ""
    confidence: str = "default"
    notes: list[str] = field(default_factory=list)


def provider_choices() -> list[tuple[str, str]]:
    return [(spec.id, spec.name) for spec in PROVIDERS]


def get_provider(provider_id: str) -> ProviderSpec:
    return _PROVIDER_BY_ID.get(provider_id) or _PROVIDER_BY_ID["custom"]


def normalize_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    lowered = raw.lower()
    for suffix in ("/chat/completions", "/completions"):
        if lowered.endswith(suffix):
            raw = raw[: -len(suffix)]
            lowered = raw.lower()
            break
    return raw.rstrip("/")


def detect_provider_from_url(url: str) -> ProviderSpec | None:
    host = urllib.parse.urlparse((url or "").strip()).netloc.lower()
    path = urllib.parse.urlparse((url or "").strip()).path.lower()
    blob = f"{host}{path}"
    if not blob:
        return None
    for spec in PROVIDERS:
        if spec.id == "custom":
            continue
        for needle in spec.host_needles:
            if needle.lower() in blob:
                return spec
    return None


def detect_provider_from_key(api_key: str) -> ProviderSpec | None:
    key = (api_key or "").strip()
    if not key:
        return None
    lowered = key.lower()
    for spec in PROVIDERS:
        for prefix in spec.key_prefixes:
            if lowered.startswith(prefix.lower()):
                return spec
    return None


def _extract_api_key(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if "\n" not in raw and " " not in raw and len(raw) < 256:
        if raw.lower().startswith("sk-") or len(raw) >= 16:
            return raw
    match = _BEARER_RE.search(raw)
    if match:
        token = match.group(0)
        if "bearer" in token.lower():
            return token.split()[-1].strip("\"'")
        if "sk-" in token.lower():
            idx = token.lower().find("sk-")
            return token[idx:].strip("\"',;")
        return match.group(1).strip("\"',;")
    for part in re.split(r"[\s\"'=]+", raw):
        if part.lower().startswith("sk-") and len(part) >= 12:
            return part.strip("\"',;")
    return ""


def detect_from_paste(text: str) -> DetectedAiConfig:
    """从密钥、接口地址或 curl/脚本片段识别厂商与默认模型。"""
    raw = (text or "").strip()
    notes: list[str] = []
    api_key = _extract_api_key(raw)
    urls = _URL_RE.findall(raw)
    base_url = ""
    for candidate in urls:
        cleaned = normalize_base_url(candidate.rstrip("\",'"))
        if cleaned:
            base_url = cleaned
            break
    model = ""
    model_match = _MODEL_RE.search(raw)
    if model_match:
        model = model_match.group(1).strip()

    by_key = detect_provider_from_key(api_key)
    by_url = detect_provider_from_url(base_url) if base_url else None
    spec = by_url or by_key
    confidence = "url" if by_url else ("key" if by_key else "default")

    if spec is None:
        if base_url:
            spec = get_provider("custom")
            confidence = "custom"
            notes.append("未识别到常见厂商，按 OpenAI 兼容接口保存。")
        else:
            spec = get_provider("deepseek")
            confidence = "default"
            if api_key:
                notes.append("只看到密钥、没有地址，默认按 DeepSeek 配置。")
            else:
                notes.append("还没有识别到密钥或地址。")

    resolved_url = base_url or spec.base_url
    resolved_model = model or spec.model or DEEPSEEK_MODEL
    if spec.id != "custom" and not base_url:
        resolved_url = spec.base_url
    if spec.id != "custom" and not model:
        resolved_model = spec.model
    if by_url:
        notes.append(f"已根据接口地址识别为 {spec.name}。")
    elif by_key:
        notes.append(f"已根据密钥前缀识别为 {spec.name}。")
    return DetectedAiConfig(
        provider_id=spec.id,
        provider_name=spec.name if spec.id != "custom" else "自定义 OpenAI 兼容",
        base_url=resolved_url or DEEPSEEK_BASE,
        model=resolved_model or DEEPSEEK_MODEL,
        api_key=api_key,
        confidence=confidence,
        notes=notes,
    )


def apply_provider_defaults(settings: AiSettings,
                            provider_id: str) -> AiSettings:
    spec = get_provider(provider_id)
    settings.provider_id = spec.id
    if spec.id != "custom":
        settings.base_url = spec.base_url
        settings.model = spec.model
    return settings


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
    base_url = normalize_base_url(str(data.get("base_url") or ""))
    model = str(data.get("model") or "").strip()
    provider_id = str(data.get("provider_id") or "").strip()
    if not provider_id:
        detected = detect_provider_from_url(base_url) if base_url else None
        if detected is None and key:
            detected = detect_provider_from_key(key)
        if detected is None and (key or base_url):
            provider_id = "deepseek" if not base_url else "custom"
            if not base_url:
                base_url = DEEPSEEK_BASE
            if not model:
                model = DEEPSEEK_MODEL
        elif detected is not None:
            provider_id = detected.id
            if not base_url:
                base_url = detected.base_url
            if not model:
                model = detected.model
        else:
            provider_id = "deepseek"
            base_url = base_url or DEEPSEEK_BASE
            model = model or DEEPSEEK_MODEL
    else:
        spec = get_provider(provider_id)
        if not base_url:
            base_url = spec.base_url or DEEPSEEK_BASE
        if not model:
            model = spec.model or DEEPSEEK_MODEL
    enabled = bool(data.get("enabled")) and bool(key) and bool(base_url)
    return AiSettings(
        base_url=base_url or DEEPSEEK_BASE,
        model=model or DEEPSEEK_MODEL,
        api_key=key,
        enabled=enabled,
        provider_id=provider_id or "deepseek",
        setup_dismissed=bool(data.get("setup_dismissed")),
    )


def _restrict_windows_acl(path: str) -> None:
    """S4 / misc F-28：Windows 上 chmod 不产生访问控制效果，改用 icacls。

    只给当前用户完全控制，移除继承权限。失败时静默（不阻断保存），
    因为是否可写取决于系统策略，真正重要的是默认关闭 + 用户自填。
    """
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{user}:F"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def save_settings(settings: AiSettings) -> None:
    directory = app_data_dir()
    os.makedirs(directory, exist_ok=True)
    path = _settings_path()
    payload = json.dumps({
        "provider_id": settings.provider_id or "deepseek",
        "base_url": normalize_base_url(settings.base_url) or DEEPSEEK_BASE,
        "model": (settings.model or "").strip() or DEEPSEEK_MODEL,
        "api_key": settings.api_key,
        "enabled": bool(settings.enabled),
        "setup_dismissed": bool(settings.setup_dismissed),
    }, ensure_ascii=False, indent=2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    _restrict_windows_acl(tmp)
    os.replace(tmp, path)
    _restrict_windows_acl(path)


def needs_ai(item, mode: str) -> bool:
    """待定，或图片识别失败时，才问外部 AI。"""
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
    """解析旧版模型输出。空选择或清单外的类别返回 None。"""
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


@dataclass
class Suggestion:
    """阶段 1 结构化建议：文件标识 + 候选 + 动作 + 依据。"""

    file: str
    direction: str
    category: str
    action: str
    evidence: str


def _norm_rel(value: str) -> str:
    return os.path.normcase(os.path.normpath(
        (value or "").replace("\\", os.sep).replace("/", os.sep)))


def _expected_target(item, mode: str, direction: str, category: str) -> str:
    if mode == MODE_BY_COURSE_THEN_TYPE:
        return _build_target_dir(mode, category, item.file.file_type)
    return _build_target_dir_v2(
        direction, item.semester or "", category, item.file.file_type)


def parse_suggestion(text: str, allowed: set[tuple[str, str]], item,
                     mode: str, require_evidence: bool = False):
    """解析并校验阶段 1 的结构化建议。

    返回 (Suggestion 或 None, 拒绝原因)。空方向/类别表示模型放弃，
    不算拒绝（原因为空字符串）。
    """
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None, "模型没有返回 JSON"
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None, "模型返回的 JSON 无法解析"
    if not isinstance(data, dict):
        return None, "模型返回的不是 JSON 对象"

    direction = str(data.get("direction") or "").strip()
    category = str(data.get("category") or data.get("course") or "").strip()
    if not direction or not category:
        return None, ""

    file_id = str(
        data.get("file") or data.get("file_id") or data.get("rel_path")
        or data.get("filename") or "").strip()
    if file_id:
        wanted = {_norm_rel(item.file.rel_path),
                  _norm_rel(item.file.filename)}
        if _norm_rel(file_id) not in wanted:
            return None, f"模型返回的文件「{file_id}」不属于本次计划"

    if (direction, category) not in allowed:
        return None, f"分类「{direction}/{category}」不在现有清单内"

    action = str(data.get("action") or "").strip().lower()
    if action not in ("", "move", "skip"):
        return None, f"动作「{action}」不受支持"

    evidence = str(
        data.get("evidence") or data.get("reason") or "").strip()
    if require_evidence:
        if len(evidence) < MIN_EVIDENCE_CHARS:
            return None, "依据为空或过短"
        if evidence.lower() in ("无", "不知道", "unknown", "n/a", "none"):
            return None, "依据为空或过短"

    target = str(
        data.get("target") or data.get("target_dir") or "").strip()
    if target:
        if os.path.isabs(target):
            return None, "模型给出的目标是绝对路径"
        rel = _norm_rel(target)
        if rel == ".." or rel.startswith(".." + os.sep):
            return None, "模型给出的目标越出了整理空间"
        expected = _expected_target(item, mode, direction, category)
        if _norm_rel(expected) != rel:
            return None, "模型给出的目标不在现有目录规则内"

    return Suggestion(
        file=file_id or item.file.rel_path,
        direction=direction,
        category=category,
        action=action or "move",
        evidence=evidence,
    ), ""


def _evidence(item) -> str:
    if (getattr(item, "ocr_error", "") or "").strip():
        return "图片识别失败"
    if (getattr(item, "content_text", "") or "").strip():
        return "正文没有命中规则"
    if (getattr(item, "ocr_text", "") or "").strip():
        return "图片文字没有命中规则"
    return "规则没有命中"


def _locally_decided(item) -> bool:
    """本地规则已经决定去向且已勾选；AI 失败/跳过不能推翻它。"""
    return (bool(item.selected)
            and item.rule_key not in ("", "fallback")
            and not getattr(item, "ambiguity", None))


def _apply_choice(item, mode: str, direction: str, category: str,
                  model_evidence: str = "") -> None:
    evidence = _evidence(item)
    item.rule_key = "ai"
    item.content_source = "AI"
    if not getattr(item, "ambiguity", None):
        # 通过校验、又不是未确认歧义的建议，默认勾选，等待用户最终确认批次。
        item.selected = True
        item.skipped = False
    suffix = f"；依据：{model_evidence}" if model_evidence else ""
    if mode == MODE_BY_COURSE_THEN_TYPE:
        item.course = category
        item.rule_applied = f"AI（{evidence}）→ {category}{suffix}"
        item.target_dir_rel = _build_target_dir(mode, category, item.file_type)
        return
    item.direction = direction
    item.category = category
    item.course = category or direction
    item.inner_path = ""
    item.rule_applied = (
        f"AI（{evidence}）→ {direction}/{category}{suffix}")
    item.target_dir_rel = _build_target_dir_v2(
        direction, item.semester or "", category, item.file_type)


def _rebuild_names(plan: OrganizePlan) -> None:
    for item in plan.items:
        item.target_filename = item.file.filename
        item.conflict_with_existing = False
        item.target_rel_path = os.path.join(item.target_dir_rel, item.file.filename)
    assign_unique_targets(plan.items, plan.root)


def _user_text(item) -> str:
    ocr = (getattr(item, "ocr_text", "") or "").strip()
    if len(ocr) > 1200:
        ocr = ocr[:1200] + "…"
    body = (getattr(item, "content_text", "") or "").strip()
    if len(body) > AI_BODY_LIMIT:
        body = body[:AI_BODY_LIMIT] + "…"
    lines = [
        f"文件名：{item.file.filename}",
        f"相对路径：{item.file.rel_path}",
        f"类型：{item.file.file_type}",
    ]
    if body:
        lines.append("正文摘录：\n" + body)
    elif (getattr(item, "content_error", "") or "").strip():
        lines.append("正文不可用：" + item.content_error)
    if ocr:
        lines.append("图片文字：\n" + ocr)
    elif (getattr(item, "ocr_error", "") or "").strip():
        lines.append("图片识别失败，没有可用文字。")
    else:
        lines.append("没有图片文字。")
    return "\n".join(lines)


def _messages(item, pairs, with_image: bool):
    # S4 / misc F-15：清单过长时截断，避免每次请求塞几百条分类
    listing_pairs = list(pairs)[:_MAX_PROMPT_PAIRS]
    listing = "\n".join(f"{direction}/{category}"
                        for direction, category in listing_pairs)
    if len(pairs) > len(listing_pairs):
        listing += (f"\n（清单过长，只列出前 {len(listing_pairs)} 条；"
                    "请只从中选择）")
    system = CLASSIFY_SYSTEM_PROMPT
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


def chat_completion(settings: AiSettings, messages, *,
                    temperature: float = 0,
                    timeout: float | None = None) -> str:
    """调用用户配置的 OpenAI 兼容 chat/completions。"""
    if settings is None or not settings.ready():
        raise AiStop("还没有配置可用的 AI 接口密钥。")
    url = settings.base_url.strip().rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": settings.model.strip(),
        "temperature": float(temperature),
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if settings.api_key.strip():
        request.add_header("Authorization", "Bearer " + settings.api_key.strip())
    wait = _REQUEST_TIMEOUT if timeout is None else float(timeout)
    try:
        with urllib.request.urlopen(request, timeout=wait) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AiStop("AI 接口拒绝了密钥。") from exc
        if exc.code == 400:
            raise AiStop("AI 接口不接受这次请求。") from exc
        raise AiStop(f"AI 接口返回 {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise AiStop(f"连不上 AI 接口（{settings.base_url.strip()}）。") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise AiStop("AI 接口没有返回可解析的结果。") from exc
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise AiStop("AI 接口没有返回文本。") from exc


def default_transport(settings: AiSettings, messages) -> str:
    return chat_completion(settings, messages, temperature=0)


def _ask(settings, item, pairs, transport) -> str:
    # deepseek-chat 只收文字。识别失败时带上失败说明，不附图片。
    return transport(settings, _messages(item, pairs, with_image=False))


def _ask_cancellable(settings, item, pairs, transport, cancel_event) -> str:
    """等待当前请求时可被取消。

    urllib 的阻塞读无法从外部打断，这里把请求放到守护线程里，
    主线程按 0.2 秒轮询取消标志——用户点取消后界面立即返回，
    后台线程最多再等到超时自行结束。
    """
    box: dict = {}

    def run():
        try:
            box["text"] = _ask(settings, item, pairs, transport)
        except BaseException as exc:      # noqa: BLE001 — 原样抛回主线程
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True,
                              name="archivebox-ai-request")
    worker.start()
    while worker.is_alive():
        if cancel_event is not None and cancel_event.is_set():
            raise _AiCancelled()
        worker.join(0.2)
    if "error" in box:
        raise box["error"]
    return str(box.get("text", ""))


def _cache_key(plan, item, pairs, structured: bool = False) -> tuple:
    return (plan.mode,
            bool(structured),
            os.path.normcase(plan.root),
            os.path.normcase(item.file.rel_path),
            int(item.file.size),
            int(getattr(item.file, "mtime_ns", 0) or 0),
            (getattr(item, "content_text", "") or "")[:200],
            (getattr(item, "ocr_text", "") or "")[:200],
            len(pairs))


_MISS = object()


def _cache_get(key):
    with _result_cache_lock:
        return _result_cache.get(key, _MISS)


def _cache_put(key, value) -> None:
    with _result_cache_lock:
        if key not in _result_cache:
            _result_cache_order.append(key)
        _result_cache[key] = value
        while len(_result_cache_order) > _RESULT_CACHE_LIMIT:
            old = _result_cache_order.pop(0)
            _result_cache.pop(old, None)


def apply_ai_fallback(plan: OrganizePlan, settings: AiSettings,
                      cancel_event=None, progress_cb=None,
                      transport=None, config=None,
                      require_evidence: bool = False) -> str:
    """就地补充分类。返回给状态栏的一句说明。

    require_evidence=True 时使用阶段 1 的结构化校验：文件必须属于本次
    计划、方向/类别在现有清单内、目标只能由现有目录函数生成、依据不能为空。
    校验不通过的建议只记录原因，不改目标、不进入可执行批次。
    """
    if plan.mode == MODE_BY_TYPE:
        return "按类型整理不使用 AI。"
    if settings is None or not settings.enabled or not settings.ready():
        return "AI 未开启，或还没有配置可用的接口密钥。"
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
    rejected = 0
    reused = 0
    stopped = ""
    for index, item in enumerate(limited, start=1):
        if cancel_event is not None and cancel_event.is_set():
            stopped = "AI 询问已取消。"
            break
        if progress_cb is not None:
            progress_cb(index, len(limited), f"询问 AI {item.file.filename}")
        cache_key = _cache_key(plan, item, pairs,
                               structured=require_evidence)
        cached = _cache_get(cache_key)
        if cached is not _MISS:
            reused += 1
        else:
            try:
                content = _ask_cancellable(settings, item, pairs, ask,
                                           cancel_event)
            except _AiCancelled:
                stopped = "AI 询问已取消（当前请求已停止等待）。"
                break
            except AiStop as exc:
                stopped = str(exc)
                break
            if require_evidence:
                cached = parse_suggestion(
                    content, allowed, item, plan.mode,
                    require_evidence=True)
            else:
                cached = parse_choice(content, allowed)
            _cache_put(cache_key, cached)

        if require_evidence:
            suggestion, reason = cached
            if reason:
                rejected += 1
                plan.ai_rejections.append({
                    "file": item.file.rel_path,
                    "reason": reason,
                })
                if not _locally_decided(item):
                    item.rule_applied = (
                        item.rule_applied
                        + f"；AI 建议未采用：{reason}（保持原位）")
                    item.selected = False
                continue
            if suggestion is None:
                skipped += 1
                continue
            if suggestion.action == "skip":
                if _locally_decided(item):
                    # 本地规则/目录已经决定去向，AI 的跳过不能覆盖。
                    plan.ai_rejections.append({
                        "file": item.file.rel_path,
                        "reason": "AI 建议跳过，未覆盖已确定的本地分类",
                    })
                    rejected += 1
                    continue
                item.selected = False
                item.skipped = True
                item.rule_applied = (
                    f"AI 建议跳过：{suggestion.evidence or '依据不足'}；保持原位")
                skipped += 1
                continue
            _apply_choice(item, plan.mode, suggestion.direction,
                          suggestion.category, suggestion.evidence)
            changed += 1
        else:
            choice = cached
            if choice is None:
                skipped += 1
                continue
            _apply_choice(item, plan.mode, choice[0], choice[1])
            changed += 1

    if changed:
        _rebuild_names(plan)
    note = f"AI 补充分类 {changed} 个"
    if reused:
        note += f"（其中 {reused} 个复用上次结果）"
    if skipped:
        note += f"，{skipped} 个仍无法判断"
    if rejected:
        note += f"，{rejected} 个建议未通过校验"
    left = len(pending) - len(limited)
    if left > 0 and not stopped:
        note += f"，另有 {left} 个未询问"
    if stopped:
        note += "。" + stopped
    else:
        note += "。"
    plan.ai_note = note
    return note
