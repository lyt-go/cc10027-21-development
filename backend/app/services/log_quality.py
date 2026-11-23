"""质量诊断：分析日志解析质量，生成完整性、成功率、延迟分布等诊断报告。

除了诊断本身，这里还负责历史诊断的**可复核对照**：

* 每份报告都会固化诊断时的规则快照、原始日志行范围（行号 / 偏移 / 内容摘要）
  以及证据快照文件，后续配置更新也不会影响原报告的定位与读取。
* 历史报告按规则版本查询、筛选；明确版本的报告可以直接比较。
* 缺版本标记的报告不会被猜成当前配置，只能使用「另一份报告明确给出的版本」
  对其原始日志范围重新解析（复算），并在复算明细里写入来源凭据。
* 复算中断时保留一条「待核」明细，不会进入已验证趋势点。
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService, DATA_ROOT
from app.store import store


VERSION_PATTERN = re.compile(r"^[vV]?\d+(?:\.\d+)*$")
UNKNOWN_VERSION_TOKENS = {
    "", "-", "--", "未知", "未标注", "未标记", "无", "无版本", "空",
    "n/a", "na", "none", "null", "unknown",
}


def normalize_rule_version(value: Any) -> str | None:
    """只把真正像版本号的标记视为已知版本，占位文本一律视为未标注。"""
    text = str(value or "").strip()
    if text.casefold() in UNKNOWN_VERSION_TOKENS:
        return None
    if not VERSION_PATTERN.match(text):
        return None
    return text


def _record_position(rec: dict[str, Any], fallback: int) -> Any:
    for key in ("line_no", "line_number", "offset"):
        value = rec.get(key)
        if value is not None and str(value) != "":
            return value
    return f"idx:{fallback}"


def _position_sort_key(value: Any) -> tuple[int, float]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, 0.0)


class LogQualityService(BaseModuleService):
    MODULE = "log_quality"
    ENTITY = "诊断报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "关联源", "解析成功率", "字段完整性", "延迟分布", "脏数据类型", "诊断时间", "报告状态"]
    REQUIRED_FIELDS = ["报告编号", "关联源", "解析成功率"]
    # 「待核」不是报告的流转状态，而是复算明细的状态（见 RECOMPUTE_FILE）。
    STATUS_ORDER = ["诊断中", "已生成", "已复核", "已归档"]
    ACTION_RULES = {"确认生成": "已生成", "标记复核": "已复核", "提交归档": "已归档"}
    NEGATIVE_ACTIONS = []

    REPORT_FILE = "quality_reports.jsonl"
    RULE_SNAPSHOT_FILE = "quality_rule_snapshots.jsonl"
    RECOMPUTE_FILE = "quality_recomputes.jsonl"

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def diagnose_source(self, source_key: str) -> dict[str, Any]:
        """对指定日志源进行质量诊断，并固化原始日志证据与规则快照。"""
        parsed_file = self._data_dir("logs") / f"{source_key}_parsed.jsonl"
        parse_error_file = self._data_dir("logs") / f"{source_key}_parse_errors.jsonl"
        log_file = self._data_dir("logs") / f"{source_key}.jsonl"

        if not parsed_file.exists() and not log_file.exists():
            return {"error": "无可用数据", "diagnosed": False, "source_key": source_key}

        raw_records = self._dedupe_records(self._read_jsonl(log_file), source_key) if log_file.exists() else []
        parsed_records = self._dedupe_records(self._read_jsonl(parsed_file), source_key) if parsed_file.exists() else []
        error_records = self._dedupe_records(self._read_jsonl(parse_error_file), source_key) if parse_error_file.exists() else []

        # 证据必须基于原始日志行；没有原始行时仍生成报告，但标记为不可复核。
        evidence = self._capture_evidence(source_key, raw_records)
        rule_set = self._capture_rule_set(source_key)
        applied_codes = {
            str(rec.get("parser_rule")).strip()
            for rec in parsed_records
            if rec.get("parser_rule")
        }
        effective_rules = [rule for rule in rule_set if rule["规则编号"] in applied_codes] or rule_set
        rule_version, version_state, version_reason = self._resolve_report_version(effective_rules, applied_codes)

        if raw_records:
            total = len(raw_records)
            raw_positions = {_record_position(rec, i) for i, rec in enumerate(raw_records)}
            parsed_ok = sum(
                1 for i, rec in enumerate(parsed_records)
                if _record_position(rec, i) in raw_positions
            )
            # 同一位置多条解析记录只算一次
            parsed_ok = min(parsed_ok, total)
            parse_errors = max(total - parsed_ok, 0)
        else:
            parsed_ok = len(parsed_records)
            parse_errors = len(error_records)
            total = parsed_ok + parse_errors

        success_rate = round(parsed_ok / max(total, 1) * 100, 2) if total > 0 else 100

        completeness = self._compute_completeness(parsed_records)
        latency_dist = self._compute_latency_distribution(parsed_records)
        dirty_types = self._classify_dirty_data(error_records)

        report_id = f"QL_{source_key}_{time.time_ns()}"
        original_metrics = {
            "parse_success_rate": success_rate,
            "parsed_ok": parsed_ok,
            "parse_errors": parse_errors,
            "total_records": total,
            "field_completeness": completeness.get("overall", 100),
            "diagnosed_at": time.time(),
        }

        report = {
            "report_id": report_id,
            "source_key": source_key,
            "diagnosed_at": original_metrics["diagnosed_at"],
            "parse_success_rate": success_rate,
            "parsed_ok": parsed_ok,
            "parse_errors": parse_errors,
            "field_completeness": completeness,
            "latency_distribution": latency_dist,
            "dirty_data_types": dirty_types,
            "total_records": total,
            # -- 可复核元数据 --
            "report_status": "已生成",
            "verified": True,
            "original_metrics": original_metrics,
            "rule_set": rule_set,
            "effective_rule_set": effective_rules,
            "rule_version": rule_version,
            "version_state": version_state,
            "version_reason": version_reason,
            "applied_rule_codes": sorted(applied_codes),
            "log_evidence": evidence,
        }

        self._write_jsonl(self._data_dir("exports") / self.REPORT_FILE, [report])
        self._register_rule_snapshots(source_key, report_id, rule_set)
        self._write_checkpoint(f"quality_{source_key}", report)

        # 诊断完成后自动触发告警评估
        if success_rate < 95 or parse_errors > 0:
            alert_r = self._trigger_alert_evaluation(source_key, {
                "parse_success_rate": success_rate,
                "field_completeness": completeness.get("overall", 100),
                "parse_errors": parse_errors,
                "source_key": source_key,
            })
            if "error" not in alert_r:
                report["auto_alert"] = alert_r

        return report

    # ------------------------------------------------------------------
    # 证据与规则快照
    # ------------------------------------------------------------------

    def _dedupe_records(self, records: list[dict[str, Any]], source_key: str) -> list[dict[str, Any]]:
        """按行号/偏移去重，保留最后一次写入；同一源外的记录不参与诊断。"""
        keyed: dict[Any, dict[str, Any]] = {}
        order: list[Any] = []
        for index, rec in enumerate(records):
            owner = rec.get("source_key")
            if owner not in (None, "", source_key):
                continue
            key = _record_position(rec, index)
            if key not in keyed:
                order.append(key)
            keyed[key] = rec
        order.sort(key=_position_sort_key)
        return [keyed[key] for key in order]

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(DATA_ROOT))
        except ValueError:
            return str(path)

    def _evidence_digest(self, records: list[dict[str, Any]]) -> str:
        lines = []
        for index, rec in enumerate(records):
            lines.append(f"{_record_position(rec, index)}|{rec.get('raw', '')}")
        return self._compute_checksum("\n".join(lines))

    def _capture_evidence(self, source_key: str, raw_records: list[dict[str, Any]]) -> dict[str, Any]:
        """把报告实际使用的原始日志范围固化成可核对证据。"""
        evidence_id = f"QE_{source_key}_{time.time_ns()}"
        captured_at = time.time()
        log_file = self._data_dir("logs") / f"{source_key}.jsonl"

        evidence: dict[str, Any] = {
            "evidence_id": evidence_id,
            "source_key": source_key,
            "raw_log_file": self._relative_path(log_file),
            "snapshot_path": None,
            "line_range": None,
            "offset_range": None,
            "content_digest": "",
            "record_count": len(raw_records),
            "complete": bool(raw_records),
            "captured_at": captured_at,
        }

        if not raw_records:
            evidence["reason"] = "原始日志文件不存在或没有可核对的行"
            return evidence

        snapshot_dir = self._data_dir("quality_evidence")
        snapshot_file = snapshot_dir / f"{evidence_id}.jsonl"
        snapshot_records = []
        for index, rec in enumerate(raw_records):
            snapshot_records.append({
                "source_key": rec.get("source_key", source_key),
                "line_no": rec.get("line_no"),
                "line_number": rec.get("line_number"),
                "offset": rec.get("offset"),
                "raw": rec.get("raw", ""),
                "collected_at": rec.get("collected_at"),
            })

        if snapshot_file.exists():
            snapshot_file.unlink()
        self._write_jsonl(snapshot_file, snapshot_records)

        line_values = [
            int(rec["line_no"]) for rec in raw_records
            if str(rec.get("line_no", "")).strip().lstrip("-").isdigit()
        ]
        offset_values = [
            int(rec["offset"]) for rec in raw_records
            if str(rec.get("offset", "")).strip().lstrip("-").isdigit()
        ]
        evidence["snapshot_path"] = self._relative_path(snapshot_file)
        evidence["content_digest"] = self._evidence_digest(raw_records)
        evidence["record_count"] = len(snapshot_records)
        if line_values:
            evidence["line_range"] = {
                "start": min(line_values),
                "end": max(line_values),
                "count": len(line_values),
            }
        if offset_values:
            evidence["offset_range"] = {
                "start": min(offset_values),
                "end": max(offset_values),
            }

        meta_file = snapshot_dir / f"{evidence_id}.json"
        meta_file.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        evidence["meta_path"] = self._relative_path(meta_file)
        return evidence

    def _parser_rows_for_source(self, source_key: str) -> list[dict[str, Any]]:
        source = next((row for row in store.rows("log_source") if row.get("源编号") == source_key), None)
        source_id = source.get("id") if source else None
        source_code = f"LOG_-{int(source_id):04d}" if source_id else None

        matches: list[dict[str, Any]] = []
        for parser in store.rows("log_parser"):
            if parser.get("status") != "已生效":
                continue
            related = str(parser.get("关联源") or "").strip()
            if not related:
                continue
            if related == source_key or related == source_code:
                matches.append(parser)
            elif source_key and source_key in related:
                matches.append(parser)
        return matches

    def _capture_rule_set(self, source_key: str) -> list[dict[str, Any]]:
        snapshots = []
        for parser in self._parser_rows_for_source(source_key):
            pattern = str(parser.get("正则表达式") or "")
            snapshots.append({
                "规则编号": parser.get("规则编号"),
                "关联源": parser.get("关联源"),
                "正则表达式": pattern,
                "字段提取": parser.get("字段提取"),
                "时间格式": parser.get("时间格式"),
                "日志级别": parser.get("日志级别"),
                "多行合并": parser.get("多行合并"),
                "规则版本": parser.get("规则版本"),
                "version_normalized": normalize_rule_version(parser.get("规则版本")),
                "pattern_digest": self._compute_checksum(pattern),
            })
        return snapshots

    def _resolve_report_version(
        self,
        effective_rules: list[dict[str, Any]],
        applied_codes: set[str],
    ) -> tuple[str | None, str, str]:
        if not effective_rules:
            return None, "unknown", "无生效解析规则快照，无法确定口径版本"
        versions = [rule.get("version_normalized") for rule in effective_rules]
        if any(version is None for version in versions):
            return None, "unknown", "规则快照中存在未标注版本的规则，不能按当前配置猜测"
        unique = sorted(set(versions))
        if len(unique) > 1:
            return None, "unknown", f"规则集合包含多个版本：{'、'.join(unique)}，无法确定单一口径"
        if not applied_codes:
            return unique[0], "known", "规则版本明确；诊断时未记录实际命中规则，按快照集合复算"
        return unique[0], "known", "规则版本明确"

    def _register_rule_snapshots(self, source_key: str, report_id: str, rule_set: list[dict[str, Any]]) -> None:
        captured_at = time.time()
        rows = []
        for rule in rule_set:
            rows.append({
                "snapshot_id": f"RS_{report_id}_{rule.get('规则编号')}",
                "report_id": report_id,
                "source_key": source_key,
                "captured_at": captured_at,
                "rule_version": rule.get("version_normalized"),
                "rule_version_raw": rule.get("规则版本"),
                "rule_code": rule.get("规则编号"),
                "pattern_digest": rule.get("pattern_digest"),
                "rule": rule,
            })
        if rows:
            self._write_jsonl(self._data_dir("exports") / self.RULE_SNAPSHOT_FILE, rows)

    # ------------------------------------------------------------------
    # 历史报告查询
    # ------------------------------------------------------------------

    def _load_reports(self) -> list[dict[str, Any]]:
        reports = self._read_jsonl(self._data_dir("exports") / self.REPORT_FILE)
        indexed: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for report in reports:
            report_id = str(report.get("report_id") or "")
            if not report_id:
                continue
            if report_id not in indexed:
                order.append(report_id)
            indexed[report_id] = report
        result = [indexed[report_id] for report_id in order]
        result.sort(key=lambda item: float(item.get("diagnosed_at") or 0))
        return result

    def _load_recomputes(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self._data_dir("exports") / self.RECOMPUTE_FILE)

    def _report_summary(self, report: dict[str, Any]) -> dict[str, Any]:
        evidence = report.get("log_evidence") or {}
        original = report.get("original_metrics") or {}
        return {
            "report_id": report.get("report_id"),
            "source_key": report.get("source_key"),
            "diagnosed_at": report.get("diagnosed_at"),
            "report_status": report.get("report_status", "已生成"),
            "verified": bool(report.get("verified", True)),
            "parse_success_rate": report.get("parse_success_rate"),
            "original_metrics": {
                "parse_success_rate": original.get("parse_success_rate", report.get("parse_success_rate")),
                "parsed_ok": original.get("parsed_ok", report.get("parsed_ok")),
                "parse_errors": original.get("parse_errors", report.get("parse_errors")),
                "total_records": original.get("total_records", report.get("total_records")),
                "field_completeness": original.get("field_completeness"),
            },
            "rule_version": report.get("rule_version"),
            "version_state": report.get("version_state", "unknown"),
            "version_reason": report.get("version_reason"),
            "applied_rule_codes": report.get("applied_rule_codes", []),
            "log_evidence": {
                "evidence_id": evidence.get("evidence_id"),
                "snapshot_path": evidence.get("snapshot_path"),
                "raw_log_file": evidence.get("raw_log_file"),
                "line_range": evidence.get("line_range"),
                "offset_range": evidence.get("offset_range"),
                "content_digest": evidence.get("content_digest"),
                "record_count": evidence.get("record_count", 0),
                "complete": bool(evidence.get("complete")),
            },
        }

    def list_quality_reports(
        self,
        source_key: str | None = None,
        rule_version: str | None = None,
        version_state: str | None = None,
        report_status: str | None = None,
    ) -> dict[str, Any]:
        """按源、规则版本、版本状态筛选历史诊断报告。

        ``version_state`` 取值：``known`` / ``unknown`` / ``all``。
        """
        reports = self._load_reports()
        if source_key:
            reports = [r for r in reports if r.get("source_key") == source_key]
        if rule_version:
            reports = [r for r in reports if r.get("rule_version") == rule_version]
        if version_state and version_state != "all":
            reports = [r for r in reports if r.get("version_state") == version_state]
        if report_status:
            reports = [r for r in reports if r.get("report_status") == report_status]

        summarized = [self._report_summary(report) for report in reports]
        # 运营查询历史报告时按诊断时间倒序，便于先看最近一次。
        summarized.reverse()
        original_values = [
            {
                "report_id": report["report_id"],
                "source_key": report["source_key"],
                "original_metrics": report["original_metrics"],
                "rule_version": report["rule_version"],
                "version_state": report["version_state"],
            }
            for report in summarized
        ]
        return {
            "items": summarized,
            "total": len(summarized),
            "original_values": original_values,
            "filters": {
                "source_key": source_key,
                "rule_version": rule_version,
                "version_state": version_state or "all",
                "report_status": report_status,
            },
        }

    def get_quality_report(self, report_id: str) -> dict[str, Any] | None:
        """按报告编号取回完整原报告（原数字始终保留，不被复算覆盖）。"""
        for report in reversed(self._load_reports()):
            if report.get("report_id") == report_id:
                return report
        return None

    def get_report_evidence(self, report_id: str, include_lines: bool = False) -> dict[str, Any] | None:
        report = self.get_quality_report(report_id)
        if report is None:
            return None
        evidence = dict(report.get("log_evidence") or {})
        snapshot_path = evidence.get("snapshot_path")
        if snapshot_path:
            path = DATA_ROOT / snapshot_path
            evidence["snapshot_exists"] = path.exists()
            if include_lines and path.exists():
                evidence["lines"] = self._read_jsonl(path)
        else:
            evidence["snapshot_exists"] = False
        evidence["report_id"] = report_id
        evidence["source_key"] = report.get("source_key")
        evidence["rule_version"] = report.get("rule_version")
        return evidence

    # ------------------------------------------------------------------
    # 对照筛选
    # ------------------------------------------------------------------

    def _comparison_rule_for_pair(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        requested_version: str | None = None,
    ) -> tuple[str | None, str]:
        left_version = left.get("rule_version")
        right_version = right.get("rule_version")
        if requested_version:
            normalized = normalize_rule_version(requested_version)
            if normalized is None:
                return None, "请求的规则版本不是可识别的明确版本"
            return normalized, "使用查询指定的明确规则版本"
        known = [version for version in (left_version, right_version) if version]
        if len(known) == 2 and known[0] == known[1]:
            return known[0], "两份报告均标注同一规则版本"
        if len(known) == 1:
            return known[0], "以报告明确标注的规则版本作为对照口径"
        if len(known) == 2:
            return None, f"两份报告规则版本不同（{'、'.join(known)}），不能直接比较，请指定版本"
        return None, "两份报告都没有可识别的规则版本，不能猜测当前配置"

    def _pair_candidate(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        requested_version: str | None = None,
    ) -> dict[str, Any]:
        version, reason = self._comparison_rule_for_pair(left, right, requested_version)
        left_summary = self._report_summary(left)
        right_summary = self._report_summary(right)
        needs_recompute: list[str] = []

        if version:
            for summary in (left_summary, right_summary):
                if summary["version_state"] != "known" or summary["rule_version"] != version:
                    needs_recompute.append(summary["report_id"])

        comparable = bool(version) and all(
            summary["log_evidence"]["complete"]
            for summary in (left_summary, right_summary)
        )

        return {
            "source_key": left.get("source_key"),
            "comparison_rule_version": version,
            "reason": reason,
            "comparable": comparable,
            "requires_recompute": needs_recompute,
            "reports": [left_summary, right_summary],
            "original_values": {
                left_summary["report_id"]: left_summary["original_metrics"],
                right_summary["report_id"]: right_summary["original_metrics"],
            },
        }

    def list_comparison_candidates(
        self,
        source_key: str | None = None,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """列出历史诊断中可进入对照筛选的报告组合。

        返回的每一项都带有原报告的日志证据定位信息和规则版本状态，
        运营可以据此选出一份明确版本 + 一份缺版本的报告。
        """
        reports = self._load_reports()
        if source_key:
            reports = [report for report in reports if report.get("source_key") == source_key]

        items: list[dict[str, Any]] = []
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for report in reports:
            by_source[str(report.get("source_key"))].append(report)

        for key, source_reports in by_source.items():
            for i in range(len(source_reports)):
                for j in range(i + 1, len(source_reports)):
                    candidate = self._pair_candidate(
                        source_reports[i], source_reports[j], rule_version)
                    if rule_version:
                        normalized = normalize_rule_version(rule_version)
                        candidate_versions = {
                            candidate["comparison_rule_version"],
                            source_reports[i].get("rule_version"),
                            source_reports[j].get("rule_version"),
                        }
                        if normalized not in candidate_versions:
                            continue
                    candidate["source_key"] = key
                    items.append(candidate)

        items.sort(
            key=lambda item: max(
                float(report.get("diagnosed_at") or 0)
                for report in item["reports"]
            ),
            reverse=True,
        )
        return {
            "items": items,
            "total": len(items),
            "filters": {"source_key": source_key, "rule_version": rule_version},
        }

    def prepare_comparison(
        self,
        report_ids: list[str],
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """校验两份被选中的历史报告，返回对照口径和复算要求。"""
        if len(report_ids) != 2:
            return {"ok": False, "message": "请选择两份历史诊断报告进行对照"}
        reports = [self.get_quality_report(report_id) for report_id in report_ids]
        if any(report is None for report in reports):
            missing = [rid for rid, report in zip(report_ids, reports) if report is None]
            return {"ok": False, "message": f"报告不存在：{'、'.join(missing)}"}
        left, right = reports  # type: ignore[misc]
        if left.get("source_key") != right.get("source_key"):
            return {"ok": False, "message": "两份报告不属于同一日志源，不能对照"}

        candidate = self._pair_candidate(left, right, rule_version)
        candidate["ok"] = True
        candidate["message"] = candidate["reason"]
        return candidate

    # ------------------------------------------------------------------
    # 复算
    # ------------------------------------------------------------------

    def _rule_snapshots_for_version(
        self,
        version: str,
        preferred_report: dict[str, Any] | None = None,
        rule_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """只从历史报告/历史快照里取确定版本的规则，不回退到当前配置。"""
        if preferred_report is not None:
            rules = preferred_report.get("effective_rule_set") or preferred_report.get("rule_set") or []
            matched = [
                rule for rule in rules
                if rule.get("version_normalized") == version
                and (not rule_code or rule.get("规则编号") == rule_code)
            ]
            if matched:
                return matched

        records = self._read_jsonl(self._data_dir("exports") / self.RULE_SNAPSHOT_FILE)
        matched_records = [
            record for record in records
            if record.get("rule_version") == version
            and (not rule_code or record.get("rule_code") == rule_code)
        ]
        if not matched_records:
            return []
        matched_records.sort(key=lambda item: float(item.get("captured_at") or 0), reverse=True)
        latest_report_id = matched_records[0].get("report_id")
        return [
            record["rule"]
            for record in matched_records
            if record.get("report_id") == latest_report_id
        ]

    def _latest_recompute(
        self,
        report_id: str,
        rule_version: str | None = None,
        verified_only: bool = False,
    ) -> dict[str, Any] | None:
        details = [
            detail for detail in self._load_recomputes()
            if detail.get("report_id") == report_id
            and (rule_version is None or detail.get("rule_version") == rule_version)
            and (not verified_only or detail.get("verified"))
        ]
        details.sort(key=lambda item: float(item.get("created_at") or 0))
        return details[-1] if details else None

    def _pending_detail(
        self,
        report: dict[str, Any],
        reason: str,
        rule_version: str | None,
        evidence: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        detail = {
            "detail_id": f"QR_{report.get('report_id')}_{time.time_ns()}",
            "report_id": report.get("report_id"),
            "source_key": report.get("source_key"),
            "status": "待核",
            "verified": False,
            "interrupted": False,
            "reason": reason,
            "rule_version": rule_version,
            "rule_code": None,
            "rule_set": [],
            "original_value": (report.get("original_metrics") or {}).get(
                "parse_success_rate", report.get("parse_success_rate")),
            "recomputed_value": None,
            "counts": {},
            "evidence": evidence,
            "provenance": self._provenance(report, evidence, None),
            "created_at": time.time(),
        }
        if extra:
            detail.update(extra)
        self._write_jsonl(self._data_dir("exports") / self.RECOMPUTE_FILE, [detail])
        return detail

    def _provenance(
        self,
        report: dict[str, Any],
        evidence: dict[str, Any],
        rule_set: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        return {
            "source_report_id": report.get("report_id"),
            "source_key": report.get("source_key"),
            "evidence_id": evidence.get("evidence_id"),
            "snapshot_path": evidence.get("snapshot_path"),
            "raw_log_file": evidence.get("raw_log_file"),
            "line_range": evidence.get("line_range"),
            "offset_range": evidence.get("offset_range"),
            "content_digest": evidence.get("content_digest"),
            "digest_verified": evidence.get("digest_verified", False),
            "rule_snapshot_digest": self._compute_checksum(
                "|".join(
                    f"{rule.get('规则编号')}:{rule.get('pattern_digest')}"
                    for rule in (rule_set or [])
                )
            ) if rule_set else None,
            "rule_codes": [rule.get("规则编号") for rule in (rule_set or [])],
        }

    def _read_evidence_records(self, evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        snapshot_path = evidence.get("snapshot_path")
        if not snapshot_path:
            return [], "报告没有原始日志证据快照"
        path = DATA_ROOT / snapshot_path
        if not path.exists():
            return [], f"原始日志证据快照不存在：{snapshot_path}"
        records = self._read_jsonl(path)
        if not records:
            return [], "原始日志证据快照为空"
        expected = evidence.get("content_digest")
        actual = self._evidence_digest(records)
        if expected and expected != actual:
            return records, f"原始日志证据摘要不一致（期望 {expected}，实际 {actual}）"
        return records, None

    def recompute_report(
        self,
        report_id: str,
        rule_version: str | None = None,
        rule_code: str | None = None,
        peer_report_id: str | None = None,
        interrupt_after: int | None = None,
    ) -> dict[str, Any]:
        """按确定的规则版本对报告原始日志范围重新解析，并写入复算明细。

        缺版本报告必须提供明确版本（或通过 ``peer_report_id`` 引用一份明确
        版本的报告）；没有确定规则时只保留「待核」明细，绝不使用当前配置兜底。
        """
        report = self.get_quality_report(report_id)
        if report is None:
            return {"ok": False, "status": "不存在", "message": f"报告 {report_id} 不存在"}

        evidence = dict(report.get("log_evidence") or {})
        own_version = report.get("rule_version")
        peer = self.get_quality_report(peer_report_id) if peer_report_id else None

        if rule_version:
            version = normalize_rule_version(rule_version)
            if version is None:
                return self._pending_detail(
                    report, f"指定的规则版本不是可识别版本：{rule_version}", None, evidence)
        elif own_version:
            version = own_version
        elif peer is not None and peer.get("rule_version"):
            version = peer.get("rule_version")
        else:
            return self._pending_detail(
                report,
                "报告缺少规则版本标记，且未提供其他明确版本的报告作为补证依据；不能按当前配置猜测",
                None,
                evidence,
            )

        preferred = report if own_version == version else peer
        rule_set = self._rule_snapshots_for_version(version, preferred, rule_code)
        if not rule_set:
            return self._pending_detail(
                report,
                f"没有找到版本 {version} 的历史规则快照，不能使用当前配置替代",
                version,
                evidence,
            )
        if rule_code and not any(rule.get("规则编号") == rule_code for rule in rule_set):
            return self._pending_detail(
                report, f"版本 {version} 下没有规则 {rule_code}", version, evidence)

        records, evidence_error = self._read_evidence_records(evidence)
        if evidence_error:
            evidence = dict(evidence)
            evidence["digest_verified"] = False
            return self._pending_detail(report, evidence_error, version, evidence)
        evidence = dict(evidence)
        evidence["digest_verified"] = True

        if interrupt_after is not None:
            try:
                interrupt_after = int(interrupt_after)
            except (TypeError, ValueError):
                return self._pending_detail(
                    report, "复算中断位置不是有效整数", version, evidence,
                    {"interrupted": True})

        try:
            parse_result = self._safe_import(
                "app.services.log_parser", "LogParserService"
            )().reparse_evidence(records, rule_set, interrupt_after=interrupt_after)
        except Exception as error:  # 复算中断也要留下待核报告
            return self._pending_detail(
                report,
                f"复算中断：{error}",
                version,
                evidence,
                {"interrupted": True, "error": str(error)},
            )

        if parse_result.get("interrupted"):
            detail = self._pending_detail(
                report,
                "复算中断，保留待核明细，不计入已验证趋势点",
                version,
                evidence,
                {
                    "interrupted": True,
                    "rule_set": rule_set,
                    "counts": parse_result.get("counts", {}),
                    "recomputed_value": None,
                    "rule_code": rule_code,
                },
            )
            return detail

        computed = parse_result.get("parse_success_rate")
        detail = {
            "detail_id": f"QR_{report_id}_{time.time_ns()}",
            "report_id": report_id,
            "source_key": report.get("source_key"),
            "status": "已复算",
            "verified": True,
            "interrupted": False,
            "reason": "按历史规则快照与原始日志证据范围复算完成",
            "rule_version": version,
            "rule_code": rule_code,
            "rule_set": rule_set,
            "original_value": (report.get("original_metrics") or {}).get(
                "parse_success_rate", report.get("parse_success_rate")),
            "recomputed_value": computed,
            "counts": parse_result.get("counts", {}),
            "evidence": evidence,
            "provenance": self._provenance(report, evidence, rule_set),
            "created_at": time.time(),
        }
        self._write_jsonl(self._data_dir("exports") / self.RECOMPUTE_FILE, [detail])
        return {"ok": True, **detail}

    def list_recompute_details(
        self,
        report_id: str | None = None,
        source_key: str | None = None,
    ) -> dict[str, Any]:
        details = self._load_recomputes()
        if report_id:
            details = [detail for detail in details if detail.get("report_id") == report_id]
        if source_key:
            details = [detail for detail in details if detail.get("source_key") == source_key]
        details.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return {"items": details, "total": len(details)}

    # ------------------------------------------------------------------
    # 可复查趋势
    # ------------------------------------------------------------------

    def build_quality_trend(
        self,
        source_key: str,
        rule_version: str | None = None,
        report_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """生成可复查趋势：只用可比结果和已完成的复算取数。

        * 明确版本的原报告：原值直接作为已验证点。
        * 缺版本报告：必须存在针对目标口径的已完成复算，才用复算值入点；
          复算中断/缺失时只保留原值可读，并列入 ``excluded``。
        """
        reports = [report for report in self._load_reports() if report.get("source_key") == source_key]
        if report_ids:
            wanted = set(report_ids)
            reports = [report for report in reports if report.get("report_id") in wanted]

        points: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        original_values: list[dict[str, Any]] = []

        for report in reports:
            summary = self._report_summary(report)
            original_values.append({
                "report_id": summary["report_id"],
                "parse_success_rate": summary["original_metrics"]["parse_success_rate"],
                "original_metrics": summary["original_metrics"],
                "rule_version": summary["rule_version"],
                "version_state": summary["version_state"],
                "log_evidence": summary["log_evidence"],
            })

            if (
                summary["version_state"] == "known"
                and (rule_version is None or summary["rule_version"] == rule_version)
                and summary["log_evidence"]["complete"]
            ):
                points.append({
                    "report_id": summary["report_id"],
                    "source_key": source_key,
                    "rule_version": summary["rule_version"],
                    "value": summary["original_metrics"]["parse_success_rate"],
                    "value_origin": "original",
                    "verified": True,
                    "original_value": summary["original_metrics"]["parse_success_rate"],
                    "diagnosed_at": summary["diagnosed_at"],
                    "evidence": summary["log_evidence"],
                    "provenance": {
                        "source_report_id": summary["report_id"],
                        "evidence_id": summary["log_evidence"].get("evidence_id"),
                        "content_digest": summary["log_evidence"].get("content_digest"),
                    },
                })
                continue

            target_version = rule_version or summary["rule_version"]
            latest_detail = self._latest_recompute(summary["report_id"], target_version)
            if latest_detail and latest_detail.get("verified"):
                points.append({
                    "report_id": summary["report_id"],
                    "source_key": source_key,
                    "rule_version": latest_detail.get("rule_version"),
                    "value": latest_detail.get("recomputed_value"),
                    "value_origin": "recomputed",
                    "verified": True,
                    "original_value": summary["original_metrics"]["parse_success_rate"],
                    "recomputed_value": latest_detail.get("recomputed_value"),
                    "detail_id": latest_detail.get("detail_id"),
                    "diagnosed_at": summary["diagnosed_at"],
                    "evidence": latest_detail.get("evidence") or summary["log_evidence"],
                    "provenance": latest_detail.get("provenance") or {},
                })
            else:
                excluded.append({
                    "report_id": summary["report_id"],
                    "source_key": source_key,
                    "original_value": summary["original_metrics"]["parse_success_rate"],
                    "rule_version": summary["rule_version"],
                    "version_state": summary["version_state"],
                    "reason": (
                        latest_detail.get("reason") if latest_detail
                        else (
                            "报告已标注规则版本，但没有可核对的原始日志范围，不能作为已验证趋势点"
                            if summary["version_state"] == "known" and not summary["log_evidence"]["complete"]
                            else "缺版本报告尚未完成补证复算，原值可读但不构成已验证趋势点"
                        )
                    ),
                    "pending_detail_id": latest_detail.get("detail_id") if latest_detail else None,
                    "pending_status": latest_detail.get("status") if latest_detail else None,
                    "evidence": summary["log_evidence"],
                })

        points.sort(key=lambda item: float(item.get("diagnosed_at") or 0))
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in points:
            groups[str(point.get("rule_version"))].append(point)

        if rule_version:
            group_version = normalize_rule_version(rule_version) or rule_version
            selected = groups.get(group_version, [])
        elif groups:
            # 默认选取最新趋势点所在的版本组，绝不跨版本拼趋势。
            latest_point = max(points, key=lambda item: float(item.get("diagnosed_at") or 0))
            group_version = str(latest_point.get("rule_version"))
            selected = groups[group_version]
        else:
            group_version = None
            selected = []

        result: dict[str, Any] = {
            "source_key": source_key,
            "rule_version": group_version,
            "points": selected,
            "excluded": excluded,
            "original_values": original_values,
            "groups": {version: len(items) for version, items in groups.items()},
            "all_points": points,
        }

        if len(selected) < 2:
            result["status"] = "需要至少两个已验证且口径一致的趋势点才能比较"
            if excluded:
                result["status"] += "（存在待核报告，已保留原值但未计入趋势）"
            return result

        latest, previous = selected[-1], selected[-2]
        latest_value = float(latest.get("value") or 0)
        previous_value = float(previous.get("value") or 0)
        result.update({
            "status": "ok",
            "latest_success_rate": latest_value,
            "previous_success_rate": previous_value,
            "trend": (
                "up" if latest_value > previous_value
                else "down" if latest_value < previous_value
                else "stable"
            ),
            "delta": round(latest_value - previous_value, 2),
        })
        return result

    def compare_quality_trend(
        self,
        source_key: str,
        rule_version: str | None = None,
        report_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """兼容原趋势入口；支持按规则版本和指定报告生成可复查趋势。"""
        trend = self.build_quality_trend(source_key, rule_version, report_ids)
        if trend.get("status") != "ok":
            trend.setdefault("latest_success_rate", None)
            trend.setdefault("previous_success_rate", None)
            trend.setdefault("trend", "insufficient")
            trend.setdefault("delta", None)
        return trend

    # ------------------------------------------------------------------
    # 诊断计算
    # ------------------------------------------------------------------

    def _compute_completeness(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        if not records:
            return {"rate": 100, "total": 0, "description": "无记录"}

        field_counts = defaultdict(lambda: {"present": 0, "total": 0})
        for rec in records:
            fields = rec.get("fields", rec)
            for k, v in fields.items():
                field_counts[k]["total"] += 1
                if v is not None and v != "":
                    field_counts[k]["present"] += 1

        result = {"fields": {}}
        for field, counts in field_counts.items():
            rate = round(counts["present"] / max(counts["total"], 1) * 100, 2)
            result["fields"][field] = rate

        rates = [v for v in result["fields"].values()]
        result["overall"] = round(sum(rates) / max(len(rates), 1), 2) if rates else 100
        return result

    def _compute_latency_distribution(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        latencies = []
        for rec in records:
            fields = rec.get("fields", rec)
            for k in ("latency_ms", "latency", "duration_ms"):
                val = fields.get(k)
                if val is not None:
                    try:
                        latencies.append(float(val))
                    except (ValueError, TypeError):
                        pass

        if not latencies:
            return {"count": 0, "description": "无延迟数据"}

        return {
            "count": len(latencies),
            "min": round(min(latencies), 2),
            "max": round(max(latencies), 2),
            "avg": round(statistics.mean(latencies), 2),
            "p50": round(statistics.median(latencies), 2),
            "p95": round(self._percentile(latencies, 95), 2),
            "p99": round(self._percentile(latencies, 99), 2),
        }

    def _percentile(self, data: list[float], percentile: float) -> float:
        if not data:
            return 0
        sorted_data = sorted(data)
        index = (percentile / 100.0) * (len(sorted_data) - 1)
        lower = int(math.floor(index))
        upper = int(math.ceil(index))
        if lower == upper:
            return sorted_data[lower]
        weight = index - lower
        return sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight

    def _classify_dirty_data(self, error_records: list[dict[str, Any]]) -> dict[str, Any]:
        types = defaultdict(int)
        for rec in error_records:
            error_msg = rec.get("parse_error", "unknown")
            if "正则" in str(error_msg):
                types["regex_mismatch"] += 1
            elif "格式" in str(error_msg):
                types["format_error"] += 1
            elif "字段" in str(error_msg):
                types["missing_field"] += 1
            else:
                types["other"] += 1
        return {"total": len(error_records), "categories": dict(types)}
