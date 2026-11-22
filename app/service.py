"""业务编排：层位登记、记录批量订正、图的局部重算与查询。

核心一致性规则：
- 一个批次（add/correct/retract 混合）整体原子生效；
- **整组重算**：受影响顶点 = 批次触碰记录的旧端点、新端点所在**整组**的全部层位。
  因此撤回/更正组内一条记录时，组内其他层位（以及它们的边）不会因“未被载入”
  而丢失；拆组、并组后的每个新组都重新做传递归约；
- **写锁内复核**：图计算尽量放在写锁之外（无关组仍可并发订正），但取得写锁后
  会重新读取相关状态；若相关组已被其他已提交批次改动（令牌变化），则以最新
  已提交状态重新校验、重算，先提交成功的改动一定纳入成环检查；
- 成环则整批回滚（409），记录与图都保持原状；
- 记录改写与新图写入在同一事务内提交，保存失败一起回滚；
- 查询直接读取已落库的归约图（graph_edges / edge_support），不做临时计算。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import graph
from .db import db_session, init_db
from .schemas import BatchRequest, GroupOut


class CycleConflict(Exception):
    """本批改写会形成闭环，HTTP 409。"""

    def __init__(self, cycle: list[str], edges: list[dict]):
        self.cycle = cycle
        self.edges = edges


class ValidationFailure(Exception):
    """请求数据不满足前置条件，HTTP 422。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- 层位登记 ----------------

def register_strata(stratum_numbers: list[str], db_path: str | None = None) -> dict:
    seen: set[str] = set()
    duplicates_in_request = [s for s in stratum_numbers if s in seen or seen.add(s)]
    if duplicates_in_request:
        raise ValidationFailure(
            f"请求中层位号重复: {sorted(set(duplicates_in_request))}"
        )
    registered: list[str] = []
    already_existed: list[str] = []
    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for no in stratum_numbers:
                exists = conn.execute(
                    "SELECT 1 FROM strata WHERE stratum_no=?", (no,)
                ).fetchone()
                if exists:
                    already_existed.append(no)
                    continue
                conn.execute(
                    "INSERT INTO strata (stratum_no, group_id, created_at) "
                    "VALUES (?, NULL, ?)",
                    (no, _now()),
                )
                registered.append(no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"registered": registered, "already_existed": already_existed}


# ---------------- 批量订正 ----------------

@dataclass(frozen=True)
class _State:
    """一次批次在某个时点看到的相关状态。

    ``token`` 覆盖 ``_plan`` 所依赖的全部数据；写锁内重新读取后若令牌不变，
    说明相关组没有被其他批次改动，可以安全复用锁外算好的重算结果。
    """

    existing: dict[str, dict[str, Any]]       # 本批触碰记录在库中的当前形态
    records: dict[str, dict[str, Any]]        # 受影响整组内的全部记录（含已撤回）
    endpoint_groups: dict[str, int | None]    # 每个相关层位当前的组
    affected_vertices: frozenset[str]         # 受影响整组的全部层位
    old_group_ids: tuple[int, ...]            # 改动前涉及的旧组
    token: tuple


@dataclass
class _Plan:
    state: _State
    # (组内层位, 归约后保留的边, 边 -> 全部支持记录号)
    new_groups: list[tuple[frozenset[str], set[graph.Edge], dict[graph.Edge, list[str]]]]
    cycle: list[str] | None = None
    cycle_edges: list[dict] | None = None


def _validate_request(operations: list) -> None:
    add_nos = [op.record_no for op in operations if op.op == "add"]
    dup_in_req = {no for no in add_nos if add_nos.count(no) > 1}
    if dup_in_req:
        raise ValidationFailure(f"批次内新增记录号重复: {sorted(dup_in_req)}")
    touched_nos = {op.record_no for op in operations}
    if len(touched_nos) != len(operations):
        raise ValidationFailure("批次内同一记录号出现了多次操作")


