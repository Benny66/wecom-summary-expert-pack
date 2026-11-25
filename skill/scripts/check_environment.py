#!/usr/bin/env python3
"""企业微信采集的本地环境只读自检。

检查四样东西，都不联网、不写文件、不启动子进程：
    采集模块   scripts/wecom/ 是否完整、PATH 里有没有 swiftc
    采集仓库   manifest.json 可解析、会话清单与覆盖状态
    输入文件   导出的 JSONL / CSV 是否具备必需字段
    本地台账   是否存在、能否解析、已记多少条

MCP 与 OAuth 授权无法由脚本判断，只能看客户端工具列表；脚本会在结尾提示这一项。

用法:
    python3 check_environment.py --capture
    python3 check_environment.py --store ~/wecom-data/wecom-chat-<标签>
    python3 check_environment.py --input exports/chat.jsonl --ledger ~/worklog/ledger.json
    python3 check_environment.py --store <仓库> --ledger <台账> --json

退出码:
    0  ready / partial
    1  needs_setup（指定的路径不可用，或没有给任何输入）
    2  unavailable（Python 低于 3.8）
"""

import argparse
import json
import os
import platform
import shutil
import sys

MIN_VERSION = (3, 8)
REQUIRED_FIELDS = ("conversation", "sender", "sent_at", "body", "message_type")
CAPTURE_MIN_SWIFT = 6
CSV_FIELDS = ("conversation", "sender", "sent_at", "body")


def python_ok():
    return sys.version_info >= MIN_VERSION


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def check_capture(base):
    """采集模块随技能分发：缺文件或缺 swiftc 都只能处理已有仓库，不能新增采集。"""
    result = {"ok": False, "swiftc": shutil.which("swiftc") or "", "files": 0, "notes": []}
    directory = os.path.join(base, "wecom")
    if not os.path.isdir(directory):
        result["notes"].append("采集模块缺失：{}".format(directory))
        return result
    names = os.listdir(directory)
    scripts = [n for n in names if n.endswith(".py")]
    tools = [n for n in names if n.endswith(".swift")]
    result["files"] = len(scripts) + len(tools)
    if "chat_repository.py" not in names:
        result["notes"].append("缺少 chat_repository.py：采集入口不可用")
        return result
    if len(tools) < CAPTURE_MIN_SWIFT:
        result["notes"].append("Swift 工具只有 {} 个，应为 {} 个；采集可能失败".format(
            len(tools), CAPTURE_MIN_SWIFT))
        return result
    if not result["swiftc"]:
        result["notes"].append("PATH 里没有 swiftc：首次采集需要 Xcode Command Line Tools")
        return result
    result["ok"] = True
    return result


def check_store(path):
    result = {"ok": False, "path": path, "conversations": [], "notes": []}
    manifest_path = os.path.join(path, "manifest.json")
    if not os.path.isdir(path):
        result["notes"].append("目录不存在")
        return result
    if not os.path.isfile(manifest_path):
        result["notes"].append("缺少 manifest.json：确认指向采集仓库根目录")
        return result
    try:
        manifest = read_json(manifest_path)
    except (ValueError, OSError) as exc:
        result["notes"].append("manifest.json 无法解析：{}".format(exc))
        return result

    conversations = manifest.get("conversations") or {}
    items = conversations.values() if isinstance(conversations, dict) else conversations
    for item in items:
        if not isinstance(item, dict):
            continue
        names = item.get("names") or []
        name = item.get("name") or (names[-1] if names else item.get("key", ""))
        count = item.get("messages") or item.get("message_count") or 0
        if not count:
            current = item.get("current_file") or os.path.join(
                "conversations", item.get("key", ""), "current.jsonl")
            current_path = current if os.path.isabs(current) else os.path.join(path, current)
            if os.path.isfile(current_path):
                with open(current_path, "r", encoding="utf-8") as fh:
                    count = sum(1 for line in fh if line.strip())
        result["conversations"].append({
            "name": name,
            "messages": count,
            "history_complete": item.get("history_complete"),
            "reached_start": item.get("reached_start"),
            "last_status": item.get("last_status"),
            "excluded": bool(item.get("excluded")),
        })
    if not result["conversations"]:
        result["notes"].append("仓库里没有任何会话记录")
        return result
    incomplete = [c["name"] for c in result["conversations"]
                  if c["history_complete"] is False and not c["excluded"]]
    if incomplete:
        result["notes"].append(
            "{} 个会话未证明采全，报告里不能写成全部历史".format(len(incomplete)))
    result["ok"] = True
    result["start"] = manifest.get("start", "")
    result["account_label"] = manifest.get("account_label", "")
    return result


def check_input(path):
    result = {"ok": False, "path": path, "kind": "", "rows": 0, "notes": []}
    if not os.path.isfile(path):
        result["notes"].append("文件不存在")
        return result
    suffix = os.path.splitext(path)[1].lower()
    result["kind"] = "csv" if suffix == ".csv" else "jsonl"
    if result["kind"] == "csv":
        import csv
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            missing = [field for field in CSV_FIELDS if field not in header]
            if missing:
                result["notes"].append("CSV 缺列：" + "、".join(missing))
                return result
            for _ in reader:
                result["rows"] += 1
    else:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                if result["rows"] == 0:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        result["notes"].append("第 {} 行不是合法 JSON".format(lineno))
                        return result
                    missing = [field for field in REQUIRED_FIELDS if field not in row]
                    if missing:
                        result["notes"].append("首行缺字段：" + "、".join(missing))
                        return result
                result["rows"] += 1
                if result["rows"] >= 100000:
                    break
    if result["rows"] == 0:
        result["notes"].append("文件里没有消息行")
        return result
    result["ok"] = True
    return result


