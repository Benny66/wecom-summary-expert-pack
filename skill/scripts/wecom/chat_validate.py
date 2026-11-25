#!/usr/bin/env python3
"""验证聊天导出的结构、日期、去重和外部身份覆盖；不输出聊天正文。"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from chat_export import range_boundary_verified, reference_datetime

REQUIRED = {"id", "conversation", "sent_at", "message_type", "body", "sender", "sender_is_external", "sender_company"}
INCOMPLETE_STOP_REASONS = {
    "in-progress", "no-scroll-test", "top-or-no-change", "max-scrolls",
    "table-lost", "scroll-position-unavailable",
    "no-new-content", "alignment-gap", "conversation-changed",
}


def reached_history_top(raw: dict) -> bool:
    """记录面板顶部就是会话历史起点：滚到顶即覆盖完整，哪怕历史起点晚于窗口起点。

    新采集直接带 ``reached_top``；历史批次按滚动值为 0 推断（面板先停到最新端，
    所以值为 0 表示整段历史都在当前这一屏内）。
    """
    if raw.get("reached_top") is True:
        return True
    value = raw.get("vertical_scroll_value")
    return isinstance(value, (int, float)) and float(value) <= 0.001


def blocking_unresolved_count(raw: dict) -> int:
    """只有「有发送人、缺时间、且不是图片/媒体」的行才阻断边界判定。

    发送人缺失是内容告警；图片/媒体行和既无发送人也无时间的系统提示（撤回、入群、改群名等）
    在记录面板里本来就不带时间。这些行仍不进日期区间（excluded_without_date），只是不否定整段覆盖。
    始终按 records 重算：旧 raw 里的 ``blocking_unresolved`` 用的是更严的旧口径。
    """
    records = raw.get("records") or []
    count = 0
    for row in raw.get("unresolved_rows") or []:
        fields = row.get("fields") or []
        index = row.get("record_index")
        record = records[index] if isinstance(index, int) and 0 <= index < len(records) else {}
        if "time_text" not in fields or "sender" in fields or record.get("message_type") == "image_or_media":
            continue
        count += 1
    return count


MERGE_GAP_ROW_TOLERANCE = 3


def merge_gap_lost_rows(raw: dict) -> int:
    """对齐失败那一屏里，没有出现在最终记录中的行数。"""
    def signature(row: dict) -> tuple:
        return (str(row.get("sender_raw") or row.get("sender") or ""), str(row.get("time_text") or ""),
                str(row.get("body_raw") or row.get("body") or "")[:40])

    known = {signature(row) for row in raw.get("records") or []}
    return sum(1 for row in raw.get("unmerged_records") or [] if signature(row) not in known)


def top_boundary_ok(raw: dict) -> bool:
    """滚到记录面板顶部时，接缝处少量行丢失不否定整段覆盖。"""
    if not reached_history_top(raw):
        return False
    if int(raw.get("sequence_gaps") or 0) > 1:
        return False
    return merge_gap_lost_rows(raw) <= MERGE_GAP_ROW_TOLERANCE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl")
    parser.add_argument("--csv")
    parser.add_argument("--raw")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--summary-output")
    args = parser.parse_args()
    jsonl_path = Path(args.jsonl)
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    csv_path = Path(args.csv) if args.csv else jsonl_path.with_suffix(".csv")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    ids = [record.get("id") for record in records]
    invalid_dates = 0
    parsed = []
    for record in records:
        try:
            value = datetime.strptime(record["sent_at"], "%Y-%m-%d %H:%M:%S")
            parsed.append(value)
            if not start <= value.date() <= end:
                invalid_dates += 1
        except (KeyError, ValueError):
            invalid_dates += 1
    missing_required = sum(bool(REQUIRED - set(record)) for record in records)
    sample_indices = sorted(set(round(i * (len(records) - 1) / 29) for i in range(30))) if len(records) >= 30 else list(range(len(records)))
    sample_valid = all(not REQUIRED - set(records[index]) for index in sample_indices)
    raw_check = {"performed": False}
    if args.raw:
        raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
        top_reached = reached_history_top(raw)
        raw_check = {"performed": True, "stop_reason": raw.get("stop_reason"), "scroll_count": raw.get("scroll_count"),
                     "checked_through": reference_datetime(raw).isoformat(sep=" "),
                     "requested_end_observable": end <= reference_datetime(raw).date(),
                     "end_day_complete": end < reference_datetime(raw).date(),
                     "declared_records": raw.get("record_count"), "actual_records": len(raw.get("records") or []),
                     "count_match": raw.get("record_count") == len(raw.get("records") or []),
                     "sequence_gaps": int(raw.get("sequence_gaps") or 0),
                     "unresolved_rows": blocking_unresolved_count(raw),
                     "unmerged_records": len(raw.get("unmerged_records") or []),
                     "reached_top": top_reached,
                     "top_boundary_ok": top_boundary_ok(raw),
                     "requested_start_reached": range_boundary_verified(raw, start) or top_reached,
                     # 只有真正滚过起始日、跨到上一年或滚到记录面板顶部才算边界被验证；「滚不动了」不算
                     "boundary_verified": bool(raw.get("boundary_verified")) or top_reached}
    warnings = []
    missing_sender = sum(not record.get("sender") for record in records)
    empty_body = sum(not record.get("body") for record in records)
    external_without_company = sum(record.get("sender_is_external") is True and not record.get("sender_company") for record in records)
    if missing_sender: warnings.append("存在发送者缺失")
    if empty_body: warnings.append("存在空正文记录，需按消息类型抽查")
    if external_without_company: warnings.append("存在外部发送者缺少企业信息")
    content_incomplete = sum(record.get("content_incomplete") is True for record in records)
    if content_incomplete: warnings.append("存在未识别的内嵌表情或非文本内容；保留占位与原始值")
    if raw_check.get("stop_reason") in INCOMPLETE_STOP_REASONS:
        warnings.append("采集停止原因表明覆盖未完成")
    if raw_check.get("performed") and not raw_check.get("boundary_verified"):
        warnings.append("边界未验证：未滚到目标起始日也未跨年，不能当作已采全，需要续采")
    if raw_check.get("boundary_verified") and not raw_check.get("requested_start_reached"):
        warnings.append("原采集的边界通过，但原始记录未越过本次请求起点，不能沿用完整标记")
    if raw_check.get("sequence_gaps"):
        warnings.append("滚动快照之间存在无法对齐的序列缺口")
    chronological_order = parsed == sorted(parsed)
    structural_failed = bool(len(records) != len(csv_rows) or len(ids) - len(set(ids)) or invalid_dates or missing_required or not sample_valid or raw_check.get("count_match") is False)
    coverage_failures = []
    if not chronological_order:
        coverage_failures.append("messages_not_chronological")
    if raw_check.get("performed"):
        top_ok = bool(raw_check.get("top_boundary_ok"))
        if not raw_check.get("requested_end_observable"):
            coverage_failures.append("requested_end_after_capture")
        if not raw_check.get("boundary_verified"):
            coverage_failures.append("boundary_not_verified")
        elif not raw_check.get("requested_start_reached"):
            coverage_failures.append("requested_start_not_reached")
        if raw_check.get("stop_reason") in INCOMPLETE_STOP_REASONS and not top_ok:
            coverage_failures.append(f"incomplete_stop_reason:{raw_check.get('stop_reason')}")
        if raw_check.get("sequence_gaps") and not top_ok:
            coverage_failures.append(f"sequence_gaps:{raw_check.get('sequence_gaps')}")
        if raw_check.get("unresolved_rows"):
            coverage_failures.append(f"unresolved_rows:{raw_check['unresolved_rows']}")
        if raw_check.get("unmerged_records") and not top_ok:
            coverage_failures.append(f"unmerged_records:{raw_check['unmerged_records']}")
    coverage_complete = not coverage_failures if raw_check.get("performed") else None
    if structural_failed:
        status = "failed"
    elif coverage_complete is False:
        status = "coverage_incomplete"
    else:
        status = "validated_with_warnings" if warnings else "structure_validated"
    report = {
        "status": status,
        "records_jsonl": len(records),
        "records_csv": len(csv_rows),
        "count_match": len(records) == len(csv_rows),
        "duplicate_ids": len(ids) - len(set(ids)),
        "invalid_or_outside_dates": invalid_dates,
        "chronological_order": chronological_order,
        "missing_required_field_records": missing_required,
        "structural_sample_size": len(sample_indices),
        "structural_sample_valid": sample_valid,
        "missing_sender": missing_sender,
        "missing_sender_by_type": dict(Counter(record.get("message_type") for record in records if not record.get("sender"))),
        "empty_body": empty_body,
        "empty_body_by_type": dict(Counter(record.get("message_type") for record in records if not record.get("body"))),
        "external_senders": sum(record.get("sender_is_external") is True for record in records),
        "external_without_company": external_without_company,
        "content_incomplete_records": content_incomplete,
        "raw_validation": raw_check,
        "capture_boundary_verified": raw_check.get("boundary_verified"),
        "boundary_verified": raw_check.get("requested_start_reached"),
        "reached_history_top": raw_check.get("reached_top"),
        "top_boundary_ok": raw_check.get("top_boundary_ok"),
        "coverage_complete": coverage_complete,
        "coverage_failures": coverage_failures,
        "warnings": warnings,
        "manual_content_review_required": True,
        "note": "结构验证通过不代表界面采集达到归档级完整；需对照企业微信抽样正文、时间和发送者。",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.summary_output:
        Path(args.summary_output).write_text(text, encoding="utf-8")
    print(text)
    if structural_failed:
        return 2
    if coverage_complete is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
