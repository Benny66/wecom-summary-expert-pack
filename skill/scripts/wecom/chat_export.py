#!/usr/bin/env python3
"""把 Accessibility 原始聊天数据推断为绝对时间，按任意闭区间过滤并导出。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fs_utils import ensure_dir
from typing import Any

DATE = "%Y-%m-%d"
DATE_PARSER_VERSION = 2
# 企微历史面板的 YY/M/D 显式表示 2000—2099 年，不能依赖 %y 的 1969/2068 分界。
EXPLICIT_DATE = re.compile(r"^(?:(19\d{2}|20\d{2})[年/\-]|(\d{2})/)(\d{1,2})[月/\-](\d{1,2})日?(?=$|\s)")
# 后缀字段（day/hour/weekday/is_outgoing/at_me/sender_raw）是为「并入库／日报周报」准备的派生字段：
# 现有 WorkReports daily/*.data.json 需要 conversations[].{type,members,sent,received,at_me,first,last}
# 与 metrics.{sent,received,at_me,hourly_sent}，这些都能由下面这些列直接聚合出来，
# 不必为了周期报告再重采一遍。
FIELDS = [
    "id", "record_id", "conversation_key", "conversation", "conversation_type", "conversation_is_external_group",
    "conversation_external_member_count", "conversation_member_count",
    "sender", "sender_raw", "sender_is_external", "sender_company", "is_outgoing",
    "sent_at", "time_precision", "time_inferred", "day", "hour", "weekday", "time_text", "year_inferred", "at_me",
    "message_type", "body", "links", "source", "captured_at", "stop_reason",
    "boundary_verified", "match_key",
    "body_raw", "attachments", "content_incomplete", "unresolved_fields", "time_source", "direction_evidence",
]

# 单聊界面不渲染发送者名，只能按头像位置判定方向；自己发出的统一标记为「我」。
SELF_LABEL = "我"


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, DATE).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def reference_datetime(capture: dict) -> datetime:
    try:
        value = datetime.fromisoformat(capture["captured_at"].replace("Z", "+00:00"))
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    except (KeyError, ValueError):
        return datetime.now()


def parse_time_text(text: str | None, reference: datetime) -> tuple[datetime | None, int | None, int | None]:
    if not text:
        return None, None, None
    text = text.strip()
    clock = re.search(r"(\d{1,2}):(\d{2})", text)
    hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (0, 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None, None
    explicit = EXPLICIT_DATE.match(text)
    if explicit:
        long_year, short_year, month, day = explicit.groups()
        year = int(long_year) if long_year else 2000 + int(short_year)
        month, day = int(month), int(day)
        try:
            return datetime(year, month, day, hour, minute), month * 32 + day, year
        except ValueError:
            return None, None, None
    # 侧栏与部分版本的消息区写「1/5 09:30」（月/日），没有「月」「日」字样；
    # 漏掉它今年内的消息会整条被当成无日期丢弃。
    slash_month_day = re.match(r"(\d{1,2})/(\d{1,2})(?=$|\s)", text)
    if slash_month_day:
        month, day = map(int, slash_month_day.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return None, month * 32 + day, None
    month_day = re.match(r"(\d{1,2})月(\d{1,2})日(?=$|\s)", text)
    if month_day:
        month, day = map(int, month_day.groups())
        return None, month * 32 + day, None
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return reference.replace(hour=hour, minute=minute, second=0, microsecond=0), None, reference.year
    if text.startswith("前天"):
        value = reference - timedelta(days=2)
        return value.replace(hour=hour, minute=minute, second=0, microsecond=0), None, value.year
    if text.startswith("今天"):
        return reference.replace(hour=hour, minute=minute, second=0, microsecond=0), None, reference.year
    if text.startswith("昨天"):
        value = reference - timedelta(days=1)
        return value.replace(hour=hour, minute=minute, second=0, microsecond=0), None, value.year
    weekday_match = re.match(r"(?:星期|周)([一二三四五六日天])", text)
    if weekday_match:
        weekday = "一二三四五六日天".index(weekday_match.group(1))
        value = reference - timedelta(days=(reference.weekday() - weekday) % 7)
        return value.replace(hour=hour, minute=minute, second=0, microsecond=0), None, value.year
    return None, None, None


def infer_datetimes(records: list[dict], reference: datetime) -> list[datetime | None]:
    """从最新到最旧推断年份，再向前补齐同组消息省略的时间。"""
    inferred: list[datetime | None] = [None] * len(records)
    context_year = reference.year
    newer_month_day = reference.month * 32 + reference.day
    for index in range(len(records) - 1, -1, -1):
        text = records[index].get("time_text")
        absolute, month_day, explicit_year = parse_time_text(text, reference)
        if absolute is not None:
            inferred[index] = absolute
            if explicit_year is not None:
                context_year = explicit_year
                newer_month_day = absolute.month * 32 + absolute.day
            continue
        if explicit_year is not None:
            context_year = explicit_year
        if month_day is not None:
            if newer_month_day is not None and month_day - newer_month_day > 180:
                context_year -= 1
            month, day = divmod(month_day, 32)
            clock = re.search(r"(\d{1,2}):(\d{2})", text or "")
            hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (0, 0)
            try:
                inferred[index] = datetime(context_year, month, day, hour, minute)
            except ValueError:
                inferred[index] = None
            newer_month_day = month_day
    last_time = None
    for index, value in enumerate(inferred):
        if value is not None:
            last_time = value
        elif records[index].get("time_text"):
            # 无效但明确存在的标签不能继承上一条时间，其后省略时间的行也保持未知。
            last_time = None
        elif last_time is not None:
            inferred[index] = last_time
    return inferred


def range_boundary_verified(capture: dict, start: date) -> bool:
    """原始边界通过还不够：原始记录须实际包含本次起点之前的日期。"""
    return bool(capture.get("boundary_verified")) and any(
        value is not None and value.date() < start
        for value in infer_datetimes(capture.get("records") or [], reference_datetime(capture))
    )


def load_json(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sender_identity(sender: str | None, conversation_meta: dict, directory: dict) -> tuple[bool | None, str | None]:
    if not sender:
        return None, None
    entry = directory.get(sender) or {}
    contact_name = conversation_meta.get("contact_name")
    if contact_name and sender == person_name(contact_name):
        entry = {**entry, "is_external": conversation_meta.get("contact_is_external", entry.get("is_external")), "company": conversation_meta.get("contact_company") or entry.get("company")}
    return entry.get("is_external"), entry.get("company") or entry.get("external_company")


def person_name(value: str | None) -> str | None:
    """统一取「english(中文)」括号内的中文姓名，避免同一人出现两种标识。"""
    if not value:
        return value
    match = re.fullmatch(r".+?[（(](.+?)[）)]", value)
    return match.group(1) if match else value


def peer_name(conversation: str | None) -> str | None:
    """单聊会话名形如 english(中文名)，取括号内中文姓名；无括号时原样返回。"""
    return person_name(conversation)


def resolve_sender(record: dict, conversation: str | None) -> str | None:
    sender = record.get("sender")
    if sender:
        return person_name(sender.rsplit("@", 1)[0])
    outgoing = record.get("is_outgoing")
    if outgoing is None:
        return None
    return SELF_LABEL if outgoing else peer_name(conversation)


SELF_NAMES: list[str] = []

# 侧栏候选清单里的 kind → conversation_type。这是比「按成员数猜」更硬的一条证据：
# 企业微信把单聊和群聊在侧栏用不同形态呈现，kind 记录了当时的判定结果。
CANDIDATE_KIND: dict[str, str] = {}
KIND_TO_TYPE = {"单聊": "single", "拼接名群": "group", "命名群/群": "group"}


def at_me_flag(body: str | None, self_names: list[str], is_outgoing: bool | None = None, sender: str | None = None) -> bool | None:
    """是否被 @ 我；未提供本人名字时保持未知，不用个人硬编码猜测。

    自己发出的消息一律不算被 @：企业微信会把「别人@我 + 我的回复」渲染成一条合并体
    （正文用 `---` 分隔），整条被记成 is_outgoing=True，正文里仍带 `@我`，纯正文匹配
    会把它误判成被 @。群聊方向可能为空，发送者匹配本人姓名或「我」时也排除。

    @ 后面要紧跟非标识符字符才算命中：`@jammyzhang2` 是另一个人，不能被
    `@jammyzhang` 前缀匹配吞进来。界面里的实体形式是 `@alias(中文名)`，括号天然构成边界。
    """
    if not self_names:
        return None
    if is_outgoing is True or sender == SELF_LABEL or (sender and person_name(sender) in {person_name(name) for name in self_names}):
        return False
    if not body:
        return False
    for name in self_names:
        if re.search(rf"@{re.escape(name)}(?![A-Za-z0-9_.])", body):
            return True
    return False


def guess_conversation_type(conversation: str | None, member_count: int | None) -> str | None:
    """按证据强弱取值：候选清单 kind > 成员数 > 未知。"""
    if conversation and conversation in CANDIDATE_KIND:
        return KIND_TO_TYPE.get(CANDIDATE_KIND[conversation])
    if member_count == 1:
        return "single"
    if member_count and member_count > 1:
        return "group"
    return None


def load_candidates(path: str | None) -> dict[str, str]:
    """读候选清单，建立 会话名 → kind 映射。找不到时返回空表，退化为按成员数判断。"""
    if not path:
        return {}
    data = load_json(path)
    mapping = {}
    for item in data.get("conversations") or []:
        name, kind = item.get("name"), item.get("kind")
        if name and kind:
            mapping[name] = kind
    return mapping


def match_key(when: datetime, record: dict, sender: str | None) -> str:
    """跨次采集的对齐指纹。

    客户端分页有节流，一个活跃会话往往要分多次才能回溯完；两次采集之间必须能认出
    「同一条消息」才能拼接。键只取消息自身稳定属性，不取采集时间、行号或滚动位置。
    """
    base = json.dumps([
        when.isoformat(sep=" "), record.get("sender") or "", sender or "",
        record.get("body_raw") or record.get("body") or "", record.get("message_type") or "", record.get("links") or [],
        record.get("is_outgoing"),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def external_group_flag(conversation: str | None, conversation_meta: dict) -> bool | None:
    """外部群在当前客户端以群名后缀「(外部)」标注。

    无后缀只能说明「未观测到外部群标记」，不等于确认内部群，因此返回 None 而非 False。
    """
    if conversation_meta.get("is_external_group") is not None:
        return conversation_meta["is_external_group"]
    if conversation and re.search(r"[（(]外部[）)]\s*$", conversation):
        return True
    return None


def transform(capture: dict, start: date, end: date, conversation: str | None, conversation_meta: dict, directory: dict, self_names: list[str] | None = None) -> tuple[list[dict], dict]:
    self_names = self_names or SELF_NAMES
    records = capture.get("records") or []
    inferred = infer_datetimes(records, reference_datetime(capture))
    history = capture.get("tool") == "wework_history_capture"
    if history:
        inferred = [value if record.get("time_text") else None for record, value in zip(records, inferred)]
    kept = []
    stats = {"raw_records": len(records), "excluded_context_rows": 0, "excluded_outside_range": 0, "excluded_without_date": 0}
    for record, when in zip(records, inferred):
        if record.get("context_only"):
            stats["excluded_context_rows"] += 1
            continue
        if when is None:
            stats["excluded_without_date"] += 1
            continue
        if not start <= when.date() <= end:
            stats["excluded_outside_range"] += 1
            continue
        sender = resolve_sender(record, conversation or capture.get("conversation"))
        sender_external, sender_company = sender_identity(sender, conversation_meta, directory)
        outgoing = record.get("is_outgoing")
        raw_sender = record.get("sender") or ""
        if history and "@" in raw_sender and outgoing is not True:
            sender_external, sender_company = True, raw_sender.rsplit("@", 1)[1]
        name = conversation or capture.get("conversation")
        member_count = capture.get("member_count") or conversation_meta.get("member_count")
        kept.append({
            # id 原样来自界面（正文哈希 + 同屏内出现序号），不含会话维度，
            # 不同会话里的两句「好的」会撞成同一个 id。加会话前缀后才能当全局主键用。
            "id": f"{name}#{record.get('id')}" if name and record.get("id") else record.get("id"),
            "record_id": None,
            "conversation_key": None,
            "conversation": name,
            "conversation_type": conversation_meta.get("conversation_type") or (capture.get("conversation_type") if capture.get("conversation_type") != "unknown" else None) or guess_conversation_type(name, member_count),
            "conversation_is_external_group": external_group_flag(conversation or capture.get("conversation"), {**capture, **conversation_meta}),
            "conversation_external_member_count": conversation_meta.get("external_member_count"),
            "conversation_member_count": capture.get("member_count") or conversation_meta.get("member_count"),
            "sender": sender,
            "sender_raw": record.get("sender"),
            "sender_is_external": sender_external,
            "sender_company": sender_company,
            "is_outgoing": outgoing,
            "sent_at": when.isoformat(sep=" "),
            "time_precision": "minute",
            "time_inferred": bool(record.get("time_inferred")),
            "day": when.date().isoformat(),
            "hour": when.hour,
            "weekday": "一二三四五六日"[when.weekday()],
            "time_text": record.get("time_text"),
            "at_me": at_me_flag(record.get("body", ""), self_names, outgoing, sender),
            "year_inferred": bool(record.get("time_inferred")) or not bool(EXPLICIT_DATE.match((record.get("time_text") or "").strip())),
            "message_type": record.get("message_type"),
            "body": record.get("body", "").replace("\ufffc", "[内嵌表情/附件：未识别]"),
            "links": record.get("links") or [],
            "source": "macos_accessibility_history" if history else "macos_accessibility_scroll",
            "captured_at": capture.get("captured_at"),
            "stop_reason": capture.get("stop_reason"),
            "boundary_verified": capture.get("boundary_verified", False),
            "match_key": match_key(when, record, sender),
            "body_raw": record.get("body_raw", record.get("body", "")),
            "attachments": record.get("attachments") or [],
            "content_incomplete": bool(record.get("content_incomplete") or "\ufffc" in record.get("body", "")),
            "unresolved_fields": record.get("unresolved_fields") or [],
            "time_source": record.get("time_source") or ("explicit_history_timestamp" if history else "main_view_context"),
            "direction_evidence": record.get("direction_evidence"),
        })
    # 导出保留原始行：技能约定「内容相同的合法重复消息必须保留」，不去重。
    # 汇总对原生面板使用序列 occurrence id；旧主聊天区沿用 match_key，显式报告折叠数。
    stats["match_key_collisions"] = len(kept) - len({record["match_key"] for record in kept})
    return kept, stats


def write_outputs(records: list[dict], prefix: Path) -> tuple[Path, Path]:
    ensure_dir(prefix.parent)
    jsonl_path = prefix.with_suffix(".jsonl")
    csv_path = prefix.with_suffix(".csv")
    with jsonl_path.with_suffix(".jsonl.tmp").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with csv_path.with_suffix(".csv.tmp").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["links"] = "\n".join(row["links"])
            for field in ("attachments", "unresolved_fields"):
                row[field] = json.dumps(row.get(field) or [], ensure_ascii=False)
            writer.writerow(row)
    jsonl_path.with_suffix(".jsonl.tmp").replace(jsonl_path)
    csv_path.with_suffix(".csv.tmp").replace(csv_path)
    return jsonl_path, csv_path


def autodiscover_inputs(prefix: Path) -> tuple[dict[str, str], list[str]]:
    """从产物目录反查候选清单与本人别名，避免这两个参数漏传导致整列为空。

    历史批次就是因为采集器没传，`conversation_type` 和 `at_me` 才 100% 为 null。
    这两条证据其实都躺在磁盘上：`<目录>/candidates-*.json` 有侧栏 kind，
    `chat-config.json` 有 self_names。找不到就返回空，导出行为与原来一致。
    """
    kinds: dict[str, str] = {}
    self_names: list[str] = []
    for root in (prefix.parent.parent, prefix.parent.parent.parent):
        if not root.is_dir():
            continue
        for cand in sorted(root.glob("candidates-*.json")):
            kinds.update(load_candidates(str(cand)))
        config_file = root / "chat-config.json"
        if config_file.exists():
            names = load_json(str(config_file)).get("self_names") or []
            if isinstance(names, list):
                self_names = [str(x).strip() for x in names if str(x).strip()]
    return kinds, self_names


def main() -> int:
    parser = argparse.ArgumentParser(description="按任意时间区间导出企业微信聊天 CSV／JSONL")
    parser.add_argument("input", help="wework_scroll_capture.swift 生成的原始 JSON")
    parser.add_argument("--start", required=True, type=parse_date)
    parser.add_argument("--end", required=True, type=parse_date)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--conversation")
    parser.add_argument("--conversation-metadata", help="会话类型、外部群和单聊企业信息 JSON")
    parser.add_argument("--sender-directory", help="发送者外部身份映射 JSON")
    parser.add_argument("--self-names", help="判定「@我」用的本人名字，逗号分隔；不提供时 at_me 为 null")
    parser.add_argument("--candidates", help="候选清单 JSON；把侧栏 kind（单聊/拼接名群/命名群）落成 conversation_type")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("结束日期不能早于开始日期")
    CANDIDATE_KIND.update(load_candidates(args.candidates))
    auto_kinds, auto_self_names = autodiscover_inputs(Path(args.output_prefix))
    for key, value in auto_kinds.items():
        CANDIDATE_KIND.setdefault(key, value)
    capture = json.loads(Path(args.input).read_text(encoding="utf-8"))
    self_names = [x.strip() for x in (args.self_names or "").split(",") if x.strip()] or auto_self_names or None
    records, stats = transform(capture, args.start, args.end, args.conversation, load_json(args.conversation_metadata), load_json(args.sender_directory), self_names)
    jsonl_path, csv_path = write_outputs(records, Path(args.output_prefix))
    dates = [record["sent_at"] for record in records]
    boundary_verified = range_boundary_verified(capture, args.start)
    sequence_gaps = int(capture.get("sequence_gaps") or 0)
    incomplete_reasons = {"in-progress", "no-scroll-test", "top-or-no-change", "no-new-content", "max-scrolls", "table-lost", "scroll-position-unavailable", "alignment-gap", "conversation-changed"}
    chronological_order = dates == sorted(dates)
    captured_at = reference_datetime(capture)
    end_observable = args.end <= captured_at.date()
    coverage_complete = end_observable and boundary_verified and not sequence_gaps and chronological_order and capture.get("stop_reason") not in incomplete_reasons and not capture.get("unresolved_rows")
    summary = {
        "status": "exported" if coverage_complete else "exported_incomplete",
        "date_parser_version": DATE_PARSER_VERSION,
        "capture_boundary_verified": bool(capture.get("boundary_verified")),
        "checked_through": captured_at.isoformat(sep=" "),
        "requested_end_observable": end_observable,
        "end_day_complete": args.end < captured_at.date(),
        "requested_range": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        **stats,
        "exported_records": len(records),
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
        "stop_reason": capture.get("stop_reason"),
        "boundary_verified": boundary_verified,
        "sequence_gaps": sequence_gaps,
        "chronological_order": chronological_order,
        "coverage_complete": coverage_complete,
        "jsonl": str(jsonl_path.resolve()),
        "csv": str(csv_path.resolve()),
        "completeness_note": "界面采集结果需结合停止原因、滚动次数、空正文和发送者缺失情况验证，不等同于合规归档。",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
