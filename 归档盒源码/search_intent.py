"""把一句查找或整理的话变成可核对的条件。

这里不做语义检索。日期写成绝对区间，修改时间和创建时间分开。
“最终版”只留下说明，不按修改时间挑一份当定稿。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from index_store import SearchQuery


@dataclass
class UnderstoodQuery:
    action: str
    text: str
    excluded: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    modified_after: dt.datetime | None = None
    modified_before: dt.datetime | None = None
    created_after: dt.datetime | None = None
    created_before: dt.datetime | None = None
    range_label: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = ["整理所选文件" if self.action == "organize" else "只查找，不移动"]
        if self.text:
            parts.append(f"关键词「{self.text}」")
        if self.excluded:
            parts.append("排除「" + "」「".join(self.excluded) + "」")
        if self.kinds:
            parts.append("类型「" + "、".join(self.kinds) + "」")
        if self.range_label:
            parts.append(self.range_label)
        parts.extend(self.notes)
        parts.append("尚未做语义检索，当前按关键词和正文查找。")
        return "；".join(parts)

    def to_search_query(self, **kwargs) -> SearchQuery:
        return SearchQuery(
            text=self.text,
            kinds=list(self.kinds),
            modified_after_ns=_ns(self.modified_after),
            modified_before_ns=_ns(self.modified_before),
            created_after_ns=_ns(self.created_after),
            created_before_ns=_ns(self.created_before),
            **kwargs)


def understand(instruction: str, now: dt.datetime | None = None) -> UnderstoodQuery:
    now = now or dt.datetime.now()
    raw = (instruction or "").strip()
    action = "organize" if re.search(r"整理|归入|移动|归档(?!盒)", raw) else "find"
    excluded = []
    for match in re.finditer(r"(?:排除|不要|不含|不包括)([^，,。；;的]+)", raw):
        for piece in re.split(r"[和与、/]", match.group(1)):
            piece = piece.strip()
            if piece:
                excluded.append(piece)
    search = re.sub(r"(?:排除|不要|不含|不包括)[^，,。；;的]*", " ", raw)
    notes = []
    if any(word in raw for word in ("最终版", "最新版", "终稿")):
        notes.append("“最终版/最新版”只展示候选和依据，不按修改时间自动认定")
        search = re.sub(r"最终版|最新版|终稿", " ", search)
    modified_after = modified_before = None
    created_after = created_before = None
    range_label = ""
    if "上周" in raw:
        start, end = _previous_week(now)
        range_label = _label("创建时间" if "创建" in raw else "修改时间", start, end)
        if "创建" in raw:
            created_after, created_before = start, end
        else:
            modified_after, modified_before = start, end
    elif "昨天" in raw:
        start, end = _day(now - dt.timedelta(days=1))
        range_label = _label("创建时间" if "创建" in raw else "修改时间", start, end)
        if "创建" in raw:
            created_after, created_before = start, end
        else:
            modified_after, modified_before = start, end
    elif "今天" in raw:
        start, end = _day(now)
        range_label = _label("创建时间" if "创建" in raw else "修改时间", start, end)
        if "创建" in raw:
            created_after, created_before = start, end
        else:
            modified_after, modified_before = start, end
    kinds = []
    if "文档" in raw:
        kinds.append("文档")
    if "代码" in raw or "程序" in raw:
        kinds.append("代码")
    for word in (
        "找一下", "帮我找", "帮我", "搜索", "查找", "找", "整理一下", "整理", "把",
        "移动", "归入", "上周", "昨天", "今天", "修改的", "创建的",
        "相关的", "相关", "一下", "的",
    ):
        search = search.replace(word, " ")
    search = re.sub(r"[，,。；;：:、！？!?]+", " ", search)
    text = " ".join(search.split())
    return UnderstoodQuery(
        action=action,
        text=text,
        excluded=excluded,
        kinds=kinds,
        modified_after=modified_after,
        modified_before=modified_before,
        created_after=created_after,
        created_before=created_before,
        range_label=range_label,
        notes=notes,
    )


def _previous_week(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = monday - dt.timedelta(days=7)
    end = monday - dt.timedelta(microseconds=1)
    return start, end


def _day(moment: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1) - dt.timedelta(microseconds=1)


def _label(kind: str, start: dt.datetime, end: dt.datetime) -> str:
    return f"{kind} {start.date().isoformat()} 至 {end.date().isoformat()}"


def _ns(moment: dt.datetime | None) -> int:
    if moment is None:
        return 0
    return int(moment.timestamp() * 1_000_000_000)
