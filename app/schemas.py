"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MessageIn(BaseModel):
    message_id: str = Field(..., min_length=1, description="RFC 2822 Message-ID")
    in_reply_to: Optional[str] = Field(None, description="直接回复的邮件 ID")
    references: list[str] = Field(default_factory=list, description="引用链，按邮件头顺序")
    sent_at: datetime = Field(..., description="发送时间，无时区时按 UTC 处理")
    subject: str

    @field_validator("message_id", "in_reply_to", "subject")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("references")
    @classmethod
    def _strip_refs(cls, v: list[str]) -> list[str]:
        return [r.strip() for r in v if r and r.strip()]

    def normalized(self) -> "MessageIn":
        """把时间统一换算到 UTC（naive datetime 视为 UTC）。"""

        dt = self.sent_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return self.model_copy(update={"sent_at": dt})


class BatchIn(BaseModel):
    messages: list[MessageIn] = Field(..., min_length=1)


class TreeNode(BaseModel):
    message_id: str
    parent_id: Optional[str]
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    children: list["TreeNode"] = Field(default_factory=list)


class DuplicateOut(BaseModel):
    message_id: str
    archived: bool = Field(..., description="True 表示该 ID 已在此前批次归档")
    first_submitted_seq: Optional[int]
    occurrences: list[int]


class MissingRefsOut(BaseModel):
    message_id: str
    missing: list[str]


class CycleOut(BaseModel):
    index: int
    members: list[str]
    invalid_messages: list[str]


class InvalidMessageOut(BaseModel):
    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    reason: str
    cycle_index: int


class BatchSummary(BaseModel):
    batch_id: str
    submitted_at: datetime
    total_count: int
    unique_count: int
    invalid_count: int
    tree_count: int


class BatchResult(BaseModel):
    batch_id: str
    submitted_at: datetime
    totals: dict
    threads: list[TreeNode]
    duplicates: list[DuplicateOut]
    missing_references: list[MissingRefsOut]
    cycles: list[CycleOut]
    invalid_messages: list[InvalidMessageOut]


class MessageOut(BaseModel):
    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    parent_id: Optional[str]
    is_invalid: bool
    batch_id: str


TreeNode.model_rebuild()
