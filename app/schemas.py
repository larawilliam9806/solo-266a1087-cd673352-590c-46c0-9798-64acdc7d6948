"""请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------- 请求 ----------

class StrataRegisterRequest(BaseModel):
    stratum_numbers: list[str] = Field(min_length=1)


class AddOperation(BaseModel):
    op: Literal["add"]
    record_no: str
    before: str
    after: str


class CorrectOperation(BaseModel):
    op: Literal["correct"]
    record_no: str
    before: str
    after: str


class RetractOperation(BaseModel):
    op: Literal["retract"]
    record_no: str


class BatchRequest(BaseModel):
    operations: list[AddOperation | CorrectOperation | RetractOperation] = Field(
        min_length=1
    )


# ---------- 响应 ----------

class StrataRegisterResponse(BaseModel):
    registered: list[str]
    already_existed: list[str]


class RecordOut(BaseModel):
    record_no: str
    before: str
    after: str
    active: bool
    created_at: str
    updated_at: str
    retracted_at: str | None


class EdgeOut(BaseModel):
    before: str
    after: str
    record_numbers: list[str]


class GroupOut(BaseModel):
    # 孤立层位（尚未出现在任何关系中）不属于任何组，group_id 为 None
    group_id: int | None
    strata: list[str]
    edges: list[EdgeOut]


class BatchResponse(BaseModel):
    applied_operations: int
    affected_groups: list[GroupOut]
    untouched_groups: list[int]


class CycleEdge(BaseModel):
    before: str
    after: str
    record_numbers: list[str]


class CycleErrorResponse(BaseModel):
    detail: str
    cycle: list[str]
    edges: list[CycleEdge]
