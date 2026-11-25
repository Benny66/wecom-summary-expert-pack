"""Filesystem operations that tolerate WorkBuddy's existing-directory mkdir shim."""
from pathlib import Path


def ensure_dir(path: Path) -> None:
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        if not path.is_dir():
            raise
