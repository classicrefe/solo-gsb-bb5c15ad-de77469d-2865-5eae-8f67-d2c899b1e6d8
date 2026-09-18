"""FastAPI 入口：启动即建表。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query

from . import database, service
from .config import settings
from .schemas import (
    BatchIn,
    BatchResult,
    BatchSummary,
    MessageOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title=settings.app_title,
    version="1.0.0",
    description="批量提交邮件头信息，归并为稳定排序的会话树，诊断重复 ID / 缺失引用 / 引用环。",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchResult,
    status_code=201,
    tags=["batches"],
    summary="提交一批邮件并归并会话",
)
def submit_batch(payload: BatchIn) -> dict:
    return service.submit_batch(payload)


@app.get(
    "/batches",
    tags=["batches"],
    summary="列出批次（按提交时间倒序）",
)
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return service.list_batches(limit, offset)


@app.get(
    "/batches/{batch_id}",
    response_model=BatchResult,
    tags=["batches"],
    summary="按批次 ID 查询归档结果（线程安全的快照）",
)
def get_batch(batch_id: str) -> dict:
    result = service.get_batch(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result


@app.get(
    "/messages/{message_id}",
    response_model=MessageOut,
    tags=["messages"],
    summary="按 message_id 查询邮件及其最终父节点",
)
def get_message(message_id: str) -> dict:
    msg = service.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    return msg


@app.get(
    "/threads/{message_id}",
    tags=["threads"],
    summary="查询某邮件所属的完整会话树",
)
def get_thread(message_id: str) -> dict:
    result = service.get_thread(message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    if result.get("invalid"):
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    return result
