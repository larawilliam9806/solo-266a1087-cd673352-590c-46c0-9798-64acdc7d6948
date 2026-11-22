"""业务编排：层位登记、记录批量订正、图的局部重算与查询。

核心一致性规则：
- 一个批次（add/correct/retract 混合）整体原子生效；
- 先在事务内模拟本批后的记录全集，成环则整体回滚，原记录与原图不动；
- 只重算改动前后涉及的层位组（顶点集合取并集），其他组的边不删除不重算，
  因此针对其他组的并发订正不会被本批阻塞读取，写锁仅在最后写入时短暂持有；
- 查询直接读取已落库的归约图（graph_edges / edge_support），不做临时计算。
"""
from __future__ import annotations

import sqlite3
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

def apply_batch(batch: BatchRequest, db_path: str | None = None) -> dict:
    operations = batch.operations

    # 1. 请求自身校验：编号唯一性与操作类型
    add_nos = [op.record_no for op in operations if op.op == "add"]
    dup_in_req = {no for no in add_nos if add_nos.count(no) > 1}
    if dup_in_req:
        raise ValidationFailure(f"批次内新增记录号重复: {sorted(dup_in_req)}")
    touched_nos = {op.record_no for op in operations}
    if len(touched_nos) != len(operations):
        raise ValidationFailure("批次内同一记录号出现了多次操作")

    # 2. 端点层位与既有记录的前置校验（读阶段，不持写锁）
    with db_session(db_path) as conn:
        placeholders = ",".join("?" * len(touched_nos))
        existing = {
            row["record_no"]: row
            for row in conn.execute(
                f"SELECT record_no, active FROM records "
                f"WHERE record_no IN ({placeholders})",
                tuple(touched_nos),
            )
        }
        endpoint_nos: set[str] = set()
        for op in operations:
            if op.op in ("add", "correct"):
                endpoint_nos.add(op.before)
                endpoint_nos.add(op.after)
                if op.before == op.after:
                    raise ValidationFailure(
                        f"记录 {op.record_no} 的先后层位不能相同（自环）"
                    )
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

        # 3. 改动前涉及的组：受触碰记录端点所在的组
        affected_vertices = set(endpoint_nos)
        for no in touched_nos:
            row = existing.get(no)
            if row is None:
                continue
            rec = conn.execute(
                "SELECT before_no, after_no FROM records WHERE record_no=?",
                (no,),
            ).fetchone()
            affected_vertices.add(rec["before_no"])
            affected_vertices.add(rec["after_no"])

        old_group_ids = [
            r["group_id"]
            for r in conn.execute(
                "SELECT DISTINCT group_id FROM strata "
                "WHERE group_id IS NOT NULL AND stratum_no IN ("
                + ",".join("?" * len(affected_vertices)) + ")",
                tuple(affected_vertices),
            )
        ]

        # 4. 载入受影响顶点上的全部记录（含撤回的），在内存中模拟本批
        record_rows = conn.execute(
            "SELECT record_no, before_no, after_no, active, created_at "
            "FROM records "
            "WHERE before_no IN (" + ",".join("?" * len(affected_vertices)) + ") "
            "OR after_no IN (" + ",".join("?" * len(affected_vertices)) + ")",
            tuple(affected_vertices) * 2,
        ).fetchall()

        # 5. 进入写事务，重算并整体生效
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now()
            # 未参与本次重算的既有组：其他组的订正不受本批影响
            all_group_ids = [
                r["group_id"]
                for r in conn.execute(
                    "SELECT group_id FROM groups ORDER BY group_id"
                )
            ]
            untouched = [gid for gid in all_group_ids if gid not in old_group_ids]
            records: dict[str, dict[str, Any]] = {
                r["record_no"]: {
                    "record_no": r["record_no"],
                    "before": r["before_no"],
                    "after": r["after_no"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in record_rows
            }

            for op in operations:
                if op.op == "add":
                    conn.execute(
                        "INSERT INTO records "
                        "(record_no, before_no, after_no, active, "
                        "created_at, updated_at, retracted_at) "
                        "VALUES (?, ?, ?, 1, ?, ?, NULL)",
                        (op.record_no, op.before, op.after, now, now),
                    )
                    records[op.record_no] = {
                        "record_no": op.record_no,
                        "before": op.before,
                        "after": op.after,
                        "active": True,
                        "created_at": now,
                    }
                elif op.op == "correct":
                    conn.execute(
                        "UPDATE records SET before_no=?, after_no=?, "
                        "updated_at=? WHERE record_no=?",
                        (op.before, op.after, now, op.record_no),
                    )
                    records[op.record_no].update(
                        before=op.before, after=op.after, active=True
                    )
                else:  # retract
                    conn.execute(
                        "UPDATE records SET active=0, updated_at=?, "
                        "retracted_at=? WHERE record_no=?",
                        (now, now, op.record_no),
                    )
                    records[op.record_no]["active"] = False

            active_triples = [
                (r["record_no"], r["before"], r["after"])
                for r in records.values()
                if r["active"]
            ]
            vertices, adjacency, edge_support = graph.build_adjacency(
                active_triples, extra_vertices=affected_vertices
            )

            # 6. 闭环检查（以本批生效时的记录为准）
            cycle = graph.find_cycle(adjacency, vertices)
            if cycle is not None:
                detailed = graph.cycle_with_records(cycle, edge_support)
                conn.rollback()
                raise CycleConflict(cycle, detailed)

            # 7. 弱连通分量（忽略方向的相连整组）+ 组内传递归约。
            #    本批后失去全部边的层位成为孤立点，不产生只有一个顶点的空组；
            #    它们的 group_id 在步骤 8 解绑后保持 NULL，下次并入关系时再成组。
            components = graph.weak_components(vertices, adjacency)
            new_groups: list[tuple[set[str], set[tuple[str, str]]]] = []
            for component in components:
                if len(component) == 1:
                    sole = next(iter(component))
                    if not adjacency.get(sole):
                        continue
                kept = graph.transitive_reduction(component, adjacency)
                new_groups.append((component, kept))

            # 8. 拆除旧组（边 -> 组 -> 层位解绑顺序，满足外键）
            if old_group_ids:
                marks = ",".join("?" * len(old_group_ids))
                conn.execute(
                    f"DELETE FROM graph_edges WHERE group_id IN ({marks})",
                    tuple(old_group_ids),
                )
                conn.execute(
                    f"DELETE FROM groups WHERE group_id IN ({marks})",
                    tuple(old_group_ids),
                )
                conn.execute(
                    "UPDATE strata SET group_id=NULL WHERE group_id IN ("
                    + marks + ")",
                    tuple(old_group_ids),
                )

            # 9. 写入新组（合并/拆分后的各组与本批一并生效）
            for component, kept_edges in new_groups:
                cur = conn.execute(
                    "INSERT INTO groups (created_at) VALUES (?)", (now,)
                )
                new_group_id = cur.lastrowid
                conn.executemany(
                    "UPDATE strata SET group_id=? WHERE stratum_no=?",
                    [(new_group_id, v) for v in component],
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
                        for rec_no in edge_support[(u, v)]
                    ],
                )

            affected_payload = [
                _load_group(conn, gid)
                for gid in sorted(
                    {
                        row["group_id"]
                        for row in conn.execute(
                            "SELECT DISTINCT group_id FROM strata "
                            "WHERE group_id IS NOT NULL AND stratum_no IN ("
                            + ",".join("?" * len(affected_vertices)) + ")",
                            tuple(affected_vertices),
                        )
                    }
                )
            ]
            conn.commit()
        except Exception:
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
