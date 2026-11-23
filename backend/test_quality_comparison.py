"""历史质量诊断对照、证据定位、补证复算与可复查趋势的回归测试。

覆盖：
* 报告固化原始日志证据与规则快照，配置更新后旧报告仍可定位、原值可读；
* 缺版本报告不会被猜成当前配置，只能按另一份报告的明确版本补证复算；
* 复算中断留下「待核」明细，不进入已验证趋势点；
* 同一日志源的两份报告只有在口径一致且完成补证后才可比较。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.services.base as base_module
import app.services.log_quality as quality_module
from app.services.log_parser import LogParserService
from app.services.log_quality import LogQualityService
from app.store import store

RAW_PATTERN = (
    r"(?P<timestamp>\S+) (?P<host>\S+) \[(?P<source>\w+)\] "
    r"(?P<level>\w+) (?P<module>\S+) (?P<message>.+)"
)
SOURCE_KEY = "LOG_-0003"
PARSER_ID = 9901


@pytest.fixture()
def quality_env(tmp_path, monkeypatch):
    """隔离数据目录，并为 LOG_-0003 准备 5 行可解析原始日志和一条生效规则。"""
    monkeypatch.setattr(base_module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(quality_module, "DATA_ROOT", tmp_path)

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    lines = [
        f"2026-09-01T00:00:0{i} host1 [app] INFO mod msg=hello latency_ms={10 + i}"
        for i in range(5)
    ]
    records = [
        {"source_key": SOURCE_KEY, "line_no": i + 1, "offset": (i + 1) * 40,
         "raw": line, "collected_at": 1.0}
        for i, line in enumerate(lines)
    ]
    (logs_dir / f"{SOURCE_KEY}.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    parser_rows = store.rows("log_parser")
    parser_rows.append({
        "id": PARSER_ID,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
        "规则编号": "PARSER-QUALITY-TEST",
        "关联源": SOURCE_KEY,
        "正则表达式": RAW_PATTERN,
        "字段提取": "auto",
        "时间格式": "iso",
        "日志级别": "INFO",
        "多行合并": "否",
        "规则版本": "v1.2",
    })

    yield {"root": tmp_path, "service": LogQualityService(), "parser": parser_rows[-1]}

    parser_rows.remove(parser_rows[-1])


def _diagnose(env, parser_version: str) -> dict:
    env["parser"]["规则版本"] = parser_version
    LogParserService().parse_log_file(SOURCE_KEY)
    return env["service"].diagnose_source(SOURCE_KEY)


def test_report_freezes_evidence_and_survives_config_change(quality_env):
    service = quality_env["service"]
    report = _diagnose(quality_env, "v1.2")

    assert report["parse_success_rate"] == 100.0
    evidence = report["log_evidence"]
    assert evidence["complete"] is True
    assert evidence["line_range"] == {"start": 1, "end": 5, "count": 5}
    assert (quality_env["root"] / evidence["snapshot_path"]).exists()

    # 配置更新（规则版本变化）后，旧报告原值与证据仍然可查。
    quality_env["parser"]["规则版本"] = "v9.9"
    stored = service.get_quality_report(report["report_id"])
    assert stored["parse_success_rate"] == 100.0
    assert stored["rule_version"] == "v1.2"
    evidence_again = service.get_report_evidence(report["report_id"], include_lines=True)
    assert evidence_again["rule_version"] == "v1.2"
    assert len(evidence_again["lines"]) == 5


def test_unknown_version_is_not_guessed(quality_env):
    service = quality_env["service"]
    _diagnose(quality_env, "v1.2")
    unknown = _diagnose(quality_env, "未标注")

    assert unknown["rule_version"] is None
    assert unknown["version_state"] == "unknown"

    result = service.recompute_report(unknown["report_id"])
    assert result["status"] == "待核"
    assert result["verified"] is False
    assert result["recomputed_value"] is None
    assert result["rule_version"] is None

    trend = service.build_quality_trend(SOURCE_KEY)
    assert trend["status"] != "ok"
    # 缺版本报告不会进入趋势点，只有明确版本的原值可作为一个已验证点。
    assert [point["rule_version"] for point in trend["points"]] == ["v1.2"]
    assert trend["excluded"][0]["report_id"] == unknown["report_id"]


def test_known_and_unknown_compare_after_recompute(quality_env):
    service = quality_env["service"]
    known = _diagnose(quality_env, "v1.2")
    unknown = _diagnose(quality_env, "未知")

    prepared = service.prepare_comparison([known["report_id"], unknown["report_id"]])
    assert prepared["ok"] is True
    assert prepared["comparison_rule_version"] == "v1.2"
    assert prepared["requires_recompute"] == [unknown["report_id"]]

    detail = service.recompute_report(unknown["report_id"], peer_report_id=known["report_id"])
    assert detail["status"] == "已复算"
    assert detail["verified"] is True
    assert detail["recomputed_value"] == unknown["original_metrics"]["parse_success_rate"]
    assert detail["provenance"]["source_report_id"] == unknown["report_id"]
    assert detail["provenance"]["digest_verified"] is True
    assert detail["rule_set"][0]["规则编号"] == "PARSER-QUALITY-TEST"

    trend = service.build_quality_trend(SOURCE_KEY, rule_version="v1.2")
    assert trend["status"] == "ok"
    assert [point["value_origin"] for point in trend["points"]] == ["original", "recomputed"]
    assert trend["points"][1]["report_id"] == unknown["report_id"]

    # 原报告数字仍单独保留，不因复算被覆盖。
    original = service.get_quality_report(unknown["report_id"])
    assert original["original_metrics"]["parse_success_rate"] == 100.0
    assert original["rule_version"] is None


def test_interrupted_recompute_stays_pending(quality_env):
    service = quality_env["service"]
    known = _diagnose(quality_env, "v1.2")
    unknown = _diagnose(quality_env, "未标记")

    detail = service.recompute_report(
        unknown["report_id"], rule_version="v1.2", interrupt_after=2)
    assert detail["status"] == "待核"
    assert detail["interrupted"] is True
    assert detail["verified"] is False
    assert detail["recomputed_value"] is None
    assert detail["counts"]["processed"] == 2

    trend = service.build_quality_trend(SOURCE_KEY, rule_version="v1.2")
    point_ids = [point["report_id"] for point in trend["points"]]
    assert point_ids == [known["report_id"]]
    excluded = {item["report_id"]: item for item in trend["excluded"]}
    assert excluded[unknown["report_id"]]["pending_status"] == "待核"
    assert trend.get("status") != "ok"


def test_recompute_without_rule_snapshot_does_not_fall_back(quality_env):
    service = quality_env["service"]
    known = _diagnose(quality_env, "v1.2")
    unknown = _diagnose(quality_env, "未标注")

    # 当前配置仍是 v1.2，但请求的是历史中从未出现过的版本。
    detail = service.recompute_report(unknown["report_id"], rule_version="v3.0")
    assert detail["status"] == "待核"
    assert detail["verified"] is False
    assert "历史规则快照" in detail["reason"]
    assert service.build_quality_trend(SOURCE_KEY).get("status") != "ok"

    # 反向证明：可用版本能复算，且已知版本报告不需要复算。
    ok_detail = service.recompute_report(unknown["report_id"], peer_report_id=known["report_id"])
    assert ok_detail["verified"] is True


def test_reports_filter_by_rule_version_and_state(quality_env):
    service = quality_env["service"]
    known = _diagnose(quality_env, "v1.2")
    unknown = _diagnose(quality_env, "未标注")

    unknown_only = service.list_quality_reports(SOURCE_KEY, version_state="unknown")
    assert [item["report_id"] for item in unknown_only["items"]] == [unknown["report_id"]]

    known_only = service.list_quality_reports(SOURCE_KEY, rule_version="v1.2")
    assert [item["report_id"] for item in known_only["items"]] == [known["report_id"]]
    assert known_only["original_values"][0]["original_metrics"]["parse_success_rate"] == 100.0

    candidates = service.list_comparison_candidates(SOURCE_KEY)
    assert candidates["total"] == 1
    assert candidates["items"][0]["comparison_rule_version"] == "v1.2"
