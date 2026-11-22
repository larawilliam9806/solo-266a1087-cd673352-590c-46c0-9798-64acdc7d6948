"""纯图算法：有向图层位关系的成环检测、弱连通分量、传递归约。

约定：
- 顶点是已登记层位号（字符串）。
- 一条有向边 u -> v 表示 u 先于 v 形成。
- 同一对层位之间可由多份现场记录支持，记录号聚合在 edge_support 中。
- 图为 DAG 时，传递归约（transitive reduction）唯一，
  即删除所有可由其他路径间接推出的边，只保留"必经直线"。
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

Vertex = str
# (record_no, before, after)：记录号、先形成层位、后形成层位
RecordTriple = tuple[str, str, str]
Edge = tuple[Vertex, Vertex]


def build_adjacency(
    records: Iterable[RecordTriple],
    extra_vertices: Iterable[Vertex] = (),
) -> tuple[set[Vertex], dict[Vertex, set[Vertex]], dict[Edge, list[str]]]:
    """把记录聚合成邻接表和边 -> 支持记录号列表。"""
    vertices: set[Vertex] = set(extra_vertices)
    adjacency: dict[Vertex, set[Vertex]] = {}
    edge_support: dict[Edge, list[str]] = {}

    for record_no, before, after in records:
        vertices.add(before)
        vertices.add(after)
        adjacency.setdefault(before, set()).add(after)
        adjacency.setdefault(after, set())
        edge_support.setdefault((before, after), []).append(record_no)

    for vertex in vertices:
        adjacency.setdefault(vertex, set())
    return vertices, adjacency, edge_support


def find_cycle(
    adjacency: dict[Vertex, set[Vertex]],
    vertices: Iterable[Vertex],
) -> list[Vertex] | None:
    """迭代式 DFS 检测有向环，返回环上顶点（首尾相同），无环返回 None。"""
    color: dict[Vertex, int] = {v: 0 for v in vertices}  # 0 白 1 灰 2 黑
    parent: dict[Vertex, Vertex | None] = {v: None for v in color}

    for start in color:
        if color[start] != 0:
            continue
        stack: list[tuple[Vertex, Iterable[Vertex]]] = [
            (start, iter(sorted(adjacency.get(start, set()))))
        ]
        color[start] = 1
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for nxt in neighbors:
                if color[nxt] == 0:
                    color[nxt] = 1
                    parent[nxt] = node
                    stack.append((nxt, iter(sorted(adjacency.get(nxt, set())))))
                    advanced = True
                    break
                if color[nxt] == 1:
                    # 回边 node -> nxt，回溯出环
                    cycle = [node]
                    cur = node
                    while cur != nxt:
                        cur = parent[cur]  # 灰色节点必有父链
                        cycle.append(cur)
                    cycle.reverse()
                    cycle.append(nxt)
                    return cycle
            if not advanced:
                color[node] = 2
                stack.pop()
    return None


def weak_components(
    vertices: Iterable[Vertex], adjacency: dict[Vertex, set[Vertex]]
) -> list[set[Vertex]]:
    """按忽略方向的连通性分组（弱连通分量），结果按组内最小层位号排序。"""
    undirected: dict[Vertex, set[Vertex]] = {v: set() for v in vertices}
    for u, neighbors in adjacency.items():
        undirected.setdefault(u, set())
        for v in neighbors:
            undirected.setdefault(v, set()).add(u)
            undirected[u].add(v)

    seen: set[Vertex] = set()
    components: list[set[Vertex]] = []
    for start in sorted(undirected):
        if start in seen:
            continue
        group: set[Vertex] = set()
        queue: deque[Vertex] = deque([start])
        seen.add(start)
        while queue:
            node = queue.popleft()
            group.add(node)
            for nxt in undirected.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        components.append(group)
    return components


def transitive_reduction(
    vertices: Iterable[Vertex],
    adjacency: dict[Vertex, set[Vertex]],
) -> set[Edge]:
    """DAG 的传递归约：边 u->v 保留当且仅当不存在绕过该边的其他路径 u~>v。

    对每条边临时删除后做可达性检查；整体复杂度 O(E*(V+E))，
    对考古层位这种小规模图足够，且语义直白不易出错。
    """
    kept: set[Edge] = set()
    vertex_list = list(vertices)
    for u in vertex_list:
        for v in sorted(adjacency.get(u, set())):
            if not _reachable_without(adjacency, u, v):
                kept.add((u, v))
    return kept


def _reachable(
    adjacency: dict[Vertex, set[Vertex]], source: Vertex, target: Vertex
) -> bool:
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for nxt in adjacency.get(node, set()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _reachable_without(
    adjacency: dict[Vertex, set[Vertex]],
    source: Vertex,
    target: Vertex,
) -> bool:
    """不走直连边 source->target 时，source 是否仍能到达 target。"""
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        for nxt in adjacency.get(node, set()):
            if node == source and nxt == target:
                continue
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def cycle_with_records(
    cycle: list[Vertex], edge_support: dict[Edge, list[str]]
) -> list[dict]:
    """把环上的每条边附上支持该关系的全部记录号（以本批生效时为准）。"""
    detailed = []
    for u, v in zip(cycle, cycle[1:]):
        detailed.append(
            {
                "before": u,
                "after": v,
                "record_numbers": list(edge_support.get((u, v), [])),
            }
        )
    return detailed
