"""原子 JSON 写工具。

写入过程：先写临时文件 → fsync → os.replace（原子重命名），
配合路径级 threading.Lock 防止并发写互相覆盖。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_locks: dict[Path, threading.RLock] = {}
_locks_meta = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    with _locks_meta:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    """Serialize a complete read-modify-write transaction for one path.

    The lock is process-local.  ``RLock`` lets callers invoke
    :func:`atomic_write_json` while holding the same transaction lock.
    Cross-process coordination remains outside the PoC contract.
    """

    normalized = Path(path).resolve(strict=False)
    with _file_lock(normalized):
        yield


def atomic_write_json(path: Path, data: Any) -> None:
    """将 data 原子写入 path（JSON 格式）。

    使用 tmpfile + os.replace 保证写入中途崩溃不会损坏原文件；
    路径级 Lock 防止并发写互相覆盖。
    """
    # Normalize ``..`` without following the final path component.  Following a
    # JSON-file symlink here would make os.replace overwrite the link target;
    # replacing the link entry itself preserves the caller's original boundary.
    path = Path(os.path.abspath(Path(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_lock(path.resolve(strict=False))
    with lock:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception as e:
            logger.debug("atomic_write_json failed, cleaning up temp file: %s", e)
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
