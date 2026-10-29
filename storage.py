from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict, Tuple

_IO_LOCK = threading.RLock()
_JSON_CACHE: Dict[str, Tuple[Tuple[int, int], Any]] = {}


def path_cache_key(path: Path | str) -> str:
    """Return a stable lexical cache key without resolving the filesystem.

    Path.resolve() can trigger extra metadata work on mapped/network drives. Cache
    identity only needs a normalized absolute spelling, not symlink resolution.
    """
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def file_signature(path: Path) -> tuple[int, int]:
    """Return a cheap change signature for a file (mtime ns, size)."""
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a text file atomically so interrupted saves do not leave partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    with _IO_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding=encoding,
                newline="",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(text)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    # Some network filesystems do not support fsync cleanly; the
                    # same-directory replace still prevents partial destination files.
                    pass
            os.replace(temp_name, path)
            temp_name = ""
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass


def load_json_cached(path: Path, default: Any, *, signature: tuple[int, int] | None = None) -> Any:
    """Read JSON with an mtime/size cache and return an isolated copy.

    Callers that already stat-ed the file can pass the signature to avoid a second
    metadata request, which is especially useful on mapped/network drives.
    """
    path = Path(path)
    key = path_cache_key(path)
    if signature is None:
        try:
            signature = file_signature(path)
        except OSError:
            return deepcopy(default)

    with _IO_LOCK:
        cached = _JSON_CACHE.get(key)
        if cached and cached[0] == signature:
            return deepcopy(cached[1])

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return deepcopy(default)

    with _IO_LOCK:
        _JSON_CACHE[key] = (signature, deepcopy(value))
    return deepcopy(value)


def save_json_atomic(path: Path, data: Any, *, indent: int = 2) -> None:
    path = Path(path)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    atomic_write_text(path, text)
    try:
        signature = file_signature(path)
    except OSError:
        return
    key = path_cache_key(path)
    with _IO_LOCK:
        _JSON_CACHE[key] = (signature, deepcopy(data))


def invalidate_json_cache(path: Path | None = None) -> None:
    with _IO_LOCK:
        if path is None:
            _JSON_CACHE.clear()
            return
        _JSON_CACHE.pop(path_cache_key(path), None)
