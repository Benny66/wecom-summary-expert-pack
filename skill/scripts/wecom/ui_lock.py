"""One UI operator per user, including inherited capture descendants.

Never unlink the lock inode or explicitly unlock it: an orphaned UI child must
continue holding the flock until its last inherited descriptor is closed.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
from pathlib import Path
from fs_utils import ensure_dir

_fd: int | None = None
_path: Path | None = None
FD_ENV = "WECOM_UI_LOCK_FD"
PATH_ENV = "WECOM_UI_LOCK_PATH"


def acquire(path: Path | None = None) -> None:
    global _fd, _path
    if _fd is not None:
        return
    target = (path or Path(os.environ.get(PATH_ENV) or
              Path.home() / "Library/Caches/extract-wecom-history/ui.lock")).resolve()
    ensure_dir(target.parent)
    inherited = os.environ.get(FD_ENV)
    if inherited is not None:
        try:
            fd = int(inherited)
            actual, expected = os.fstat(fd), target.stat()
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("lock descriptor mismatch")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as exc:
            raise RuntimeError("无法核验继承的企业微信 UI 锁，未启动操作") from exc
    else:
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError("已有企业微信 UI 操作者或残留子进程；先对原仓库运行 --stop，不能并发启动") from exc
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid()}).encode())
    _fd, _path = fd, target


def child_options() -> dict:
    if _fd is None:
        return {}
    return {"pass_fds": (_fd,), "env": {**os.environ, FD_ENV: str(_fd), PATH_ENV: str(_path)}}


def release() -> None:
    global _fd, _path
    if _fd is not None:
        os.close(_fd)
        _fd = _path = None


atexit.register(release)
