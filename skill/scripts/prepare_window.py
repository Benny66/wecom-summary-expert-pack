#!/usr/bin/env python3
"""按时间窗切出企业微信单聊/群聊的可读对话稿与统计。

只做确定性工作：过滤、分组、排序、统计、识别「谁欠谁一句回复」。
不判断消息是不是工作项，也不写任何结论——定性交给模型。

用法:
    # 先看仓库里有哪些会话
    python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales --list

    # 切一周
    python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales \
        --week 2026-W48 --out ~/worklog/runs/2026-W48

    # 或用日期手写（两端都含）
    python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales \
        --since 2026-11-23 --until 2026-11-29 \
        --out ~/worklog/runs/2026-W48

    # 切整月；只写 --month 就是本月
    python3 prepare_window.py --store ~/wecom-data/wecom-chat-sales \
        --month 2026-11 --out ~/worklog/runs/2026-11

    # 只看某几个客户（单聊用 --singles，客户群用 --groups，可同时给）
    python3 prepare_window.py --store <仓库> --since 2026-11-25 --until 2026-11-25 \
        --out <目录> --singles "张三,李四(某科技)" --groups "灵动科技项目对接群"

输出（--out 目录）:
    window.md         按客户分组的可读对话稿（模型读这个）
    window.json       统计与结构信号（模型取数字）
    window.msgs.jsonl 窗口内原始消息（check_ledger.py 核对证据）

群聊说明:
    群里我方同事和客户混在一起，只数「我 / 对方」会失真的。本脚本用
    `sender_is_external` 把每条消息分成 我 / 我方同事 / 客户方 / 未标注 四类；
    该字段整段为空时分边未知，此时不产出 silent / awaiting_* 结论，只标
    side_unknown，避免把同事的话当成客户回应。

退出码:
    0  正常（窗口内 0 条消息也算正常，会在 stderr 提示）
    1  输入不可用（仓库/清单缺失、没有任何可用会话）
    2  参数错误
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

DATE_FMT = "%Y-%m-%d"
WEEKDAYS = "一二三四五六日"

PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
IDCARD = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
BANK = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")

SEP = "\n---\n"
# 这些类型算「含媒体」；quote 是引用文字，不算媒体
MEDIA_TYPES = ("image_or_media", "image", "file", "video", "voice",
               "sticker", "emotion", "location", "link_card")
PROJECT_FIELDS = (
    "record_id", "conversation", "conversation_key", "conversation_type",
    "conversation_is_external_group",
    "sender", "sender_raw", "sender_is_external", "sender_company",
    "is_outgoing", "sent_at", "day",
    "message_type", "body", "links", "content_incomplete", "at_me",
)


def err(msg, code=1):
    sys.stderr.write("prepare_window: {}\n".format(msg))
    sys.exit(code)


def warn(msg):
    sys.stderr.write("prepare_window: {}\n".format(msg))


# ---------- 脱敏 ----------

def _mask_phone(m):
    s = m.group(1)
    return s[:3] + "****" + s[-4:]


def _mask_tail(m):
    s = m.group(1)
    keep = 4 if len(s) > 8 else 2
    return "*" * (len(s) - keep) + s[-keep:]


def _mask_email(m):
    s = m.group(0)
    local, _, domain = s.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return "{}***@{}".format(head, domain)


def redact_text(text, mode):
    """pii: 打码手机号/证件号/银行卡/邮箱；all: 在 pii 基础上再由调用方换人名。"""
    if mode == "none" or not text:
        return text
    out = PHONE.sub(_mask_phone, text)
    out = IDCARD.sub(_mask_tail, out)
    out = BANK.sub(_mask_tail, out)
    out = EMAIL.sub(_mask_email, out)
    return out


# ---------- 读取 ----------

def load_manifest(store):
    path = os.path.join(store, "manifest.json")
    if not os.path.isfile(path):
        err("找不到 manifest.json，确认 --store 指向采集仓库根目录：{}".format(store))
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as exc:
            err("manifest.json 解析失败：{}".format(exc))


def conversation_file(store, key, meta):
    rel = meta.get("current_file")
    if rel:
        path = rel if os.path.isabs(rel) else os.path.join(store, rel)
        if os.path.isfile(path):
            return path
    path = os.path.join(store, "conversations", key, "current.jsonl")
    return path if os.path.isfile(path) else None


def load_messages(store, manifest, name_filter):
    """返回 {record_id: row}，同一 record_id 保留正文更长的一条。"""
    rows = {}
    bad_lines = 0
    no_id = 0
    for key, meta in (manifest.get("conversations") or {}).items():
        path = conversation_file(store, key, meta)
        if not path:
            continue
        names = meta.get("names") or []
        default_name = names[0] if names else key
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad_lines += 1
                    continue
                row.setdefault("conversation_key", key)
                if not row.get("conversation"):
                    row["conversation"] = default_name
                if name_filter and row["conversation"] not in name_filter:
                    continue
                rid = row.get("record_id")
                if not rid:
                    no_id += 1
                    continue
                row["_history_complete"] = bool(meta.get("history_complete"))
                row["_reached_start"] = bool(meta.get("reached_start"))
                prev = rows.get(rid)
                if prev is None:
                    rows[rid] = row
                else:
                    a = len(str(row.get("body") or ""))
                    b = len(str(prev.get("body") or ""))
                    if a > b:
                        rows[rid] = row
    if bad_lines:
        warn("跳过 {} 行无法解析的 JSONL".format(bad_lines))
    if no_id:
        warn("跳过 {} 条缺 record_id 的消息（无法作为证据）".format(no_id))
    return list(rows.values())


# ---------- 整理 ----------

def split_quote(body):
    """引用消息形如「被引用内容 + --- + 新内容」。"""
    if body and SEP in body:
        quoted, _, reply = body.partition(SEP)
        return quoted.strip(), reply.strip()
    return "", body or ""


def one_line(text, limit):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def is_mine(row, self_names):
    """判断是不是本人发的。

    首选 is_outgoing（采集器按气泡方向判定，最可靠）；缺失时回退到
    sender / sender_raw 是否命中 --self-names。单聊里自己发的 sender 可能是
    中文名（如「温志聪」）也可能是「我」，写死任何一种都会判错。
    """
    flag = row.get("is_outgoing")
    if flag is True:
        return True
    if flag is False:
        return False
    sender = (row.get("sender") or "").strip()
    raw = (row.get("sender_raw") or "").strip()
    return sender in self_names or raw in self_names


def parse_self_names(text):
    names = {x.strip() for x in (text or "").split(",") if x.strip()}
    names.add("我")
    return names


def side_of(row, self_names):
    """me / ours / theirs / unknown。

    群里必须把「我方同事」和「客户方」分开：只有一方在说时，把同事的话
    当成客户回应会让 silent / awaiting_client 全部失真。
    """
    if is_mine(row, self_names):
        return "me"
    flag = row.get("sender_is_external")
    if flag is True:
        return "theirs"
    if flag is False:
        return "ours"
    return "unknown"


def side_word(side, is_group):
    if side == "me":
        return "我"
    if not is_group:
        return "客户"
    return {"ours": "我方同事", "theirs": "客户方"}.get(side, "未标注")


def last_from_word(frm, is_group):
    return {"me": "我", "ours": "我方同事", "unknown": "未标注",
            "client": "客户方" if is_group else "客户"}.get(frm, "客户")


def display_sender(row, self_names, mode, is_group=False):
    """群里给非本人的发言人标一个分边。分边未知时不加括号，不要硬标。

    分边词统一走 side_word()，别在这里再写一份映射——两处映射迟早会不一致。
    """
    if is_mine(row, self_names):
        return "我"
    raw = redact_text((row.get("sender") or "").strip() or "对方", mode)
    if not is_group:
        return raw
    side = side_of(row, self_names)
    if side in ("ours", "theirs"):
        return "{}（{}）".format(raw, side_word(side, True))
    return raw


def render_body(row, mode, max_chars):
    """把一条消息渲染成一行。引用消息拆成「引用段 + 回复段」，不重复打类型标。"""
    body = redact_text(row.get("body") or "", mode)
    mtype = row.get("message_type") or "text"
    quoted, reply = split_quote(body)
    reply = re.sub(r"\s+", " ", reply).strip()
    if len(reply) > max_chars:
        reply = "{}…（截断，原文 {} 字）".format(reply[:max_chars], len(reply))
    if mtype == "quote":
        prefix = "[引用「{}」] ".format(one_line(quoted, 40)) if quoted else "[引用] "
        return prefix + (reply or "（引用正文为空）")
    if mtype == "text":
        return reply or "[空消息]"
    if reply in ("", "[非文本消息：内容未暴露]", "[图片]", "[文件]"):
        return "[{}·内容未暴露]".format(mtype)
    return "[{}] {}".format(mtype, reply)


def weekday_of(day):
    try:
        return WEEKDAYS[datetime.strptime(day, DATE_FMT).weekday()]
    except ValueError:
        return "?"


def infer_is_group(items, self_names):
    """判断这个会话是群还是单聊。

    conversation_type 缺失时不能默认成单聊：客户群里我方同事和客户混在一起，
    把同事的话算成「客户回应」会让 silent / awaiting_client 全部失真。缺失时
    用「除我之外出现过几个不同发言人」兜底。
    """
    ctype = (items[0].get("conversation_type") or "").strip()
    if ctype == "single":
        return False
    if ctype == "group":
        return True
    others = {(r.get("sender") or "").strip() for r in items
              if not is_mine(r, self_names)}
    others.discard("")
    return len(others) > 1


def build(rows, since, until, self_names, args):
    # 周期末晚于今天时，静默/欠回复一律按今天算。否则月中跑整月报，
    # 会把这个月离月底的天数加进去，把 3 天前刚聊过的客户算成 10 天没动静。
    now_d = datetime.now().date()
    ref_d = min(datetime.strptime(until, DATE_FMT).date(), now_d)

    by_conv = {}
    for row in rows:
        day = str(row.get("sent_at") or "")[:10]
        if not day:
            continue
        if day < since or day > until:
            continue
        by_conv.setdefault(row.get("conversation_key"), []).append(row)

    conversations = []
    name_map = {}
    for idx, (key, items) in enumerate(sorted(by_conv.items(), key=lambda kv: kv[0])):
        # 只按时间排，不拿 record_id 兜底：采集仓库的落盘顺序就是气泡的视觉顺序，
        # 同分钟消息靠稳定排序保住这个顺序。按 record_id 排会把同分钟的对话打乱。
        items.sort(key=lambda r: str(r.get("sent_at") or ""))
        name = items[0].get("conversation") or key
        display = name
        if args.redact == "all":
            display = "客户{}".format(chr(ord("A") + idx) if idx < 26 else str(idx + 1))
        name_map[key] = {"raw": name, "display": display}

        is_group = infer_is_group(items, self_names)
        sides = [side_of(r, self_names) for r in items]
        if not is_group:
            # 单聊里没有「我方同事」这个概念，非我即客户。不规整的话，
            # sender_is_external 为 null 的单聊会被判成 unknown，
            # awaiting_me / awaiting_client 会静默消失。
            sides = ["me" if s == "me" else "theirs" for s in sides]
        mine = [r for r, s in zip(items, sides) if s == "me"]
        if is_group:
            theirs = [r for r, s in zip(items, sides) if s == "theirs"]
            ours = [r for r, s in zip(items, sides) if s == "ours"]
            unknown = [r for r, s in zip(items, sides) if s == "unknown"]
        else:
            # 单聊里对方就是客户，不需要 sender_is_external
            theirs = [r for r, s in zip(items, sides) if s != "me"]
            ours, unknown = [], []

        # 群里只有 sender_is_external 有值时才分得清谁是我方、谁是客户方；
        # 全为 null 时不下「谁在等谁」的结论，只标 side_unknown 让人看原文。
        # 只看对方的消息：我自己那条通常是 False（我当然是内部的），拿它当
        # 「分边已知」的证据会让整段客户消息都是 null 的群误判成已知。
        marked = [r.get("sender_is_external") for r, s in zip(items, sides) if s != "me"]
        client_side_known = (not is_group) or any(f is not None for f in marked)

        last = items[-1]
        last_day = str(last.get("sent_at") or "")[:10]
        try:
            gap = (ref_d - datetime.strptime(last_day, DATE_FMT).date()).days
        except ValueError:
            gap = 0
        last_side = sides[-1]
        days = sorted({str(r.get("sent_at") or "")[:10] for r in items})

        flags = []
        if client_side_known:
            if not theirs:
                flags.append("silent")
            if gap >= args.awaiting_days:
                if last_side in ("me", "ours"):
                    flags.append("awaiting_client")
                elif last_side == "theirs":
                    flags.append("awaiting_me")
        else:
            flags.append("side_unknown")
            if gap >= args.awaiting_days:
                flags.append("awaiting_check")
        if gap >= args.silence_days:
            flags.append("stalled")
        media = [r for r in items if (r.get("message_type") or "text") in MEDIA_TYPES]
        if media:
            flags.append("has_media")
        incomplete = any(bool(r.get("content_incomplete")) for r in items)
        if incomplete:
            flags.append("content_incomplete")

        participants = []
        if is_group:
            seen = {}
            for row, side in zip(items, sides):
                who = (row.get("sender") or "?").strip()
                rec = seen.setdefault(who, {
                    "name": redact_text(who, args.redact),
                    "side": side,
                    "side_word": side_word(side, True),
                    "company": row.get("sender_company") or "",
                    "messages": 0,
                })
                rec["messages"] += 1
            participants = sorted(seen.values(), key=lambda p: -p["messages"])

        tail = []
        for row, side in list(zip(items, sides))[-2:]:
            tail.append({
                "at": str(row.get("sent_at") or ""),
                "who": side_word(side, is_group),
                "body": one_line(redact_text(row.get("body") or "", args.redact), 120),
            })

        # 「群名带 (外部)」只说明观测到外部标记；没有标记不等于确认内部群，
        # 所以保留 None，不要压成 False。
        ext_group = items[0].get("conversation_is_external_group")
        entry = {
            "key": key,
            "name": display,
            "type": "single" if not is_group else "group",
            "is_customer_group": None if ext_group is None else bool(ext_group),
            "messages": len(items),
            "my_messages": len(mine),
            "their_messages": len(theirs),
            "ours_messages": len(ours),
            "unknown_messages": len(unknown),
            "client_side_known": client_side_known,
            "participants": participants,
            "first_at": str(items[0].get("sent_at") or ""),
            "last_at": str(last.get("sent_at") or ""),
            "active_days": len(days),
            "days_since_last": gap,
            "last_from": {"me": "me", "ours": "ours",
                          "theirs": "client", "unknown": "unknown"}.get(last_side, "client"),
            "last_side_word": side_word(last_side, is_group),
            "flags": flags,
            "last_two": tail,
            "media_count": len(media),
            "history_complete": bool(items[0].get("_history_complete")),
            "reached_start": bool(items[0].get("_reached_start")),
        }
        if args.redact != "all":
            entry["name_raw"] = name
        conversations.append(entry)

    def priority(conv):
        flags = conv["flags"]
        if "awaiting_me" in flags:
            return 0
        if "silent" in flags:
            return 1
        if "awaiting_client" in flags or "awaiting_check" in flags:
            return 2
        if "stalled" in flags:
            return 3
        return 4

    conversations.sort(key=lambda c: (priority(c), -c["days_since_last"]))
    return conversations, name_map


def write_md(path, conversations, meta, args):
    lines = []
    lines.append("# 客户沟通窗口 {} ~ {}".format(meta["window"]["since"], meta["window"]["until"]))
    lines.append("")
    ref = meta["window"].get("ref_date") or meta["window"]["until"]
    if meta["window"]["until"] > ref:
        # 月中跑整月报时别让读者以为「距最后一条」是从月底倒推的
        lines.append("> 周期未结束：距最后一条与静默天数按 {} 算，不按周期末 {} 算。".format(
            ref, meta["window"]["until"]))
        lines.append("")
    totals = meta["totals"]
    lines.append("> 仓库：{} ｜ 会话 {} 个（单聊 {} / 群 {}）｜ 消息 {} 条（我 {} / 对方 {}）".format(
        meta["store"], meta["coverage"]["conversations"],
        meta["coverage"]["singles"], meta["coverage"]["groups"],
        totals["messages"], totals["my_messages"], totals["their_messages"]))
    if meta["coverage"]["groups"]:
        # 群里的「对方」还分我方同事和客户方，单看一个数字会误判成「客户在说话」
        gs = totals["group_sides"]
        lines.append("> 群聊分边（群消息 {} 条）：我 {} ｜ 我方同事 {} ｜ 客户方 {} ｜ 未标注 {}".format(
            gs["messages"], gs["me"], gs["ours"], gs["theirs"], gs["unknown"]))
    incomplete = meta["coverage"]["incomplete"]
    if incomplete:
        lines.append("> 覆盖：本窗口不是完整历史 —— {}".format("、".join(incomplete)))
    else:
        lines.append("> 覆盖：全部会话 history_complete=true（账号可见范围内完整）")
    sig = meta["signals"]
    if any(sig.values()):
        parts = []
        for label, field in (("欠我回复", "awaiting_me"), ("我欠回复", "awaiting_client"),
                             ("客户零回复", "silent"), ("待人工确认", "awaiting_check"),
                             ("分边未知", "side_unknown")):
            if sig[field]:
                parts.append("{} {}".format(label, "、".join(sig[field])))
        lines.append("> 结构信号：" + "｜".join(parts))
    else:
        lines.append("> 结构信号：无（所有会话都在最近 {} 天内有来有回）".format(args.awaiting_days))
    if args.redact == "all":
        lines.append("> 已脱敏：会话名替换为代号，手机号/证件号/银行卡/邮箱已打码。"
                     "正文里出现的真名未替换，外发前需人工复核。")
    elif args.redact == "pii":
        lines.append("> 已脱敏：手机号/证件号/银行卡/邮箱已打码，客户名保留")
    lines.append("")

    if not conversations:
        lines.append("_本窗口内没有任何消息。_")
        lines.append("")

    self_names = parse_self_names(args.self_names)

    for i, conv in enumerate(conversations, 1):
        is_group = conv["type"] != "single"
        lines.append("---")
        lines.append("")
        if not is_group:
            kind = "单聊"
        elif conv["is_customer_group"]:
            kind = "群聊·外部"
        else:
            kind = "群聊"
        lines.append("## {}. {}｜{}｜{} 条｜{} ~ {}".format(
            i, conv["name"], kind, conv["messages"],
            conv["first_at"][5:10], conv["last_at"][5:10]))
        lines.append("")
        flags = "、".join(conv["flags"]) or "无"
        lines.append("`flags`: {} ｜ 距最后一条 {} 天 ｜ 最后发言：{}".format(
            flags, conv["days_since_last"],
            last_from_word(conv["last_from"], is_group)))
        if is_group:
            tally = "我 {} / 我方同事 {} / 客户方 {}".format(
                conv["my_messages"], conv["ours_messages"], conv["their_messages"])
            if conv["unknown_messages"]:
                tally += " / 未标注 {}".format(conv["unknown_messages"])
            lines.append("")
            lines.append("群成员分边（按发言数）：{}".format(tally))
            roster = "、".join("{}（{}）{} 条".format(
                p["name"], p["side_word"], p["messages"]) for p in conv["participants"][:8])
            if len(conv["participants"]) > 8:
                roster += "、…共 {} 人".format(len(conv["participants"]))
            lines.append("  - {}".format(roster))
            if not conv["client_side_known"]:
                lines.append("")
                lines.append("> 该群全部发言人 `sender_is_external` 均为空，分不清谁是我方谁是客户。"
                             "下面只按原文说话人标注，不标分边；不要据此写「客户在等我们回复」。")
        if not conv["history_complete"]:
            lines.append("")
            lines.append("> 该会话 history_complete=false，本窗口不是完整历史。")
        lines.append("")

        current_day = None
        for row in conv["_rows"]:
            day = str(row.get("sent_at") or "")[:10]
            if day != current_day:
                current_day = day
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append("### {} 周{}".format(day[5:], weekday_of(day)))
                lines.append("")
            who = display_sender(row, self_names, args.redact, is_group)
            stamp = str(row.get("sent_at") or "")[11:16]
            lines.append("- `{}` **{}**：{}".format(
                stamp, who, render_body(row, args.redact, args.max_chars)))
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def week_range(spec):
    """ISO 周（周一起、周日止）。'2026-W48' 或空串（=本周）→ (since, until)。

    周报窗口手写最容易错——跨月、跨年、记错那天是周几。默认按 ISO 周算，
    跟 `2026-W48-weekly.md` 的文件名口径一致。
    """
    spec = (spec or "").strip()
    if spec:
        m = re.fullmatch(r"(\d{4})-?[Ww](\d{1,2})", spec)
        if not m:
            err("--week 格式应为 2026-W48（或只写 --week 表示本周）", 2)
        try:
            monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            err("--week 的年份/周序号不合法：{}".format(spec), 2)
    else:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def month_range(spec):
    """自然月。'2026-11' 或空串（=本月）→ (since, until)。"""
    spec = (spec or "").strip()
    if spec:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", spec)
        if not m:
            err("--month 格式应为 2026-11（或只写 --month 表示本月）", 2)
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            err("--month 的月份不合法：{}".format(spec), 2)
    else:
        today = date.today()
        year, month = today.year, today.month
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(description="切企业微信聊天的可读窗口")
    ap.add_argument("--store", required=True, help="采集仓库根目录（含 manifest.json）")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD（含），缺省为今天")
    ap.add_argument("--week", nargs="?", const="", default=None,
                    help="按 ISO 周切窗口：--week 2026-W48，或只写 --week 表示本周。"
                         "给了就忽略 --since/--until")
    ap.add_argument("--month", nargs="?", const="", default=None,
                    help="按自然月切窗口：--month 2026-11，或只写 --month 表示本月。"
                         "给了就忽略 --since/--until")
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--singles", help="只保留这些单聊会话名，逗号分隔，须与侧栏一致")
    ap.add_argument("--groups", help="只保留这些群会话名，逗号分隔")
    ap.add_argument("--self-names", default="我",
                    help="本人的发送者名，逗号分隔（中文名和账号都填上），默认「我」。"
                         "优先用 is_outgoing 判定，这里只是缺失时的兜底")
    ap.add_argument("--silence-days", type=int, default=7, help="多久算停滞，默认 7")
    ap.add_argument("--awaiting-days", type=int, default=2, help="多久算欠一句回复，默认 2")
    ap.add_argument("--redact", choices=("none", "pii", "all"), default="pii",
                    help="pii 打码数字类敏感信息（默认）；all 连客户名一起换代号")
    ap.add_argument("--max-chars", type=int, default=500, help="单条消息在 md 里的最大字数")
    ap.add_argument("--list", action="store_true", help="只列出仓库里的会话，不切窗口")
    args = ap.parse_args(argv)
    self_names = parse_self_names(args.self_names)

    store = os.path.abspath(os.path.expanduser(args.store))
    manifest = load_manifest(store)

    names = None
    if args.singles or args.groups:
        names = set()
        for chunk in (args.singles, args.groups):
            if chunk:
                names.update(x.strip() for x in chunk.split(",") if x.strip())

    rows = load_messages(store, manifest, names)

    if args.list:
        seen = {}
        for row in rows:
            name = row.get("conversation")
            item = seen.setdefault(name, {"type": row.get("conversation_type"), "n": 0, "last": ""})
            item["n"] += 1
            item["last"] = max(item["last"], str(row.get("sent_at") or ""))
        print("仓库：{}".format(store))
        print("会话 {} 个".format(len(seen)))
        for name in sorted(seen):
            item = seen[name]
            print("  [{}] {}  {} 条  最后 {}".format(
                "single" if item["type"] == "single" else "group",
                name, item["n"], item["last"][:16]))
        return 0

    if args.week is not None and args.month is not None:
        err("--week 与 --month 只能给一个", 2)
    if args.week is not None or args.month is not None:
        if args.since or args.until:
            warn("--week/--month 已给出，忽略 --since/--until")
        if args.week is not None:
            args.since, args.until = week_range(args.week)
        else:
            args.since, args.until = month_range(args.month)

    if not args.since or not args.out:
        err("--since 与 --out 必填（或用 --week/--month 指定周期，或用 --list 只列会话）", 2)
    until = args.until or datetime.now().strftime(DATE_FMT)
    since_d = datetime.strptime(args.since, DATE_FMT)
    until_d = datetime.strptime(until, DATE_FMT)
    if since_d > until_d:
        err("--since 不能晚于 --until", 2)

    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)

    conversations, name_map = build(rows, args.since, until, self_names, args)

    # 把窗口内消息挂到对应会话上，供 md 渲染
    by_conv = {}
    for row in rows:
        day = str(row.get("sent_at") or "")[:10]
        if day and args.since <= day <= until:
            by_conv.setdefault(row.get("conversation_key"), []).append(row)
    for conv in conversations:
        conv["_rows"] = sorted(by_conv.get(conv["key"], []),
                               key=lambda r: str(r.get("sent_at") or ""))

    singles = [c for c in conversations if c["type"] == "single"]
    groups = [c for c in conversations if c["type"] != "single"]
    signals = {"awaiting_me": [], "awaiting_client": [], "silent": [],
               "awaiting_check": [], "side_unknown": []}
    for conv in conversations:
        flags = conv["flags"]
        for field, flag in (("silent", "silent"), ("awaiting_me", "awaiting_me"),
                            ("awaiting_client", "awaiting_client"),
                            ("awaiting_check", "awaiting_check"),
                            ("side_unknown", "side_unknown")):
            if flag in flags:
                signals[field].append(conv["name"])

    window_msgs = [r for c in conversations for r in c["_rows"]]
    # 静默/欠回复按「周期末与今天之间较早的那个」算，报告里要能看到这个基准
    ref_date = min(datetime.strptime(until, DATE_FMT).date(), datetime.now().date())
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "store": store,
        "window": {"since": args.since, "until": until,
                   "ref_date": ref_date.isoformat()},
        "params": {
            "self_names": args.self_names,
            "silence_days": args.silence_days,
            "awaiting_days": args.awaiting_days,
            "redact": args.redact,
        },
        "coverage": {
            "conversations": len(conversations),
            "singles": len(singles),
            "groups": len(groups),
            "customer_groups": sum(1 for c in groups if c["is_customer_group"]),
            "groups_side_unknown": sum(1 for c in groups if not c["client_side_known"]),
            "incomplete": [c["name"] for c in conversations if not c["history_complete"]],
            "all_complete": all(c["history_complete"] for c in conversations) if conversations else False,
        },
        "totals": {
            "messages": len(window_msgs),
            "my_messages": sum(1 for r in window_msgs if is_mine(r, self_names)),
            "their_messages": sum(1 for r in window_msgs if not is_mine(r, self_names)),
            # 与上面逐会话的统计口径保持一致（单聊的非我消息算客户方）
            "ours_messages": sum(c["ours_messages"] for c in conversations),
            "client_side_messages": sum(c["their_messages"] for c in conversations),
            "unknown_messages": sum(c["unknown_messages"] for c in conversations),
            # 只统计群消息里的分边，避免和单聊的「对方」混在一个数字里
            "group_sides": {
                "messages": sum(c["messages"] for c in groups),
                "me": sum(c["my_messages"] for c in groups),
                "ours": sum(c["ours_messages"] for c in groups),
                "theirs": sum(c["their_messages"] for c in groups),
                "unknown": sum(c["unknown_messages"] for c in groups),
            },
            "clients": len(conversations),
            "clients_active": sum(1 for c in conversations if c["messages"] and "silent" not in c["flags"]),
            "active_days": len({str(r.get("sent_at") or "")[:10] for r in window_msgs}),
        },
        "signals": signals,
        "conversations": [{k: v for k, v in c.items() if not k.startswith("_")} for c in conversations],
        "name_map": None if args.redact == "all" else name_map,
    }

    write_md(os.path.join(out, "window.md"), conversations, meta, args)

    with open(os.path.join(out, "window.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out, "window.msgs.jsonl"), "w", encoding="utf-8") as f:
        for row in sorted(window_msgs, key=lambda r: str(r.get("sent_at") or "")):
            keep = {}
            for field in PROJECT_FIELDS:
                if field not in row:
                    continue
                value = row.get(field)
                if field == "body" and args.redact != "none":
                    value = redact_text(value or "", args.redact)
                keep[field] = value
            keep["conversation_key"] = row.get("conversation_key")
            f.write(json.dumps(keep, ensure_ascii=False) + "\n")

    if not window_msgs:
        warn("窗口 {} ~ {} 内没有任何消息，仍写出了空窗口；报告里应说明这段时间无沟通，"
             "不要写成「无进展」".format(args.since, until))
    print("窗口已生成：{}".format(out))
    print("  会话 {}（单聊 {} / 群 {}）｜消息 {} 条".format(
        len(conversations), len(singles), len(groups), len(window_msgs)))
    if groups:
        gs = meta["totals"]["group_sides"]
        print("  群聊分边（群消息 {} 条）：我 {} / 我方同事 {} / 客户方 {} / 未标注 {}".format(
            gs["messages"], gs["me"], gs["ours"], gs["theirs"], gs["unknown"]))
    print("  欠我回复：{}".format("、".join(signals["awaiting_me"]) or "无"))
    print("  我欠回复：{}".format("、".join(signals["awaiting_client"]) or "无"))
    print("  客户零回复：{}".format("、".join(signals["silent"]) or "无"))
    if signals["awaiting_check"]:
        print("  待人工确认（群分边未知）：{}".format("、".join(signals["awaiting_check"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
