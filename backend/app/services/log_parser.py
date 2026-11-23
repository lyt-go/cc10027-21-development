"""日志解析：按规则解析原始日志行，提取结构化字段。"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class LogParserService(BaseModuleService):
    MODULE = "log_parser"
    ENTITY = "解析规则"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["规则编号", "关联源", "正则表达式", "字段提取", "时间格式", "日志级别", "多行合并", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "关联源", "正则表达式"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def parse_log_file(self, source_key: str) -> dict[str, Any]:
        """解析指定日志源已采集的日志文件。"""
        log_file = self._data_dir("logs") / f"{source_key}.jsonl"
        if not log_file.exists():
            return {"error": f"日志源 {source_key} 无已采集数据", "parsed": 0}

        parsers = [p for p in store.rows(self.MODULE) if p.get("status") == "已生效"]
        records = self._read_jsonl(log_file)

        parsed = []
        errors = []
        for rec in records:
            parsed_entry = self._apply_parsers(rec, parsers)
            if parsed_entry.get("parse_error"):
                errors.append(parsed_entry)
            else:
                parsed.append(parsed_entry)

        output_file = self._data_dir("logs") / f"{source_key}_parsed.jsonl"
        written = self._write_jsonl(output_file, parsed)

        if errors:
            self._write_jsonl(
                self._data_dir("logs") / f"{source_key}_parse_errors.jsonl",
                errors
            )

        self._write_checkpoint(f"{source_key}_parse", {
            "total": len(records),
            "parsed": written,
            "errors": len(errors),
            "completed_at": time.time(),
        })

        return {
            "source_key": source_key,
            "total": len(records),
            "parsed": written,
            "errors": len(errors),
            "output": str(output_file),
        }

    def reparse_evidence(
        self,
        records: list[dict[str, Any]],
        rule_set: list[dict[str, Any]],
        interrupt_after: int | None = None,
    ) -> dict[str, Any]:
        """用一份**确定的**历史规则集合重新解析给定原始日志记录。

        与 ``parse_log_file`` 的区别：
        * 不读取当前生效规则，只使用调用方传入的规则快照；
        * 输入是报告固化的原始日志范围，不读写当前日志文件；
        * ``interrupt_after`` 用于模拟/暴露复算中断，中断时不给出成功率。

        返回的计数口径与质量诊断一致（命中规则和默认兜底都算解析成功），
        因此同一份规则与同一段原始日志可以复现原报告的解析成功率。
        """
        total = len(records)
        parsed_ok = 0
        parse_errors = 0
        rule_matched = 0
        fallback_parsed = 0
        errors: list[dict[str, Any]] = []

        for index, rec in enumerate(records):
            if interrupt_after is not None and index >= interrupt_after:
                return {
                    "interrupted": True,
                    "rule_version": None,
                    "parse_success_rate": None,
                    "counts": {
                        "total": total,
                        "processed": index,
                        "parsed_ok": parsed_ok,
                        "parse_errors": parse_errors,
                        "rule_matched": rule_matched,
                        "fallback_parsed": fallback_parsed,
                    },
                    "message": f"复算在完成 {index}/{total} 行后中断",
                }

            result = self._apply_parsers(rec, rule_set)
            if result.get("parse_error"):
                parse_errors += 1
                errors.append({
                    "line_no": rec.get("line_no"),
                    "offset": rec.get("offset"),
                    "parse_error": result.get("parse_error"),
                })
            else:
                parsed_ok += 1
                if result.get("parser_rule"):
                    rule_matched += 1
                else:
                    fallback_parsed += 1

        return {
            "interrupted": False,
            "parse_success_rate": round(parsed_ok / max(total, 1) * 100, 2) if total else None,
            "counts": {
                "total": total,
                "processed": total,
                "parsed_ok": parsed_ok,
                "parse_errors": parse_errors,
                "rule_matched": rule_matched,
                "fallback_parsed": fallback_parsed,
            },
            "errors": errors[:50],
        }

    def _apply_parsers(self, rec: dict[str, Any], parsers: list[dict[str, Any]]) -> dict[str, Any]:
        raw = rec.get("raw", "")
        result = dict(rec)
        result["fields"] = {}
        result["parse_error"] = None

        if not parsers:
            # 无规则时使用默认解析
            m = re.match(r"(\S+) (\S+) \[(\w+)\] (\S+) (\S+) (.+)", raw)
            if m:
                result["fields"] = {
                    "timestamp": m.group(1),
                    "host": m.group(2),
                    "source": m.group(3),
                    "level": m.group(4),
                    "module": m.group(5),
                    "message": m.group(6),
                }
            else:
                result["parse_error"] = "默认正则未匹配"
            return result

        for parser in parsers:
            try:
                pattern = parser.get("正则表达式", "")
                if pattern and pattern not in ("", "日志解析样例1"):
                    m = re.search(pattern, raw)
                    if m:
                        result["fields"] = m.groupdict() if m.groupdict() else {str(i): g for i, g in enumerate(m.groups())}
                        result["parser_rule"] = parser["规则编号"]
                        return result
            except re.error:
                continue

        # 默认解析兜底
        result["fields"]["raw_message"] = raw
        result["fields"]["collected_at"] = rec.get("collected_at")
        return result

    def match_source_to_parser(self, source_id: int) -> list[dict[str, Any]]:
        """查找匹配指定日志源的解析规则。"""
        source_code = f"LOG_-{source_id:04d}"
        matches = []
        for p in store.rows(self.MODULE):
            if p.get("status") == "已生效" and (p.get("关联源") == source_code or p.get("关联源") == str(source_id)):
                matches.append(p)
        return matches