"""把整理顾问的对话收成可扫读的 HTML。

版式对齐常见对话产品（DeepSeek 这一类）：
自己的话靠右成块，顾问的话靠左按标题展开，需要拍板的问题单独铺底。
输出给 QTextBrowser 的富文本，不用它不支持的圆角和阴影。
"""

from __future__ import annotations

import html
import re

from ui_theme import AMBER, AMBER_BG, INK, MUTE, STAMP, STAMP_SOFT

# 较长的标题放前面，避免「需要确认」截走「需要用户确认的问题」。
_KNOWN_TITLES = (
    "需要用户确认的问题",
    "需要你确认",
    "需要用户确认",
    "需要确认",
    "建议目标结构",
    "建议怎么收",
    "建议分步动作",
    "先做哪几步",
    "这轮改了什么",
    "本轮改了什么",
    "现状判断",
)

_MD_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$")
_NUM_HEADING = re.compile(r"^(?:\d+|[一二三四五六七八九])[.、．]\s+(.+?)\s*$")
_BULLET = re.compile(r"^[-*•–—]\s+(\S.*)$")
_NUMBER = re.compile(r"^(\d+)[.、．]\s+(\S.*)$")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def render_transcript(messages: list[tuple[str, str]]) -> str:
    blocks = [render_message(role, text) for role, text in messages]
    return (
        "<html><body style=\"margin:0;background-color:#FFFFFF;"
        f"color:{INK};font-size:14px;\">"
        f"{''.join(blocks)}</body></html>"
    )


def render_message(role: str, text: str) -> str:
    if role == "user":
        return _user_block(text)
    if role == "assistant":
        return _assistant_block(text)
    if role == "thinking":
        return _thinking_block(text)
    return _system_block(text)


def _thinking_block(text: str) -> str:
    return (
        '<table width="100%" cellspacing="0" cellpadding="0">'
        "<tr>"
        f'<td width="36" valign="middle" style="color:{STAMP};font-weight:700;">AI</td>'
        f'<td style="color:{MUTE};font-size:13px;padding:8px 0 14px 0;">'
        f"{html.escape(text)}</td>"
        "</tr></table>"
    )


def _user_block(text: str) -> str:
    # QTextBrowser 会把嵌套表格拉满宽。左右两列才能把气泡留在右侧。
    return (
        '<table width="100%" cellspacing="0" cellpadding="0">'
        "<tr>"
        '<td width="28%"></td>'
        f'<td width="72%" style="background-color:{STAMP_SOFT};padding:8px 12px;">'
        f'<p style="margin:0 0 4px 0;color:{STAMP};font-size:13px;font-weight:650;">我</p>'
        f"{_plain_blocks(text)}"
        "</td></tr></table>"
        '<p style="margin:0 0 12px 0;font-size:4px;"> </p>'
    )


def _system_block(text: str) -> str:
    return (
        '<table width="100%" cellspacing="0" cellpadding="0">'
        '<tr><td style="padding-bottom:14px;">'
        f"{_plain_blocks(text, color=MUTE, size='13px')}"
        "</td></tr></table>"
    )


def _assistant_block(text: str) -> str:
    sections = _questions_first(split_sections(text))
    parts = [
        f'<p style="margin:0 0 6px 0;color:{STAMP};font-size:13px;font-weight:650;">顾问</p>'
    ]
    if not sections:
        parts.append(_body_html("（没有返回内容）"))
    for index, (title, note, body) in enumerate(sections):
        parts.append(_section_html(title, note, body, first=index == 0))
    inner = "".join(parts)
    return (
        '<table width="100%" cellspacing="0" cellpadding="0">'
        '<tr><td style="padding:2px 2px 18px 0;">'
        f"{inner}</td></tr></table>"
    )


def _questions_first(
        sections: list[tuple[str | None, str, str]],
) -> list[tuple[str | None, str, str]]:
    """需要拍板的问题放在这条回复最上面，其余顺序不动。"""
    asks = []
    rest = []
    for section in sections:
        title = section[0]
        if title and _classify(title)[1] == "ask":
            asks.append(section)
        else:
            rest.append(section)
    return asks + rest


def split_sections(text: str) -> list[tuple[str | None, str, str]]:
    """按约定标题切开。(标题, 括号里的补充, 正文)。标题为空表示开场白。"""
    sections: list[tuple[str | None, str, str]] = []
    title: str | None = None
    note = ""
    buf: list[str] = []
    saw_heading = False

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if title or body:
            sections.append((title, note, body))
        buf = []

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        parsed = parse_heading(line)
        if parsed is None:
            buf.append(line)
            continue
        flush()
        title, note = parsed
        saw_heading = True
    flush()
    if not saw_heading:
        stripped = text.strip()
        return [(None, "", stripped)] if stripped else []
    return sections