def _prepare(conn: sqlite3.Connection, operations: list) -> _State:
    """在给定连接上校验请求并读取本批涉及的全部相关状态。

    相关范围是“整组”：先找出被触碰记录的旧端点、新端点，再把这些端点所在的
    旧组整体纳入，最后载入组内全部记录（含已撤回记录）。
    """
    touched_nos = {op.record_no for op in operations}
    placeholders = ",".join("?" * len(touched_nos))
    existing = {
        row["record_no"]: {
            "record_no": row["record_no"],
            "before": row["before_no"],
            "after": row["after_no"],
            "active": bool(row["active"]),
        }
        for row in conn.execute(
            f"SELECT record_no, before_no, after_no, active FROM records "
            f"WHERE record_no IN ({placeholders})",
            tuple(touched_nos),
        )
    }

    endpoint_nos: set[str] = set()
    for op in operations:
        if op.op in ("add", "correct"):
            if op.before == op.after:
                raise ValidationFailure(
                    f"记录 {op.record_no} 的先后层位不能相同（自环）"
                )
            endpoint_nos.add(op.before)
            endpoint_nos.add(op.after)
    # 更正/撤回前的旧端点同样属于“改动前后涉及”的范围
    for no in touched_nos:
        row = existing.get(no)
        if row is not None:
            endpoint_nos.add(row["before"])
            endpoint_nos.add(row["after"])

    missing = [
        no
        for no in sorted(endpoint_nos)
        if not conn.execute(
            "SELECT 1 FROM strata WHERE stratum_no=?", (no,)
        ).fetchone()
    ]
    if missing:
        raise ValidationFailure(f"层位尚未登记: {missing}")

    for op in operations:
        row = existing.get(op.record_no)
        if op.op == "add" and row is not None:
            raise ValidationFailure(f"记录号已存在: {op.record_no}")
        if op.op in ("correct", "retract") and row is None:
            raise ValidationFailure(f"记录号不存在: {op.record_no}")
        if op.op == "retract" and row is not None and not row["active"]:
            raise ValidationFailure(f"记录已撤回，不能重复撤回: {op.record_no}")
        if op.op == "correct" and row is not None and not row["active"]:
            raise ValidationFailure(f"记录已撤回，不能更正: {op.record_no}")

    # 相关端点所在的整组都纳入重算范围；端点若为孤立层位则只纳入自身
    endpoint_groups: dict[str, int | None] = {}
    old_group_ids: set[int] = set()
    for no in sorted(endpoint_nos):
        row = conn.execute(
            "SELECT group_id FROM strata WHERE stratum_no=?", (no,)
        ).fetchone()
        gid = row["group_id"] if row is not None else None
        endpoint_groups[no] = gid
        if gid is not None:
            old_group_ids.add(gid)

    affected_vertices: set[str] = set(endpoint_nos)
    if old_group_ids:
        marks = ",".join("?" * len(old_group_ids))
        affected_vertices.update(
            r["stratum_no"]
            for r in conn.execute(
                f"SELECT stratum_no FROM strata WHERE group_id IN ({marks})",
                tuple(sorted(old_group_ids)),
            )
        )

    if affected_vertices:
        marks = ",".join("?" * len(affected_vertices))
        record_rows = conn.execute(
            f"SELECT record_no, before_no, after_no, active FROM records "
            f"WHERE before_no IN ({marks}) OR after_no IN ({marks})",
            tuple(affected_vertices) * 2,
        ).fetchall()
    else:  # pragma: no cover - 每个合法批次至少有一个端点
        record_rows = []

    records = {
        r["record_no"]: {
            "record_no": r["record_no"],
            "before": r["before_no"],
            "after": r["after_no"],
            "active": bool(r["active"]),
        }
        for r in record_rows
    }

    token = (
        tuple(
            sorted(
                (no, r["before"], r["after"], r["active"])
                for no, r in existing.items()
            )
        ),
        tuple(sorted(endpoint_groups.items())),
        tuple(
            sorted(
                (r["record_no"], r["before"], r["after"], r["active"])
                for r in records.values()
            )
        ),
        tuple(sorted(old_group_ids)),
        tuple(sorted(affected_vertices)),
    )
    return _State(
        existing=existing,
        records=records,
        endpoint_groups=endpoint_groups,
        affected_vertices=frozenset(affected_vertices),
        old_group_ids=tuple(sorted(old_group_ids)),
        token=token,
    )


