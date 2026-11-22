"""图算法单元测试：成环检测、弱连通分量、传递归约。"""
from __future__ import annotations

from app.graph import (
    build_adjacency,
    find_cycle,
    transitive_reduction,
    weak_components,
)


def _reduction(records, extra=()):
    vertices, adjacency, support = build_adjacency(records, extra)
    assert find_cycle(adjacency, vertices) is None
    kept = transitive_reduction(vertices, adjacency)
    return kept, support, adjacency, vertices


def test_transitive_reduction_removes_inferable_edges():
    # A->B, B->C, A->C：A->C 可间接推出，归约后删除
    records = [("r1", "A", "B"), ("r2", "B", "C"), ("r3", "A", "C")]
    kept, _, _, _ = _reduction(records)
    assert kept == {("A", "B"), ("B", "C")}


def test_diamond_shape_keeps_all_sides():
    # B、C 两条并列路径都无法互相替代，四条边全部保留
    records = [
        ("r1", "A", "B"),
        ("r2", "A", "C"),
        ("r3", "B", "D"),
        ("r4", "C", "D"),
    ]
    kept, _, _, _ = _reduction(records)
    assert kept == {("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")}


def test_multiple_records_aggregate_on_one_edge():
    records = [
        ("r1", "A", "B"),
        ("r2", "A", "B"),
        ("r3", "B", "C"),
    ]
    kept, support, _, _ = _reduction(records)
    assert kept == {("A", "B"), ("B", "C")}
    assert sorted(support[("A", "B")]) == ["r1", "r2"]


def test_edge_restored_when_indirect_path_disappears():
    # 先有 A->B->C 与 A->C，删除 B->C 后 A->C 不可再间接推出，应补回
    records = [("r1", "A", "B"), ("r2", "B", "C"), ("r3", "A", "C")]
    kept1, _, adj, verts = _reduction(records)
    assert ("A", "C") not in kept1

    adj["B"].discard("C")
    assert find_cycle(adj, verts) is None
    kept2 = transitive_reduction(verts, adj)
    assert kept2 == {("A", "B"), ("A", "C")}


def test_cycle_detection_returns_cycle_path():
    records = [("r1", "A", "B"), ("r2", "B", "C"), ("r3", "C", "A")]
    vertices, adjacency, _ = build_adjacency(records)
    cycle = find_cycle(adjacency, vertices)
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"A", "B", "C"}


def test_self_loop_is_a_cycle():
    vertices, adjacency, _ = build_adjacency([("r1", "A", "A")])
    assert find_cycle(adjacency, vertices) == ["A", "A"]


def test_weak_components_ignore_direction():
    # A->B 与 C->B 忽略方向后属于同一组；D 单独一组
    records = [("r1", "A", "B"), ("r2", "C", "B")]
    vertices, adjacency, _ = build_adjacency(records, extra_vertices=("D",))
    components = weak_components(vertices, adjacency)
    groups = sorted(sorted(g) for g in components)
    assert groups == [["A", "B", "C"], ["D"]]


def test_long_chain_reduces_to_adjacent_edges():
    records = [(f"r{i}", f"L{i}", f"L{i + 1}") for i in range(5)]
    vertices, adjacency, _ = build_adjacency(records)
    kept = transitive_reduction(vertices, adjacency)
    assert kept == {(f"L{i}", f"L{i + 1}") for i in range(5)}
