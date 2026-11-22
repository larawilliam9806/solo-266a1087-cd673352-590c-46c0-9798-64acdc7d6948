"""FastAPI 入口：考古层位先后关系图供数接口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .schemas import (
    BatchRequest,
    BatchResponse,
    GroupOut,
    StrataRegisterRequest,
    StrataRegisterResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.ensure_initialized()
    yield


app = FastAPI(
    title="考古层位关系图服务",
    description=(
        "登记层位、批量补录/更正/撤回先后关系记录，"
        "并输出只保留不可间接推出连线的层位关系图。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ValidationFailure)
def _validation_handler(_request: Request, exc: service.ValidationFailure):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(service.CycleConflict)
def _cycle_handler(_request: Request, exc: service.CycleConflict):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "本批改动会形成闭环，已整体驳回，原有记录与关系图保持不变",
            "cycle": exc.cycle,
            "edges": exc.edges,
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/strata", response_model=StrataRegisterResponse, status_code=201)
def register_strata(payload: StrataRegisterRequest) -> StrataRegisterResponse:
    result = service.register_strata(payload.stratum_numbers)
    return StrataRegisterResponse(**result)


@app.get("/strata")
def get_strata() -> list[dict]:
    return service.list_strata()


@app.post("/records/batch", response_model=BatchResponse, status_code=201)
def apply_batch(payload: BatchRequest) -> BatchResponse:
    result = service.apply_batch(payload)
    return BatchResponse(**result)


@app.get("/records")
def get_records(
    active_only: bool = Query(default=False, description="仅返回未撤回的记录"),
) -> list[dict]:
    return service.list_records(active_only=active_only)


@app.get("/groups", response_model=list[GroupOut])
def get_groups() -> list[GroupOut]:
    return service.list_groups()


@app.get("/groups/by-stratum/{stratum_no}", response_model=GroupOut)
def get_group_by_stratum(stratum_no: str) -> GroupOut:
    # 未与任何记录相连的孤立层位 group_id 为 None，仍可正常返回
    result = service.get_group_for_stratum(stratum_no)
    return GroupOut(**result)