def _plan(state: _State, operations: list) -> _Plan:
    """在内存中模拟本批后的记录全集，检查成环并逐组重算归约图。"""
    records = {no: dict(r) for no, r in state.records.items()}
    for op in operations:
        if op.op == "add":
            records[op.record_no] = {
                "record_no": op.record_no,
                "before": op.before,
                "after": op.after,
                "active": True,
            }
        elif op.op == "correct":
            records[op.record_no].update(
                before=op.before, after=op.after, active=True
            )
        else:  # retract
            records[op.record_no]["active"] = False

    active_triples = [
        (r["record_no"], r["before"], r["after"])
        for r in records.values()
        if r["active"]
    ]
    vertices, adjacency, edge_support = graph.build_adjacency(
        active_triples, extra_vertices=state.affected_vertices
    )

    # 成环检查以本批生效时（含已提交改动）的记录全集为准
    cycle = graph.find_cycle(adjacency, vertices)
    if cycle is not None:
        return _Plan(
            state=state,
            new_groups=[],
            cycle=cycle,
            cycle_edges=graph.cycle_with_records(cycle, edge_support),
        )

    # 弱连通分量（忽略方向的相连整组）+ 组内传递归约。
    # 本批后失去全部边的层位成为孤立点，不产生只有一个顶点的空组。
    components = graph.weak_components(vertices, adjacency)
    new_groups: list[
        tuple[frozenset[str], set[graph.Edge], dict[graph.Edge, list[str]]]
    ] = []
    for component in components:
        if len(component) == 1:
            sole = next(iter(component))
            if not adjacency.get(sole):
                continue
        kept = graph.transitive_reduction(component, adjacency)
        new_groups.append((frozenset(component), kept, edge_support))
    return _Plan(state=state, new_groups=new_groups)


def _persist(
    conn: sqlite3.Connection, plan: _Plan, operations: list, now: str
) -> None:
    """在当前写事务内改写记录，并把受影响的旧组整体替换为重算后的新组。"""
    for op in operations:
        if op.op == "add":
            conn.execute(
                "INSERT INTO records "
                "(record_no, before_no, after_no, active, "
                "created_at, updated_at, retracted_at) "
                "VALUES (?, ?, ?, 1, ?, ?, NULL)",
                (op.record_no, op.before, op.after, now, now),
            )
        elif op.op == "correct":
            conn.execute(
                "UPDATE records SET before_no=?, after_no=?, updated_at=? "
                "WHERE record_no=?",
                (op.before, op.after, now, op.record_no),
            )
        else:  # retract
            conn.execute(
                "UPDATE records SET active=0, updated_at=?, retracted_at=? "
                "WHERE record_no=?",
                (now, now, op.record_no),
            )

    # 拆除旧组（边 -> 组 -> 层位解绑顺序，满足外键）；
    # 受影响组的全部层位都先解绑，重算后仍是孤立点的层位保持无组。
    old_group_ids = plan.state.old_group_ids
    if old_group_ids:
        marks = ",".join("?" * len(old_group_ids))
        conn.execute(
            f"DELETE FROM graph_edges WHERE group_id IN ({marks})",
            old_group_ids,
        )
        conn.execute(
            f"DELETE FROM groups WHERE group_id IN ({marks})",
            old_group_ids,
        )
        conn.execute(
            f"UPDATE strata SET group_id=NULL WHERE group_id IN ({marks})",
            old_group_ids,
        )

    # 写入新组（合并/拆分后的各组与本批记录一并生效）
    for component, kept_edges, edge_support in plan.new_groups:
        cur = conn.execute("INSERT INTO groups (created_at) VALUES (?)", (now,))
        new_group_id = cur.lastrowid
        conn.executemany(
            "UPDATE strata SET group_id=? WHERE stratum_no=?",
            [(new_group_id, v) for v in sorted(component)],
        )
        conn.executemany(
            "INSERT INTO graph_edges (group_id, before_no, after_no) "
            "VALUES (?, ?, ?)",
            [(new_group_id, u, v) for u, v in sorted(kept_edges)],
        )
        conn.executemany(
            "INSERT INTO edge_support (before_no, after_no, record_no) "
            "VALUES (?, ?, ?)",
            [
                (u, v, rec_no)
                for u, v in sorted(kept_edges)
                for rec_no in sorted(edge_support[(u, v)])
            ],
        )


def _load_affected_groups(
    conn: sqlite3.Connection, affected_vertices: frozenset[str]
) -> list[dict]:
    if not affected_vertices:
        return []
    marks = ",".join("?" * len(affected_vertices))
    ids = sorted(
        {
            row["group_id"]
            for row in conn.execute(
                "SELECT DISTINCT group_id FROM strata "
                f"WHERE group_id IS NOT NULL AND stratum_no IN ({marks})",
                tuple(affected_vertices),
            )
        }
    )
    return [_load_group(conn, gid) for gid in ids]