def parse_heading(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    matched = _MD_HEADING.match(stripped) or _NUM_HEADING.match(stripped)
    if not matched:
        return None
    raw = matched.group(1).strip()
    note_match = re.search(r"[（(]([^）)]*)[）)]\s*$", raw)
    note = note_match.group(1).strip() if note_match else ""
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", raw).strip(" ：:.")
    if _known_title(cleaned) and _tail_after_title(cleaned) <= 12:
        return cleaned, note
    if stripped.startswith("#") and 0 < len(cleaned) <= 18 and not re.search(r"[，。；]", cleaned):
        return cleaned, note
    return None


def _known_title(cleaned: str) -> bool:
    return any(cleaned == key or cleaned.startswith(key) for key in _KNOWN_TITLES)


def _tail_after_title(cleaned: str) -> int:
    for key in _KNOWN_TITLES:
        if cleaned.startswith(key):
            return len(cleaned[len(key):].strip(" ：:."))
    return len(cleaned)


def _section_html(title: str | None, note: str, body: str, *, first: bool) -> str:
    if title is None:
        return _body_html(body)
    label, kind = _classify(title)
    note_html = ""
    if note:
        note_html = (
            f'<p style="margin:0 0 6px 0;color:{MUTE};font-size:13px;">'
            f"{html.escape(note)}</p>"
        )
    body_html = _body_html(body)
    if kind == "ask":
        return (
            '<table width="100%" cellspacing="0" cellpadding="0">'
            '<tr>'
            f'<td width="4" style="background-color:{AMBER};"></td>'
            f'<td style="background-color:{AMBER_BG};padding:10px 12px;">'
            f'<p style="margin:0 0 6px 0;color:{AMBER};font-size:15px;font-weight:650;">'
            f"{html.escape(label)}</p>"
            f"{note_html}{body_html}"
            "</td></tr></table>"
            '<p style="margin:0 0 8px 0;font-size:4px;"> </p>'
        )
    if kind == "delta":
        return (
            f'<p style="margin:14px 0 4px 0;color:{MUTE};font-size:13px;font-weight:650;">'
            f"{html.escape(label)}</p>{body_html}"
        )
    top = "2px" if first else "14px"
    return (
        f'<p style="margin:{top} 0 4px 0;color:{INK};font-size:15px;font-weight:650;">'
        f"{html.escape(label)}</p>{note_html}{body_html}"
    )


def _classify(title: str) -> tuple[str, str]:
    if "确认" in title:
        return "需要你确认", "ask"
    if "改了什么" in title:
        return "这轮改了什么", "delta"
    if "目标结构" in title or "怎么收" in title:
        return "建议怎么收", "plan"
    if "分步" in title or "哪几步" in title:
        return "先做哪几步", "steps"
    if "现状" in title:
        return "现状判断", "status"
    return title, "section"


def _body_html(text: str) -> str:
    if not text.strip():
        return f'<p style="margin:2px 0;color:{MUTE};">无</p>'
    lines = text.split("\n")
    parts: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        bullet = _bullet_text(lines[index])
        if bullet is not None:
            items: list[tuple[str, str]] = []
            while index < len(lines):
                item = _bullet_text(lines[index])
                if item is None:
                    break
                items.append(("•", item))
                index += 1
            parts.append(_items_html(items))
            continue
        numbered = _number_text(lines[index])
        if numbered is not None:
            items = []
            while index < len(lines):
                item = _number_text(lines[index])
                if item is None:
                    break
                items.append(item)
                index += 1
            parts.append(_items_html(items))
            continue
        paragraph = [lines[index].strip()]
        index += 1
        while index < len(lines) and lines[index].strip():
            if _bullet_text(lines[index]) is not None or _number_text(lines[index]) is not None:
                break
            paragraph.append(lines[index].strip())
            index += 1
        parts.append(
            f'<p style="margin:2px 0 6px 0;">{render_inline(" ".join(paragraph))}</p>'
        )
    return "".join(parts)


def _plain_blocks(text: str, *, color: str = INK, size: str = "14px") -> str:
    chunks = re.split(r"\n{2,}", text.strip())
    blocks = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        line = render_inline(chunk).replace("\n", "<br>")
        blocks.append(
            f'<p style="margin:2px 0;color:{color};font-size:{size};">{line}</p>'
        )
    return "".join(blocks) or f'<p style="margin:0;color:{MUTE};">（空）</p>'


def _items_html(items: list[tuple[str, str]]) -> str:
    rows = []
    for mark, item in items:
        rows.append(
            "<tr>"
            f'<td width="28" valign="top" style="padding:1px 0 5px 0;color:{INK};">{html.escape(mark)}</td>'
            f'<td valign="top" style="padding:1px 0 5px 0;">{render_inline(item)}</td>'
            "</tr>"
        )
    return (
        '<table width="100%" cellspacing="0" cellpadding="0" style="margin:2px 0 4px 0;">'
        + "".join(rows)
        + "</table>"
    )


def _bullet_text(line: str) -> str | None:
    matched = _BULLET.match(line.strip())
    return matched.group(1).strip() if matched else None


def _number_text(line: str) -> tuple[str, str] | None:
    matched = _NUMBER.match(line.strip())
    if not matched:
        return None
    return f"{matched.group(1)}.", matched.group(2).strip()


def render_inline(raw: str) -> str:
    pieces = _CODE.split(raw)
    rendered: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2 == 1:
            rendered.append(
            '<span style="background-color:#D6E3F0;color:#16324F;'
            'font-family:Menlo,monospace;">'
            f"{html.escape(piece)}</span>"
            )
            continue
        escaped = html.escape(piece)
        escaped = _BOLD.sub(r"<b>\1</b>", escaped)
        rendered.append(escaped)
    return "".join(rendered)
