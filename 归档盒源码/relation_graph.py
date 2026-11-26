"""
归档盒 — 卷宗关系图（当前预览方案）

节点：文件 + 课程/类别簇。
边：同一份内容、沾边归类、同类别。
只读：点节点回传路径，不移动文件。不做全库 Obsidian 式星空图。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QPainterPath
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from operation_feedback import install
from ui_theme import INK, LINE, MUTE, PAPER, RIBBON, STAMP, STAMP_SOFT, WHITE


EDGE_CONTENT = "content"
EDGE_NEIGHBOR = "neighbor"
EDGE_CATEGORY = "category"

EDGE_COLORS = {
    EDGE_CONTENT: QColor(RIBBON),
    EDGE_NEIGHBOR: QColor(STAMP),
    EDGE_CATEGORY: QColor("#94A3B8"),
}

EDGE_LABELS = {
    EDGE_CONTENT: "同一份内容",
    EDGE_NEIGHBOR: "沾边归类",
    EDGE_CATEGORY: "同类别",
}


@dataclass
class GraphNode:
    node_id: str
    kind: str          # file | cluster
    label: str
    rel_path: str = ""
    x: float = 0.0
    y: float = 0.0


@dataclass
class GraphEdge:
    a: str
    b: str
    kind: str
    note: str = ""


@dataclass
class RelationGraph:
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)

    def by_id(self) -> Dict[str, GraphNode]:
        return {n.node_id: n for n in self.nodes}


def build_relation_graph(plan) -> RelationGraph:
    """从 OrganizePlan 抽出可核对关系（上限控制，避免上千点卡死）。"""
    items = list(getattr(plan, "items", []) or [])
    if not items:
        return RelationGraph()

    # 优先：有邻域、有重复、或已勾选；其余截断
    scored = []
    for it in items:
        score = 0
        rk = it.rule_key or ""
        if rk.startswith("neighbor:"):
            score += 3
        if it.dup_count > 1 or it.dup_group_id:
            score += 3
        if it.selected:
            score += 1
        if it.category:
            score += 1
        scored.append((score, it))
    scored.sort(key=lambda x: (-x[0], x[1].file.rel_path.lower()))
    chosen = [it for s, it in scored if s > 0][:80]
    if len(chosen) < 12:
        chosen = [it for _, it in scored[:40]]

    nodes: Dict[str, GraphNode] = {}
    edges: List[GraphEdge] = []
    edge_keys = set()

    def add_node(nid: str, kind: str, label: str, rel: str = "") -> None:
        if nid not in nodes:
            nodes[nid] = GraphNode(node_id=nid, kind=kind, label=label, rel_path=rel)

    def add_edge(a: str, b: str, kind: str, note: str = "") -> None:
        if a == b or a not in nodes or b not in nodes:
            return
        key = (kind, tuple(sorted((a, b))))
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(GraphEdge(a=a, b=b, kind=kind, note=note))

    # 类别簇
    for it in chosen:
        cat = (it.category or it.direction or "待定").strip() or "待定"
        cid = f"c:{cat}"
        add_node(cid, "cluster", cat)
        fid = f"f:{it.file.rel_path}"
        name = it.file.filename
        add_node(fid, "file", name, it.file.rel_path)
        add_edge(fid, cid, EDGE_CATEGORY, f"同类别「{cat}」")

    # 沾边：连到同目标类别下已登记的样本（简化：连簇即可；额外连同 stem 文件）
    by_stem: Dict[str, List[str]] = {}
    for it in chosen:
        stem = os.path.splitext(it.file.filename)[0].strip().lower()
        if len(stem) >= 2:
            by_stem.setdefault(stem, []).append(f"f:{it.file.rel_path}")
        if (it.rule_key or "").startswith("neighbor:"):
            # 与同类别其他文件弱连
            cat = (it.category or "").strip()
            for other in chosen:
                if other is it:
                    continue
                if (other.category or "") == cat and not (other.rule_key or "").startswith("neighbor:"):
                    add_edge(
                        f"f:{it.file.rel_path}",
                        f"f:{other.file.rel_path}",
                        EDGE_NEIGHBOR,
                        "沾边归类",
                    )
                    break

    for paths in by_stem.values():
        if len(paths) < 2:
            continue
        for i in range(len(paths) - 1):
            add_edge(paths[i], paths[i + 1], EDGE_NEIGHBOR, "文件名主干相近")

    # 同一份内容
    by_dup: Dict[str, List[str]] = {}
    for it in chosen:
        gid = it.dup_group_id or ""
        if gid and it.dup_count > 1:
            by_dup.setdefault(gid, []).append(f"f:{it.file.rel_path}")
    for gid, paths in by_dup.items():
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                add_edge(paths[i], paths[j], EDGE_CONTENT, f"指纹组 {gid}")

    graph = RelationGraph(nodes=list(nodes.values()), edges=edges)
    _layout_clusters(graph)
    return graph


def _layout_clusters(graph: RelationGraph) -> None:
    clusters = [n for n in graph.nodes if n.kind == "cluster"]
    files = [n for n in graph.nodes if n.kind == "file"]
    if not clusters:
        for i, n in enumerate(files):
            ang = 2 * math.pi * i / max(1, len(files))
            n.x = 220 * math.cos(ang)
            n.y = 160 * math.sin(ang)
        return
    for i, c in enumerate(clusters):
        ang = 2 * math.pi * i / len(clusters) - math.pi / 2
        c.x = 160 * math.cos(ang)
        c.y = 120 * math.sin(ang)
    # 文件挂在所属簇周围
    owned: Dict[str, List[GraphNode]] = {c.node_id: [] for c in clusters}
    node_map = graph.by_id()
    for e in graph.edges:
        if e.kind != EDGE_CATEGORY:
            continue
        a, b = node_map.get(e.a), node_map.get(e.b)
        if not a or not b:
            continue
        if a.kind == "file" and b.kind == "cluster":
            owned.setdefault(b.node_id, []).append(a)
        elif b.kind == "file" and a.kind == "cluster":
            owned.setdefault(a.node_id, []).append(b)
    for c in clusters:
        kids = owned.get(c.node_id, [])
        for j, f in enumerate(kids):
            ang = 2 * math.pi * j / max(1, len(kids))
            f.x = c.x + 90 * math.cos(ang)
            f.y = c.y + 70 * math.sin(ang)


class RelationGraphCanvas(QWidget):
    nodeActivated = Signal(str)  # rel_path

    def __init__(self, graph: RelationGraph, parent=None):
        super().__init__(parent)
        self.graph = graph
        self._hover: Optional[str] = None
        self._offset = QPointF(0, 0)
        self._drag_origin: Optional[QPointF] = None
        self._scale = 1.0
        self.setMinimumSize(520, 360)
        self.setMouseTracking(True)
        self.setBackgroundRole(self.backgroundRole())

    def set_graph(self, graph: RelationGraph) -> None:
        self.graph = graph
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(PAPER))
        cx = self.width() / 2 + self._offset.x()
        cy = self.height() / 2 + self._offset.y()
        p.translate(cx, cy)
        p.scale(self._scale, self._scale)

        node_map = self.graph.by_id()
        for e in self.graph.edges:
            a, b = node_map.get(e.a), node_map.get(e.b)
            if not a or not b:
                continue
            color = EDGE_COLORS.get(e.kind, QColor(LINE))
            pen = QPen(color, 1.6 if e.kind != EDGE_CATEGORY else 1.0)
            if e.kind == EDGE_CATEGORY:
                pen.setStyle(Qt.PenStyle.DotLine)
            p.setPen(pen)
            p.drawLine(QPointF(a.x, a.y), QPointF(b.x, b.y))

        font = QFont(self.font())
        for n in self.graph.nodes:
            if n.kind == "cluster":
                r = 28
                brush = QBrush(QColor(STAMP))
                text_color = QColor(WHITE)
            else:
                r = 18
                brush = QBrush(QColor(WHITE))
                text_color = QColor(INK)
            if n.node_id == self._hover:
                r += 3
            p.setBrush(brush)
            p.setPen(QPen(QColor(STAMP if n.kind == "cluster" else LINE), 1.4))
            p.drawEllipse(QPointF(n.x, n.y), r, r)
            font.setPointSize(8 if n.kind == "file" else 9)
            font.setBold(n.kind == "cluster")
            p.setFont(font)
            p.setPen(text_color if n.kind == "cluster" else QColor(MUTE))
            label = n.label if len(n.label) <= 8 else n.label[:7] + "…"
            p.drawText(
                QRectF(n.x - 46, n.y + r + 2, 92, 16),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )
        p.end()

    def _hit(self, pos) -> Optional[GraphNode]:
        cx = self.width() / 2 + self._offset.x()
        cy = self.height() / 2 + self._offset.y()
        x = (pos.x() - cx) / self._scale
        y = (pos.y() - cy) / self._scale
        best = None
        best_d = 9999.0
        for n in self.graph.nodes:
            d = math.hypot(n.x - x, n.y - y)
            limit = 32 if n.kind == "cluster" else 22
            if d <= limit and d < best_d:
                best, best_d = n, d
        return best

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None:
            delta = event.position() - self._drag_origin
            self._offset += delta
            self._drag_origin = event.position()
            self.update()
            return
        hit = self._hit(event.position())
        nid = hit.node_id if hit else None
        if nid != self._hover:
            self._hover = nid
            if hit and hit.rel_path:
                self.setToolTip(hit.rel_path)
            elif hit:
                self.setToolTip(f"类别：{hit.label}")
            else:
                self.setToolTip("")
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit(event.position())
            if hit and hit.kind == "file" and hit.rel_path:
                self.nodeActivated.emit(hit.rel_path)
            else:
                self._drag_origin = event.position()

    def mouseReleaseEvent(self, event):
        self._drag_origin = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        factor = 1.1 if delta > 0 else 0.9
        self._scale = max(0.5, min(2.2, self._scale * factor))
        self.update()


class RelationGraphDialog(QDialog):
    focusPath = Signal(str)

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("卷宗关系 — 当前预览")
        self.resize(720, 520)
        self.setModal(False)
        lay = QVBoxLayout(self)
        tip = QLabel(
            "只展示当前预览里的可核对关系：同一份内容（红）、沾边归类（蓝）、"
            "同类别（灰点线）。拖动画布，滚轮缩放；点文件节点可回主表定位。"
            "不会移动任何文件。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{MUTE};padding:4px;")
        lay.addWidget(tip)
        legend = QLabel(
            "● 红线 · 同一份内容　　● 蓝线 · 沾边归类　　· 灰点 · 同类别簇"
        )
        legend.setStyleSheet(f"color:{STAMP};font-size:12px;padding:0 4px 6px 4px;")
        lay.addWidget(legend)
        graph = build_relation_graph(plan)
        self.canvas = RelationGraphCanvas(graph)
        self.canvas.nodeActivated.connect(self.focusPath.emit)
        lay.addWidget(self.canvas, 1)
        stats = QLabel(
            f"节点 {len(graph.nodes)} · 连线 {len(graph.edges)} "
            f"（已截断过大预览，避免卡顿）"
        )
        stats.setStyleSheet(f"color:{MUTE};font-size:12px;")
        lay.addWidget(stats)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        lay.addWidget(bb)
        self.feedback = install(lay)
        self.feedback.reflect(
            f"已载入 {len(graph.nodes)} 个节点、{len(graph.edges)} 条关系。")
