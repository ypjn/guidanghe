"""卷宗关系图：从预览抽出边，不移动文件。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from relation_graph import (  # noqa: E402
    EDGE_CATEGORY, EDGE_CONTENT, EDGE_NEIGHBOR, build_relation_graph,
)


def _item(rel, *, category="实验", rule_key="keyword", selected=True,
          dup_group_id=None, dup_count=1, direction="学习"):
    name = os.path.basename(rel)
    return SimpleNamespace(
        file=SimpleNamespace(rel_path=rel, filename=name),
        category=category,
        direction=direction,
        rule_key=rule_key,
        selected=selected,
        dup_group_id=dup_group_id,
        dup_count=dup_count,
    )


def test_build_relation_graph_edges():
    plan = SimpleNamespace(items=[
        _item("大二上/实验一.pdf", category="实验", rule_key="keyword"),
        _item("images/实验一.png", category="实验",
              rule_key="neighbor:stem"),
        _item("副本/实验一.pdf", category="实验", rule_key="keyword",
              dup_group_id="g1", dup_count=2),
        _item("副本/实验一_copy.pdf", category="实验", rule_key="keyword",
              dup_group_id="g1", dup_count=2),
    ])
    graph = build_relation_graph(plan)
    kinds = {e.kind for e in graph.edges}
    assert EDGE_CATEGORY in kinds
    assert EDGE_NEIGHBOR in kinds
    assert EDGE_CONTENT in kinds
    assert any(n.kind == "cluster" for n in graph.nodes)
    assert any(n.rel_path.endswith("实验一.png") for n in graph.nodes)


def test_build_relation_graph_empty():
    graph = build_relation_graph(SimpleNamespace(items=[]))
    assert graph.nodes == []
    assert graph.edges == []
