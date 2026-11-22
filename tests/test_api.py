"""FastAPI 端到端测试：批次原子性、局部重算、并发与错误处理。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ARCH_DB_PATH", str(db_path))
    # 延迟导入，确保环境变量生效
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _register(client, *numbers):
    resp = client.post("/strata", json={"stratum_numbers": list(numbers)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _batch(client, operations, expected=201):
    resp = client.post("/records/batch", json={"operations": operations})
    assert resp.status_code == expected, resp.text
    return resp.json()


def _group(client, stratum_no):
    resp = client.get(f"/groups/by-stratum/{stratum_no}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_transitive_reduction_end_to_end(client):
    _register(client, "L1", "L2", "L3")
    result = _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L2", "after": "L3"},
        {"op": "add", "record_no": "R3", "before": "L1", "after": "L3"},
    ])
    edges = {(e["before"], e["after"]): e for e in result["affected_groups"][0]["edges"]}
    assert set(edges) == {("L1", "L2"), ("L2", "L3")}
    # 图查询直接读到已生效的归约图
    assert {
        (e["before"], e["after"]) for e in _group(client, "L1")["edges"]
    } == {("L1", "L2"), ("L2", "L3")}


def test_each_edge_carries_all_supporting_records(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R3", "before": "L2", "after": "L3"},
    ])
    group = _group(client, "L1")
    edge = next(e for e in group["edges"] if (e["before"], e["after"]) == ("L1", "L2"))
    assert edge["record_numbers"] == ["R1", "R2"]


def test_edge_restored_after_indirect_path_lost(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L2", "after": "L3"},
        {"op": "add", "record_no": "R3", "before": "L1", "after": "L3"},
    ])
    # 撤回 R2 后 L1->L3 失去间接路径，R3 支撑的直连线应补回
    result = _batch(client, [{"op": "retract", "record_no": "R2"}])
    edges = {
        (e["before"], e["after"])
        for e in result["affected_groups"][0]["edges"]
    }
    assert edges == {("L1", "L2"), ("L1", "L3")}


def test_cycle_rejected_with_cycle_and_record_numbers(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L2", "after": "L3"},
    ])
    payload = [{"op": "add", "record_no": "R3", "before": "L3", "after": "L1"}]
    body = _batch(client, payload, expected=409)
    assert body["cycle"][0] == body["cycle"][-1]
    assert set(body["cycle"]) == {"L1", "L2", "L3"}
    # 环上每条边都附带以本批为准的支持记录号
    edge_map = {(e["before"], e["after"]): e["record_numbers"] for e in body["edges"]}
    assert edge_map[("L3", "L1")] == ["R3"]
    assert edge_map[("L1", "L2")] == ["R1"]

    # 原记录与关系图保持不变：R3 不存在，图仍是两条边
    records = client.get("/records", params={"active_only": True}).json()
    assert {r["record_no"] for r in records} == {"R1", "R2"}
    edges = {(e["before"], e["after"]) for e in _group(client, "L1")["edges"]}
    assert edges == {("L1", "L2"), ("L2", "L3")}


def test_failed_batch_is_atomic(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    # 同一批次中合法更正 + 非法新增（成环）：整批驳回
    _batch(client, [
        {"op": "correct", "record_no": "R1", "before": "L2", "after": "L1"},
        {"op": "add", "record_no": "R2", "before": "L1", "after": "L2"},
    ], expected=409)
    record = next(
        r for r in client.get("/records").json() if r["record_no"] == "R1"
    )
    assert record["before"] == "L1" and record["after"] == "L2"
    assert all(r["record_no"] != "R2" for r in client.get("/records").json())


def test_correct_rewrites_endpoints(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    result = _batch(client, [
        {"op": "correct", "record_no": "R1", "before": "L2", "after": "L3"},
    ])
    edges = {
        (e["before"], e["after"])
        for e in result["affected_groups"][0]["edges"]
    }
    assert edges == {("L2", "L3")}
    record = next(
        r for r in client.get("/records").json() if r["record_no"] == "R1"
    )
    assert record["before"] == "L2" and record["after"] == "L3" and record["active"]


def test_groups_merge_and_split(client):
    _register(client, "L1", "L2", "L3", "L4")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    _batch(client, [
        {"op": "add", "record_no": "R2", "before": "L3", "after": "L4"},
    ])
    groups_before = client.get("/groups").json()
    assert len(groups_before) == 2

    # 合并：L2 -> L3 把两组连成一组
    merged = _batch(client, [
        {"op": "add", "record_no": "R3", "before": "L2", "after": "L3"},
    ])
    assert len(merged["affected_groups"]) == 1
    group = merged["affected_groups"][0]
    assert set(group["strata"]) == {"L1", "L2", "L3", "L4"}

    # 拆分：撤回桥接记录，恢复为两组
    split = _batch(client, [{"op": "retract", "record_no": "R3"}])
    assert len(split["affected_groups"]) == 2
    strata_sets = {tuple(g["strata"]) for g in split["affected_groups"]}
    assert strata_sets == {("L1", "L2"), ("L3", "L4")}


def test_untouched_group_not_recomputed(client):
    _register(client, "L1", "L2", "L3", "L4")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    _batch(client, [
        {"op": "add", "record_no": "R2", "before": "L3", "after": "L4"},
    ])
    result = _batch(client, [
        {"op": "add", "record_no": "R3", "before": "L1", "after": "L2"},
    ])
    # L3/L4 组的 group_id 出现在 untouched 列表，且本批只影响一个组
    all_ids = {g["group_id"] for g in client.get("/groups").json()}
    assert set(result["untouched_groups"]) == all_ids - {
        g["group_id"] for g in result["affected_groups"]
    }
    untouched_id = (all_ids - {
        g["group_id"] for g in result["affected_groups"]
    }).pop()
    untouched = next(
        g for g in client.get("/groups").json() if g["group_id"] == untouched_id
    )
    assert untouched["edges"][0]["record_numbers"] == ["R2"]


def test_isolated_stratum_returns_singleton_group(client):
    _register(client, "LX")
    group = _group(client, "LX")
    assert group["group_id"] is None
    assert group["strata"] == ["LX"]
    assert group["edges"] == []


def test_validation_errors(client):
    _register(client, "L1")
    # 未登记层位
    body = _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L9"},
    ], expected=422)
    assert "L9" in body["detail"]

    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L1"},
    ], expected=422)

    # 重复记录号
    _register(client, "L2")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ], expected=422)

    # 撤回不存在的记录
    _batch(client, [{"op": "retract", "record_no": "NOPE"}], expected=422)

    # 重复撤回
    _batch(client, [{"op": "retract", "record_no": "R1"}])
    _batch(client, [{"op": "retract", "record_no": "R1"}], expected=422)


def test_register_strata_is_idempotent(client):
    first = _register(client, "L1", "L2")
    assert set(first["registered"]) == {"L1", "L2"}
    second = _register(client, "L2", "L3")
    assert second["already_existed"] == ["L2"]
    assert second["registered"] == ["L3"]


def test_retracting_only_edge_returns_strata_to_isolated(client):
    _register(client, "L1", "L2")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
    ])
    assert _group(client, "L1")["group_id"] is not None

    result = _batch(client, [{"op": "retract", "record_no": "R1"}])
    assert result["affected_groups"] == []
    # 旧组已删除，两个层位回到无组孤立状态
    assert _group(client, "L1") == {
        "group_id": None, "strata": ["L1"], "edges": []
    }
    assert _group(client, "L2") == {
        "group_id": None, "strata": ["L2"], "edges": []
    }
    assert client.get("/groups").json() == []
    # 再次建立关系时重新成组
    again = _batch(client, [
        {"op": "add", "record_no": "R2", "before": "L1", "after": "L2"},
    ])
    assert len(again["affected_groups"]) == 1


def test_redundant_multi_record_edge_restored_with_all_records(client):
    _register(client, "L1", "L2", "L3")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L2", "after": "L3"},
        {"op": "add", "record_no": "R3", "before": "L1", "after": "L3"},
        {"op": "add", "record_no": "R4", "before": "L1", "after": "L3"},
    ])
    group = _group(client, "L1")
    assert all(
        (e["before"], e["after"]) != ("L1", "L3") for e in group["edges"]
    )
    # 撤回间接路径后，边补回且挂着全部直接支持记录
    result = _batch(client, [{"op": "retract", "record_no": "R2"}])
    edge = next(
        e
        for e in result["affected_groups"][0]["edges"]
        if (e["before"], e["after"]) == ("L1", "L3")
    )
    assert edge["record_numbers"] == ["R3", "R4"]


def test_concurrent_batches_on_disjoint_groups(client):
    _register(client, *[f"L{i}" for i in range(1, 7)])
    errors: list[Exception] = []

    def worker(records):
        try:
            _batch(client, [
                {"op": "add", "record_no": no, "before": b, "after": a}
                for no, b, a in records
            ])
        except Exception as exc:  # pragma: no cover - 仅用于失败时报告
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=([
        ("R1", "L1", "L2"), ("R2", "L2", "L3"),
    ],))
    t2 = threading.Thread(target=worker, args=([
        ("R3", "L4", "L5"), ("R4", "L5", "L6"),
    ],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    groups = client.get("/groups").json()
    assert len(groups) == 2
    active = {
        r["record_no"]
        for r in client.get("/records", params={"active_only": True}).json()
    }
    assert active == {"R1", "R2", "R3", "R4"}
