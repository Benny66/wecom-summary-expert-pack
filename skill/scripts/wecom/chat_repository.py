#!/usr/bin/env python3
"""Keep repeated WeCom capture rounds in one recoverable, append-only repository."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time as systime
import uuid
import ui_lock
from datetime import date, datetime, time, timedelta
from pathlib import Path

from chat_merge import merge
from chat_preflight import run_preflight
import conversation_rename
from conversation_rename import active
from fs_utils import ensure_dir
from swift_runtime import swift_command

BASE = Path(__file__).resolve().parent
# wework_open_row exit codes that mean "this conversation was not opened":
# 6 no sidebar, 7 not found, 8 identity check failed, 11 date seek failed, 13 re-find failed.
OPEN_FAILURE_EXITS = {6, 7, 8, 11, 13}
# An unknown, undismissable or recurring dialog blocked this conversation (wework_open_row / history_capture).
DIALOG_BLOCKED_EXIT = 25
SKIP_AFTER_DIALOG_BLOCKS = 2
CONSECUTIVE_DIALOG_BLOCK = "连续 3 个会话被同一类弹窗阻塞"
SKIP_AFTER_OPEN_FAILURES = 3
MAX_LOCATE_SCREENS = 20
CONSECUTIVE_OPEN_FAILURE = "连续 3 个会话定位失败"
# A capture child that dies before writing the locate result is a per-conversation fault; only a
# long unbroken streak means the post-processing path itself is broken.
ABORT_AFTER_CAPTURE_FAILURES = 10
# Window geometry / layout moved: coordinates are re-read on every open, so one re-anchored retry
# is enough to tell a transient move (user came back) from a broken layout.
REANCHOR_EXITS = {4, 5, 22}
REANCHOR_RETRIES = 1

_children: dict[int, subprocess.Popen] = {}
_lock_path: Path | None = None
_lock_owner: dict | None = None


def atomic_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def encode_jsonl(rows: list[dict]) -> bytes:
    return ("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)).encode("utf-8")


def load_manifest(store: Path, start: date, account: str) -> dict:
    path = store / "manifest.json"
    if not path.exists():
        return {"schema_version": 1, "start": start.isoformat(), "account_label": account,
                "conversations": {}, "runs": []}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("start") != start.isoformat() or manifest.get("account_label") != account:
        raise ValueError("仓库起点或账号标签不一致；使用原参数或新建仓库")
    return manifest


def reconcile(manifest: dict, candidates: list[dict], *, aliases: dict[str, str] | None = None) -> tuple[list[tuple[dict, dict]], list[dict]]:
    aliases = aliases or {}
    names = [str(item.get("name") or "").strip() for item in candidates]
    selected, review = [], []
    known_names = {name for entry in manifest["conversations"].values() if active(entry) for name in entry["names"]}
    missing_old = sorted(known_names - set(names))
    # Duplicate names never get an identity (reported as duplicate_name below), so they are not "new".
    new_names = sorted({n for n in names if names.count(n) == 1} - known_names - set(aliases))
    if missing_old and new_names:
        review.append({"reason": "possible_rename", "missing_previous_names": missing_old,
                       "new_names": new_names, "note": "新会话仍会采集；运行 --rename-suggestions 查看配对证据，确认后用 --apply-renames 关联"})
    for candidate, name in zip(candidates, names):
        if not name:
            review.append({"name": name, "reason": "missing_name"})
            continue
        if names.count(name) > 1:
            review.append({"name": name, "reason": "duplicate_name"})
            continue
        if name in aliases:
            key = aliases[name]
            if key not in manifest["conversations"]:
                review.append({"name": name, "reason": "alias_target_missing"})
                continue
            entry = manifest["conversations"][key]
            if name not in entry["names"]:
                entry["names"].append(name)
        else:
            matches = [entry for entry in manifest["conversations"].values() if active(entry) and name in entry["names"]]
            if len(matches) > 1:
                review.append({"name": name, "reason": "ambiguous_existing_identity"})
                continue
            if matches:
                entry = matches[0]
            else:
                key = uuid.uuid4().hex
                entry = {"key": key, "names": [name], "history_complete": False,
                         "checked_through": None, "latest_message_at": None,
                         "snapshot": None, "snapshot_sha256": None, "capture_count": 0}
                manifest["conversations"][key] = entry
        entry["last_activity"] = candidate.get("last_activity")
        selected.append((candidate, entry))
    return selected, review


# 单聊右栏标题常取到岗位/部门标签（实测「后台研发三组员工」「Android研发组组长」
# 「TME人力资源部副总经理」「财务平台开发X-Lead」「测试及运维X-Lead」），不是对方姓名。
# 把它登记成别名会让会话名变成岗位名，下一轮按这个名字定位必然失败，最终被 name_unresolved 永久排除。
JOB_TITLE_SUFFIX = re.compile(
    r"(员工|总监|组长|副组长|负责人|主管|成员|经理|助理|工程师|专家|架构师|主任|书记|总裁|董事长|X-Lead)$")


def small_overlap_fallback(previous: list[dict], current: list[dict], covers_previous: bool):
    """消息不足 2 条时 merge 凑不出「≥2 条相邻且不同」的可核验重叠区，
    结果是这些会话每轮都重采、每次都丢弃，永远停在 review。
    这里退化处理：先确认两侧确有同一条消息（排除点错行），再采信更完整的一侧。"""
    if len(previous) > 2 and len(current) > 2:
        return None

    def sig(row: dict):
        return (str(row.get("sent_at") or "")[:19], str(row.get("body") or "")[:40])

    if not (set(map(sig, previous)) & set(map(sig, current))):
        return None
    return (current if covers_previous else previous), "small_overlap"


def is_job_title_alias(current_name: str, opened_name: str) -> bool:
    """单聊（旧名不含顿号）回读到岗位式标题时，判定为误读而非改名。"""
    if not opened_name or "、" in opened_name:
        return False
    if not JOB_TITLE_SUFFIX.search(opened_name):
        return False
    # 群名的当前名通常带顿号（自动拼接的成员列表）；只有 1:1 单聊才需要拦。
    if "、" in (current_name or ""):
        return False
    # 与当前名同源（共同前缀或互相包含）说明是同一会话真改名，不是误读岗位标签。
    a, b = (current_name or "").strip(), opened_name.strip()
    if a and (a[:4] == b[:4] or a in b or b in a):
        return False
    return True


def should_capture(entry: dict, latest_activity: str | None, cutoff: datetime | None = None,
                   window_end: date | None = None, reach_beginning: bool = False) -> bool:
    # 会话的真实起点晚于窗口终点：整个窗口内这个会话还不存在，无需打开。
    # 起点优先取 conversation_start（日历法或人工确认的会话真实起点），它不依赖 reached_start；
    # 否则才退回 reached_start 为真时的 earliest_message（那只是采集窗口下界，不可靠）。
    # review 早退从不写 reached_start，若只认旧条件，这类会话会永远每轮入队空转。
    # 名字反复对不上（改名/离职），每轮都重试只会空转：累计 3 次就停下等人工处理。
    if int(entry.get("name_unresolved") or 0) >= 3:
        return False
    start = entry.get("conversation_start")
    if not start and entry.get("reached_start"):
        start = entry.get("earliest_message")
    if start and window_end:
        if date.fromisoformat(start[:10]) > window_end:
            return False
    # 深度遍历模式：这些会话虽已检查到窗口终点，但没证明翻到会话开头，
    # earliest_message 只是窗口下界。必须重新打开一次才能学到真实起点。
    if reach_beginning and not entry.get("reached_start"):
        return True
    if not entry.get("history_complete") or not entry.get("checked_through"):
        return True
    # Already checked up to this round's cutoff: later messages lie outside the requested window.
    if cutoff is not None:
        checked = datetime.fromisoformat(entry["checked_through"])
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=cutoff.tzinfo)
        if checked >= cutoff:
            return False
    if not latest_activity:
        return True
    # Same-day activity may have arrived after the earlier run.
    return date.fromisoformat(latest_activity[:10]) >= date.fromisoformat(entry["checked_through"][:10])


# Raw stop reasons that mean the capture was cut short (or still has budget to grow), so another
# attempt can learn more. Any other partial is a complete look at the conversation as it is now.
INTERRUPTED_STOPS = {"in-progress", "blocked-by-dialog", "dialog-stuck", "conversation-changed", "table-lost",
                     "scroll-position-unavailable", "scroll-target-offscreen", "no-scroll-test", "max-scrolls"}
STALL_AFTER_ATTEMPTS = 2


def note_partial_attempt(entry: dict, status: str, open_exit: int | None, raw_stop: str | None,
                         last_activity: str | None) -> None:
    """Count clean partial captures per sidebar last_activity; a new activity date starts over."""
    if status == "ok" or entry.get("history_complete"):
        entry.pop("partial_attempts", None)
        return
    if status == "review":
        # review 是 commit 的早退分支（重叠未核验等），打开其实已成功。
        # 以前只有 partial 计数，导致这类会话每轮重新入队、永远停在 review。
        # 纳入同一停滞计数后，连续两次仍 review 就先跳过，需 --retry-partial 才重新采集。
        if not raw_stop or raw_stop in INTERRUPTED_STOPS:
            return
    elif status != "partial" or open_exit != 0 or not raw_stop or raw_stop in INTERRUPTED_STOPS:
        return
    record = entry.get("partial_attempts") or {}
    if record.get("last_activity") != last_activity:
        record = {"last_activity": last_activity, "count": 0, "stop_reasons": []}
    # 历史条目可能只写了 last_activity（早版本遗留），缺字段时按空值起算
    record["count"] = int(record.get("count") or 0) + 1
    record["stop_reasons"] = (list(record.get("stop_reasons") or []) + [raw_stop])[-STALL_AFTER_ATTEMPTS:]
    entry["partial_attempts"] = record


def is_stalled(entry: dict, last_activity: str | None) -> bool:
    # 无滚动条的「一屏即全部」已确认过一次、还差第二次：必须再打开一次才能定论。
    # 这批会话此前已累积多轮 partial，若按停滞跳过就永远等不到第二次确认。
    if int(entry.get("top_no_bar_checks") or 0) == 1:
        return False
    record = entry.get("partial_attempts") or {}
    return bool(not entry.get("history_complete") and record.get("last_activity") == last_activity
                and int(record.get("count") or 0) >= STALL_AFTER_ATTEMPTS)


def dialog_block_reason(stage: Path, open_exit: int | None, history_exit: int | None) -> str | None:
    """Description of the dialog that blocked this attempt, or None if no dialog blocked it."""
    if DIALOG_BLOCKED_EXIT not in (open_exit, history_exit):
        return None
    reason = "弹窗阻塞（未记录详情）"
    for log in stage.glob("chat-*.log"):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("弹窗阻塞本会话："):
                reason = line.split("：", 1)[1][:300]
    return reason


def note_dialog_block(entry: dict, reason: str, run_id: str) -> None:
    entry["dialog_blocks"] = int(entry.get("dialog_blocks") or 0) + 1
    entry["last_dialog_block"] = {"run": run_id, "reason": reason, "at": datetime.now().astimezone().isoformat()}


def dialog_blocked_list(manifest: dict) -> list[dict]:
    return [{"name": e["names"][-1], "key": e["key"], "dialog_blocks": e.get("dialog_blocks"),
             "last_dialog": (e.get("last_dialog_block") or {}).get("reason"),
             "last_run": (e.get("last_dialog_block") or {}).get("run")}
            for e in manifest.get("conversations", {}).values()
            if not e.get("merged_into") and int(e.get("dialog_blocks") or 0) >= SKIP_AFTER_DIALOG_BLOCKS]


def schedule_dialog_blocks(planned: list[tuple[dict, dict]], retry: str | None) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Skip conversations blocked by a dialog in ≥2 attempts until a human re-includes them."""
    retry_keys = None if retry in (None, "all") else {k.strip() for k in retry.split(",") if k.strip()}
    run, skipped = [], []
    for candidate, entry in planned:
        blocked = int(entry.get("dialog_blocks") or 0) >= SKIP_AFTER_DIALOG_BLOCKS
        if blocked and retry is not None and (retry_keys is None or entry["key"] in retry_keys
                                              or any(entry["key"].startswith(k) for k in retry_keys)):
            entry["dialog_blocks"] = 0
            blocked = False
        if blocked:
            skipped.append({"name": entry["names"][-1], "key": entry["key"], "reason": "dialog_blocked",
                            "dialog_blocks": entry["dialog_blocks"],
                            "last_dialog": (entry.get("last_dialog_block") or {}).get("reason"),
                            "note": "已在 2 次采集中被弹窗阻塞，本轮跳过；人工确认后用 --retry-dialog-blocked 重新纳入"})
        else:
            run.append((candidate, entry))
    return run, skipped


