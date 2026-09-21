#!/usr/bin/env python3
"""Safely update DeepSeek V4.1 Flash reasoning levels in an explicit catalog.

This helper changes only the matching model entry.  It never discovers a
CODEX_HOME from the environment and never reads credentials.  The target must
be supplied explicitly so an update cannot silently touch another installation.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EFFORTS = ("low", "high", "max")
SLUG = "deepseek-v4.1-flash"


def update(target: Path) -> Path:
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"目标必须是普通 JSON 文件：{target}")
    original = target.read_bytes()
    try:
        document = json.loads(original.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"目标 JSON 无法解析：{exc}") from exc
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        raise ValueError("目标目录缺少 models 数组")
    matches = [m for m in models if isinstance(m, dict) and m.get("slug") == SLUG]
    if len(matches) != 1:
        raise ValueError(f"目标中应恰好有一个 {SLUG}，实际 {len(matches)} 个")

    model = matches[0]
    model["supported_reasoning_levels"] = [
        {"effort": effort, "description": f"Reasoning effort: {effort}"}
        for effort in EFFORTS
    ]
    model["default_reasoning_level"] = "high"
    updated = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if updated == original:
        return target

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}.before-reasoning-{stamp}.bak")
    backup.write_bytes(original)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="将 DeepSeek V4.1 Flash 设为 low/high/max，默认 high")
    parser.add_argument("--target", type=Path, required=True, help="显式 model_catalog_json 文件路径")
    args = parser.parse_args(argv)
    try:
        backup = update(args.target)
    except (OSError, ValueError) as exc:
        print(f"未更新：{exc}")
        return 1
    print(f"已更新 {SLUG}: low, high, max；默认 high")
    if backup != args.target:
        print(f"原文件备份：{backup}")
    else:
        print("内容已经符合目标，未写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
