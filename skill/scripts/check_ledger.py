#!/usr/bin/env python3
"""校验事项台账，并给出派生状态建议。

退出码非 0 不许渲染报告。只判断、不改文件。

用法:
    python3 check_ledger.py --ledger ~/worklog/ledger.json \
        --window ~/worklog/runs/2026-11-25/window.msgs.jsonl \
        --until 2026-11-25 --suggest

    python3 check_ledger.py --ledger ~/worklog/ledger.json --json /tmp/lint.json

检查分三级:
    ERROR  必须改，否则退出码 1
    WARN   逐条看，不必全改
    SUGGEST（--suggest）派生状态，只建议不改文件

退出码:
    0  无 ERROR
    1  有 ERROR
    2  参数错误 / 台账不可读
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M"

STAGES = ("contact", "discovery", "solution", "quote",
          "negotiation", "contract", "delivery", "won", "lost")
STATUSES = ("active", "blocked", "done", "lost", "vanished")

ITEM_ID_RE = re.compile(r"^[0-9a-f]{6}-\d{8}-\d{2}$")
MSG_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

PROMISE_WORDS = ("明天", "后天", "今天晚点", "稍后", "马上", "尽快", "回头",
                 "下周", "这周", "本周内", "今晚", "我发你", "我给你", "我安排",
                 "我确认一下", "我问问", "我协调")

TITLE_MAX = 40
QUOTE_MAX = 200


class Report(object):
    def __init__(self):
        self.errors = []
        self.warns = []
        self.suggests = []

    def error(self, code, where, msg):
        self.errors.append({"level": "ERROR", "code": code, "where": where, "msg": msg})

    def warn(self, code, where, msg):
        self.warns.append({"level": "WARN", "code": code, "where": where, "msg": msg})

    def suggest(self, code, where, msg):
        self.suggests.append({"level": "SUGGEST", "code": code, "where": where, "msg": msg})

    def print_all(self):
        for row in self.errors:
            print("[ERROR] {:22s} {:28s} {}".format(row["code"], row["where"], row["msg"]))
        for row in self.warns:
            print("[WARN ] {:22s} {:28s} {}".format(row["code"], row["where"], row["msg"]))
        for row in self.suggests:
            print("[SUGG ] {:22s} {:28s} {}".format(row["code"], row["where"], row["msg"]))


def parse_date(text):
    try:
        return datetime.strptime(str(text)[:10], DATE_FMT).date()
    except (TypeError, ValueError):
        return None


def load_window(path):
    """{record_id: row} 与 {conversation_key: [rows 按时间排序]}"""
    index = {}
    by_conv = {}
    if not path:
        return index, by_conv
    if not os.path.isfile(path):
        sys.stderr.write("check_ledger: 找不到窗口消息 {}\n".format(path))
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rid = row.get("record_id")
            if rid:
                index[rid] = row
            by_conv.setdefault(row.get("conversation_key"), []).append(row)
    for key in by_conv:
        by_conv[key].sort(key=lambda r: str(r.get("sent_at") or ""))
    return index, by_conv


def amount_variants(value):
    """把 300000 展开成 300000 / 300,000 / 30万 / 30w / 30 万 等写法。"""
    out = set()
    try:
        num = float(value)
    except (TypeError, ValueError):
        return out
    if num != int(num):
        out.add("{:g}".format(num))
        return out
    whole = int(num)
    out.add(str(whole))
    out.add("{:,}".format(whole))
    if whole and whole % 10000 == 0:
        wan = whole // 10000
        out.update({u"{}万".format(wan), u"{} 万".format(wan),
                    "{}w".format(wan), "{}W".format(wan)})
    if whole and whole % 1000 == 0:
        out.update({"{}k".format(whole // 1000), "{}K".format(whole // 1000)})
    return out


def normalized(text):
    return re.sub(r"[\s,，]", "", text or "")


def parse_self_names(text):
    names = {x.strip() for x in (text or "").split(",") if x.strip()}
    names.add("我")
    return names


def is_mine(row, self_names):
    flag = row.get("is_outgoing")
    if flag is True:
        return True
    if flag is False:
        return False
    sender = (row.get("sender") or "").strip()
    raw = (row.get("sender_raw") or "").strip()
    return sender in self_names or raw in self_names


def scan_pii(text):
    hits = []
    if PHONE_RE.search(text or ""):
        hits.append("手机号")
    if IDCARD_RE.search(text or ""):
        hits.append("证件号")
    if EMAIL_RE.search(text or ""):
        hits.append("邮箱")
    if BANK_RE.search(text or ""):
        hits.append("银行卡")
    return hits


def check(ledger, window_index, window_by_conv, until, args, rep):
    if not isinstance(ledger, dict):
        rep.error("E_SCHEMA", "ledger", "台账顶层不是对象")
        return
    if ledger.get("schema_version") != 1:
        rep.error("E_SCHEMA", "ledger", "schema_version 必须为 1")
    items = ledger.get("items")
    if not isinstance(items, list):
        rep.error("E_SCHEMA", "ledger.items", "items 必须是数组")
        return
    if not items:
        rep.warn("W_EMPTY", "ledger.items", "台账为空，第一次跑可忽略")
        return

    seen_ids = set()
    seen_titles = {}

    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            rep.error("E_SCHEMA", "items[{}]".format(pos), "条目不是对象")
            continue
        item_id = str(item.get("item_id") or "")
        where = item_id or "items[{}]".format(pos)

        # 标识
        if not ITEM_ID_RE.match(item_id):
            rep.error("E_ID_FORMAT", where, "item_id 须为 <key前6位>-<yyyymmdd>-<两位序号>")
        if item_id in seen_ids:
            rep.error("E_ID_DUP", where, "item_id 重复")
        seen_ids.add(item_id)

        # 基础字段
        client = str(item.get("client") or "").strip()
        if not client:
            rep.error("E_CLIENT_EMPTY", where, "client 不能为空")

        title = str(item.get("title") or "").strip()
        if not title:
            rep.error("E_TITLE_EMPTY", where, "title 不能为空")
        elif len(title) > TITLE_MAX:
            rep.error("E_TITLE_LEN", where, "title {} 字，超过 {} 字上限".format(len(title), TITLE_MAX))

        stage = item.get("stage")
        status = item.get("status")
        if stage not in STAGES:
            rep.error("E_STAGE_ENUM", where, "stage={} 不在枚举内".format(stage))
        if status not in STATUSES:
            rep.error("E_STATUS_ENUM", where, "status={} 不在枚举内".format(status))

        if status == "blocked" and not str(item.get("blocked_by") or "").strip():
            rep.error("E_BLOCKED_BY", where, "status=blocked 必须填 blocked_by")
        if status == "active" and not str(item.get("next_action") or "").strip():
            rep.warn("W_NEXT_ACTION", where, "active 但没有 next_action，等于没在管")

        # 日期
        opened = parse_date(item.get("opened_at"))
        updated = parse_date(item.get("last_update_at"))
        if not opened:
            rep.error("E_DATE_FORMAT", where, "opened_at 缺失或格式不是 YYYY-MM-DD")
        if not updated:
            rep.error("E_DATE_FORMAT", where, "last_update_at 缺失或格式不是 YYYY-MM-DD")
        if opened and updated and updated < opened:
            rep.error("E_DATE_ORDER", where, "last_update_at 早于 opened_at")
        if updated and updated > until:
            rep.error("E_DATE_FUTURE", where, "last_update_at={} 晚于 --until={}".format(updated, until))

        if status in ("done", "lost"):
            closed = parse_date(item.get("closed_at"))
            if not closed:
                rep.error("E_CLOSED_AT", where, "status={} 必须填 closed_at".format(status))

        # 证据
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            rep.error("E_NO_EVIDENCE", where, "至少需要一条 evidence")
            evidence = []

        for ei, ev in enumerate(evidence):
            ew = "{}#ev{}".format(where, ei)
            if not isinstance(ev, dict):
                rep.error("E_EVIDENCE_SHAPE", ew, "evidence 条目不是对象")
                continue
            msg_id = str(ev.get("msg_id") or "")
            if not MSG_ID_RE.match(msg_id):
                rep.error("E_EVIDENCE_ID", ew, "msg_id 须为 32 位十六进制 record_id")
            elif window_index and msg_id not in window_index:
                if args.strict_window:
                    rep.error("E_EVIDENCE_OUTSIDE", ew, "msg_id 不在本窗口消息里")
                else:
                    rep.warn("W_EVIDENCE_OUTSIDE", ew,
                             "msg_id 不在本窗口（历史证据可忽略；新证据必须在本窗口）")
            quote = str(ev.get("quote") or "")
            if not quote:
                rep.warn("W_QUOTE_EMPTY", ew, "evidence 没有 quote，报告里无法引用")
            elif len(quote) > QUOTE_MAX:
                rep.warn("W_QUOTE_LEN", ew, "quote {} 字，超过 {} 字".format(len(quote), QUOTE_MAX))
            hits = scan_pii(quote)
            if hits:
                rep.error("E_PII", ew, "quote 里有{}，必须打码".format("/".join(hits)))
            if not parse_date(ev.get("at")):
                rep.warn("W_EVIDENCE_TIME", ew, "evidence.at 缺失或格式不是 YYYY-MM-DD…")

        # 金额
        amount = item.get("amount")
        if amount is not None:
            if not isinstance(amount, dict):
                rep.error("E_AMOUNT_SHAPE", where, "amount 必须是对象")
            else:
                if amount.get("value") in (None, ""):
                    rep.error("E_AMOUNT_FIELDS", where, "amount.value 缺失")
                if not amount.get("currency"):
                    rep.error("E_AMOUNT_FIELDS", where, "amount.currency 缺失（默认 CNY 也要显式写）")
                if not str(amount.get("source") or "").strip():
                    rep.warn("W_AMOUNT_SOURCE", where, "amount 缺 source，无法看出数字从哪来")
                ev_id = str(amount.get("evidence_msg_id") or "")
                if not MSG_ID_RE.match(ev_id):
                    rep.error("E_AMOUNT_FIELDS", where, "amount.evidence_msg_id 缺失或格式错")
                elif window_index:
                    src = window_index.get(ev_id)
                    if not src:
                        rep.error("E_AMOUNT_NOT_GROUNDED", where,
                                  "金额出处 msg_id 不在本窗口，无法核对原文")
                    else:
                        body = normalized(src.get("body"))
                        variants = {normalized(v) for v in amount_variants(amount.get("value"))}
                        if variants and not any(v and v in body for v in variants):
                            rep.error("E_AMOUNT_NOT_GROUNDED", where,
                                      "原文里找不到金额 {}（试过 {} 种写法）".format(
                                          amount.get("value"), len(variants)))

        # 状态历史
        history = item.get("status_history")
        if not isinstance(history, list) or not history:
            rep.error("E_HISTORY_EMPTY", where, "status_history 至少一条")
            history = []
        prev_date = None
        for hi, hop in enumerate(history):
            hw = "{}#sh{}".format(where, hi)
            if not isinstance(hop, dict):
                rep.error("E_HISTORY_SHAPE", hw, "status_history 条目不是对象")
                continue
            at = parse_date(hop.get("at"))
            if not at:
                rep.error("E_HISTORY_DATE", hw, "at 缺失或格式不是 YYYY-MM-DD")
                continue
            if prev_date and at < prev_date:
                rep.error("E_HISTORY_ORDER", hw, "status_history 日期倒流（{} < {}）".format(at, prev_date))
            prev_date = at
            if hop.get("to") not in STAGES:
                rep.error("E_HISTORY_STAGE", hw, "to={} 不在 stage 枚举内".format(hop.get("to")))
        if history and stage in STAGES and history[-1].get("to") != stage:
            rep.error("E_HISTORY_TAIL", where,
                      "status_history 最后一条 to={} 与当前 stage={} 不一致".format(
                          history[-1].get("to"), stage))

        # 文本快扫
        for field in ("title", "next_action"):
            hits = scan_pii(str(item.get(field) or ""))
            if hits:
                rep.error("E_PII", where, "{} 里有{}".format(field, "/".join(hits)))

        # 同客户活跃项重名
        key = (client, status)
        if status == "active":
            seen_titles.setdefault(client, []).append(title)

    for client, titles in seen_titles.items():
        dup = {t for t in titles if titles.count(t) > 1 and t}
        for title in sorted(dup):
            rep.warn("W_DUP_TITLE", client, "同一客户有多个 active 项标题相同：{}".format(title))


def suggest(items, until, args, rep, window_by_conv):
    silence = args.silence_days
    today = until
    self_names = parse_self_names(args.self_names)
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id") or "?")
        client = str(item.get("client") or "?")
        where = "{} {}".format(item_id, client)
        if item.get("status") != "active":
            continue
        updated = parse_date(item.get("last_update_at"))
        due = parse_date(item.get("next_action_due"))
        gap = (today - updated).days if updated else None

        if gap is not None and gap > silence:
            rep.suggest("S_STALLED", where,
                        "已 {} 天无更新（阈值 {}），考虑进风险段或确认是否搁置".format(gap, silence))
        if gap is not None and gap > silence * 2:
            rep.suggest("S_VANISHED", where,
                        "已 {} 天无更新（>2×{}），人工确认是丢了还是搁置后改 status".format(gap, silence))
        if due and due < today:
            rep.suggest("S_OVERDUE", where,
                        "next_action_due={} 已过期".format(due))

        evidence = item.get("evidence") or []
        if evidence and isinstance(evidence[-1], dict):
            quote = str(evidence[-1].get("quote") or "")
            at = str(evidence[-1].get("at") or "")
            if any(w in quote for w in PROMISE_WORDS) and window_by_conv:
                key = item.get("conversation_key")
                rows = window_by_conv.get(key) or []
                after = [r for r in rows if str(r.get("sent_at") or "") > at]
                mine = [r for r in after if is_mine(r, self_names)]
                if not mine:
                    rep.suggest("S_STALE_PROMISE", where,
                                "最后一条证据是我方承诺「{}」，之后没有我方消息".format(
                                    quote[:24]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验事项台账")
    ap.add_argument("--ledger", required=True, help="台账 JSON 路径")
    ap.add_argument("--window", help="window.msgs.jsonl，用于核对证据与金额原文")
    ap.add_argument("--until", help="报告截止日 YYYY-MM-DD，默认今天")
    ap.add_argument("--silence-days", type=int, default=7, help="停滞阈值，默认 7")
    ap.add_argument("--self-names", default="我",
                    help="本人的发送者名，逗号分隔；优先用 is_outgoing 判定")
    ap.add_argument("--strict-window", action="store_true",
                    help="证据必须在本窗口内（默认只告警，因为历史证据会在旧窗口）")
    ap.add_argument("--suggest", action="store_true", help="输出派生状态建议")
    ap.add_argument("--json", help="把结果另存为 JSON")
    args = ap.parse_args(argv)

    ledger_path = os.path.abspath(os.path.expanduser(args.ledger))
    if not os.path.isfile(ledger_path):
        sys.stderr.write("check_ledger: 找不到台账 {}\n".format(ledger_path))
        return 2
    try:
        with open(ledger_path, encoding="utf-8") as f:
            ledger = json.load(f)
    except ValueError as exc:
        sys.stderr.write("check_ledger: 台账不是合法 JSON：{}\n".format(exc))
        return 2

    until = parse_date(args.until) or datetime.now().date()
    window_index, window_by_conv = load_window(args.window)

    rep = Report()
    check(ledger, window_index, window_by_conv, until, args, rep)
    if args.suggest:
        suggest(ledger.get("items") or [], until, args, rep, window_by_conv)

    rep.print_all()

    print("")
    print("台账：{}".format(ledger_path))
    print("条目：{} ｜ ERROR {} ｜ WARN {} ｜ SUGGEST {}".format(
        len(ledger.get("items") or []), len(rep.errors), len(rep.warns), len(rep.suggests)))
    if not args.window:
        print("提示：未传 --window，跳过证据与金额的原文核对（这一步不能省）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "ledger": ledger_path,
                "until": str(until),
                "errors": rep.errors,
                "warns": rep.warns,
                "suggests": rep.suggests,
            }, f, ensure_ascii=False, indent=2)

    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