# 会话历史的下界：企业微信里最早的会话到 2017 年 8 月，绝大多数群在 2018 年后建立。
# 与日历法 --lower 默认 2017-07 保持一致；不要用 2000-01-01，那会白扫 17 年空月份。
HISTORY_LOWER_BOUND = date(2017, 7, 1)


def capture_start(entry: dict, start: date) -> date:
    # 还没证明「读到过会话开头」时，本次一路翻到底：翻到翻不动为止。
    # 这样 earliest_message 才是会话的真实起点，而不是窗口下界；
    # 每个会话只付一次这个成本，之后 reached_start 为真就走下面的增量逻辑。
    # reached_start 为 False 也表示「没证明读到过开头」（历史数据里大量如此），
    # 不能只认 None，否则这些会话永远不会做深度遍历，最早时间也永远学不到。
    if entry.get("reach_beginning") and not entry.get("reached_start"):
        return HISTORY_LOWER_BOUND
    if not entry.get("history_complete") or not entry.get("checked_through"):
        return start
    if "merge_anchor_at" in entry:
        # 跨轮合并要 ≥2 条相邻且不同的消息重叠：从倒数第 3 条消息那天读起；不足 3 条直接从起点重读。
        anchor = entry["merge_anchor_at"]
        if not anchor:
            return start
    else:
        anchor = entry.get("latest_message_at") or entry["checked_through"]
    return max(start, date.fromisoformat(anchor[:10]) - timedelta(days=1))


def old_snapshot(store: Path, entry: dict) -> list[dict]:
    relative = entry.get("snapshot")
    if not relative:
        return []
    path = store / relative
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.get("snapshot_sha256"):
        raise ValueError(f"会话 {entry['key']} 的已提交快照校验失败，停止续采")
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


INDEX_FIELDS = ["conversation_key", "name", "previous_names", "last_activity", "history_complete",
                "checked_through", "conversation_start", "earliest_message", "latest_message", "messages", "months",
                "last_status", "stall", "dialog_blocks", "open_failures", "last_run", "current_file", "snapshot", "snapshot_check"]


def write_current(store: Path, entry: dict, data: bytes) -> None:
    """Mirror the published snapshot as conversations/<key>/current.jsonl plus a small meta.json.

    Derived views only: manifest.json and the immutable snapshots remain the source of truth.
    """
    folder = store / "conversations" / entry["key"]
    atomic_bytes(folder / "current.jsonl", data)
    atomic_json(folder / "meta.json", {
        "conversation_key": entry["key"], "name": entry["names"][-1], "names": entry["names"],
        "snapshot": entry.get("snapshot"), "snapshot_sha256": entry.get("snapshot_sha256"),
        "history_complete": bool(entry.get("history_complete")), "checked_through": entry.get("checked_through"),
        "conversation_start": entry.get("conversation_start"),
        "last_status": entry.get("last_status"), "note": "派生文件，内容与 snapshot 相同；以 manifest.json 为准"})


