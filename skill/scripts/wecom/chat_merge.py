#!/usr/bin/env python3
"""把同一个会话的多次采集结果对齐并合并。

为什么需要它：客户端分页可能暂停，更早的历史需要分多次续采。单次运行内的记录标识
解决不了「跨次拼接」——两次采集的同一条消息
必须有稳定指纹才能认出来，否则合并出来的是重复或断裂的时间线。

对齐口径：
  * 从绝对时间、原始时间、发送者、完整正文、类型、链接和方向重算稳定键；
  * 用有序序列对齐，保留内容相同的合法重复消息；
  * 没有有序重叠 → 记为 gap：不能证明两次采集连续，仍要人工或再次续采确认。

用法：
    python3 chat_merge.py --previous c001.prev.jsonl --current c001.jsonl --output-prefix c001
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_export import FIELDS  # noqa: E402


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def order_key(record: dict):
    try:
        return (datetime.strptime(record["sent_at"], "%Y-%m-%d %H:%M:%S"),)
    except (KeyError, ValueError):
        return (datetime.max,)


def alignment_key(record: dict) -> str:
    """跨版本重算对齐键；不依赖旧文件里可能变化的 match_key 实现。"""
    payload = [
        record.get("sent_at"), record.get("sender_raw"), record.get("sender"),
        record.get("body_raw") or record.get("body"), record.get("message_type"),
        record.get("links") or [], record.get("is_outgoing"),
    ]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def unique_ids(records: list[dict]) -> list[dict]:
    """按最终有序序列分配 occurrence，保留内容完全相同的合法重复消息。"""
    counts: dict[str, int] = {}
    output = []
    for source in records:
        record = dict(source)
        key = alignment_key(record)
        occurrence = counts.get(key, 0)
        counts[key] = occurrence + 1
        record["match_key"] = key
        record["id"] = f"{key}#{occurrence}"
        output.append(record)
    return output


def merge(previous: list[dict], current: list[dict], *, key=None,
          prefer_previous: bool = False) -> tuple[list[dict], dict]:
    """key: alignment function (default alignment_key). prefer_previous keeps the earlier rendering of
    overlapping messages (used when linking renamed conversations, where old sender names may be unmasked)."""
    key = key or alignment_key
    previous = sorted(previous, key=order_key)
    current = sorted(current, key=order_key)
    prev_keys = [key(r) for r in previous]
    curr_keys = [key(r) for r in current]
    matcher = SequenceMatcher(None, prev_keys, curr_keys, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    overlap_count = sum(block.size for block in blocks)
    # 单条常用短语不能证明两次扫描连续；至少两条相邻消息组成锚点。
    anchored = any(block.size >= 2 and len(set(prev_keys[block.a:block.a + block.size])) >= 2
                   for block in blocks)

    # SequenceMatcher 按顺序对齐，既不会把同一天多条“收到”压成一条，也能保留任一侧独有的记录。
    merged: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for old, fresh in zip(previous[i1:i2], current[j1:j2]):
                item = dict(old if prefer_previous else fresh)
                if old.get("record_id"):
                    item["record_id"] = old["record_id"]
                merged.append(item)  # 默认新采集字段优先，持久消息身份沿用旧值
        elif tag == "delete":
            merged.extend(previous[i1:i2])
        elif tag == "insert":
            merged.extend(current[j1:j2])
        else:
            merged.extend(previous[i1:i2])
            merged.extend(current[j1:j2])
    if not anchored:
        merged = previous + current
    merged = unique_ids(sorted(merged, key=order_key))
    duplicates = len(previous) + len(current) - len(merged)
    stats = {
        "previous_records": len(previous),
        "current_records": len(current),
        "overlap_records": overlap_count,
        "new_from_current": max(0, len(current) - overlap_count),
        "only_in_previous": max(0, len(previous) - overlap_count),
        "merged_records": len(merged),
        "collapsed_duplicates": duplicates,
        # 交集为空说明两次采集没有可验证的接续点
        "alignment": "found" if anchored else "gap",
        "boundary_verified": all(r.get("boundary_verified") for r in merged) if merged else False,
        "warning": "" if anchored else "两次采集缺少至少两条相邻消息的可靠重叠，无法验证接续；合并结果需再次续采或人工确认",
    }
    return merged, stats


def write_outputs(records: list[dict], prefix: Path) -> tuple[Path, Path]:
    jsonl = prefix.with_suffix(".jsonl")
    csv_path = prefix.with_suffix(".csv")
    with jsonl.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["links"] = "\n".join(row.get("links") or [])
            for field in ("attachments", "unresolved_fields"):
                row[field] = json.dumps(row.get(field) or [], ensure_ascii=False)
            writer.writerow(row)
    return jsonl, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="合并同一会话的多次采集结果")
    parser.add_argument("--previous", required=True, help="上一次采集的 jsonl")
    parser.add_argument("--current", required=True, help="本次采集的 jsonl")
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--summary-output", help="合并报告 JSON；默认 <output-prefix>-merge.json")
    args = parser.parse_args()

    previous = load(Path(args.previous))
    current = load(Path(args.current))
    merged, stats = merge(previous, current)
    jsonl, csv_path = write_outputs(merged, Path(args.output_prefix))
    stats["jsonl"] = str(jsonl)
    stats["csv"] = str(csv_path)
    summary = Path(args.summary_output) if args.summary_output else Path(str(args.output_prefix) + "-merge.json")
    summary.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["alignment"] == "found" else 1


if __name__ == "__main__":
    raise SystemExit(main())
