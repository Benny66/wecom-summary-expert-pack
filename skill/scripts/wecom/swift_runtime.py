#!/usr/bin/env python3
"""按源码和编译器指纹缓存 Swift 工具；不访问企业微信。"""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from fs_utils import ensure_dir


@lru_cache(maxsize=1)
def compiler_version() -> str:
    return subprocess.check_output(["swiftc", "--version"], text=True).strip()


def swift_command(source: Path, cache_dir: Path) -> list[str]:
    source = source.resolve()
    digest = hashlib.sha256(source.read_bytes() + compiler_version().encode() + platform.machine().encode()).hexdigest()[:20]
    ensure_dir(cache_dir)
    target = cache_dir / f"{source.stem}-{digest}"
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{source.stem}-", dir=cache_dir)
        os.close(fd)
        temporary = Path(temporary)
        try:
            subprocess.run(["swiftc", "-O", str(source), "-o", str(temporary)], check=True)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return [str(target)]
