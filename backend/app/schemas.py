"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class QualityComparisonPayload(BaseModel):
    """历史诊断对照：被选中的两份报告编号与可选的目标规则版本。"""

    report_ids: list[str] = Field(min_length=2, max_length=2)
    rule_version: str | None = None


class QualityRecomputePayload(BaseModel):
    """复算入参：明确规则版本、可选规则编号、补证报告和中断位置。"""

    rule_version: str | None = None
    rule_code: str | None = None
    peer_report_id: str | None = None
    interrupt_after: int | None = Field(default=None, ge=0)
