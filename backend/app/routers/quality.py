"""质量诊断扩展接口：历史报告按规则版本筛选、对照准备、复算与可复查趋势。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import QualityComparisonPayload, QualityRecomputePayload
from app.services import service_for
from app.services.log_quality import LogQualityService

router = APIRouter(prefix="/api/log_quality", tags=["质量诊断对照"])


def _service() -> LogQualityService:
    service = service_for("log_quality")
    assert isinstance(service, LogQualityService)
    return service


@router.get("/reports")
def list_reports(
    source_key: str | None = Query(default=None),
    rule_version: str | None = Query(default=None),
    version_state: str | None = Query(default="all"),
    report_status: str | None = Query(default=None),
) -> dict[str, Any]:
    """按日志源、规则版本和版本状态查询历史诊断报告。"""
    return _service().list_quality_reports(
        source_key=source_key,
        rule_version=rule_version,
        version_state=version_state,
        report_status=report_status,
    )


@router.get("/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    """取回单份报告的原值；复算不会覆盖这里的原数字。"""
    report = _service().get_quality_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"诊断报告 {report_id} 不存在")
    return report


@router.get("/reports/{report_id}/evidence")
def get_report_evidence(
    report_id: str,
    include_lines: bool = Query(default=False),
) -> dict[str, Any]:
    """定位报告关联的原始日志：行范围、偏移、摘要与证据快照。"""
    evidence = _service().get_report_evidence(report_id, include_lines=include_lines)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"诊断报告 {report_id} 不存在")
    return evidence


@router.get("/comparison-candidates")
def comparison_candidates(
    source_key: str | None = Query(default=None),
    rule_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出可进入历史诊断对照筛选的报告组合。"""
    return _service().list_comparison_candidates(source_key=source_key, rule_version=rule_version)


@router.post("/comparison/prepare")
def prepare_comparison(payload: QualityComparisonPayload) -> dict[str, Any]:
    """核对两份被选中的报告：给出对照口径、可比结论与需要补证复算的报告。"""
    result = _service().prepare_comparison(payload.report_ids, payload.rule_version)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/reports/{report_id}/recompute")
def recompute_report(report_id: str, payload: QualityRecomputePayload) -> dict[str, Any]:
    """按确定的规则版本复算报告原始日志范围，写入有来源凭据的复算明细。"""
    return _service().recompute_report(
        report_id,
        rule_version=payload.rule_version,
        rule_code=payload.rule_code,
        peer_report_id=payload.peer_report_id,
        interrupt_after=payload.interrupt_after,
    )


@router.get("/recompute-details")
def recompute_details(
    report_id: str | None = Query(default=None),
    source_key: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询复算明细，包括中断后保留的待核记录。"""
    return _service().list_recompute_details(report_id=report_id, source_key=source_key)


@router.get("/trend")
def quality_trend(
    source_key: str = Query(...),
    rule_version: str | None = Query(default=None),
    report_ids: str | None = Query(default=None),
) -> dict[str, Any]:
    """生成可复查趋势：只使用可比结果和补证后的复算取数。"""
    selected = [item for item in (report_ids or "").split(",") if item.strip()]
    return _service().build_quality_trend(
        source_key,
        rule_version=rule_version,
        report_ids=selected or None,
    )
