#!/usr/bin/env python3
"""给定时间区间，从已建立的时间线索引里秒级圈出候选会话。

前提：先用 wework_timeline_scan.swift 生成 timeline.json（一次全量约 4~5 分钟），
之后任何区间查询都不再需要滚动列表。

为什么能这么算（列表按「最后活跃时间」倒序）：
  * 任一在 [start, end] 内有消息的会话，其 last_activity 必然 >= start；
  * 反过来，last_activity >= start 的会话，在 [start, 今天] 内一定有活动。
  ⇒ 当 end >= 今天、索引连续且无未解析条目时，`last_activity >= start` 才是精确候选集。
  ⇒ 当 end < 今天（历史区间）时，它只是**超集**：会混进「2025 年没说话、2026 年才活跃」
     的会话，必须进会话看消息才能剔除。这一点必须在交付时说明。
索引未从顶部连续覆盖到查询边界时，状态为 index_incomplete，不得宣称精确。

用法：
    python3 timeline_lookup.py exports/timeline.json --start 2026-01-01
    python3 timeline_lookup.py exports/timeline.json --start 2025-03-01 --end 2025-03-31
    python3 timeline_lookup.py exports/timeline.json --start 2026-09-01 --format csv > s.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from fs_utils import ensure_dir

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from timeline_resolve import resolve  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", help="默认取索引的抓取日（今天）")
    ap.add_argument("--today", help="覆盖今天（YYYY-MM-DD）")
    ap.add_argument("--format", choices=["table", "csv", "json", "config"], default="table")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--order", choices=["newest-first", "oldest-first"], default="newest-first",
                    help="config 里会话的采集顺序；oldest-first＝从最早（最后活跃最旧）的会话开始采")
    ap.add_argument("--max-scrolls", type=int, default=3000,
                    help="config 里的 max_scrolls；整年窗口要比单月回溯得深，默认 3000")
    ap.add_argument("--output", help="json/config 输出文件；不提供时写 stdout")
    args = ap.parse_args()

    payload = json.load(open(args.path, encoding="utf-8"))
    captured = payload.get("captured_at")
    if args.today:
        today = date.fromisoformat(args.today)
    elif captured:
        today = datetime.fromisoformat(captured.replace("Z", "+00:00")).astimezone().date()
    else:
        today = date.today()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else today
    if end < start:
        ap.error("结束日期不能早于开始日期")

    rows = []
    unresolved = []
    for c in payload.get("conversations", []):
        d, _ = resolve(c.get("last_activity"), today)
        index = c.get("order")
        if d is None or not isinstance(index, int):
            unresolved.append({"name": c.get("name"), "index": index, "raw": c.get("last_activity"), "reason": "date_unresolved" if d is None else "index_missing"})
            continue
        rows.append({"name": c["name"], "index": index, "date": d,
                     "raw": c.get("last_activity") or ""})
    rows.sort(key=lambda r: r["date"], reverse=True)   # 与列表同向：越新越靠前

    in_window = [r for r in rows if start <= r["date"] <= end]
    after_start = [r for r in rows if r["date"] >= start]      # 当前区间（end≥今天）的精确候选
    before_start = [r for r in rows if r["date"] < start]

    indexes = sorted(c["order"] for c in payload.get("conversations", []) if isinstance(c.get("order"), int))
    missing_indexes = []
    if indexes:
        present = set(indexes)
        missing_indexes = [i for i in range(indexes[0], indexes[-1] + 1) if i not in present]
    stop_reason = payload.get("stop_reason") or "unknown"
    total_rows = payload.get("total_rows")
    # A loaded row count or old-year row is not an actual list endpoint.
    scanned_to_end = bool(indexes and isinstance(total_rows, int) and total_rows > 0
                          and indexes[-1] == total_rows and payload.get("end_verified") is True
                          and stop_reason == "verified-list-end")
    # 边界扫描：侧栏按最新消息时间从新到旧排列，扫到连续多行明确早于窗口起点年份后，
    # 后续会话不可能有窗口内消息，因此「从 1 连续覆盖到 crossed_at」即足够。
    # 索引必须从 1 开始且无缺口，否则不能宣称候选完整。
    crossed_at = payload.get("crossed_at")
    crossed_start = bool(stop_reason.startswith("crossed-start-") and isinstance(crossed_at, int)
                         and indexes and indexes[0] == 1 and indexes[-1] >= crossed_at
                         and not missing_indexes)
    boundary_sufficient = scanned_to_end or crossed_start
    unindexed_rows = int(payload.get("unindexed_rows") or 0)
    index_complete = bool(indexes and indexes[0] == 1 and not missing_indexes and not unresolved and not unindexed_rows and boundary_sufficient)
    exact = end == today and index_complete
    unknown_candidates = [{"name": r["name"], "index": r["index"], "date": None,
                           "raw": r.get("raw") or ""} for r in unresolved
                          if r.get("name") and isinstance(r.get("index"), int)]
    candidates = after_start + unknown_candidates
    coverage_status = "exact" if exact else ("superset" if end < today and index_complete else "index_incomplete")
    metadata = {
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "captured_at": captured,
        "today": today.isoformat(),
        "coverage_status": coverage_status,
        "scan_stop_reason": stop_reason,
        "end_verified": scanned_to_end,
        "total_entries": len(payload.get("conversations", [])),
        "resolved_entries": len(rows),
        "unresolved_entries": len(unresolved),
        "unresolved_candidates_included": len(unknown_candidates),
        "unindexed_rows": unindexed_rows,
        "missing_index_count": len(missing_indexes),
        "starts_at_top": bool(indexes and indexes[0] == 1),
        "boundary_sufficient": boundary_sufficient,
    }

    selected = candidates[: args.limit] if args.limit else candidates
    metadata["candidate_count"] = len(candidates)
    metadata["selected_count"] = len(selected)
    if len(selected) < len(candidates):
        coverage_status = metadata["coverage_status"] = "limited"

    if args.format == "csv":
        if args.output:
            ensure_dir(Path(args.output).parent)
        handle = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
        w = csv.writer(handle)
        w.writerow(["coverage_status", "unresolved_entries", "last_activity_date", "index", "raw_time", "name"])
        for r in selected:
            w.writerow([coverage_status, len(unresolved), r["date"].isoformat() if r["date"] else "", r["index"], r["raw"], r["name"]])
        if args.output:
            handle.close()
        return 0
    if args.format == "json":
        result = {**metadata, "candidates": [{**r, "date": r["date"].isoformat() if r["date"] else None} for r in selected], "unresolved": unresolved}
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            output = Path(args.output)
            ensure_dir(output.parent)
            output.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.format == "config":
        conversations = []
        for r in selected:
            digest = hashlib.sha1(f"{r['index']}\x1f{r['name']}".encode("utf-8")).hexdigest()[:10]
            d = r["date"]
            # seek_date 必须是侧栏能原样渲染出来的文本：今年 M/D、跨年 YYYY/M/D。
            # 「14:28」「昨天」「星期三」这类相对/时刻文本无法用于日期校准，只能靠序号定位。
            raw_text = (r.get("raw") or "").strip()
            seek = raw_text if re.fullmatch(r"\d{1,4}/\d{1,2}(/\d{1,2})?", raw_text) else None
            conversations.append({
                "name": r["name"], "index": r["index"],
                **({"seek_date": seek} if seek else {}),
                "last_activity": d.isoformat() if d else None,
                # certain：最后活跃就在窗口内，窗口内必有消息
                # verify：最后活跃晚于窗口结束，可能也含窗口内旧消息，必须进会话读到才能确认
                "tier": "unknown" if d is None else ("certain" if start <= d <= end else "verify"),
                "output_prefix": f"chat-{r['index']:04d}-{digest}",
            })
        # 先采确定档，再采待验证档。同档内的次序由 --order 决定：
        #   newest-first（默认）：最后活跃越新越先采，离列表顶部近、序号漂移小
        #   oldest-first：从最早的会话开始采。整年窗口下早期会话回溯浅、出量快，
        #                 先拿确定性收益；需要回溯到年初的深滚会话排在后面。
        newest_first = args.order == "newest-first"
        def age_days(text: str | None) -> int:
            return date.fromisoformat(text).toordinal() if text else 0
        conversations.sort(key=lambda c: ({"certain": 0, "verify": 1, "unknown": 2}[c["tier"]],
                                          -age_days(c["last_activity"]) if newest_first else age_days(c["last_activity"])))
        result = {
            "candidate_source": "timeline", "candidate_coverage": metadata,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat(),
                       "rule": "候选 = last_activity >= window.start，加上日期未知但有名称和序号的会话；verify/unknown 需进入会话核验"},
            "candidate_tiers": dict(Counter(c["tier"] for c in conversations)),
            "candidate_order": args.order,
            "output_dir": "exports", "scroll_delta": 120, "max_scrolls": args.max_scrolls,
            "idle_limit": 6, "checkpoint_every": 10, "settle_ms": 800,
            "continue_on_error": True, "conversations": conversations,
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            output = Path(args.output)
            ensure_dir(output.parent)
            output.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0

    print(f"索引 {args.path}（抓取于 {captured}，基准今天 {today}）")
    print(f"索引总条目 {len(payload.get('conversations', []))}，可解析 {len(rows)}，未解析 {len(unresolved)}")
    print(f"查询区间 [{start} ~ {end}]  覆盖状态：{coverage_status}")
    print()
    print(f"候选会话（last_activity >= {start}）：{len(candidates)} 个")
    print(f"其中 last_activity 恰好落在区间内：{len(in_window)} 个")
    if before_start:
        print(f"可直接跳过（last_activity < {start}）：{len(before_start)} 个")
    if candidates:
        candidate_indexes = [r["index"] for r in candidates if isinstance(r.get("index"), int)]
        if candidate_indexes:
            print(f"候选在列表里的位置：idx {min(candidate_indexes)} ~ {max(candidate_indexes)}")
        print("\n按月分布：")
        for m, n in sorted(Counter(r["date"].strftime("%Y-%m") if r["date"] else "未知" for r in candidates).items()):
            print(f"  {m}  {n}")
    print("\n最新 10 个候选：")
    for r in candidates[:10]:
        print(f"  {r['date']}  idx={r['index']:<5} {r['name'][:52]}")
    print("\n最旧 10 个候选（离边界最近，进会话时要滚最久）：")
    for r in candidates[-10:]:
        print(f"  {r['date']}  idx={r['index']:<5} {r['name'][:52]}")
    if coverage_status == "superset":
        print(f"\n⚠️ end={end} 早于今天 {today}：以上只是超集。"
              f"last_activity 落在 {end} 之后、但之前也活跃过的会话会混进来，"
              f"必须进入会话读到 {end} 之前的消息才能确认。")
    elif coverage_status == "index_incomplete":
        print(f"\n⚠️ 索引不完整：未解析 {len(unresolved)}，缺失 AXIndex {len(missing_indexes)}，"
              f"停止原因 {stop_reason}；不得把候选集称为精确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
