#!/usr/bin/env python3
"""按配置逐个会话执行准备、滚动、日期过滤和验证；失败会话不冒充成功。"""
from __future__ import annotations

import argparse
import os
import json
import re
import shutil
import subprocess
import sys
import time
import ui_lock
from datetime import date, datetime
from pathlib import Path

from history_supplement import supplement
from chat_export import DATE_PARSER_VERSION
from fs_utils import ensure_dir
from swift_runtime import swift_command

BASE = Path(__file__).resolve().parent
# wework_open_row / wework_history_capture: an unknown, undismissable or recurring dialog blocked this conversation.
DIALOG_BLOCKED_EXIT = 25


def run(command: list[str], log_path: Path) -> int:
    redacted = []
    hide_next = False
    for token in command:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
        else:
            redacted.append(Path(token).name if token.startswith("/") else token)
            hide_next = token in {"--conversation", "--conversation-metadata", "--sender-directory"}
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n$ " + " ".join(redacted) + "\n")
        log.flush()
        return subprocess.run(command, cwd=BASE, stdout=log, stderr=subprocess.STDOUT, **ui_lock.child_options()).returncode


def label_main_raw(raw_path: Path, name: str) -> None:
    """Record the conversation on a main-channel raw file.

    wework_scroll_capture does not write the conversation name; the repository rejects
    raw files whose conversation does not match the expected identity. It is only called
    after wework_open_row verified the selected row by name, and never overwrites a name.
    """
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if raw.get("conversation"):
        return
    raw["conversation"] = name
    raw["conversation_source"] = "verified_open_row"
    tmp = raw_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(raw_path)



def plausible_rename(old: str, new: str) -> bool:
    """判断新名是否可能是旧名改名而来：成员重合，或互为前缀/后缀。
    否则就是点错了行，不能登记为别名——否则会把邻行的部门名写进会话名。"""
    if not old or not new:
        return False
    a, b = old.split("、"), new.split("、")
    if len(a) >= 2 and len(b) >= 2:
        sa, sb = set(a), set(b)
        common = len(sa & sb)
        # 纯加人 / 纯减人（离职导致自动群名变短）也是真改名，不能只按 60% 重合卡住。
        if common >= 2 and (sa <= sb or sb <= sa):
            return True
        return common >= 2 and common / max(len(a), len(b)) >= 0.6
    sa, sb = old.strip(), new.strip()
    return sa.startswith(sb) or sb.startswith(sa) or sa in sb or sb in sa