def _load_untouched_group_ids(
    conn: sqlite3.Connection, old_group_ids: tuple[int, ...]
) -> list[int]:
    if not old_group_ids:
        return [
            r["group_id"]
            for r in conn.execute("SELECT group_id FROM groups ORDER BY group_id")
        ]
    marks = ",".join("?" * len(old_group_ids))
    return [
        r["group_id"]
        for r in conn.execute(
            f"SELECT group_id FROM groups WHERE group_id NOT IN ({marks}) "
            f"ORDER BY group_id",
            old_group_ids,
        )
    ]


def apply_batch(batch: BatchRequest, db_path: str | None = None) -> dict:
    operations = batch.operations
    _validate_request(operations)

    with db_session(db_path) as conn:
        # 1. 锁外的读阶段：在一致快照上完成校验、整组载入与内存重算。
        #    图计算不持写锁，因此无关层位组的订正可以在此时正常完成。
        conn.execute("BEGIN")
        try:
            state = _prepare(conn, operations)
            plan = _plan(state, operations)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        # 2. 写锁内复核：重新读取相关状态。相关组若已被其他成功提交的批次
        #    改动（令牌变化），则用最新状态重新校验、重算，确保先提交成功的
        #    相关改动一定纳入成环检查与重算。
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = _prepare(conn, operations)
            if fresh.token != state.token:
                plan = _plan(fresh, operations)

            if plan.cycle is not None:
                conn.execute("ROLLBACK")
                raise CycleConflict(plan.cycle, plan.cycle_edges or [])

            # 未被本批重算的组：写入前快照，写入后这些组的 id 与内容均不变
            untouched = _load_untouched_group_ids(
                conn, plan.state.old_group_ids
            )

            _persist(conn, plan, operations, _now())

            affected_payload = _load_affected_groups(
                conn, plan.state.affected_vertices
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    return {
        "applied_operations": len(operations),
        "affected_groups": affected_payload,
        "untouched_groups": untouched,
    }


# ---------------- 查询（直接读已生效的图） ----------------

def _load_group(conn: sqlite3.Connection, group_id: int) -> dict:
    strata = [
        r["stratum_no"]
        for r in conn.execute(
            "SELECT stratum_no FROM strata WHERE group_id=? ORDER BY stratum_no",
            (group_id,),
        )
    ]
    edges: list[dict] = []
    edge_rows = conn.execute(
        "SELECT before_no, after_no FROM graph_edges "
        "WHERE group_id=? ORDER BY before_no, after_no",
        (group_id,),
    ).fetchall()
    for edge in edge_rows:
        records = [
            r["record_no"]
            for r in conn.execute(
                "SELECT record_no FROM edge_support "
                "WHERE before_no=? AND after_no=? ORDER BY record_no",
                (edge["before_no"], edge["after_no"]),
            )
        ]
        edges.append(
            {"before": edge["before_no"], "after": edge["after_no"],
             "record_numbers": records}
        )
    return {"group_id": group_id, "strata": strata, "edges": edges}


def get_group_for_stratum(stratum_no: str, db_path: str | None = None) -> dict:
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM strata WHERE stratum_no=?", (stratum_no,)
        ).fetchone()
        if row is None:
            raise ValidationFailure(f"层位不存在: {stratum_no}")
        if row["group_id"] is None:
            return {
                "group_id": None,
                "strata": [stratum_no],
                "edges": [],
            }
        return _load_group(conn, row["group_id"])


def list_groups(db_path: str | None = None) -> list[dict]:
    with db_session(db_path) as conn:
        ids = [
            r["group_id"]
            for r in conn.execute(
                "SELECT group_id FROM groups ORDER BY group_id"
            )
        ]
        return [_load_group(conn, gid) for gid in ids]


def list_strata(db_path: str | None = None) -> list[dict]:
    with db_session(db_path) as conn:
        return [
            {"stratum_no": r["stratum_no"], "group_id": r["group_id"]}
            for r in conn.execute(
                "SELECT stratum_no, group_id FROM strata ORDER BY stratum_no"
            )
        ]


def list_records(
    active_only: bool, db_path: str | None = None
) -> list[dict]:
    sql = (
        "SELECT record_no, before_no, after_no, active, created_at, "
        "updated_at, retracted_at FROM records"
    )
    params: tuple = ()
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY record_no"
    with db_session(db_path) as conn:
        return [
            {
                "record_no": r["record_no"],
                "before": r["before_no"],
                "after": r["after_no"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "retracted_at": r["retracted_at"],
            }
            for r in conn.execute(sql, params)
        ]


def ensure_initialized(db_path: str | None = None) -> None:
    init_db(db_path)
