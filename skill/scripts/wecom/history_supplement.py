#!/usr/bin/env python3
"""用两个唯一正文锚点之间的一一对应行补证；不覆盖记录面板已知字段。"""
from __future__ import annotations

import copy
import re
from collections import Counter

from chat_export import infer_datetimes, peer_name, reference_datetime


def supplement(primary: dict, main: dict, conversation: str, conversation_type: str) -> dict:
    result = copy.deepcopy(primary)
    rows = result.get("records") or []
    evidence = main.get("records") or []
    body = lambda r: r.get("body_raw", r.get("body", ""))
    counts_a, counts_b = (Counter(body(r) for r in items) for items in (rows, evidence))
    anchors = {}
    if not main.get("sequence_gaps"):
        index_b = {body(r): i for i, r in enumerate(evidence) if counts_b[body(r)] == 1}
        anchors = {i: index_b[body(r)] for i, r in enumerate(rows)
                   if len(body(r)) >= 4 and counts_a[body(r)] == counts_b[body(r)] == 1}
    dates = infer_datetimes(evidence, reference_datetime(main))
    repaired = 0
    for i, row in enumerate(rows):
        if not row.get("unresolved_fields"):
            continue
        left = max((j for j in anchors if j < i), default=None)
        right = min((j for j in anchors if j > i), default=None)
        if left is None or right is None or right - left != anchors[right] - anchors[left]:
            continue
        if any(body(rows[k]) and body(rows[k]) != body(evidence[anchors[left] + k - left])
               for k in range(left + 1, right)):
            continue
        j = anchors[left] + i - left
        candidate = evidence[j]
        changed = []
        if not row.get("time_text") and dates[j] is not None:
            row["time_text"] = dates[j].strftime("%Y/%m/%d %H:%M")
            row["time_source"] = "main_view_context_supplement"
            row["time_inferred"] = True
            changed.append("time_text")
        if not row.get("sender"):
            outgoing = candidate.get("is_outgoing")
            sender = candidate.get("sender")
            if outgoing is True:
                sender = "我"
            elif outgoing is False and conversation_type == "single":
                sender = peer_name(conversation)
            if sender:
                row["sender"] = sender
                row["is_outgoing"] = outgoing
                row["direction_evidence"] = "main_view_supplement"
                changed.append("sender")
        if not row.get("body_raw") and candidate.get("body") and not re.fullmatch(r"\d{1,2}:\d{2}", candidate["body"]):
            row["body_raw"] = candidate["body"]
            row["body"] = candidate["body"].replace("\ufffc", "[内嵌表情/附件：未识别]")
            changed.append("body")
        row["unresolved_fields"] = [field for field in ("sender", "time_text") if not row.get(field)]
        if changed:
            row["supplemented_fields"] = changed
            row["supplement_anchor_indices"] = [left, right]
            repaired += 1
    result["unresolved_rows"] = [{"record_index": i, "fields": row.get("unresolved_fields")}
                                 for i, row in enumerate(rows) if row.get("unresolved_fields")]
    result["main_view_supplement"] = {"repaired_rows": repaired,
                                      "evidence_records": len(evidence),
                                      "evidence_sequence_gaps": main.get("sequence_gaps"),
                                      "unresolved_remaining": len(result["unresolved_rows"])}
    result["coverage_complete"] = bool(result.get("boundary_verified") and not result.get("sequence_gaps") and not result["unresolved_rows"])
    return result