def refresh_views(store: Path) -> Path:
    """Re-sync every current.jsonl from its verified snapshot and rewrite index.csv (read from disk)."""
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for entry in manifest.get("conversations", {}).values():
        row = {"conversation_key": entry["key"], "name": entry["names"][-1],
               "previous_names": "；".join(entry["names"][:-1]), "last_activity": entry.get("last_activity") or "",
               "history_complete": "是" if entry.get("history_complete") else "否",
               "checked_through": entry.get("checked_through") or "",
               "conversation_start": entry.get("conversation_start") or "",
               "last_status": f"merged_into:{entry['merged_into']}" if entry.get("merged_into") else (entry.get("last_status") or ""),
               "stall": ("停滞：最后活跃未变，已 {} 次完整读取仍为 partial（{}）；--retry-partial 可重新纳入".format(
                         entry["partial_attempts"]["count"], "、".join(entry["partial_attempts"]["stop_reasons"]))
                         if is_stalled(entry, entry.get("last_activity")) else ""),
               "open_failures": entry.get("open_failures") or 0,
               "dialog_blocks": (f"{entry['dialog_blocks']}（已跳过，待人工确认）" if int(entry.get("dialog_blocks") or 0) >= SKIP_AFTER_DIALOG_BLOCKS
                                 else (entry.get("dialog_blocks") or 0)), "last_run": entry.get("last_run") or "",
               "snapshot": entry.get("snapshot") or ""}
        if entry.get("snapshot"):
            data = (store / entry["snapshot"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry.get("snapshot_sha256"):
                row["snapshot_check"] = "hash_mismatch"
            else:
                row["snapshot_check"] = "ok"
                current = store / "conversations" / entry["key"] / "current.jsonl"
                if not current.exists() or hashlib.sha256(current.read_bytes()).hexdigest() != entry["snapshot_sha256"] \
                        or not (current.parent / "meta.json").exists():
                    write_current(store, entry, data)
                row["current_file"] = str(current.relative_to(store))
                records = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
                stamps = sorted(r["sent_at"] for r in records if r.get("sent_at"))
                row.update(messages=len(records), earliest_message=stamps[0] if stamps else "",
                           latest_message=stamps[-1] if stamps else "",
                           months=";".join(sorted({s[:7] for s in stamps})))
        rows.append(row)
    rows.sort(key=lambda r: (r["last_activity"], r["name"]), reverse=True)
    path = store / "index.csv"
    tmp = path.with_name(f".index.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
    return path


def publish_snapshot(store: Path, entry: dict, rows: list[dict], tag: str) -> bytes:
    """Write rows as a new immutable snapshot version of entry and point the entry at it."""
    for record in rows:
        record["conversation_key"] = entry["key"]
        record["record_id"] = record.get("record_id") or uuid.uuid4().hex
    data = encode_jsonl(rows)
    digest = hashlib.sha256(data).hexdigest()
    relative = Path("conversations") / entry["key"] / "snapshots" / f"{tag}-{digest[:12]}.jsonl"
    atomic_bytes(store / relative, data)
    entry["snapshot"] = str(relative)
    entry["snapshot_sha256"] = digest
    entry["capture_count"] = int(entry.get("capture_count") or 0) + 1
    stamps = sorted(row["sent_at"] for row in rows if row.get("sent_at"))
    entry["latest_message_at"] = stamps[-1] if stamps else None
    entry["merge_anchor_at"] = stamps[-3] if len(stamps) >= 3 else None
    return data


def start_confirmed(entry: dict, window_start: date) -> bool:
    """会话历史起点已确认、采集器判到顶、且采到的最早消息与应有边界吻合 → 认可覆盖完整。

    老群（起点早于窗口）按窗口起点算应有边界；窗口内建的群按真实起点算。
    双条件缺一不可：只有采集器自己也判到顶，外部/自动学到的起点才可当覆盖证据。
    """
    origin = entry.get("conversation_start")
    if not origin or not entry.get("reached_start"):
        return False
    earliest = (entry.get("earliest_message") or "")[:10]
    if not earliest:
        return False
    try:
        expected = max(date.fromisoformat(origin), window_start)
        gap = (date.fromisoformat(earliest) - expected).days
    except ValueError:
        return False
    return 0 <= gap <= 7


def commit_capture(store: Path, manifest: dict, entry: dict, run_id: str,
                   stage: Path, since: date, original_start: date, cutoff: datetime) -> dict:
    key = entry["key"]
    prefix = stage / f"chat-{key}"
    raw_path = Path(str(prefix) + "-raw.json")
    current_path = Path(str(prefix) + ".jsonl")
    validation_path = Path(str(prefix) + "-validation.json")
    if not all(p.exists() for p in (raw_path, current_path, validation_path)):
        return {"key": key, "status": "failed", "reason": "capture_artifacts_missing"}
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"key": key, "status": "review", "reason": "capture_artifacts_invalid"}
    if validation.get("status") == "failed":
        return {"key": key, "status": "review", "reason": "structural_validation_failed"}
    if raw.get("conversation") not in entry["names"]:
        return {"key": key, "status": "review", "reason": "conversation_identity_changed"}
    current = [row for row in read_jsonl(current_path)
               if datetime.fromisoformat(row["sent_at"]).replace(tzinfo=cutoff.tzinfo) <= cutoff]
    previous = old_snapshot(store, entry)
    # 「一屏即全部」的收敛规则：本轮没有窗口内可导出的新消息，且连续两次独立打开都确认
    # 滚了多次仍无新增行（no-new-content / 无滚动条 / 已到顶），并且没有序列缺口或未解析行。
    # 这时旧快照就是窗口内全部内容；若还拿空 current 去 merge，会永远停在 review 每轮重采。
    if not current and previous:
        stop = raw.get("stop_reason")
        clean = not raw.get("sequence_gaps") and not raw.get("unresolved_rows")
        one_screen = (stop in {"no-new-content", "top-no-scrollbar", "reached-top"} and clean
                      and (stop == "top-no-scrollbar" or int(raw.get("scroll_count") or 0) >= 3))
        if one_screen:
            checks = int(entry.get("one_screen_checks") or 0) + 1
            entry["one_screen_checks"] = checks
            if checks >= 2:
                entry["history_complete"] = True
                entry["checked_through"] = cutoff.isoformat()
                entry["last_status"] = "ok"
                entry["last_run"] = run_id
                atomic_json(store / "manifest.json", manifest)
                return {"key": key, "status": "ok", "alignment": "one_screen_confirmed",
                        "messages": len(previous), "stage": str(stage.relative_to(store))}
        else:
            entry.pop("one_screen_checks", None)
    # 记录面板顶部就是会话历史起点：历史起点晚于窗口起点时，回到顶部同样算覆盖完整。
    top_reached = raw_reached_history_top(raw)
    # 无滚动条的「一屏即全部」（stop_reason=top-no-scrollbar）依据弱于「滚动条归零」：
    # 无法排除「面板尚未渲染完只画出几行」。要求连续两次独立打开都得出同一结论才采信，
    # 最多确认两次——第三次不再重试，避免既丢数据又空转。
    no_bar_top = raw.get("stop_reason") == "top-no-scrollbar"
    if no_bar_top:
        entry["top_no_bar_checks"] = int(entry.get("top_no_bar_checks") or 0) + 1
    else:
        entry.pop("top_no_bar_checks", None)
    top_confirmed = bool(entry.get("top_confirmed")) or (
        no_bar_top and int(entry.get("top_no_bar_checks") or 0) >= 2)
    if top_confirmed:
        entry["top_confirmed"] = True
    # 覆盖完整通常要求「记录中存在早于窗口起点的日期」，但 2026 年才出现的会话永远满足不了；
    # 对这类会话，「两次独立打开都确认一屏即全部」就是覆盖完整的等价证据。
    start_ok = start_confirmed(entry, original_start)
    coverage_ok = validation.get("coverage_complete") is True or top_confirmed or start_ok
    coverage_ok_basic = coverage_ok
    # A full fresh capture supersedes earlier observations. Otherwise a verified
    # two-message overlap is required before publishing a merged timeline.
    # 「读到顶」不等于「本次导出包含全部历史」：增量采集的导出是从 since 起截断的。
    # 只有导出起点不比旧快照更晚（即覆盖了旧数据范围），才允许整体替换；
    # 否则走合并，宁可记为 partial 也不能把更早的月份冲掉。
    prev_earliest = min((r.get("sent_at") for r in (previous or []) if r.get("sent_at")), default=None)
    cur_earliest = min((r.get("sent_at") for r in current if r.get("sent_at")), default=None)
    covers_previous = prev_earliest is None or (cur_earliest is not None and cur_earliest <= prev_earliest)
    full = ((since == original_start or top_reached or top_confirmed or start_ok) and covers_previous
            and coverage_ok)
    if full or not previous:
        rows, alignment = current, "full" if full else "first_partial"
    else:
        rows, stats = merge(previous, current)
        alignment = stats["alignment"]
        if alignment != "found":
            fallback = small_overlap_fallback(previous, current, covers_previous)
            if fallback is None:
                return {"key": key, "status": "review", "reason": "overlap_not_verified",
                        "stage": str(stage.relative_to(store))}
            rows, alignment = fallback
    # 适度放宽：消息已采到、合并锚点已验证、导出没有把更早月份冲掉，且校验失败项
    # 只涉及「边界未验证 / 序列缺口 / 未并入行」时，推定覆盖完整。
    # alignment-gap 的未并入行多为图片/无时间占位行，本身不是可导出的有效消息；
    # 结构性错误（条数不符、乱序、缺字段等）一律不放宽。推定结果写标记，便于回溯。
    presumed = False
    if not coverage_ok_basic and rows and covers_previous and alignment in {"found", "full"}:
        failures = [str(f) for f in (validation.get("coverage_failures") or [])]
        structural = (validation.get("count_match") is False
                      or validation.get("chronological_order") is False
                      or any(any(k in f for k in ("count_match", "duplicate_ids", "chronological",
                                                  "missing_required", "invalid_or_outside",
                                                  "missing_sender", "empty_body", "validation_failed"))
                             for f in failures))
        stop = raw.get("stop_reason")
        # 安全阀：最多只容忍一处序列缺口；多段缺口说明可能有实质漏采，不放宽。
        gaps = int(raw.get("sequence_gaps") or 0)
        if (not structural and gaps <= 1 and failures
                and stop in {"alignment-gap", "reached-top", "top-no-scrollbar",
                             "no-new-content", "reached-since"}):
            presumed = True
    coverage_ok = coverage_ok_basic or presumed
    # 打开会话时回读到的新名称：登记为最新别名（不覆盖旧名，保留改名历史）。
    opened_path = Path(str(prefix) + "-opened.json")
    if opened_path.exists():
        try:
            opened_name = json.loads(opened_path.read_text(encoding="utf-8")).get("opened_name")
            if opened_name and opened_name not in entry["names"] and not is_job_title_alias(
                    entry["names"][-1] if entry.get("names") else "", opened_name):
                old_last = entry["names"][-1] if entry.get("names") else ""
                # 只追加别名，绝不覆盖或删除旧名：名字是唯一身份，覆盖会导致下轮定位到别的会话。
                entry["names"] = [n for n in entry["names"] if n != opened_name] + [opened_name]
                sync_skip_aliases(store, old_last, opened_name)
                entry.setdefault("renames_observed", []).append(
                    {"to": opened_name, "run": run_id, "at": datetime.now().astimezone().isoformat()})
        except (OSError, ValueError):
            pass
    data = publish_snapshot(store, entry, rows, run_id)
    if coverage_ok:
        captured = datetime.fromisoformat(raw["captured_at"].replace("Z", "+00:00")).astimezone(cutoff.tzinfo)
        entry["checked_through"] = min(captured, cutoff).isoformat()
        # 校验通过 + 合并锚点已验证 + 导出没有冲掉更早月份 + 有消息，也判完整。
        # 否则 alignment-gap 会话会卡死：stop 不是 reached-top 所以 top_reached/start_ok 全为假，
        # reach_beginning 又让 since != original_start，于是 validation 明明通过却永远 partial，
        # should_capture 每轮重开、--retry-partial 绕过 stall → 每轮重复采同一批消息。
        if (since == original_start or top_reached or top_confirmed or start_ok or presumed
                or (alignment in {"found", "full"} and covers_previous and rows)):
            entry["history_complete"] = True
            if presumed:
                entry["coverage_presumed"] = True
                entry["coverage_presumed_reason"] = (
                    f"alignment={alignment}; stop={raw.get('stop_reason')}; "
                    f"仅边界类失败: {','.join(validation.get('coverage_failures') or [])[:200]}")
    # 落盘会话的真实边界：earliest_message 只有在 reached_start 为真时才是会话属性，
    # 否则它只是本次窗口的下界。后续判断「某年份是否还需要采」全靠这两项。
    stamps = sorted(r["sent_at"] for r in rows if r.get("sent_at"))
    if stamps:
        entry["earliest_message"] = stamps[0]
        entry["latest_message_at"] = stamps[-1]
    entry["reached_start"] = bool(top_reached or top_confirmed)
    # 采集器翻到顶即学到会话历史起点；此前没有值时自动补上，供后续边界校验使用。
    if entry["reached_start"] and not entry.get("conversation_start") and stamps:
        entry["conversation_start"] = stamps[0][:10]
    # 面板打开后自始至终没有任何行（no-new-content + 0 条 + 之前也没有快照）＝ 该会话确实没有聊天记录。
    # 这是「已验证为空」，不是「没采到」：记为 ok 并停止重试，等有新消息再采。
    # 其它零记录原因（alignment-gap 等）保持 partial，因为它们只说明没读到，不等于没有。
    if not rows and not previous and raw.get("stop_reason") == "no-new-content":
        entry["verified_empty"] = True
        entry["history_complete"] = True
        # 也要写明「检查到什么时候」：否则 checked_through 为空会让 should_capture 每轮都返回 True，
        # 这个空会话会被反复打开。写入后只在列表最后活跃时间晚于此时才重采——正是我们想要的行为。
        try:
            captured = datetime.fromisoformat(raw["captured_at"].replace("Z", "+00:00")).astimezone(cutoff.tzinfo)
            entry["checked_through"] = min(captured, cutoff).isoformat()
        except (KeyError, ValueError):
            entry["checked_through"] = cutoff.isoformat()
    status = "ok" if entry.get("history_complete") and coverage_ok else "partial"
    if entry.get("verified_empty"):
        status = "ok"
    entry["last_status"] = status
    entry["last_run"] = run_id
    entry["last_alignment"] = alignment
    atomic_json(store / "manifest.json", manifest)
    write_current(store, entry, data)
    return {"key": key, "status": status, "alignment": alignment, "messages": len(rows),
            "stage": str(stage.relative_to(store))}


REUSE_SCAN_HOURS_WEEKDAY = 36
REUSE_SCAN_HOURS_WEEKEND = 48


def scan_reuse_hours(now: datetime) -> int:
    """工作日 36 小时、周末 48 小时：候选集变化慢，少重扫一次就省 15–20 分钟前台占用。

    用户明确要求重启后不轻易重扫；索引陈旧的代价由「定位失败成簇」来判断，而不是按固定小时数猜。
    """
    return REUSE_SCAN_HOURS_WEEKEND if now.weekday() >= 5 else REUSE_SCAN_HOURS_WEEKDAY


def reusable_scan(store: Path, end: date, now: datetime) -> Path | None:
    """Latest full sidebar scan within the reuse window, usable only for a historical window.

    A conversation created after the scan cannot hold messages on or before ``end`` when ``end``
    is earlier than the scan date, so reusing it does not lose candidates for this window.
    """
    window = timedelta(hours=scan_reuse_hours(now))
    best: tuple[datetime, Path] | None = None
    for path in (store / "discovery").glob("*/timeline.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            captured = datetime.fromisoformat(str(data["captured_at"]).replace("Z", "+00:00")).astimezone(now.tzinfo)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        stop = str(data.get("stop_reason") or "")
        if not (stop.startswith("crossed-") or stop == "verified-list-end"):
            continue
        if now - captured > window or captured.date() <= end:
            continue
        if best is None or captured > best[0]:
            best = (captured, path)
    return best[1] if best else None


def require_plausible_scan(store: Path, index: Path) -> None:
    """拦截「把别的页当成聊天列表」的假完整扫描。

    侧栏切到通讯录/工作台时，AXXTable 只剩几十行应用节点，扫描却仍会写
    ``verified-list-end``。与历史正常行数一比就能立刻看出来。
    """
    try:
        rows = int(json.loads(index.read_text(encoding="utf-8")).get("total_rows") or 0)
    except (OSError, ValueError, TypeError):
        return
    known = 0
    for path in (store / "discovery").glob("*/timeline.json"):
        if path == index:
            continue
        try:
            known = max(known, int(json.loads(path.read_text(encoding="utf-8")).get("total_rows") or 0))
        except (OSError, ValueError, TypeError):
            continue
    if known >= 500 and rows < known * 0.2:
        index.rename(index.with_suffix(".invalid-scan"))
        raise RuntimeError(
            f"会话列表只读到 {rows} 行，历史上是 {known} 行：侧栏可能被切到通讯录或工作台，"
            f"这不是聊天列表。请在企业微信里切回「消息」页后重跑")


def build_candidates(store: Path, start: date, end: date, run_id: str, args: argparse.Namespace) -> dict:
    if args.config:
        return json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    discovery = store / "discovery" / run_id
    ensure_dir(discovery)
    index = discovery / "timeline.json"
    reused = None if args.timeline_index or getattr(args, "rescan", False) else \
        reusable_scan(store, end, datetime.now().astimezone())
    if args.timeline_index:
        source = Path(args.timeline_index).expanduser()
        index.write_bytes(source.read_bytes())
    elif reused:
        index.write_bytes(reused.read_bytes())
        print(f"复用 {reused.parent.name} 的会话列表扫描"
              f"（{scan_reuse_hours(datetime.now().astimezone())} 小时内的完整扫描；--rescan 可强制重扫）", flush=True)
    else:
        command = swift_command(BASE / "wework_timeline_scan.swift", store / ".swift-bin")
        scan = command + ["--output", str(index), "--max-scrolls", "3000"]
        # 普通扫描按窗口起点年份提前停止（实测省约 71% 时间）；
        # --rescan 是用户显式要求的全量兜底，传 0 禁用边界判断。
        scan += ["--stop-year", "0" if getattr(args, "rescan", False) else str(start.year)]
        code = run_child(scan)
        if code in REANCHOR_EXITS:
            # Coordinates are re-read on the next run, so a window moved mid-scan usually recovers
            # instead of costing the whole round.
            print(f"扫描期间窗口或布局变化（退出码 {code}），重新锚定坐标后重试一次", flush=True)
            code = run_child(scan)
        if code:
            raise RuntimeError(f"会话列表扫描失败：{code}")
        require_plausible_scan(store, index)
    config_path = discovery / "chat-config.json"
    code = subprocess.run([sys.executable, str(BASE / "timeline_lookup.py"), str(index),
                           "--start", start.isoformat(), "--end", end.isoformat(),
                           "--format", "config", "--output", str(config_path)]).returncode
    if code:
        raise RuntimeError(f"候选配置生成失败：{code}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if reused:
        config["timeline_reused_from"] = reused.parent.name
    return config


def recover_interrupted(store: Path, manifest: dict, start: date) -> list[dict]:
    recovered = []
    changed = False
    for run in manifest["runs"]:
        if run.get("status") != "running":
            continue
        run_id = run["run_id"]
        outcomes = {o["key"]: o for o in run.get("outcomes", [])}
        for stage in sorted((store / "runs" / run_id).glob("*")):
            if not stage.is_dir() or stage.name not in manifest["conversations"]:
                continue
            entry = manifest["conversations"][stage.name]
            if entry.get("last_run") == run_id:
                continue
            setup = stage / "chat-config.json"
            raw = stage / f"chat-{entry['key']}-raw.json"
            if not setup.exists() or not raw.exists():
                continue
            try:
                entry["last_scroll_count"] = max(int(entry.get("last_scroll_count") or 0),
                    int(json.loads(raw.read_text(encoding="utf-8")).get("scroll_count") or 0))
            except (OSError, ValueError):
                continue
            one = json.loads(setup.read_text(encoding="utf-8"))
            since = date.fromisoformat(one["capture_window"]["start"])
            end = date.fromisoformat(one["capture_window"]["end"])
            cutoff = datetime.fromisoformat(one["capture_window"]["cutoff"])
            prefix = stage / f"chat-{entry['key']}"
            jsonl = Path(str(prefix) + ".jsonl")
            validation = Path(str(prefix) + "-validation.json")
            if not jsonl.exists() or not validation.exists():
                export = subprocess.run([sys.executable, str(BASE / "chat_export.py"), str(raw),
                    "--start", since.isoformat(), "--end", end.isoformat(),
                    "--output-prefix", str(prefix), "--conversation", entry["names"][-1]],
                    stdout=subprocess.DEVNULL).returncode
                if export:
                    continue
                subprocess.run([sys.executable, str(BASE / "chat_validate.py"), str(jsonl),
                    "--raw", str(raw), "--start", since.isoformat(), "--end", end.isoformat(),
                    "--summary-output", str(validation)], stdout=subprocess.DEVNULL)
            outcome = commit_capture(store, manifest, entry, run_id, stage, since, start, cutoff)
            recovered.append(outcome)
            outcomes[entry["key"]] = outcome
        # Recover attempts even if no raw was published, or the process died
        # between snapshot commit and saving the round outcome.
        for stage in sorted((store / "runs" / run_id).glob("*")):
            if not stage.is_dir() or stage.name not in manifest["conversations"]:
                continue
            key = stage.name
            if key in outcomes:
                continue
            entry = manifest["conversations"][key]
            outcome = {"key": key, "status": "interrupted", "reason": "no_committed_capture",
                       "stage": str(stage.relative_to(store))}
            if entry.get("last_run") == run_id:
                outcome.update(status=entry.get("last_status") or "partial", reason="recovered_committed_snapshot",
                               messages=len(old_snapshot(store, entry)))
            else:
                try:
                    progress = json.loads((stage / "progress.json").read_text(encoding="utf-8"))
                    result = (progress.get("conversations") or [{}])[-1]
                    outcome["open_exit"] = result.get("open_exit")
                    if result.get("status") == "failed":
                        outcome.update(status="failed", reason="recovered_failed_attempt")
                except (OSError, ValueError, IndexError):
                    pass
            outcomes[key] = outcome
        run["outcomes"] = list(outcomes.values())
        run["status"] = "interrupted_recovered"
        changed = True
    if changed:
        atomic_json(store / "manifest.json", manifest)
    return recovered


def load_skip_names(store: Path) -> set[str]:
    """用户确认过永不采集的会话名，来自仓库根目录 skip-conversations.json。"""
    path = store / "skip-conversations.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(n).strip() for n in data if str(n).strip()}


# 机器人会话：会话名右侧有 BOT 徽章（AX 侧栏同行的 AXStaticText），名字本身也常带
# 「机器人 / robot / 问答客服」。扫描到就自动写进用户白名单，不必逐个人工维护。
# 若以后确认 BOT 徽章在 AX 里的确切文本，可在扫描工具输出 bot_badge 字段后并入这里。
BOT_NAME_RE = re.compile(r"(机器人|robot|问答客服|(?:^|[\s\-_\[\]【】])BOT(?:[\s\-_\]】]|$))", re.I)


def alignment_gap_stalled(entry: dict) -> bool:
    """该会话是否连续因 alignment-gap 停在 partial（关联到复制内嵌表情扰动）。"""
    stops = (entry.get("partial_attempts") or {}).get("stop_reasons") or []
    return bool(stops) and all(s == "alignment-gap" for s in stops)


def sync_skip_aliases(store: Path, old_name: str, new_name: str) -> None:
    """旧名在用户白名单里时，把新别名也加进去，否则会话改名后白名单失效。"""
    if not old_name or not new_name or old_name == new_name:
        return
    path = store / "skip-conversations.json"
    try:
        names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        return
    if old_name not in names or new_name in names:
        return
    names.append(new_name)
    path.write_text(json.dumps(sorted(set(names)), ensure_ascii=False, indent=1), encoding="utf-8")


def bot_name_hits(entries) -> set[str]:
    """从一批会话条目里挑出机器人会话名。"""
    hits = set()
    for entry in entries:
        names = entry.get("names") or []
        if names and BOT_NAME_RE.search(names[-1]):
            hits.add(names[-1])
    return hits


def timeline_bot_names(store: Path) -> set[str]:
    """侧栏扫描工具检出的 BOT 徽章会话名（timeline.json 的 bots 字段）。"""
    files = sorted((store / "discovery").glob("*/timeline.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return set()
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(n).strip() for n in (data.get("bots") or []) if str(n).strip()}


def auto_whitelist_bots(store: Path, chosen: list, skip_names: set[str]) -> list[str]:
    """扫描后自动把机器人会话追加到 skip-conversations.json（幂等），返回新增名字。"""
    hits = bot_name_hits(entry for _cand, entry in chosen) | timeline_bot_names(store)
    new = sorted(hits - skip_names)
    if not new:
        return []
    path = store / "skip-conversations.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        current = []
    merged = sorted({str(n).strip() for n in current if str(n).strip()} | set(new))
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return new


def raw_reached_history_top(raw: dict) -> bool:
    """记录面板顶部即会话历史起点；历史数据按滚动值为 0 推断。"""
    if raw.get("reached_top") is True:
        return True
    # 显式 false 必须尊重：滚动值为 0 只说明面板在顶部，空面板、刚打开未加载
    # 都会是 0，用它覆盖明确的 false 会把「没读到顶」误判成「读到顶」，
    # 进而让增量导出替换掉完整快照（实测影响 163 个会话的判定）。
    if raw.get("reached_top") is False:
        return False
    value = raw.get("vertical_scroll_value")
    return isinstance(value, (int, float)) and float(value) <= 0.001


def stop_reason_for_open(open_exit: int | None, capture_exit: int, consecutive: int,
                         capture_failures: int = 0) -> str | None:
    if open_exit in {2, 3, 4, 5, 20, 21, 22, 23}:
        return f"会话定位环境失败（退出码 {open_exit}）"
    if consecutive >= 3:
        return CONSECUTIVE_OPEN_FAILURE
    if capture_exit != 0 and open_exit is None and capture_failures >= ABORT_AFTER_CAPTURE_FAILURES:
        return (f"连续 {capture_failures} 个会话采集子进程异常退出（退出码 {capture_exit}），"
                f"未写入会话定位结果")
    return None


class OpenFailureTracker:
    """Attribute open failures to a conversation only after the UI is proven usable.

    A failure streak followed by a successful open (a later conversation or the
    reference probe) is conversation-specific; a streak that the reference cannot
    break is an environment problem and must not penalise those conversations.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.pending: list[tuple[dict, int]] = []
        self.consecutive = 0
        self.reference: tuple[dict, dict] | None = None

    def observe(self, candidate: dict, entry: dict, open_exit: int | None) -> None:
        if open_exit in OPEN_FAILURE_EXITS:
            self.pending.append((entry, open_exit))
            self.consecutive += 1
            return
        self.consecutive = 0
        if open_exit == 0:
            self.confirm()
            entry["open_failures"] = 0
            self.reference = (candidate, entry)

    def confirm(self) -> None:
        for entry, code in self.pending:
            entry["open_failures"] = int(entry.get("open_failures") or 0) + 1
            entry["last_open_exit"] = code
            entry["last_open_failure_run"] = self.run_id
        self.pending = []

    def discard(self) -> None:
        self.pending = []

    def resolve_streak(self, probe) -> str | None:
        if self.reference is None:
            self.discard()
            return CONSECUTIVE_OPEN_FAILURE + "；本轮尚无成功打开的对照会话，按环境问题停止"
        code = probe(*self.reference)
        if code != 0:
            # A single failed reference open can be as transient as any other open; retry once
            # before blaming the environment.
            print(f"对照会话打开失败（退出码 {code}），重试一次以排除偶发", flush=True)
            code = probe(*self.reference)
        if code == 0:
            self.confirm()
            self.consecutive = 0
            return None
        self.discard()
        return f"待判定定位失败；对照会话也无法打开（退出码 {code}），按环境问题停止"


def schedule_open_failures(planned: list[tuple[dict, dict]], retry: bool) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Keep the candidate order, put never-captured conversations first, move repeated open failures
    last and defer hopeless ones to review."""
    # 未采过的始终排在已有快照之前；否则失败过 1 次的未采会话会被大量 partial 重试挤到队尾饿死。
    ordered = sorted(planned, key=lambda item: (1 if item[1].get("snapshot") or item[1].get("capture_count") else 0,
                                                int(item[1].get("open_failures") or 0)))
    run, deferred = [], []
    for candidate, entry in ordered:
        failures = int(entry.get("open_failures") or 0)
        if failures >= SKIP_AFTER_OPEN_FAILURES and not retry:
            deferred.append({"name": entry["names"][-1], "key": entry["key"], "reason": "repeated_open_failure",
                             "open_failures": failures, "last_open_exit": entry.get("last_open_exit"),
                             "note": "已跨轮多次定位失败，本轮未采；确认可打开后用 --retry-open-failures 重新纳入"})
        else:
            run.append((candidate, entry))
    return run, deferred


def open_command(store: Path, candidate: dict, name: str, max_screens: int) -> list[str]:
    command = swift_command(BASE / "wework_open_row.swift", store / ".swift-bin") + ["--conversation", name]
    if candidate.get("seek_date"):
        command += ["--seek-date", str(candidate["seek_date"])]
    if candidate.get("index"):
        command += ["--index", str(candidate["index"]), "--drift", "0"]
    return command + ["--max-screens", str(max_screens)]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # macOS 对未回收的僵尸进程也返回 EPERM：交给下面的 ps 判断，而不是直接当作存活
    # An exited process whose parent has not reaped it yet still answers kill(0).
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group(pgid: int, proc: subprocess.Popen | None, grace: float) -> bool:
    deadline = systime.monotonic() + grace
    while systime.monotonic() < deadline:
        if proc is not None:
            proc.poll()
        if not group_alive(pgid):
            return True
        systime.sleep(0.2)
    return False


def stop_group(pgid: int, proc: subprocess.Popen | None = None, grace: float = 10.0) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS：组里只剩未回收的僵尸组长时 killpg 返回 EPERM。能回收（有 proc）就回收后复查，
            # 否则才放弃；以前直接返回 False，导致 --stop 报「子进程组未停止」并残留运行锁。
            if proc is None:
                return False
        if _wait_group(pgid, proc, grace):
            break
    if proc is not None:
        proc.poll()
    return not group_alive(pgid)


def _write_lock() -> None:
    if _lock_path is not None and _lock_owner is not None:
        _lock_owner["child_pgids"] = sorted(_children)
        atomic_json(_lock_path, _lock_owner)


def run_child(command: list[str], **kwargs) -> int:
    """Run a UI operator in its own process group so the whole tree can be stopped and awaited."""
    proc = subprocess.Popen(command, start_new_session=True, **ui_lock.child_options(), **kwargs)
    _children[proc.pid] = proc
    _write_lock()
    try:
        return proc.wait()
    finally:
        # A wrapper may exit while its Swift child is still driving the UI.
        # Keep the group in the recovery lock until every descendant is gone.
        if not group_alive(proc.pid) or stop_group(proc.pid, proc):
            _children.pop(proc.pid, None)
        _write_lock()
        if proc.pid in _children:
            raise RuntimeError(f"子进程组 {proc.pid} 未停止；保留运行锁，禁止继续采集")


def _terminate(signum, _frame) -> None:
    # Ignore repeated termination while the first cleanup is waiting.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    # 信号处理器常打断 run_child 里的 proc.wait()：此时 wait() 持有 Popen 的 _waitpid_lock，
    # proc.poll() 拿不到锁、回收不了子进程，cleanup 会白等 2×10 秒并报「子进程组未停止」。
    # 这里只发 SIGTERM；等待与回收交给 run_child / main 的 finally（届时锁已释放）。
    for pgid in list(_children):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    raise SystemExit(128 + signum)


def cleanup_children() -> None:
    for pgid, proc in list(_children.items()):
        if stop_group(pgid, proc):
            _children.pop(pgid, None)
    _write_lock()


def lock_blocker(lock: Path) -> str | None:
    """Return why a new round must not start, or None after removing a fully dead lock."""
    if not lock.exists():
        return None
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return "运行锁无法核验；请检查原进程后处理"
    if pid_alive(pid):
        return f"仓库已有采集进程 {pid}；需要停止时运行 --stop"
    alive = [g for g in owner.get("child_pgids") or [] if group_alive(int(g))]
    if alive:
        return f"上一轮主进程已退出，但子进程组 {alive} 仍可能在操作企业微信；先运行 --stop"
    # 改名归档而不是删除：既避开文件删除保护，也保留上一轮异常退出的现场。
    archive = lock.with_name(f"{lock.name}.stale-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        lock.replace(archive)
    except OSError:
        return "陈旧运行锁无法归档；请手动处理后重试"
    print(f"陈旧运行锁（pid {pid} 已退出）已归档为 {archive.name}", flush=True)
    return None


def stop_repository(store: Path, wait: float = 60.0) -> int:
    lock = store / ".repository.lock"
    if not lock.exists():
        print("没有运行中的采集进程")
        return 0
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        print("运行锁无法解析，未执行停止；请人工检查", file=sys.stderr)
        return 1
    groups = {int(g) for g in owner.get("child_pgids") or []}
    if pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = systime.monotonic() + wait
        while pid_alive(pid) and systime.monotonic() < deadline:
            systime.sleep(0.5)
    if lock.exists():
        try:
            groups |= {int(g) for g in json.loads(lock.read_text(encoding="utf-8")).get("child_pgids") or []}
        except (OSError, ValueError):
            pass
    leftovers = [g for g in sorted(groups) if not stop_group(g)]
    if pid_alive(pid) or leftovers:
        print(f"停止未完成：主进程 {pid} 存活={pid_alive(pid)}，残留进程组 {leftovers}", file=sys.stderr)
        return 1
    lock.unlink(missing_ok=True)
    print(f"已停止采集进程 {pid} 及其全部子进程；已落盘结果保留，下次用相同参数续跑")
    return 0


ACTIVE_STORE = Path.home() / "Library/Caches/extract-wecom-history/active-store"


def remember_active_store(store: Path) -> None:
    """让 chat_dashboard.py / wecom-top 不带参数就能找到正在跑的仓库。"""
    try:
        ensure_dir(ACTIVE_STORE.parent)
        ACTIVE_STORE.write_text(str(store), encoding="utf-8")
    except OSError:
        pass
    print(f"实时控制台：在终端运行 wecom-top（或 python3 {BASE / 'chat_dashboard.py'} --store {store}）；"
          f"纯文本快照加 --once", flush=True)


def repository_status(store: Path) -> dict:
    lock = store / ".repository.lock"
    status: dict = {"store": str(store), "running": False}
    if lock.exists():
        try:
            owner = json.loads(lock.read_text(encoding="utf-8"))
            status.update(running=pid_alive(int(owner["pid"])), pid=owner.get("pid"), run_id=owner.get("run_id"),
                          live_child_groups=[g for g in owner.get("child_pgids") or [] if group_alive(int(g))])
        except (OSError, ValueError, KeyError, TypeError):
            status["lock"] = "unreadable"
    path = store / "manifest.json"
    if not path.exists():
        return status
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = [e for e in manifest.get("conversations", {}).values() if active(e)]
    status["conversations"] = {
        "total": len(entries),
        "merged_renames": len(manifest.get("conversations", {})) - len(entries),
        "history_complete": sum(1 for e in entries if e.get("history_complete")),
        "with_snapshot": sum(1 for e in entries if e.get("snapshot")),
        "deferred_open_failures": sum(1 for e in entries if int(e.get("open_failures") or 0) >= SKIP_AFTER_OPEN_FAILURES),
        "never_captured": sum(1 for e in entries if not e.get("snapshot") and not e.get("capture_count")
                              and not e.get("excluded")),
        "excluded": sum(1 for e in entries if e.get("excluded")),
        "stalled_partial": sum(1 for e in entries if is_stalled(e, e.get("last_activity"))),
        "dialogs_dismissed_latest_run": 0,
    }
    # 需要人工确认：被弹窗阻塞 ≥2 次、已自动跳过的会话。周期汇报必须列出。
    status["needs_human_dialog_review"] = dialog_blocked_list(manifest)
    if manifest.get("runs"):
        run = manifest["runs"][-1]
        status["conversations"]["dialogs_dismissed_latest_run"] = sum(
            int(json.loads(p.read_text(encoding="utf-8")).get("dialogs_dismissed") or 0)
            for p in (store / "runs" / run["run_id"]).glob("*/chat-*-raw.json") if p.stat().st_size)
        counts: dict[str, int] = {}
        for outcome in run.get("outcomes") or []:
            counts[outcome.get("status", "unknown")] = counts.get(outcome.get("status", "unknown"), 0) + 1
        status["latest_run"] = {k: run.get(k) for k in ("run_id", "status", "started_at", "finished_at", "stop_reason")}
        status["latest_run"]["outcomes"] = counts
        states = sorted((store / "runs" / run["run_id"]).glob("*/run-state.json"), key=lambda p: p.stat().st_mtime)
        if states:
            state = json.loads(states[-1].read_text(encoding="utf-8"))
            current = {k: state.get(k) for k in ("conversation", "phase", "updated_at")}
            current["updated_at_is_heartbeat"] = False
            folder = states[-1].parent
            raw_path = folder / f"chat-{folder.name}-raw.json"
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                current["checkpoint"] = {
                    "updated_at": datetime.fromtimestamp(raw_path.stat().st_mtime).astimezone().isoformat(),
                    "scroll_count": raw.get("scroll_count"),
                    "record_count": raw.get("record_count", len(raw.get("records") or [])),
                    "stop_reason": raw.get("stop_reason")}
            except (OSError, ValueError):
                pass
            log_path = folder / f"chat-{folder.name}.log"
            if log_path.exists():
                stat = log_path.stat()
                current["log"] = {"updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                                  "bytes": stat.st_size}
            status["current_stage"] = current
    return status


def apply_renames(store: Path, confirmed: Path) -> int:
    blocker = lock_blocker(store / ".repository.lock")
    if blocker:
        print(f"未关联：{blocker}", file=sys.stderr)
        return 1
    pairs = conversation_rename.read_confirmed(confirmed)
    if not pairs:
        print("确认文件中没有 confirm 为「是」的行，未做任何修改")
        return 0
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("rename-%Y%m%d-%H%M%S")

    def publish(entry: dict, rows: list[dict], _tag: str) -> None:
        publish_snapshot(store, entry, rows, stamp)

    results = conversation_rename.apply(store, manifest, pairs, publish)
    atomic_json(store / "manifest.json", manifest)
    refresh_views(store)
    for item in results:
        if item["result"] != "linked":
            print(f"跳过 {item['new_key'][:8]} → {item['old_key'][:8]}：{item['reason']}")
            continue
        action = {"kept_old": "新 key 无数据，沿用旧数据", "adopted_new": "旧 key 无数据，采用新 key 的数据",
                  "merged": "两边快照重叠核验通过，已合并为新版本",
                  "overlap_not_verified": "两边快照无法核验重叠，未合并；旧 key 下一轮从起点重采"}[item["data"]]
        print(f"已关联 {item['new_key'][:8]} → {item['old_key'][:8]}：{action}")
    return 0


def main() -> int:
    global _lock_path, _lock_owner
    parser = argparse.ArgumentParser(description="企业微信跨轮次会话采集仓库")
    parser.add_argument("--store", required=True, help="持续使用的采集仓库目录")
    parser.add_argument("--start", type=date.fromisoformat, help="仓库固定起点；--stop/--status 时可省略")
    parser.add_argument("--end", type=date.fromisoformat, help="本轮截止日期，默认今天")
    parser.add_argument("--account-label", help="当前登录账号的固定本地标签；--stop/--status 时可省略")
    parser.add_argument("--self-names", help="本人在聊天中的名字或别名，逗号分隔")
    parser.add_argument("--config", help="已有的新候选配置；不传则刷新侧栏时间线")
    parser.add_argument("--timeline-index", help="已扫描的新时间线；不传则现场刷新")
    parser.add_argument("--aliases", help="仅确认过的会话改名映射 JSON：新名称到既有 conversation_key")
    parser.add_argument("--retry-open-failures", action="store_true",
                        help=f"重新纳入跨轮累计定位失败 ≥{SKIP_AFTER_OPEN_FAILURES} 次的会话")
    parser.add_argument("--only-new", action="store_true",
                        help="只采集从未采过的会话（无快照），用于优先补齐新纳入窗口的空白会话")
    parser.add_argument("--reach-beginning", action="store_true",
                        help="每个会话一次性翻到会话开头（翻到翻不动），记录真实最早消息；"
                             "已记录过的会话不再重复翻，之后按最早消息判断各年份是否需要采")
    parser.add_argument("--retry-partial", action="store_true",
                        help=f"重新纳入最后活跃未变、已 {STALL_AFTER_ATTEMPTS} 次完整读取仍为 partial 的会话")
    parser.add_argument("--rescan", action="store_true",
                        help=f"强制重扫会话列表，不复用 {REUSE_SCAN_HOURS_WEEKDAY}（工作日）/"
                             f"{REUSE_SCAN_HOURS_WEEKEND}（周末）小时内的扫描")
    parser.add_argument("--retry-dialog-blocked", nargs="?", const="all", metavar="KEYS",
                        help="人工确认后重新纳入被弹窗阻塞 ≥2 次的会话；不带值为全部，或逗号分隔的 conversation_key（可用前 8 位）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true", help="只读输出运行状态与进度，不操作企业微信")
    parser.add_argument("--stop", action="store_true", help="停止本仓库的采集主进程及全部子进程并等待退出")
    parser.add_argument("--index", action="store_true",
                        help="只读快照，重建 index.csv 与各会话 current.jsonl/meta.json；不操作企业微信")
    parser.add_argument("--rename-suggestions", action="store_true",
                        help="只读：找出从列表消失的会话及其可能的新名字，写 rename-suggestions.csv")
    parser.add_argument("--apply-renames", metavar="FILE",
                        help="关联已确认的改名：rename-suggestions.csv 中 confirm 填「是」的行，或 {新key: 旧key} JSON")
    args = parser.parse_args()
    # 子进程（chat_capture.py 等）的输出重定向到日志文件时默认块缓冲；逐行落盘才能实时看到进展。
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    store = Path(args.store).expanduser().resolve()
    if args.status:
        print(json.dumps(repository_status(store), ensure_ascii=False, indent=2))
        return 0
    if args.stop:
        return stop_repository(store)
    if args.index or args.rename_suggestions or args.apply_renames:
        if not (store / "manifest.json").exists():
            parser.error("仓库中没有 manifest.json")
    if args.index:
        print(f"已更新总索引：{refresh_views(store)}")
        return 0
    if args.rename_suggestions:
        manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
        rows = conversation_rename.suggest(store, manifest)
        path = store / "rename-suggestions.csv"
        conversation_rename.write_suggestions(path, rows)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["confidence"]] = counts.get(row["confidence"], 0) + 1
        print(f"已写入 {path}：{counts or '没有从列表消失的会话'}；确认的行在 confirm 列填「是」，再运行 --apply-renames {path.name}")
        return 0
    if args.apply_renames:
        return apply_renames(store, Path(args.apply_renames).expanduser())
    if not args.start or not args.account_label:
        parser.error("采集需要 --start 与 --account-label")
    end = args.end or date.today()
    if end < args.start or end > date.today():
        parser.error("本轮截止日期必须介于起点与今天之间")
    ensure_dir(store)
    ui_lock.acquire()  # also covers --dry-run when it refreshes the live sidebar
    lock = store / ".repository.lock"
    blocker = lock_blocker(lock)
    if blocker:
        raise SystemExit(blocker)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    now = datetime.now().astimezone()
    cutoff = now if end == now.date() else datetime.combine(end, time.max, tzinfo=now.tzinfo)
    manifest = load_manifest(store, args.start, args.account_label)
    if not args.dry_run:
        if run_preflight(store / ".swift-bin") != 0:
            raise SystemExit("采集前检查未通过，未启动本轮采集；请按错误提示检查窗口和权限")
        owner = {"pid": os.getpid(), "run_id": run_id, "child_pgids": []}
        try:
            with lock.open("x", encoding="utf-8") as handle:
                json.dump(owner, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise SystemExit("仓库已被另一个采集进程占用")
        _lock_path, _lock_owner = lock, owner
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _terminate)
        remember_active_store(store)
    try:
        if not args.dry_run:
            recover_interrupted(store, manifest, args.start)
        return run_round(store, manifest, args, end, run_id, now, cutoff)
    finally:
        if not args.dry_run:
            try:
                if (store / "manifest.json").exists():
                    refresh_views(store)
            except Exception as exc:  # derived views must never mask the round result
                print(f"总索引更新失败（不影响已提交数据，可稍后运行 --index）：{exc}", file=sys.stderr)
            cleanup_children()
            if not _children:
                lock.unlink(missing_ok=True)
            else:
                print("仍有子进程组存活，已保留锁；先运行 --stop", file=sys.stderr)
            _lock_path = _lock_owner = None
        ui_lock.release()


def run_round(store: Path, manifest: dict, args: argparse.Namespace, end: date,
              run_id: str, now: datetime, cutoff: datetime) -> int:
    config = build_candidates(store, args.start, end, run_id, args)
    aliases = json.loads(Path(args.aliases).read_text(encoding="utf-8")) if args.aliases else {}
    chosen, review = reconcile(manifest, config.get("conversations") or [], aliases=aliases)
    candidate_meta = config.get("candidate_coverage") or {}
    candidate_status = candidate_meta.get("coverage_status")
    named_unknowns_included = bool(candidate_meta.get("starts_at_top") and candidate_meta.get("boundary_sufficient")
        and candidate_meta.get("missing_index_count") == 0 and candidate_meta.get("unindexed_rows") == 0
        and candidate_meta.get("unresolved_entries") == candidate_meta.get("unresolved_candidates_included")
        and candidate_meta.get("selected_count") == candidate_meta.get("candidate_count"))
    candidate_complete = bool(not args.config and not args.timeline_index and
        (candidate_status in {"exact", "superset"} or
         (candidate_status == "index_incomplete" and named_unknowns_included)))
    planned, stalled = [], []
    skip_names = load_skip_names(store)
    new_bots = auto_whitelist_bots(store, chosen, skip_names)
    if new_bots:
        skip_names |= set(new_bots)
        print(f"机器人会话自动加入不采白名单：{len(new_bots)} 个（{', '.join(new_bots[:3])}）", flush=True)
    user_skipped = 0
    for cand, entry in chosen:
        # exclude_reason 是持久排除（面聊 / 空会话）：标记由 mark_face_to_face.py 写入，
        # 每轮都要保留，否则会被下面这行清掉又重新进队列。
        if not entry.get("exclude_reason"):
            entry.pop("excluded", None)
        if entry["names"][-1] in skip_names:
            # 用户确认过永不采集：不进队列，也不计入 never_captured。
            entry["excluded"] = True
            user_skipped += 1
            continue
        if entry.get("exclude_reason"):
            # 面聊 / 空会话：由 mark_face_to_face.py 按数据特征写入，属于永久排除。
            # 不依赖名字（会话会改名），也不写入用户维护的清单。
            entry["excluded"] = True
            continue
        if not should_capture(entry, cand.get("last_activity"), cutoff, end,
                              getattr(args, "reach_beginning", False)):
            continue
        if not getattr(args, "retry_partial", False) and is_stalled(entry, cand.get("last_activity")):
            record = entry["partial_attempts"]
            stalled.append({"name": entry["names"][-1], "key": entry["key"], "reason": "stalled_partial",
                            "attempts": int(record.get("count") or 0),
                            "stop_reasons": record.get("stop_reasons") or [],
                            "note": "最后活跃未变且已多次完整读取仍为 partial，本轮跳过；--retry-partial 可重新纳入"})
            continue
        planned.append((cand, entry))
    if user_skipped:
        print(f"按 skip-conversations.json 跳过 {user_skipped} 个已确认不采集的会话", flush=True)
    if getattr(args, "only_new", False):
        # 只跑「从未采过」的会话：这批是窗口前延后才进入范围、此前完全没有数据的，
        # 先把它们拿下能最快补齐 2025 年的空白；已有快照的留到后续轮次。
        before = len(planned)
        planned = [(c, e) for c, e in planned if not e.get("snapshot")]
        print(f"仅新会话模式：{before} → {len(planned)} 个（跳过已有快照的 {before - len(planned)} 个）", flush=True)
    if getattr(args, "reach_beginning", False):
        for _, entry in planned:
            entry["reach_beginning"] = True
    skipped_unchanged = len(chosen) - len(planned) - len(stalled) - user_skipped
    planned, deferred = schedule_open_failures(planned, args.retry_open_failures)
    review.extend(deferred)
    review.extend(stalled)
    planned, dialog_skipped = schedule_dialog_blocks(planned, getattr(args, "retry_dialog_blocked", None))
    review.extend(dialog_skipped)
    # 每轮每个会话都是独立批次，drift 补偿拿不到上一次的落点；序号被新消息顶偏后只能靠
    # 多滚几屏把目标找回来，所以回退轮数取上限而不是 8。
    max_screens = min(MAX_LOCATE_SCREENS, max(1, int(config.get("max_screens", 16))))
    plan = {"run_id": run_id, "start": args.start.isoformat(), "end": end.isoformat(),
            "round_cutoff": cutoff.isoformat(),
            "candidate_coverage": config.get("candidate_coverage"),
            "candidate_coverage_complete": candidate_complete,
            "timeline_reused_from": config.get("timeline_reused_from"),
            "candidates": len(config.get("conversations") or []), "planned": len(planned),
            "never_captured_planned": sum(1 for _, e in planned if not e.get("snapshot") and not e.get("capture_count")),
            "skipped_unchanged": skipped_unchanged, "stalled_partial": len(stalled),
            "skipped_user": user_skipped,
            "deferred_open_failures": len(deferred), "skipped_dialog_blocked": len(dialog_skipped),
            "review": review}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    manifest["runs"].append({"run_id": run_id, "candidate_coverage": config.get("candidate_coverage"),
                             "candidate_coverage_complete": candidate_complete, "planned": len(planned),
                             "end": end.isoformat(), "timeline_reused_from": config.get("timeline_reused_from"),
                             "started_at": now.isoformat(), "round_cutoff": cutoff.isoformat(), "status": "running", "outcomes": [], "review": review})
    atomic_json(store / "manifest.json", manifest)
    outcomes = manifest["runs"][-1]["outcomes"]
    tracker = OpenFailureTracker(run_id)
    dialog_streak: list[dict] = []
    capture_failures = 0
    abort_reason = None
    probes = 0

    def probe(candidate: dict, entry: dict) -> int:
        nonlocal probes
        probes += 1
        log_path = store / "runs" / run_id / f"reference-open-{probes}.log"
        ensure_dir(log_path.parent)
        print(f"存在待判定的定位失败，重开本轮已成功的对照会话以区分环境与会话问题（日志 {log_path.name}）", flush=True)
        with log_path.open("w", encoding="utf-8") as log:
            return run_child(open_command(store, candidate, entry["names"][-1], max_screens),
                             stdout=log, stderr=subprocess.STDOUT)

    for candidate, entry in planned:
        since = capture_start(entry, args.start)
        stage = store / "runs" / run_id / entry["key"]
        ensure_dir(stage)
        item = dict(candidate)
        item["output_prefix"] = f"chat-{entry['key']}"
        item["name"] = entry["names"][-1]
        source_dir = Path(args.config).expanduser().resolve().parent if args.config else store / "discovery" / run_id
        for field in ("metadata", "sender_directory"):
            if item.get(field):
                item[field] = str((source_dir / item[field]).resolve())
        names = [x.strip() for x in (args.self_names or "").split(",") if x.strip()]
        one = {"conversations": [item], "self_names": names or config.get("self_names") or [],
               # 主聊天区补证默认关闭：10-30/31 实测 1035 次补证 0 次转为完整、耗时 15.2h。
               "capture_engine": "history", "main_fallback": bool(config.get("main_fallback", False)),
               "main_fallback_max_scrolls": int(config.get("main_fallback_max_scrolls", 20)),
               "compiler_cache": str(store / ".swift-bin"),
               "capture_window": {"start": since.isoformat(), "end": end.isoformat(), "cutoff": cutoff.isoformat()},
               "scroll_delta": min(120, max(1, abs(int(config.get("scroll_delta", 120))))),
               "settle_ms": max(800, int(config.get("settle_ms", 800))),
               "idle_limit": max(6, int(config.get("idle_limit", 6))),
               "checkpoint_every": min(10, max(1, int(config.get("checkpoint_every", 5)))),
               # alignment-gap 的实测根因：复制内嵌表情的「选中+复制」扰动选中/滚动状态，
               # Swift 置 selectionMoved 后直接判失败。仅对这类会话关闭该步骤（表情保留占位符）。
               "copy_inline_text": bool(config.get("copy_inline_text", True)) and not alignment_gap_stalled(entry),
               "capture_inline_images": bool(config.get("capture_inline_images", False)),
               "max_screens": max_screens,
               "max_scrolls": max(int(config.get("max_scrolls", 1000)),
                                  int(entry.get("last_scroll_count") or 0) + 300)}
        one_path = stage / "chat-config.json"
        atomic_json(one_path, one)
        command = [sys.executable, str(BASE / "chat_capture.py"), "--config", str(one_path),
                   "--start", since.isoformat(), "--end", end.isoformat(),
                   "--output-dir", str(stage), "--repository-stage"]
        reanchors = 0
        began = systime.monotonic()
        while True:
            code = run_child(command)
            raw_file = stage / f"chat-{entry['key']}-raw.json"
            raw_stop = None
            if raw_file.exists():
                try:
                    raw_meta = json.loads(raw_file.read_text(encoding="utf-8"))
                    entry["last_scroll_count"] = raw_meta.get("scroll_count") or 0
                    raw_stop = raw_meta.get("stop_reason")
                except (OSError, ValueError):
                    pass
            outcome = commit_capture(store, manifest, entry, run_id, stage, since, args.start, cutoff)
            outcome["capture_exit"] = code
            progress_path = stage / "progress.json"
            try:
                stage_progress = json.loads(progress_path.read_text(encoding="utf-8"))
                last_item = (stage_progress.get("conversations") or [{}])[-1]
                open_exit, history_exit = last_item.get("open_exit"), last_item.get("history_exit")
            except (OSError, ValueError, IndexError):
                open_exit = history_exit = None
            if open_exit in REANCHOR_EXITS and reanchors < REANCHOR_RETRIES:
                # Exact coordinates are re-read on every open, so retrying once re-anchors to the
                # window as it is now — a moved window (user came back) recovers instead of losing
                # the whole round.
                reanchors += 1
                print(f"窗口或布局变化（退出码 {open_exit}），重新锚定坐标后重试一次："
                      f"{entry['names'][-1]}", flush=True)
                continue
            break
        outcome["open_exit"] = open_exit
        if code == 27:
            # 会话名对不上（改名或成员离职），既不是数据故障也不是采集失败：
            # 记为待确认，等侧栏出现新名字或人工确认后再处理。
            entry["last_status"] = "review:name_changed"
            entry["name_unresolved"] = int(entry.get("name_unresolved") or 0) + 1
            outcome["reason"] = "name_changed"
            print(f"[名称待确认] {entry['names'][-1]}", flush=True)
        if code == 26:
            # 网页应用：没有聊天记录可采。写入 exclude_reason 后每轮直接跳过，不再重试。
            entry["excluded"] = True
            entry["exclude_reason"] = "网页应用"
            entry["last_status"] = "excluded:网页应用"
            entry.pop("partial_attempts", None)
            outcome["reason"] = "webview"
            print(f"[网页应用] {entry['names'][-1]} → 标记不采", flush=True)
        if code != 0 and open_exit is None:
            capture_failures += 1
            print(f"采集子进程异常退出：{entry['names'][-1]}（退出码 {code}，未写入会话定位结果，"
                  f"连续 {capture_failures}/{ABORT_AFTER_CAPTURE_FAILURES}）；按会话失败记录，本轮继续",
                  flush=True)
        else:
            capture_failures = 0
        blocked_by = dialog_block_reason(stage, open_exit, history_exit)
        if blocked_by:
            outcome["dialog_blocked"] = blocked_by
            note_dialog_block(entry, blocked_by, run_id)
            dialog_streak.append(entry)
            print(f"弹窗阻塞：{entry['names'][-1]}（累计 {entry['dialog_blocks']} 次）{blocked_by}", flush=True)
        else:
            dialog_streak.clear()
        outcomes.append(outcome)
        tally = {s: sum(1 for o in outcomes if o["status"] == s) for s in ("ok", "partial", "failed", "review")}
        print(f"[轮次进度] {len(outcomes)}/{len(planned)} · ok {tally['ok']} partial {tally['partial']} "
              f"failed {tally['failed']} review {tally['review']} · {entry['names'][-1]} → {outcome['status']}"
              f"（{systime.monotonic() - began:.0f}s）", flush=True)
        if outcome["status"] == "failed":
            # code 27（名称待确认）上面已写 review:name_changed，别被 failed 覆盖掉。
            if not str(entry.get("last_status") or "").startswith("review:"):
                entry["last_status"] = "failed"
        elif outcome["status"] == "review":
            # review 是 commit 的早退分支（重叠未核验等），不发布快照也不写覆盖字段；
            # 若这里不回写，last_status 会停在上一轮的 failed/ok，缺口报告就会误标。
            entry["last_status"] = f"review:{outcome.get('reason') or 'unspecified'}"
        note_partial_attempt(entry, outcome["status"], open_exit, raw_stop, candidate.get("last_activity"))
        tracker.observe(candidate, entry, open_exit)
        atomic_json(store / "manifest.json", manifest)
        abort_reason = stop_reason_for_open(open_exit, code, tracker.consecutive, capture_failures)
        if not abort_reason and len(dialog_streak) >= 3:
            # The same dialog is blocking every conversation: an environment problem, not theirs.
            for blocked in dialog_streak:
                blocked["dialog_blocks"] = max(0, int(blocked.get("dialog_blocks") or 0) - 1)
            abort_reason = f"{CONSECUTIVE_DIALOG_BLOCK}：{blocked_by}"
        if abort_reason == CONSECUTIVE_OPEN_FAILURE:
            abort_reason = tracker.resolve_streak(probe)
            if abort_reason is None:
                print("对照会话可正常打开：本次连续失败计入各会话自身，继续后续会话", flush=True)
        atomic_json(store / "manifest.json", manifest)
        if abort_reason:
            print(f"已停止本轮：{abort_reason}；已落盘结果保留", flush=True)
            break
    if not abort_reason and tracker.pending:
        if tracker.reference is not None:
            abort_reason = tracker.resolve_streak(probe)
        else:
            review.extend({"key": e["key"], "reason": "open_failure_unconfirmed", "open_exit": code}
                          for e, code in tracker.pending)
    tracker.discard()
    manifest["runs"][-1]["status"] = "stopped_environment" if abort_reason else "finished"
    manifest["runs"][-1]["finished_at"] = datetime.now().astimezone().isoformat()
    if abort_reason:
        manifest["runs"][-1]["stop_reason"] = abort_reason
    manifest["runs"][-1]["review"] = review
    manifest["runs"][-1]["outcomes"] = outcomes
    manifest["runs"][-1]["reference_probes"] = probes
    manifest["runs"][-1]["coverage_complete"] = bool(not abort_reason and candidate_complete and not review and
        all(x["status"] == "ok" for x in outcomes) and
        all(entry.get("history_complete") for _, entry in chosen))
    atomic_json(store / "manifest.json", manifest)
    print(json.dumps({**plan, "outcomes": outcomes}, ensure_ascii=False, indent=2))
    return 0 if manifest["runs"][-1]["coverage_complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