def check_ledger(path):
    result = {"ok": False, "path": path, "items": 0, "states": {}, "exists": False, "notes": []}
    if not os.path.isfile(path):
        result["notes"].append("台账不存在：首次运行会新建，属正常")
        return result
    result["exists"] = True
    try:
        ledger = read_json(path)
    except (ValueError, OSError) as exc:
        result["notes"].append("台账无法解析：{}".format(exc))
        return result
    items = ledger.get("items") or {}
    result["items"] = len(items)
    for item in items.values():
        state = item.get("write_state", "unknown")
        result["states"][state] = result["states"].get(state, 0) + 1
    pending = [key for key, item in items.items()
               if item.get("write_state") in ("pending_create", "pending_update", "failed")]
    if pending:
        result["notes"].append(
            "{} 条处于 pending/failed，写入前先用标题检索确认上次是否已建".format(len(pending)))
    result["ok"] = True
    return result


def decide(args, capture, store, inp, ledger):
    if args.capture and not capture["ok"]:
        return "needs_setup", "采集模块或 Swift 工具链不可用"
    if args.capture and not args.store and not args.input:
        return "ready", "采集模块与 Swift 工具链可用"
    if not args.store and not args.input:
        return "needs_setup", "至少给一个输入：--store 采集仓库或 --input 导出文件"
    if args.store and not store["ok"]:
        return "needs_setup", "采集仓库不可用"
    if args.input and not inp["ok"]:
        return "needs_setup", "输入文件不可用"
    if ledger["exists"] and not ledger["ok"]:
        return "needs_setup", "台账无法解析"
    if not ledger["exists"]:
        return "partial", "能跑，但没有台账：首次运行，写入前会自动新建"
    return "ready", "输入与台账都可用"


def render_text(args, status, reason, capture, store, inp, ledger):
    lines = ["企业微信采集环境自检", "- 总体状态：{}（{}）".format(status, reason)]
    lines.append("- 采集模块：{} 个文件，swiftc={}".format(
        capture["files"], capture["swiftc"] or "未找到"))
    for note in capture["notes"]:
        lines.append("  提示：{}".format(note))
    lines.append("- Python：{}（要求 >= {}.{}）".format(
        platform.python_version(), MIN_VERSION[0], MIN_VERSION[1]))
    lines.append("- 系统：{}".format(platform.system()))
    if args.store:
        lines.append("- 采集仓库：{}".format(args.store))
        if store["ok"]:
            lines.append("  会话 {} 个，起始 {}".format(
                len(store["conversations"]), store.get("start") or "未记录"))
            for conv in store["conversations"][:20]:
                lines.append("    {}：{} 条，完整={}，状态={}".format(
                    conv["name"], conv["messages"], conv["history_complete"], conv["last_status"]))
        for note in store["notes"]:
            lines.append("  提示：{}".format(note))
    if args.input:
        lines.append("- 输入文件：{}（{}，{} 行）".format(
            args.input, inp.get("kind", ""), inp["rows"]))
        for note in inp["notes"]:
            lines.append("  提示：{}".format(note))
    if args.ledger:
        lines.append("- 台账：{}".format(args.ledger))
        if ledger["exists"]:
            lines.append("  已记 {} 条，状态 {}".format(
                ledger["items"], json.dumps(ledger["states"], ensure_ascii=False)))
        for note in ledger["notes"]:
            lines.append("  提示：{}".format(note))
    lines.append("- 下一项必须人工确认：客户端工具列表里有 search_knowledge_base、"
                 "create_document、update_document、submit_publish_request")
    lines.append("- 说明：本脚本只读本地文件，不联网、不写文件、不验证授权")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="企业微信采集的本地环境只读自检")
    parser.add_argument("--capture", action="store_true",
                        help="检查新增采集所需的模块与 Swift 工具链")
    parser.add_argument("--store", help="采集仓库根目录；由本技能或上游采集产生")
    parser.add_argument("--input", help="导出的消息 JSONL 或 CSV")
    parser.add_argument("--ledger", help="本地台账 JSON 路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    if not python_ok():
        payload = {"status": "unavailable", "reason": "Python 低于 3.8"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("Python {} 低于 3.8，升级后重试".format(platform.python_version()))
        return 2

    base = os.path.dirname(os.path.abspath(__file__))
    capture = check_capture(base)
    store = check_store(args.store) if args.store else {"ok": False, "notes": [], "conversations": []}
    inp = check_input(args.input) if args.input else {"ok": False, "notes": [], "rows": 0}
    ledger = check_ledger(args.ledger) if args.ledger else {
        "ok": False, "notes": [], "items": 0, "states": {}, "exists": False}

    status, reason = decide(args, capture, store, inp, ledger)
    if args.json:
        print(json.dumps({
            "status": status, "reason": reason, "capture": capture,
            "store": store, "input": inp, "ledger": ledger,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_text(args, status, reason, capture, store, inp, ledger))
    return 0 if status in ("ready", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
