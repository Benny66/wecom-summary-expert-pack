#!/usr/bin/env python3
"""Detect and link conversations whose displayed name changed. Never touches WeCom.

WeCom exposes no stable conversation id, so the repository identifies a conversation by its
displayed name. That name changes when a group is renamed, when members join or leave an
auto-named group ("a、b、c" → "a、b、c、d"), or when a departed member is masked ("林俊达" →
"**达"). Messages that already exist do not change, so identity evidence is ranked:

* 强: the two conversations' captured snapshots share consecutive identical messages
  (time + content; sender is ignored because its rendering can be masked later).
* 中: the name changed in a way consistent with a rename or membership change, and the
  new name first appeared in the list scan right after the old one disappeared. Several
  groups can share the same members, so this alone is never treated as proof.

``suggest`` lists candidates with evidence; nothing is linked until the user confirms.
``apply`` links confirmed pairs: the old key keeps the identity, the new name is appended,
the new key is marked ``merged_into``. Messages are merged only with a verified overlap;
otherwise the old key keeps its data and is re-captured from the repository start.
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

from chat_merge import merge

SUGGESTION_FIELDS = ["confirm", "confidence", "evidence", "old_key", "old_name", "old_last_seen_run",
                     "old_messages", "new_key", "new_name", "new_first_seen_run", "new_messages"]
CONFIRM_WORDS = {"是", "y", "yes", "1", "true", "确认", "√"}
MIN_SHARED_RUN = 2
MIN_MEMBER_OVERLAP = 0.6


def active(entry: dict) -> bool:
    return not entry.get("merged_into")


def _token_equal(a: str, b: str) -> bool:
    """Equal, or equal treating ``*`` as one masked character (``**达`` vs ``林俊达``).

    A fully masked token carries no evidence and never matches.
    """
    if a == b:
        return True
    if len(a) != len(b) or ("*" not in a and "*" not in b) or not a.strip("*") or not b.strip("*"):
        return False
    return all(x == y or x == "*" or y == "*" for x, y in zip(a, b))


def _members(name: str) -> list[str]:
    return [t.strip() for t in name.split("、") if t.strip()]


def member_change(old: str, new: str) -> tuple[int, int, int] | None:
    """For auto-named groups ("a、b、c"): (kept, joined, left) members, mask-aware. None if not member lists."""
    a, b = _members(old), _members(new)
    if len(a) < 2 or len(b) < 2:
        return None
    used, kept = set(), 0
    for x in a:
        for i, y in enumerate(b):
            if i not in used and _token_equal(x, y):
                used.add(i)
                kept += 1
                break
    return kept, len(b) - kept, len(a) - kept


def member_similarity(old: str, new: str) -> tuple[float, bool]:
    """(jaccard, same member set). Non-list names compare as single tokens."""
    change = member_change(old, new)
    if change is None:
        return (1.0, True) if _token_equal(old, new) else (0.0, False)
    kept, joined, left = change
    return kept / (kept + joined + left), joined == left == 0


def content_key(record: dict) -> tuple:
    """Message identity that survives sender re-rendering (masking) and conversation renames."""
    return (record.get("sent_at") or "", record.get("body_raw") or record.get("body") or "",
            record.get("message_type") or "")


def _fingerprints(rows: list[dict]) -> list[tuple]:
    return [content_key(r) for r in rows if r.get("sent_at") and (r.get("body_raw") or r.get("body") or "").strip()]


def shared_run(old_rows: list[dict], new_rows: list[dict]) -> int:
    """Longest run of consecutive messages present in both, counted only if it has ≥2 distinct messages."""
    a, b = _fingerprints(old_rows), _fingerprints(new_rows)
    positions: dict[tuple, list[int]] = {}
    for j, item in enumerate(b):
        positions.setdefault(item, []).append(j)
    best = 0
    run_at: dict[int, int] = {}
    for i, item in enumerate(a):
        current: dict[int, int] = {}
        for j in positions.get(item, ()):
            length = run_at.get(j - 1, 0) + 1
            current[j] = length
            if length > best and len(set(a[i - length + 1:i + 1])) >= 2:
                best = length
        run_at = current
    return best


def _snapshot_rows(store: Path, entry: dict) -> list[dict]:
    if not entry.get("snapshot"):
        return []
    data = (store / entry["snapshot"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.get("snapshot_sha256"):
        raise ValueError(f"会话 {entry['key']} 的快照校验失败")
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def discovery_history(store: Path) -> list[tuple[str, set[str]]]:
    history = []
    for path in sorted(glob.glob(str(store / "discovery" / "*" / "chat-config.json"))):
        try:
            names = {c.get("name") for c in json.loads(Path(path).read_text(encoding="utf-8")).get("conversations") or []}
        except (OSError, ValueError):
            continue
        history.append((Path(path).parent.name, {n for n in names if n}))
    return history


def _name_evidence(old: str, new: str) -> str | None:
    change = member_change(old, new)
    if change is None:
        return None
    kept, joined, left = change
    total = kept + joined + left
    if not kept or kept / total < MIN_MEMBER_OVERLAP:
        return None
    if joined == left == 0:
        return "群名成员一致（仅 ** 遮挡不同）"
    return f"群名成员保留 {kept} 人，新增 {joined} 人，减少 {left} 人"


def suggest(store: Path, manifest: dict) -> list[dict]:
    history = discovery_history(store)
    if not history:
        return []
    runs = [run for run, _ in history]
    latest = history[-1][1]
    entries = [e for e in manifest["conversations"].values() if active(e)]
    seen = {e["key"]: [i for i, (_, names) in enumerate(history) if any(n in names for n in e["names"])]
            for e in entries}
    rows_cache: dict[str, list[dict]] = {}

    def rows_of(entry: dict) -> list[dict]:
        if entry["key"] not in rows_cache:
            rows_cache[entry["key"]] = _snapshot_rows(store, entry)
        return rows_cache[entry["key"]]

    vanished = [e for e in entries if seen[e["key"]] and not any(n in latest for n in e["names"])]
    out = []
    for old in vanished:
        last = seen[old["key"]][-1]
        old_rows = rows_of(old)
        base = {"old_key": old["key"], "old_name": old["names"][-1], "old_last_seen_run": runs[last],
                "old_messages": len(old_rows)}
        found = []
        for new in entries:
            first = seen[new["key"]]
            if new is old or not first or first[0] <= last:
                continue
            new_rows = rows_of(new)
            run = shared_run(old_rows, new_rows) if old_rows and new_rows else 0
            name_note = _name_evidence(old["names"][-1], new["names"][-1])
            if run < MIN_SHARED_RUN and not name_note:
                continue
            evidence = []
            if run >= MIN_SHARED_RUN:
                evidence.append(f"已采消息中有 {run} 条连续相同（时间+内容）")
            if name_note:
                evidence.append(name_note)
            if first[0] == last + 1:
                evidence.append("旧名消失后的下一次列表扫描出现新名")
            if old.get("last_activity") and old.get("last_activity") == new.get("last_activity"):
                evidence.append(f"侧栏最后活跃同为 {old['last_activity']}")
            if not old_rows:
                evidence.append("旧会话没有已采消息，关联不改动任何消息")
            elif not new_rows:
                evidence.append("新会话尚未采集；采到后重跑本命令可用消息核验")
            strong = run >= MIN_SHARED_RUN
            found.append((strong, len(evidence), new, evidence, len(new_rows)))
        found.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if not found:
            out.append({**base, "confidence": "无候选",
                        "evidence": "旧名已从列表消失，未找到名字或消息相符的新会话（可能被删除、退出，或改成了完全不同的名字且两边都还没有可比对的消息）"})
            continue
        for strong, _, new, evidence, count in found[:3]:
            out.append({**base, "confidence": "强" if strong else "中", "evidence": "；".join(evidence),
                        "new_key": new["key"], "new_name": new["names"][-1],
                        "new_first_seen_run": runs[seen[new["key"]][0]], "new_messages": count})
    return out


def write_suggestions(path: Path, rows: list[dict]) -> None:
    """Write suggestions, keeping confirm marks the user already entered for the same pair."""
    previous = {}
    if path.exists():
        try:
            for row in csv.DictReader(_decode(path.read_bytes()).splitlines()):
                if (row.get("confirm") or "").strip():
                    previous[(row.get("old_key"), row.get("new_key"))] = row["confirm"]
        except ValueError:
            pass
    for row in rows:
        row["confirm"] = previous.get((row.get("old_key"), row.get("new_key")), row.get("confirm", ""))
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUGGESTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别确认文件的编码")


def read_confirmed(path: Path) -> list[tuple[str, str]]:
    text = _decode(path.read_bytes())
    if path.suffix.lower() == ".json":
        return [(new, old) for new, old in json.loads(text).items()]
    pairs = []
    for row in csv.DictReader(text.splitlines()):
        if (row.get("confirm") or "").strip().lower() in CONFIRM_WORDS and row.get("new_key"):
            pairs.append((row["new_key"].strip(), row["old_key"].strip()))
    return pairs


def apply(store: Path, manifest: dict, pairs: list[tuple[str, str]], publish) -> list[dict]:
    """Link confirmed (new_key, old_key) pairs. ``publish(entry, rows, tag)`` writes a snapshot."""
    conversations = manifest["conversations"]
    results = []
    for new_key, old_key in pairs:
        # Successive renames (a → b → c) are confirmed pair by pair; always link into the surviving root.
        seen_keys = set()
        while old_key in conversations and conversations[old_key].get("merged_into") and old_key not in seen_keys:
            seen_keys.add(old_key)
            old_key = conversations[old_key]["merged_into"]
        old, new = conversations.get(old_key), conversations.get(new_key)
        if not old or not new or old is new:
            results.append({"old_key": old_key, "new_key": new_key, "result": "skipped", "reason": "key 不存在或相同"})
            continue
        if not active(old) or not active(new):
            results.append({"old_key": old_key, "new_key": new_key, "result": "skipped", "reason": "已合并过"})
            continue
        old_rows, new_rows = _snapshot_rows(store, old), _snapshot_rows(store, new)
        if not new_rows:
            rows, data = None, "kept_old"
        elif not old_rows:
            rows, data = new_rows, "adopted_new"
        else:
            # Same messages may now show a masked sender; align on time+content and keep the earlier rendering.
            merged, stats = merge(old_rows, new_rows, key=lambda r: repr(content_key(r)), prefer_previous=True)
            rows, data = (merged, "merged") if stats["alignment"] == "found" else (None, "overlap_not_verified")
        old_names = list(old["names"])
        old["names"] += [n for n in new["names"] if n not in old["names"]]
        if data == "adopted_new":
            for field in ("history_complete", "checked_through", "latest_message_at", "last_status",
                          "last_run", "last_alignment", "last_scroll_count"):
                old[field] = new.get(field)
        elif data == "merged":
            old["history_complete"] = bool(old.get("history_complete") or new.get("history_complete"))
            old["checked_through"] = max(filter(None, [old.get("checked_through"), new.get("checked_through")]), default=None)
        elif data == "overlap_not_verified":
            old["history_complete"] = False
            old["checked_through"] = None
        if rows is not None:
            publish(old, rows, "rename")
        old["last_activity"] = new.get("last_activity") or old.get("last_activity")
        old["open_failures"] = 0
        new["merged_into"] = old_key
        new["merged_at"] = datetime.now().astimezone().isoformat()
        record = {"old_key": old_key, "new_key": new_key, "old_names": old_names, "new_names": list(new["names"]),
                  "at": new["merged_at"], "data": data}
        manifest.setdefault("renames", []).append(record)
        results.append({**record, "result": "linked"})
    return results
