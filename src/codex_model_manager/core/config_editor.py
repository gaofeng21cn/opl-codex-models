"""Edit ~/.codex/config.toml to point `model_catalog_json` at the merged catalog.

Faithful port of CodexConfigurationEditor.setModelCatalog from
Sources/CodexModelCore/SetupService.swift, but using tomlkit so comments,
other top-level settings, profile tables, and unrelated config are preserved
verbatim. Only the *top-level* `model_catalog_json` is updated; profile-scoped
keys are left untouched (same as the original line-based editor).

Credentials / tokens / auth are never read or emitted by this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from .backup import atomic_write, backup_file


def _set_toplevel(parsed: TOMLDocument, key: str, value: str) -> bool:
    """Set a top-level key. Returns True if something changed."""
    if str(parsed.get(key, "")) == value:
        return False
    parsed[key] = value
    return True


def _current_toplevel(config_path: str, key: str):
    """Return (parse_ok, current_value_or_None) for a top-level key."""
    config = Path(config_path)
    if not config.is_file():
        return True, None
    text = config.read_text(encoding="utf-8")
    if not text.strip():
        return True, None
    try:
        parsed = tomlkit.parse(text)
    except TOMLKitError:
        return False, None
    val = parsed.get(key)
    return True, (None if val is None else str(val))


def set_model_catalog(
    catalog_value: str,
    config_path: str,
    backup_dir: str,
) -> Optional[str]:
    """Set the top-level `model_catalog_json` to `catalog_value`.

    `catalog_value` is the string the target runtime should read: a Windows path
    for a native backend, a Linux path for WSL. Returns the previous value (str
    or None when the key was absent), so callers can record it for undo. Comments
    and unrelated settings are preserved (tomlkit). A backup is taken before the
    atomic write.
    """
    config = Path(config_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    original = config.read_bytes() if config.exists() else b""

    if original:
        try:
            parsed = tomlkit.parse(original.decode("utf-8"))
        except TOMLKitError as exc:
            raise ValueError(f"无法解析 config.toml（已保留原文件）：{exc}") from exc
    else:
        parsed = tomlkit.document()

    previous = parsed.get("model_catalog_json")
    previous = None if previous is None else str(previous)

    changed = _set_toplevel(parsed, "model_catalog_json", catalog_value)
    if not changed:
        return previous

    updated = tomlkit.dumps(parsed).encode("utf-8")
    if updated == original:
        return previous

    if config.exists():
        backup_file(config_path, backup_dir, prefix="config.toml")
    atomic_write(config_path, updated)
    return previous


def undo_model_catalog(
    config_path: str,
    backup_dir: str,
    prev_value: Optional[str],
    applied_value: str,
) -> str:
    """Undo the last apply of `model_catalog_json`.

    Restores the previous value (or deletes the key when it was absent), keeping
    unrelated post-apply changes. Conflict detection: if the current value no
    longer equals `applied_value`, someone (or something) changed it after our
    apply, so we refuse and return a conflict message. Returns a status string.
    """
    config = Path(config_path)
    if not config.is_file():
        return f"目标 config 不存在：{config_path}"
    text = config.read_text(encoding="utf-8")
    try:
        parsed = tomlkit.parse(text)
    except TOMLKitError as exc:
        raise ValueError(f"无法解析 config.toml（已保留原文件）：{exc}") from exc

    current = parsed.get("model_catalog_json")
    current = None if current is None else str(current)
    if current != applied_value:
        return (
            "冲突：model_catalog_json 当前值与上次应用值不一致"
            f"（当前={current!r}，上次应用={applied_value!r}），"
            "可能被外部修改，未自动撤销。请先预览确认。"
        )

    if prev_value is None:
        if "model_catalog_json" in parsed:
            del parsed["model_catalog_json"]
        else:
            return "无需撤销：model_catalog_json 已不存在。"
    else:
        if str(parsed.get("model_catalog_json", "")) == prev_value:
            return "无需撤销：model_catalog_json 已等于撤销目标值。"
        parsed["model_catalog_json"] = prev_value

    updated = tomlkit.dumps(parsed).encode("utf-8")
    if updated == text.encode("utf-8"):
        return "无需撤销：内容未变化。"
    backup_file(config_path, backup_dir, prefix="config.toml.undo")
    atomic_write(config_path, updated)
    return "已撤销上次 apply：model_catalog_json 已恢复到应用前值。"


def _provider_table(parsed: TOMLDocument, provider: str):
    providers = parsed.get("model_providers")
    if providers is None or provider not in providers:
        raise ValueError(f"config.toml 中不存在 model_providers.{provider}")
    table = providers[provider]
    if not hasattr(table, "get"):
        raise ValueError(f"model_providers.{provider} 不是 TOML 表")
    return table


def read_provider_base_url(config_path: str, provider: Optional[str] = None) -> tuple[str, str]:
    """Return ``(provider_name, base_url)`` from a Codex ``config.toml``.

    ``provider`` defaults to the file's top-level ``model_provider``.  This is
    shared by the CLI and the GUI so the two can never drift apart.
    """
    config = Path(config_path)
    if not config.is_file():
        raise ValueError(f"目标 config 不存在：{config_path}")
    try:
        parsed = tomlkit.parse(config.read_text(encoding="utf-8"))
    except TOMLKitError as exc:
        raise ValueError(f"无法解析 config.toml（已保留原文件）：{exc}") from exc
    name = provider or parsed.get("model_provider")
    if not name:
        raise ValueError("config.toml 没有顶层 model_provider，请显式提供 provider。")
    name = str(name)
    table = _provider_table(parsed, name)
    url = table.get("base_url")
    if not url:
        raise ValueError(f"model_providers.{name}.base_url 不存在。")
    return name, str(url)


def set_provider_base_url(
    config_path: str,
    provider: str,
    base_url: str,
    backup_dir: str,
) -> Optional[str]:
    """Set one provider's base_url, preserving unrelated TOML and comments."""
    config = Path(config_path)
    if not config.is_file():
        raise ValueError(f"目标 config 不存在：{config_path}")
    original = config.read_bytes()
    try:
        parsed = tomlkit.parse(original.decode("utf-8"))
    except TOMLKitError as exc:
        raise ValueError(f"无法解析 config.toml（已保留原文件）：{exc}") from exc
    table = _provider_table(parsed, provider)
    previous = table.get("base_url")
    previous = None if previous is None else str(previous)
    if previous == base_url:
        return previous
    table["base_url"] = base_url
    updated = tomlkit.dumps(parsed).encode("utf-8")
    backup_file(config_path, backup_dir, prefix="config.toml.bridge")
    atomic_write(config_path, updated)
    return previous


def undo_provider_base_url(
    config_path: str,
    provider: str,
    previous_url: Optional[str],
    applied_url: str,
    backup_dir: str,
) -> str:
    """Restore a bridge change only when the provider URL is unchanged."""
    config = Path(config_path)
    if not config.is_file():
        return f"目标 config 不存在：{config_path}"
    try:
        parsed = tomlkit.parse(config.read_text(encoding="utf-8"))
    except TOMLKitError as exc:
        raise ValueError(f"无法解析 config.toml（已保留原文件）：{exc}") from exc
    table = _provider_table(parsed, provider)
    current = table.get("base_url")
    current = None if current is None else str(current)
    if current != applied_url:
        return (f"冲突：model_providers.{provider}.base_url 当前值与桥接写入值不一致，"
                "未自动撤销。")
    if previous_url is None:
        table.pop("base_url", None)
    else:
        table["base_url"] = previous_url
    updated = tomlkit.dumps(parsed).encode("utf-8")
    if updated == config.read_bytes():
        return "无需撤销：base_url 内容未变化。"
    backup_file(config_path, backup_dir, prefix="config.toml.bridge.undo")
    atomic_write(config_path, updated)
    return f"已恢复 model_providers.{provider}.base_url。"