def main() -> int:
    parser = argparse.ArgumentParser(description="企业微信会话批量只读采集")
    parser.add_argument("--config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--progress", help="进度文件；默认 <output-dir>/progress.json")
    parser.add_argument("--no-resume", action="store_true", help="忽略进度文件，全部重跑")
    parser.add_argument("--self-names", help="判定「@我」用的本人名字，逗号分隔；默认读配置里的 self_names")
    parser.add_argument("--capture-engine", choices=["history", "main"], help="默认原生记录面板；main 为主聊天区兼容通道")
    parser.add_argument("--repository-stage", action="store_true", help="由 chat_repository.py 调用时只报告阶段进度")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        parser.error("结束日期不能早于开始日期")
    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engine = args.capture_engine or config.get("capture_engine", "history")
    if engine not in {"history", "main"}:
        parser.error("capture_engine 只能为 history 或 main")
    output_dir = Path(args.output_dir or config.get("output_dir") or "exports").expanduser()
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    ensure_dir(output_dir)
    conversations = config.get("conversations") or []
    if not conversations:
        raise SystemExit("配置中的 conversations 不能为空；先运行会话目录脚本或手工指定会话。")
    # at_me 依赖本人名字做「@我」判定；不传时 at_me 恒为 null（历史批次就是因为漏传才全空）
    raw_self = args.self_names or ",".join(config.get("self_names") or [])
    self_names = [x.strip() for x in str(raw_self).split(",") if x.strip()]
    profile = {"engine": engine, "schema_version": 3, "self_names": sorted(self_names),
               "date_parser_version": DATE_PARSER_VERSION,
               "capture_inline_images": bool(config.get("capture_inline_images", False)),
               "copy_inline_text": bool(config.get("copy_inline_text", True))}
    progress_path = Path(args.progress) if args.progress else output_dir / "progress.json"
    progress: dict[str, dict] = {}
    profile_changed = False
    if progress_path.exists() and not args.no_resume:
        try:
            loaded = json.loads(progress_path.read_text(encoding="utf-8"))
            if loaded.get("requested_range") and loaded["requested_range"] != {"start": str(start), "end": str(end)}:
                raise SystemExit("进度文件的时间区间与本次不符；请使用新的输出目录或 --no-resume。")
            progress = {str(item.get("prefix")): item for item in loaded.get("conversations", [])}
            if loaded.get("capture_profile") != profile:
                profile_changed = True
                for item in progress.values():
                    item["status"] = "partial"
                    item["note"] = "采集通道或结构版本更新，旧成品留档后重采"
        except (json.JSONDecodeError, AttributeError):
            progress = {}
    # 进度文件可能落后于磁盘（进程被杀、写入未落盘），以磁盘产物为准做一次校正
    for prefix in list(progress):
        entry = progress[prefix]
        if entry.get("status") == "ok" and not (output_dir / f"{prefix}.jsonl").exists():
            entry["status"] = "failed"
            entry["note"] = "进度记为完成但磁盘无成品，按未完成处理"
    if progress:
        done = sum(1 for item in progress.values() if item.get("status") == "ok")
        print(f"沿用进度：已完成 {done} / {len(progress)}，本次只跑未完成项")

    def save_progress() -> None:
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"),
                   "requested_range": {"start": str(start), "end": str(end)},
                   "config": str(config_path), "drift": drift, "capture_profile": profile,
                   "conversations": [progress[key] for key in sorted(progress)]}
        tmp = progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(progress_path)

    ui_lock.acquire()
    # 单实例锁：两个批次同时跑会各自持有一份进度快照，后写的覆盖先写的，
    # 造成"磁盘有成品、进度文件没记录"。启动前先确认没有同目录的批次在跑。
    lock_path = output_dir / ".capture.lock"
    if lock_path.exists():
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid") or 0)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)          # 信号 0：只探测进程是否存在
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if alive:
                raise SystemExit(f"已有采集批次在跑（pid {pid}，开始于 {payload.get('started_at')}）。"
                                 f"先停止它，或确认已退出后删除 {lock_path}")
        except (json.JSONDecodeError, ValueError):
            pass
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds"),
                                     "config": str(config_path)}, ensure_ascii=False), encoding="utf-8")
    batch_started = time.monotonic()
    durations: list[float] = []
    total = len(conversations)
    done_before = sum(1 for item in progress.values() if item.get("status") == "ok")

    def fmt(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"

    def progress_line(index: int, name: str, state: str, elapsed_item: float | None = None) -> str:
        ok = sum(1 for item in progress.values() if item.get("status") == "ok")
        partial = sum(1 for item in progress.values() if item.get("status") == "partial")
        failed = sum(1 for item in progress.values() if item.get("status") == "failed")
        messages = sum(item.get("messages") or 0 for item in progress.values())
        used = time.monotonic() - batch_started
        avg = (sum(durations) / len(durations)) if durations else 0.0
        remaining_items = total - index
        eta = avg * remaining_items if avg else 0.0
        tail = f"｜本条 {fmt(elapsed_item)}" if elapsed_item is not None else ""
        return (f"[进度] {index}/{total} ({index * 100 // total}%) "
                f"｜ok {ok} partial {partial} failed {failed} "
                f"｜消息 {messages} ｜用时 {fmt(used)} ｜均 {fmt(avg)}/个 ｜预计剩余 {fmt(eta)} {tail}｜{state}：{name[:28]}")

    # 长跑批次（整年上千个会话）必然会被打断：屏保、锁屏、超时、人为中止。
    # 除了每个会话落盘一次的 progress.json，再写两份"过程状态"产物：
    #   run-state.json  实时游标：当前跑到第几个、哪个会话、处于哪个阶段
    #   run-ledger.jsonl 追加式台账：每次尝试一行，跨重启累积，可回溯"哪个会话重试过几次"
    state_path = output_dir / "run-state.json"
    ledger_path = output_dir / "run-ledger.jsonl"
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_started_at = datetime.now().isoformat(timespec="seconds")

    def tally() -> dict:
        return {
            "ok": sum(1 for item in progress.values() if item.get("status") == "ok"),
            "partial": sum(1 for item in progress.values() if item.get("status") == "partial"),
            "failed": sum(1 for item in progress.values() if item.get("status") == "failed"),
            "messages": sum(item.get("messages") or 0 for item in progress.values()),
        }

    def mark_state(index: int | None = None, name: str | None = None, prefix: str | None = None,
                   phase: str | None = None, note: str | None = None) -> None:
        payload = {"batch_id": batch_id, "pid": os.getpid(),
                   "started_at": batch_started_at,
                   "updated_at": datetime.now().isoformat(timespec="seconds"),
                   "config": str(config_path),
                   "requested_range": {"start": str(start), "end": str(end)},
                   "order": config.get("candidate_order"),
                   "total": total, "index": index, "conversation": name, "prefix": prefix,
                   "phase": phase, "note": note, **tally()}
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(state_path)

    def ledger(entry: dict) -> None:
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                     "batch_id": batch_id, **entry}, ensure_ascii=False) + "\n")

    mark_state(phase="starting", note="批次已启动，尚未开始第一个会话")
    print(f"批次启动：{total} 个会话，区间 {start} ~ {end}，进度文件 {progress_path}")
    tier_counts: dict[str, int] = {}
    for item in conversations:
        tier = item.get("tier") or "unknown"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    if len(tier_counts) > 1 or "tier" in (conversations[0] or {}):
        # certain：最后活跃在窗口内，窗口内必有消息；verify：晚于窗口结束，需进会话读到才能确认
        print(f"候选分层：{tier_counts}；采集顺序已按 certain → verify、同档内由新到旧排列")
    print(f"窗口口径：每个会话回溯到 {start}，跨月消息在同一次采集中连续落盘")
    missing_seek = [x for x in conversations if x.get("index") and not x.get("seek_date")]
    if missing_seek:
        sample = "、".join(str(x.get("output_prefix") or "?") for x in missing_seek[:6])
        more = "…" if len(missing_seek) > 6 else ""
        print(f"⚠️ {len(missing_seek)} 个会话没有 seek_date，只能按序号定位（{sample}{more}）")
        print("   序号会漂移，但打开成功后一律按「选中行行名」校验，对不上重试三轮再判失败；补上日期能明显减少重试")
    if not self_names:
        print("⚠️ 未提供 self_names：at_me 将恒为 null，无法统计「被@」")
    print(f"[进度] 起点：已有 {done_before} 个完成，本次待处理 {total - done_before} 个")

    caffeinate = subprocess.Popen(["caffeinate", "-dims"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    drift = 0
    consecutive_open_failures = 0
    abort_reason = None
    def tool(name: str) -> list[str]:
        return swift_command(BASE / f"{name}.swift", Path(config.get("compiler_cache") or output_dir / ".swift-bin"))

    def main_capture(prefix: Path, common: list[str], conversation_type: str | None, supplement_only: bool = False) -> list[str]:
        limit = config.get("main_fallback_max_scrolls", 20) if supplement_only else config.get("max_scrolls", 1000)
        return tool("wework_scroll_capture") + ["--year", str(start.year), "--since", str(start),
            "--output-prefix", str(prefix), "--max-scrolls", str(limit),
            "--idle-limit", str(config.get("idle_limit", 6)), "--checkpoint-every", str(config.get("checkpoint_every", 10)),
            "--conversation-type", conversation_type if conversation_type in {"single", "group"} else "unknown", *common,
            *(["--pixel-direction"] if config.get("pixel_direction") else [])]
    try:
        subprocess.run(["open", "-a", "企业微信"], check=True)
        for index, item in enumerate(conversations, 1):
            item_started = time.monotonic()
            name = (item.get("name") or "").strip()
            if name:
                print(f"[开始] {index}/{total} {name[:28]}", flush=True)
            if not name:
                results.append({"success": False, "error": "缺少会话名称"})
                continue
            prefix = Path(item.get("output_prefix") or f"conversation-{index:04d}").name
            mark_state(index, name, prefix, phase="opening")
            if progress.get(prefix, {}).get("conversation") not in (None, name):
                progress[prefix]["status"] = "partial"
            if progress.get(prefix, {}).get("status") == "ok" and not args.no_resume:
                mark_state(index, name, prefix, phase="skipped")
                print(progress_line(index, name, "跳过已完成"), flush=True)
                results.append({**progress[prefix], "conversation": name, "success": True, "skipped": True})
                continue
            output_prefix = output_dir / prefix
            log_path = output_dir / f"{prefix}.log"
            log_path.write_text(f"会话输出前缀：{prefix}\n请求区间：{start} 至 {end}\n开始时间：{datetime.now().isoformat()}\n", encoding="utf-8")
            common = ["--scroll-delta", str(config.get("scroll_delta", 120)), "--settle-ms", str(config.get("settle_ms", 800))]
            raw_path = Path(str(output_prefix) + "-raw.json")
            raw_mtime_before = raw_path.stat().st_mtime_ns if raw_path.exists() else None
            # 迁移通道时不把旧解析错误的记录并回新结果；留档放子目录，汇总不会重复扫描。
            jsonl_path = Path(str(output_prefix) + ".jsonl")
            migration = profile_changed and jsonl_path.exists()
            if jsonl_path.exists():
                old_rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                expected_source = "macos_accessibility_history" if engine == "history" else "macos_accessibility_scroll"
                migration = migration or any(row.get("source") != expected_source for row in old_rows)
                if raw_path.exists():
                    try:
                        old_raw = json.loads(raw_path.read_text(encoding="utf-8"))
                        migration = migration or old_raw.get("date_parser_version", 1) != DATE_PARSER_VERSION
                        if engine == "history" and old_raw.get("tool") == "wework_history_capture":
                            old_copy = bool((old_raw.get("inline_text_copy") or {}).get("enabled", False))
                            migration = migration or old_copy != bool(config.get("copy_inline_text", True))
                    except (OSError, json.JSONDecodeError):
                        migration = True
                if migration:
                    archive = output_dir / "_previous" / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
                    archive.mkdir(parents=True)
                    for suffix in (".jsonl", ".csv", "-raw.json", "-validation.json", ".prev.jsonl"):
                        old = Path(str(output_prefix) + suffix)
                        if old.exists():
                            shutil.copy2(old, archive / old.name)
            conversation_type = item.get("conversation_type")
            if not conversation_type and item.get("metadata"):
                try:
                    metadata_path = (config_path.parent / item["metadata"]).resolve()
                    conversation_type = json.loads(metadata_path.read_text(encoding="utf-8")).get("conversation_type")
                except (OSError, json.JSONDecodeError):
                    conversation_type = None
            # 有列表序号时按行点击打开（搜索框对自动群名无效，且写值不触发搜索），
            # 无序号时仍按名字查找列表；序号会随新消息漂移，用上一次的 OPENED 序号累积补偿。
            index_hint = item.get("index")
            seek_date = item.get("seek_date")
            # 绝对日期是唯一稳定的锚点：侧栏的时间文本会变（「昨天」→「星期三」→「9/21」），
            # 行号会随新增/删除整体漂移。定位器拿到绝对日期后自行把每行文本还原成日期再比较。
            seek_day = (item.get("last_activity") or "")[:10]
            if seek_date:
                # 先用序号跳个大概（快），再用绝对日期插值定位（序号会漂移，日期不会）
                open_step = tool("wework_open_row") + ["--conversation", name,
                             *(["--seek-day", seek_day] if seek_day else []),
                             "--seek-date", str(seek_date),
                             *(["--index", str(index_hint), "--drift", str(drift)] if index_hint else []),
                             "--max-screens", str(config.get("max_screens", 8))]
            elif index_hint:
                open_step = tool("wework_open_row") + ["--conversation", name,
                             *(["--seek-day", seek_day] if seek_day else []),
                             "--index", str(index_hint), "--drift", str(drift),
                             "--max-screens", str(config.get("max_screens", 8))]
            else:
                open_step = tool("wework_open_row") + ["--conversation", name,
                             *(["--seek-day", seek_day] if seek_day else []),
                             "--max-screens", str(config.get("max_screens", 8))]
            open_exit = run(open_step, log_path)
            if open_exit == 7:
                # 最后兜底：日期与行号都没命中时改走左侧搜索框。
                # wework_prepare_chat 把名字写进搜索框，等 AXSystemDialog 浮层渲染，
                # 取最上方唯一匹配后 AXPress，并用右侧标题校验；结束时清空搜索框。
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write("按日期/行号未命中，改用搜索框兜底\n")
                open_exit = run(tool("wework_prepare_chat") + ["--conversation", name], log_path)
                if open_exit != 0 and "、" in name:
                    # 整串名字搜不到，多半是群名变了（成员离职后自动群名变短，或本人改过名）。
                    # 改用最长的一个成员名做查询词，再按「成员子集」核对结果是否是同一个会话。
                    token = max((x.strip() for x in name.split("、")), key=len)
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"整串名未搜到，改用成员名「{token}」再搜一次\n")
                    open_exit = run(tool("wework_prepare_chat") +
                                    ["--conversation", token, "--expect", name], log_path)
                if open_exit == 28:
                    # 搜到了也点开了，但右侧没有会话标题 —— 点开是网页而不是聊天，按网页应用处理。
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write("搜索命中但无会话标题，按网页应用处理\n")
                    sys.exit(26)
                if open_exit != 0:
                    # 两种都搜不到：这不是采集故障，而是名称对不上（改名/离职）。
                    # 单独上报，避免和真正的失败混在一起，也不再每轮重复重试。
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write("按名与按成员名均未定位到，标记为名称待确认\n")
                    sys.exit(27)
            history_exit = None
            execution_success = open_exit == 0
            consecutive_open_failures = consecutive_open_failures + 1 if open_exit in {6, 7, 8, 11, 13} else 0
            if execution_success:
                # 网页应用（服务号/技术支持机器人/微盘等）点开后右侧是网页，没有聊天记录可采。
                # 判据用界面而不是名字：右侧出现 AXWebArea 即为网页应用，直接退出 26 交给仓库标记。
                if run(tool("wework_webview_probe"), log_path) == 0:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write("识别为网页应用（右侧 AXWebArea），标记不采\n")
                    sys.exit(26)
                mark_state(index, name, prefix, phase="scrolling")
                if engine == "main":
                    execution_success = run(main_capture(output_prefix, common, conversation_type), log_path) == 0
                    if execution_success:
                        label_main_raw(raw_path, name)
                else:
                    history_tool = tool("wework_history_capture")
                    command = history_tool + ["--conversation", name, "--since", str(start), "--output-prefix", str(output_prefix),
                        "--self-names", ",".join(self_names), "--conversation-type", conversation_type or "unknown",
                        "--max-scrolls", str(config.get("max_scrolls", 1000)), "--idle-limit", str(config.get("idle_limit", 6)),
                        "--checkpoint-every", str(config.get("checkpoint_every", 10)), *common,
                        *(["--capture-inline-images"] if config.get("capture_inline_images", False) else []),
                        *(["--no-copy-inline-text"] if not config.get("copy_inline_text", True) else [])]
                    code = run(command, log_path)
                    history_exit = code
                    # 采集器发现标题已改名（成员减少）时会打 RENAMED：同样登记为别名，
                    # 否则下一轮扫描还拿旧名，永远对不上。
                    renamed = re.search(r"RENAMED (.+)", log_path.read_text(encoding="utf-8", errors="replace"))
                    if renamed:
                        new_name = renamed.group(1).strip()
                        if new_name and new_name != name and plausible_rename(name, new_name):
                            Path(str(output_prefix) + "-opened.json").write_text(
                                json.dumps({"opened_name": new_name, "requested": name}, ensure_ascii=False),
                                encoding="utf-8")
                    fresh_checkpoint = False
                    if code != 0 and raw_path.exists() and raw_path.stat().st_mtime_ns != raw_mtime_before:
                        try:
                            checkpoint = json.loads(raw_path.read_text(encoding="utf-8"))
                            fresh_checkpoint = checkpoint.get("tool") == "wework_history_capture" and bool(checkpoint.get("records"))
                        except (OSError, json.JSONDecodeError):
                            pass
                    if code != 0 and fresh_checkpoint:
                        # 突然中断时保留本轮原生检查点；兼容通道会覆盖同名 raw。
                        log_path.open("a", encoding="utf-8").write("原生通道中断：保留已落盘检查点，按 partial 导出；不覆盖为兼容通道。\n")
                    elif code in {2, 3, 4, 8, 20, 21, 22, 23, DIALOG_BLOCKED_EXIT}:
                        execution_success = False # 身份、环境、权限和弹窗阻塞都不能降级去读另一个通道。
                    elif code != 0:
                        ready = run(history_tool + ["--conversation", name, "--close-panel", "--main-latest"], log_path) == 0
                        execution_success = ready and run(main_capture(output_prefix, common, conversation_type), log_path) == 0
                        if execution_success:
                            label_main_raw(raw_path, name)
                    else:
                        primary = json.loads(raw_path.read_text(encoding="utf-8"))
                        if primary.get("unresolved_rows") and config.get("main_fallback", True):
                            evidence_dir = output_dir / "_evidence"
                            ensure_dir(evidence_dir)
                            evidence_prefix = evidence_dir / f"{prefix}-main"
                            shutil.copy2(raw_path, evidence_dir / f"{prefix}-history-raw.json")
                            ready = run(history_tool + ["--conversation", name, "--close-panel", "--main-latest"], log_path) == 0
                            if ready and run(main_capture(evidence_prefix, common, conversation_type, supplement_only=True), log_path) == 0:
                                main = json.loads(Path(str(evidence_prefix) + "-raw.json").read_text(encoding="utf-8"))
                                combined = supplement(primary, main, name, conversation_type or "unknown")
                                raw_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
            if execution_success and index_hint:
                # 打开成功后回读实际序号，更新漂移量，供后续会话使用
                opened = re.search(r"OPENED (\d+)(?: (.*))?", log_path.read_text(encoding="utf-8", errors="replace"))
                if opened:
                    drift = int(opened.group(1)) - int(index_hint)
                    # 打开后行名是「当前真实名称」：群成员离职会让自动群名变短，
                    # 侧栏只有打开过才刷新。把它写下来交给仓库登记成新别名，
                    # 否则下次扫描拿旧名永远对不上，会话会被反复判为找不到。
                    opened_name = (opened.group(2) or "").strip()
                    if opened_name and opened_name != name and plausible_rename(name, opened_name):
                        Path(str(output_prefix) + "-opened.json").write_text(
                            json.dumps({"opened_name": opened_name, "requested": name}, ensure_ascii=False),
                            encoding="utf-8")
            # 上一次采集的成品留档，供本次导出后做跨次对齐合并（活跃会话要分多次才能回溯完）
            jsonl_path = Path(str(output_prefix) + ".jsonl")
            prev_path = Path(str(output_prefix) + ".prev.jsonl")
            if jsonl_path.exists() and not migration:
                prev_path.write_text(jsonl_path.read_text(encoding="utf-8"), encoding="utf-8")
            if execution_success:
                mark_state(index, name, prefix, phase="exporting")
                export_command = [sys.executable, str(BASE / "chat_export.py"), str(raw_path), "--start", str(start), "--end", str(end), "--output-prefix", str(output_prefix), "--conversation", name]
                if item.get("metadata"):
                    export_command += ["--conversation-metadata", str((config_path.parent / item["metadata"]).resolve())]
                if item.get("sender_directory"):
                    export_command += ["--sender-directory", str((config_path.parent / item["sender_directory"]).resolve())]
                if self_names:
                    export_command += ["--self-names", ",".join(self_names)]
                execution_success = run(export_command, log_path) == 0
            merge_stats = None
            # 完整原生窗口本身已覆盖本次区间，无需把旧的半成品或旧通道并回来。
            native_complete = False
            if execution_success:
                current_raw = json.loads(raw_path.read_text(encoding="utf-8"))
                native_complete = current_raw.get("tool") == "wework_history_capture" and current_raw.get("coverage_complete") is True
            if execution_success and prev_path.exists() and not migration and not native_complete:
                previous_sources = {json.loads(line).get("source") for line in prev_path.read_text(encoding="utf-8").splitlines() if line.strip()}
                current_sources = {json.loads(line).get("source") for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()}
                if previous_sources and current_sources and previous_sources != current_sources:
                    migration = True
                    log_path.open("a", encoding="utf-8").write("采集回退导致通道变化：保留 prev 留档，不跨通道合并。\n")
            if execution_success and prev_path.exists() and not migration and not native_complete:
                merge_output = Path(str(output_prefix) + "-merge.json")
                ok = run([sys.executable, str(BASE / "chat_merge.py"), "--previous", str(prev_path),
                          "--current", str(jsonl_path), "--output-prefix", str(output_prefix)], log_path) == 0
                if merge_output.exists():
                    try:
                        merge_stats = json.loads(merge_output.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        merge_stats = None
                # 无重叠（无法验证接续）不算失败，但要显式记录，不能当已采全
                if not ok and merge_stats:
                    log_path.open("a", encoding="utf-8").write(
                        f"警告：{merge_stats.get('warning')}\n")
            validation_report = None
            if execution_success:
                mark_state(index, name, prefix, phase="validating")
                validation_path = Path(str(output_prefix) + "-validation.json")
                validation_code = run([sys.executable, str(BASE / "chat_validate.py"), str(output_prefix) + ".jsonl", "--raw", str(raw_path), "--start", str(start), "--end", str(end), "--summary-output", str(validation_path)], log_path)
                if validation_path.exists():
                    try:
                        validation_report = json.loads(validation_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        validation_report = None
                if validation_code == 2:
                    execution_success = False
            coverage_complete = bool(
                execution_success
                and validation_report
                and validation_report.get("coverage_complete") is True
                and (not merge_stats or (
                    merge_stats.get("alignment") == "found"
                    and merge_stats.get("boundary_verified") is True
                ))
            )
            success = execution_success and coverage_complete
            # 每跑完一个会话就落盘一次进度：采集可能随时被打断（屏保、锁屏、超时），
            # 只有 progress.json 才能说明"跑到哪了、哪些没成"，中断后重跑同一条命令即可续上。
            # 三态：ok＝采到且覆盖完整；partial＝采到了但边界未验证（可续采补）；failed＝没采到
            status = "failed"
            if execution_success:
                status = "ok" if coverage_complete else "partial"
            entry = {"prefix": prefix, "conversation": name, "index": index_hint,
                     "tier": item.get("tier"), "last_activity": item.get("last_activity"),
                     "seek_date_present": bool(item.get("seek_date")),
                     "status": status,
                     "open_exit": open_exit,
                     "history_exit": history_exit,
                     "engine": engine,
                     "execution_success": execution_success, "coverage_complete": coverage_complete,
                     "messages": None, "stop_reason": None, "boundary_verified": None, "log": str(log_path)}
            jsonl_file = Path(str(output_prefix) + ".jsonl")
            if jsonl_file.exists():
                try:
                    exported = [json.loads(line) for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
                    entry["messages"] = len(exported)
                    entry["stop_reason"] = sorted({row.get("stop_reason") for row in exported if row.get("stop_reason")}) or None
                    entry["boundary_verified"] = all(row.get("boundary_verified") for row in exported) if exported else None
                except (OSError, json.JSONDecodeError):
                    pass
            progress[prefix] = entry
            save_progress()
            ledger({"prefix": prefix, "conversation": name, "index": index_hint,
                    "tier": item.get("tier"), "status": status, "messages": entry.get("messages"),
                    "stop_reason": entry.get("stop_reason"), "boundary_verified": entry.get("boundary_verified"),
                    "elapsed_s": round(time.monotonic() - item_started, 1)})
            durations.append(time.monotonic() - item_started)
            print(progress_line(index, name, "完成" if success else "未完整", time.monotonic() - item_started), flush=True)
            results.append({
                "conversation": name, "index": index_hint, "drift": drift,
                "execution_success": execution_success, "coverage_complete": coverage_complete,
                "success": success, "merge": merge_stats,
                "csv": str(output_prefix) + ".csv" if Path(str(output_prefix) + ".csv").exists() else None,
                "jsonl": str(output_prefix) + ".jsonl" if Path(str(output_prefix) + ".jsonl").exists() else None,
                "validation": str(output_prefix) + "-validation.json" if validation_report else None,
                "log": str(log_path),
            })
            if open_exit in {2, 3, 4, 5, 20, 21, 22, 23, 24}:
                abort_reason = f"会话定位环境失败（退出码 {open_exit}），已停止批次，避免后续会话连续空采"
            elif consecutive_open_failures >= 3:
                abort_reason = "连续 3 个会话定位失败，已停止批次；检查企业微信窗口、前台焦点和候选索引"
            if abort_reason:
                mark_state(index, name, prefix, phase="blocked", note=abort_reason)
                print(abort_reason, flush=True)
                break
            if not execution_success and not config.get("continue_on_error", True):
                break
        if not abort_reason:
            mark_state(phase="finished", note="本批次正常跑完；是否采全要看 progress.json 与各会话 validation")
    finally:
        caffeinate.terminate()
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            caffeinate.wait(timeout=3)
        except subprocess.TimeoutExpired:
            caffeinate.kill()
    execution_success = all(item.get("execution_success", False) for item in results)
    conversation_coverage_complete = all(item.get("coverage_complete", False) for item in results)
    candidate_coverage = config.get("candidate_coverage") or {}
    candidate_source = config.get("candidate_source")
    candidate_coverage_complete = not args.repository_stage and (
        not candidate_source or candidate_coverage.get("coverage_status") in {"exact", "user_confirmed"})
    summary = {
        "requested_range": {"start": str(start), "end": str(end)},
        "execution_success": execution_success,
        "conversation_coverage_complete": conversation_coverage_complete,
        "candidate_coverage_complete": candidate_coverage_complete,
        "coverage_complete": conversation_coverage_complete and candidate_coverage_complete,
        "success": all(item["success"] for item in results),
        "results": results,
        "candidate_source": candidate_source,
        "candidate_coverage": candidate_coverage or None,
        "catalog_completeness_note": "仅采集配置中列出的会话；配置生成过程和未解决候选项决定目录覆盖完整性。",
    }
    summary_path = output_dir / "chat-run-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.repository_stage:
        print("单会话阶段已落盘；账号候选与跨轮覆盖请以仓库 manifest.json 的本轮结果为准。")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    pending = sum(1 for item in progress.values() if item.get("status") != "ok")
    if pending and not args.repository_stage:
        # 中断后重跑同一条命令即可续上：进度文件已落盘，ok 的会被跳过
        print(f"\n还有 {pending} 个会话未达 ok。查看状态："
              f"python3 {BASE / 'chat_progress.py'} {output_dir}"
              f"；续跑：重跑本次 chat_capture.py 命令（加 --no-resume 则全部重跑）")
    if not args.repository_stage:
        print(f"实时状态：{state_path}　过程台账：{ledger_path}")
    return 0 if summary["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
