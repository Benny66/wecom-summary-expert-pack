#!/usr/bin/env python3
"""把会话列表的「最后聊天时间」文本统一成日期，并输出分布与时间线。

侧栏时间文本有五种形态，含义不同，不能直接字符串排序：
    10:08          今天
    昨天 / 前天     相对今天
    星期三 / 周三   最近 7 天内（今天显示为 HH:MM，所以星期X 一定是 1~6 天前）
    1/4            今年内（只有斜杠，没有「月」「日」，最容易漏）
    10月23日        部分版本写法，同样是今年内
    2025/12/25     绝对日期，带年份

用法：
    python3 timeline_resolve.py exports/timeline-2026.json --format csv
    python3 timeline_resolve.py exports/timeline-2026.json --group month
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta

WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def resolve(text: str | None, today: date):
    """返回 (date|None, precision)。precision: day / minute。"""
    if not text:
        return None, None
    t = text.strip()

    if t in ("今天",):
        return today, "day"
    if t == "昨天":
        return today - timedelta(days=1), "day"
    if t == "前天":
        return today - timedelta(days=2), "day"

    # 星期三 / 周三
    if len(t) in (2, 3) and t[0] in "星期周" and t[-1] in WEEKDAYS:
        target = WEEKDAYS[t[-1]]
        back = (today.weekday() - target) % 7
        if back == 0:          # 理论上今天会显示 HH:MM，兜底按 7 天前
            back = 7
        return today - timedelta(days=back), "day"

    # HH:MM
    if ":" in t and "/" not in t:
        hh, _, mm = t.partition(":")
        if hh.isdigit() and mm.isdigit():
            return today, "minute"

    # 2025/12/25
    if "/" in t:
        parts = t.split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            y, m, d = (int(x) for x in parts)
            if len(parts[0]) == 2:
                y += 2000
            try:
                return date(y, m, d), "day"
            except ValueError:
                return None, None
        # 1/4 —— 客户端「今年内」的写法，只有斜杠，没有年月日字样
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            m, d = int(parts[0]), int(parts[1])
            try:
                cand = date(today.year, m, d)
            except ValueError:
                return None, None
            if cand > today:          # 晚于今天 => 去年
                try:
                    cand = date(today.year - 1, m, d)
                except ValueError:
                    return None, None
            return cand, "day"

    # 10月23日
    if "月" in t and "日" in t:
        try:
            m, d = t.replace("日", "").split("月")
            m, d = int(m), int(d)
            cand = date(today.year, m, d)
            if cand > today:
                cand = date(today.year - 1, m, d)
            return cand, "day"
        except ValueError:
            return None, None

    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--today", help="覆盖今天（YYYY-MM-DD），默认取系统日期")
    ap.add_argument("--format", choices=["table", "csv", "json"], default="table")
    ap.add_argument("--group", choices=["month", "year"], default="month")
    ap.add_argument("--limit", type=int, default=0, help="只输出前 N 条")
    args = ap.parse_args()

    payload = json.load(open(args.path, encoding="utf-8"))
    captured = payload.get("captured_at")
    if args.today:
        today = date.fromisoformat(args.today)
    elif captured:
        today = datetime.fromisoformat(captured.replace("Z", "+00:00")).astimezone().date()
    else:
        today = date.today()

    rows = []
    unresolved = []
    for c in payload.get("conversations", []):
        d, precision = resolve(c.get("last_activity"), today)
        if d is None:
            unresolved.append(c)
        rows.append({
            "name": c["name"],
            "last_activity": c.get("last_activity"),
            "date": d.isoformat() if d else "",
            "year": d.year if d else "",
            "month": d.strftime("%Y-%m") if d else "",
            "precision": precision or "",
            "_d": d,
        })

    resolved = [r for r in rows if r["_d"]]
    resolved.sort(key=lambda r: r["_d"])

    counts = Counter(r["month"] for r in resolved)
    years = Counter(r["year"] for r in resolved)

    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(["resolved", "date", "year", "month", "precision", "last_activity", "name"])
        selected = rows if not args.limit else rows[: args.limit]
        for r in selected:
            w.writerow([bool(r["_d"]), r["date"], r["year"], r["month"], r["precision"], r["last_activity"], r["name"]])
        return 0
    if args.format == "json":
        selected = rows if not args.limit else rows[: args.limit]
        json.dump({
            "total_entries": len(rows), "resolved_entries": len(resolved),
            "unresolved_entries": len(unresolved),
            "entries": [{k: v for k, v in r.items() if k != "_d"} for r in selected],
        }, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    print(f"基准今天 = {today}（{payload.get('captured_at')}）")
    print(f"总数 {len(rows)}，可解析 {len(resolved)}，未解析 {len(unresolved)}")
    print("\n年份分布：")
    for y, n in sorted(years.items()):
        print(f"  {y}  {n}")
    print(f"\n按月分布（{args.group}）：")
    for m, n in sorted(counts.items()):
        print(f"  {m}  {n}")
    print("\n最早 15 条：")
    for r in resolved[: 15]:
        print(f"  {r['date']}  {r['last_activity']:<10}  {r['name'][:50]}")
    print("\n最晚 15 条：")
    for r in resolved[-15:]:
        print(f"  {r['date']}  {r['last_activity']:<10}  {r['name'][:50]}")
    if unresolved:
        print(f"\n未解析 {len(unresolved)} 条（时间文本为空或形态未覆盖），前 10：")
        for c in unresolved[:10]:
            print(f"  {c.get('last_activity')!r}  {c['name'][:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
