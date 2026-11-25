#!/usr/bin/env python3
"""按群采集企业微信聊天记录的入口。

底层是移植进来的采集仓库（`scripts/wecom/`）：它会独占企业微信前台、用 macOS
辅助功能读取消息并模拟滚动。本脚本只做三件事：环境前置检查、把 `--groups`
转成一份只含这些会话的采集配置、把参数原样交给采集仓库执行。

不做的事：不读本地数据库、不注入进程、不发消息、不修改聊天内容。

用法：
    # 只采两个群最近三个月
    python3 scripts/capture_chat.py --store ~/wecom-data/wecom-chat-<标签> \
        --groups "群A,群B" --start 2026-08-01 --end 2026-11-20 \
        --account-label <标签> --self-names "<本人中文名>,<本人账号>"

    # 只生成配置，不启动采集
    python3 scripts/capture_chat.py --store <仓库> --groups "群A" --start 2026-08-01 \
        --account-label <标签> --prepare-only

    # 只读状态 / 停止 / 重建索引（不传 --groups 时原样转发）
    python3 scripts/capture_chat.py --store <仓库> --status
    python3 scripts/capture_chat.py --store <仓库> --stop

退出码与采集仓库一致：0 正常；20/21 锁屏或息屏；22 布局变化；
23 前台焦点丢失；25 弹窗阻塞；26 遇到网页应用会话。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CAPTURE_DIR = BASE / "wecom"
ENTRY = CAPTURE_DIR / "chat_repository.py"

# 这些参数只读或只维护仓库，不需要群清单
PASSTHROUGH_ONLY = (
    "--status", "--stop", "--index", "--rename-suggestions", "--apply-renames",
)

CONFIG_DEFAULTS = {
    "output_dir": "exports",
    "scroll_delta": 120,
    "max_scrolls": 1000,
    "idle_limit": 6,
    "checkpoint_every": 10,
    "settle_ms": 800,
    "continue_on_error": True,
}


def fail(message: str, code: int = 2) -> int:
    print("采集未启动：{}".format(message), file=sys.stderr)
    return code


def check_environment() -> str:
    if platform.system() != "Darwin":
        return "采集依赖 macOS 辅助功能，当前系统是 {}".format(platform.system())
    if not ENTRY.is_file():
        return "采集模块缺失：{}".format(ENTRY)
    if not shutil.which("swiftc"):
        return "未找到 swiftc，先安装 Xcode Command Line Tools（xcode-select --install）"
    return ""


def parse_targets(groups: str, singles: str) -> tuple[list, str]:
    """把 --groups / --singles 解析成 [(会话名, 类型)]，返回 (目标, 错误信息)。

    群与单聊在采集器里是两套判定（群要核成员、单聊按头像判方向），必须显式区分，
    不能靠名字形态去猜。
    """
    targets, seen = [], set()
    for raw, kind in ((groups or "", "group"), (singles or "", "single")):
        for item in raw.split(","):
            name = item.strip()
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            targets.append({"name": name, "conversation_type": kind})
    if not targets:
        return [], "没有有效的会话名"
    return targets, ""


def subset_config(store: Path, targets: list, start: str, end: str, self_names: str) -> Path:
    """生成只含指定会话的采集配置。

    output_prefix 按「类型 + 会话名」取摘要：群沿用 chat-group-<digest>，与历史仓库一致，
    重跑不会分叉；单聊用 chat-single-<digest>，两者不会互相覆盖。
    """
    discovery = store / "discovery"
    discovery.mkdir(parents=True, exist_ok=True)
    conversations = []
    for target in targets:
        name = target["name"]
        kind = target["conversation_type"]
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
        conversations.append({
            "name": name,
            "output_prefix": "chat-{}-{}".format(kind, digest),
            "conversation_type": kind,
        })
    names = sorted(target["name"] for target in conversations)
    digest = hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()[:8]
    kinds = {target["conversation_type"] for target in conversations}
    group_count = sum(1 for target in conversations if target["conversation_type"] == "group")
    single_count = len(conversations) - group_count
    payload = dict(CONFIG_DEFAULTS)
    payload.update({
        "candidate_source": "groups_subset" if kinds == {"group"} else "targets_subset",
        "candidate_coverage": {
            "coverage_status": "group_subset" if kinds == {"group"} else "targets_subset",
            "candidate_count": len(conversations),
            "unresolved_entries": 0,
            "coverage_note": "只采集指定会话（群 {} 个、单聊 {} 个），未做全账号候选覆盖；"
                             "不能据此声称采全账号".format(group_count, single_count),
        },
        "self_names": [n for n in (self_names or "").split(",") if n.strip()],
        "window": {"start": start, "end": end},
        "conversations": conversations,
    })
    path = discovery / "targets-{}.json".format(digest)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按会话采集企业微信聊天记录（群与单聊；移植的采集仓库入口）",
        epilog="其余参数原样交给 scripts/wecom/chat_repository.py，例如 --rescan、--reach-beginning。",
    )
    parser.add_argument("--store", required=True, help="采集仓库绝对路径；一个账号一个仓库")
    parser.add_argument("--groups", help="群名，逗号分隔；不传则按采集仓库默认的全账号候选")
    parser.add_argument("--singles", help="单聊对象名，逗号分隔（企业微信侧栏显示的名字）")
    parser.add_argument("--start", help="固定起点日期 YYYY-MM-DD，同一仓库内不能变")
    parser.add_argument("--end", help="本轮截止日期，默认今天")
    parser.add_argument("--account-label", help="当前登录账号的固定本地标签")
    parser.add_argument("--self-names", default="", help="本人在群里的名字，逗号分隔")
    parser.add_argument("--config", help="已有的采集配置；传了就不再按 --groups 生成")
    parser.add_argument("--prepare-only", action="store_true",
                        help="只生成配置并打印路径，不启动采集")
    args, passthrough = parser.parse_known_args(argv)

    problem = check_environment()
    if problem:
        return fail(problem, 3 if platform.system() != "Darwin" else 2)

    store = Path(args.store).expanduser()
    if not store.is_absolute():
        return fail("--store 必须是绝对路径，避免不同账号写进同一个仓库")
    store.mkdir(parents=True, exist_ok=True)

    read_only_flag = next((flag.split("=")[0] for flag in passthrough
                           if flag.split("=")[0] in PASSTHROUGH_ONLY), "")
    if read_only_flag:
        if args.groups or args.singles:
            print("提示：{} 模式下忽略 --groups / --singles".format(read_only_flag), file=sys.stderr)
        command = [sys.executable, str(ENTRY), "--store", str(store)] + passthrough
        os.execv(sys.executable, command)

    config_path = Path(args.config).expanduser() if args.config else None
    if config_path is None and (args.groups or args.singles):
        targets, problem = parse_targets(args.groups, args.singles)
        if problem:
            return fail(problem)
        if not args.start:
            return fail("定向采集必须给 --start，否则无法确定回溯起点")
        if args.end and args.end < args.start:
            return fail("--end 早于 --start")
        config_path = subset_config(store, targets, args.start, args.end or "", args.self_names)
        print("已生成定向配置：{}（群 {} 个、单聊 {} 个）".format(
            config_path,
            sum(1 for t in targets if t["conversation_type"] == "group"),
            sum(1 for t in targets if t["conversation_type"] == "single")))
        for target in targets:
            print("  - [{}] {}".format(target["conversation_type"], target["name"]))

    if args.prepare_only:
        if config_path is None:
            return fail("--prepare-only 需要 --groups / --singles 或 --config")
        return 0

    command = [sys.executable, str(ENTRY), "--store", str(store)]
    if config_path is not None:
        command += ["--config", str(config_path)]
    if args.start:
        command += ["--start", args.start]
    if args.end:
        command += ["--end", args.end]
    if args.account_label:
        command += ["--account-label", args.account_label]
    if args.self_names:
        command += ["--self-names", args.self_names]
    command += passthrough
    os.execv(sys.executable, command)


if __name__ == "__main__":
    sys.exit(main())
