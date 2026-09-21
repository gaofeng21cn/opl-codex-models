"""Backup / restore helpers with crash-safe semantics.

Before any write that replaces a file, the current file is copied to the backup
directory. Restore also keeps the current file first, so a wrong restore can be
undone. All writes are atomic (temp file + os.replace).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_file(path: str, backup_dir: str, prefix: str = "file") -> Optional[str]:
    """Copy `path` into backup_dir. Returns backup path or None if missing."""
    src = Path(path)
    if not src.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{prefix}.{_stamp()}.{uuid.uuid4().hex}.bak"
    try:
        shutil.copy2(src, target)
    except OSError as exc:
        raise OSError(f"备份失败 {src} -> {target}：{exc}") from exc
    return str(target)


def atomic_write(path: str, data: bytes) -> None:
    """Write data atomically: temp file in the same dir then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def restore_file(backup_path: str, target_path: str, backup_dir: str, prefix: str = "file") -> str:
    """Restore a backup onto target, keeping the current target first.

    Returns the backup created of the current (pre-restore) target file.
    """
    backup = Path(backup_path)
    if not backup.is_file():
        raise FileNotFoundError(f"备份文件不存在：{backup_path}")
    current_backup = backup_file(target_path, backup_dir, prefix=prefix + ".before-restore")
    with open(backup, "rb") as fh:
        data = fh.read()
    atomic_write(target_path, data)
    return current_backup or ""


def classify_backup(path: str) -> str:
    """Classify a backup file by content, not filename.

    Returns one of:
      - 'model_catalog_json'  a JSON object whose top-level key is "models"
                             (official/custom/merged model catalog)
      - 'toplevel_config'     a TOML map that does NOT look like a model catalog
                             (i.e. a codex config.toml backup)
      - 'unknown'             unreadable / parseable as neither JSON-with-models
                             nor TOML

    Used to forbid restoring a config.toml backup onto a model JSON target (and
    vice versa) without guessing from the filename.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return "unknown"
    # 1) Try JSON catalog first (fast path: top-level "models" array).
    text = data.decode("utf-8", errors="replace")
    try:
        import json

        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("models"), list):
            return "model_catalog_json"
    except (ValueError, TypeError):
        pass
    # 2) Try TOML parse. A model catalog JSON would have already matched above or
    #    failed JSON; a config.toml backup parses as a TOML doc.
    try:
        import tomlkit
        from tomlkit.exceptions import TOMLKitError

        tomlkit.parse(text)
        return "toplevel_config"
    except (TOMLKitError, ValueError):
        return "unknown"