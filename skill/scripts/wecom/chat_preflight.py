#!/usr/bin/env python3
"""Check the current WeCom window and active displays without opening a chat."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from swift_runtime import swift_command

BASE = Path(__file__).resolve().parent


def run_preflight(cache_dir: Path) -> int:
    try:
        command = swift_command(BASE / "wework_preflight.swift", cache_dir)
    except FileNotFoundError:
        print("采集前检查失败：未找到 Swift 编译器；请确认 Xcode Command Line Tools 可用")
        return 127
    except subprocess.CalledProcessError:
        print("采集前检查失败：Swift 工具编译失败")
        return 126
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查企业微信窗口、辅助功能权限和活动显示器")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "extract-wecom-history-swift")
    args = parser.parse_args()
    return run_preflight(args.cache_dir)


if __name__ == "__main__":
    raise SystemExit(main())
