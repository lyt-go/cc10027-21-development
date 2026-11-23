"""质量诊断演示数据：让历史诊断对照面板首次启动就有可核对的两份历史报告。

演示数据只写一次（以标记文件为准），并且刻意还原真实差异：

* 2026-09-01 的报告当时没有记录规则版本（缺版本），只有原始日志和规则快照；
* 2026-11-20 的报告使用同一套解析口径，并明确标注 ``v1.2``；
* 当前生效的解析配置已经更新，不是 ``v1.2``。因此既不能用当前配置回填旧报告，
  也不能把缺版本的旧报告直接算成当前口径；补证复算后应能复现原报告的计数。

原始日志与规则快照都固化在证据文件里，后续改动配置不影响它们。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.services import base as base_module

SOURCE_KEY = "LOG_-0003"
DEMO_MARKER = "quality_demo_seeded.json"

PARSER_PATTERN = (
    r"(?P<timestamp>\S+) (?P<host>\S+) \[(?P<source>\w+)\] "
    r"(?P<level>\w+) (?P<module>\S+) (?P<message>.+)"
)

# 早期报告的证据范围是原始日志第 1–6 行（时间更早，格式也旧一些），
# 其中第 3 行是采集时就夹带的坏行。
EARLY_LINES = [
    "2026-08-30T09:00:00 host-a [svc] INFO ingest msg=ingested_0 latency_ms=40",
    "2026-08-30T09:00:01 host-a [svc] INFO ingest msg=ingested_1 latency_ms=43",
    "bad line without expected fields",
    "2026-08-30T09:00:03 host-a [svc] INFO ingest msg=ingested_3 latency_ms=49",
    "2026-08-30T09:00:04 host-a [svc] INFO ingest msg=ingested_4 latency_ms=52",
    "2026-08-30T09:00:05 host-a [svc] INFO ingest msg=ingested_5 latency_ms=55",
]

# 后期报告的证据范围是原始日志第 201–206 行，可作为同一口径下的对照报告。
LATE_LINES = [
    f"2026-11-19T18:00:{i:02d} host-a [svc] INFO ingest msg=ingested_{i} latency_ms={35 + i * 2}"
    for i in range(6)
]


def _data_root() -> Path:
    return base_module.DATA_ROOT


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _relative(path: Path) -> str:
    root = _data_root()
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _digest(lines: list[str], start_line: int) -> str:
    content = "\n".join(f"{start_line + index}|{line}" for index, line in enumerate(lines))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _seed_report(
    service: Any,
    *,
    report_id: str,
    diagnosed_at: float,
    lines: list[str],
    start_line: int,
    rule_version: str | None,
    version_state: str,
    version_reason: str,
) -> dict[str, Any]:
    root = _data_root()
    evidence_id = f"QE_{SOURCE_KEY}_{report_id}"
    snapshot_file = root / "quality_evidence" / f"{evidence_id}.jsonl"
    if snapshot_file.exists():
        snapshot_file.unlink()
    snapshot_records = [
        {
            "source_key": SOURCE_KEY,
            "line_no": start_line + index,
            "offset": (start_line + index) * 64,
            "raw": line,
            "collected_at": diagnosed_at,
        }
        for index, line in enumerate(lines)
    ]
    _write_jsonl(snapshot_file, snapshot_records)

    evidence = {
        "evidence_id": evidence_id,
        "source_key": SOURCE_KEY,
        "raw_log_file": f"logs/{SOURCE_KEY}.jsonl",
        "snapshot_path": _relative(snapshot_file),
        "line_range": {
            "start": start_line,
            "end": start_line + len(lines) - 1,
            "count": len(lines),
        },
        "offset_range": {"start": start_line * 64, "end": (start_line + len(lines) - 1) * 64},
        "content_digest": _digest(lines, start_line),
        "record_count": len(lines),
        "complete": True,
        "captured_at": diagnosed_at,
    }
    meta_file = root / "quality_evidence" / f"{evidence_id}.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence["meta_path"] = _relative(meta_file)

    rule_snapshot = [{
        "规则编号": "PARSER-DEMO",
        "关联源": SOURCE_KEY,
        "正则表达式": PARSER_PATTERN,
        "字段提取": "命名分组",
        "时间格式": "ISO8601",
        "日志级别": "INFO",
        "多行合并": "否",
        "规则版本": rule_version or "未标注",
        "version_normalized": rule_version,
        "pattern_digest": service._compute_checksum(PARSER_PATTERN),
    }]

    # 用当时的规则对固化证据复算一次，得到与原报告一致的计数。
    parse_result = service._safe_import(
        "app.services.log_parser", "LogParserService"
    )().reparse_evidence(snapshot_records, rule_snapshot)
    counts = parse_result["counts"]
    success_rate = parse_result["parse_success_rate"]

    report = {
        "report_id": report_id,
        "source_key": SOURCE_KEY,
        "diagnosed_at": diagnosed_at,
        "parse_success_rate": success_rate,
        "parsed_ok": counts["parsed_ok"],
        "parse_errors": counts["parse_errors"],
        "field_completeness": {"overall": 100},
        "latency_distribution": {"count": counts["parsed_ok"], "avg": 47.0},
        "dirty_data_types": {
            "total": counts["parse_errors"],
            "categories": {"other": counts["parse_errors"]},
        },
        "total_records": counts["total"],
        "report_status": "已生成",
        "verified": True,
        "original_metrics": {
            "parse_success_rate": success_rate,
            "parsed_ok": counts["parsed_ok"],
            "parse_errors": counts["parse_errors"],
            "total_records": counts["total"],
            "field_completeness": 100,
            "diagnosed_at": diagnosed_at,
        },
        "rule_set": rule_snapshot,
        "effective_rule_set": rule_snapshot,
        "rule_version": rule_version,
        "version_state": version_state,
        "version_reason": version_reason,
        "applied_rule_codes": ["PARSER-DEMO"],
        "log_evidence": evidence,
        "demo": True,
    }
    _write_jsonl(root / "exports" / service.REPORT_FILE, [report])
    service._register_rule_snapshots(SOURCE_KEY, report_id, rule_snapshot)
    return report


def seed_quality_demo() -> None:
    """写入一次性演示报告；标记存在或已有演示报告时跳过。"""
    root = _data_root()
    marker = root / "exports" / DEMO_MARKER
    if marker.exists():
        return

    from app.services.log_quality import LogQualityService

    service = LogQualityService()
    existing = [
        report for report in service._load_reports()
        if str(report.get("report_id", "")).startswith("QL_DEMO_")
    ]
    if not existing:
        _seed_report(
            service,
            report_id="QL_DEMO_20260901",
            diagnosed_at=1788253200.0,
            lines=EARLY_LINES,
            start_line=1,
            rule_version=None,
            version_state="unknown",
            version_reason="历史规则表没有记录版本标记，不能按当前配置猜测",
        )
        _seed_report(
            service,
            report_id="QL_DEMO_20261120",
            diagnosed_at=1795197600.0,
            lines=LATE_LINES,
            start_line=201,
            rule_version="v1.2",
            version_state="known",
            version_reason="规则版本明确",
        )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"seeded_at": time.time()}, ensure_ascii=False), encoding="utf-8")
