"""Safe preview of the one config value `apply` changes.

The config.toml may hold unrelated settings (and credentials / tokens). To avoid
leaking any of that into a diff, CLI/GUI never show a full-file unified diff.
Instead they render ONLY the `model_catalog_json` key's current vs proposed
value, isolated to the top-level table (so a `[profiles.*]` copy of the same key
is never shown as "the" current value that would be overwritten).

Parse failures are surfaced without echoing raw config content.
"""

from __future__ import annotations

from pathlib import Path


def _read_text(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _current_value(text: str):
    """Return (parse_ok, current_top_level_value).

    `current` is None when the key is absent OR the file could not be parsed.
    `parse_ok` distinguishes the two so callers can refuse to write on a real
    parse failure (which is different from "key not set yet").
    """
    stripped = text.strip()
    if not stripped:
        return True, None
    try:
        import tomlkit
        from tomlkit.exceptions import TOMLKitError
    except ImportError:  # pragma: no cover - tomlkit is a hard dep
        return False, None
    try:
        parsed = tomlkit.parse(text)
    except (TOMLKitError, ValueError):
        return False, None
    # tomlkit: top-level key is reachable directly; a profile-scoped one only via
    # parsed["profiles"]... so parsed.get is top-level and safe.
    val = parsed.get("model_catalog_json")
    return True, (None if val is None else str(val))


def preview(config_path: str, merged_catalog: str) -> dict:
    """Return a dict describing only the intended change.

    Keys: config_path, merged_catalog, current (str|None), proposed (str),
    parse_ok (bool), note (str|None).
    """
    text = _read_text(config_path)
    parse_ok, current = _current_value(text)
    return {
        "config_path": str(Path(config_path).absolute()),
        "merged_catalog": merged_catalog,
        "current": current,
        "proposed": str(Path(merged_catalog).absolute()),
        "parse_ok": parse_ok,
        "note": None,
    }


def render_preview(info: dict, label_current: str = "当前", label_proposed: str = "拟写入") -> str:
    """Render only the model_catalog_json old/new value, never unrelated keys."""
    lines = [f"目标 config: {info['config_path']}"]
    lines.append(f"{label_current} model_catalog_json: {info['current'] if info['current'] is not None else '（未设置）'}")
    lines.append(f"{label_proposed} model_catalog_json: {info['proposed']}")
    return "\n".join(lines)


def same_as_current(info: dict) -> bool:
    """True when the proposed value already equals the current top-level value."""
    return info["proposed"] == info["current"]


def parse_error_note(config_path: str) -> str:
    """Safe error copy which never echoes the raw/config content."""
    return f"无法解析 config.toml（已保留原文件，未尝试写入）：{Path(config_path).absolute()}"


from dataclasses import dataclass
from typing import Optional


@dataclass
class ApplyGate:
    """Result of the shared write-gate used by BOTH CLI and GUI `apply`.

    `writable` True only when a real write is permitted. When False, `reason`
    explains why (missing/unverified runtime, invalid catalog, demo out-of-sandbox,
    or real mode without verified compatibility). A read-only preview (--diff /
    --dry-run / GUI preview) always yields a non-writable gate WITHOUT a reason,
    because not writing is the expected behaviour there, not a block.
    """

    target: str
    writable: bool
    reason: Optional[str] = None


def _resolve_runtime(config):
    """Return an existing runtime path or None (never raises).

    Handles both native Windows and WSL backends via the unified
    `config.runtime_target()` so a WSL-only machine is not wrongly rejected.
    """
    import os

    from .errors import CodexModelError

    try:
        target = config.runtime_target()
        return target.executable if os.path.isfile(target.executable) else None
    except CodexModelError:
        return None


def _int_or_none(value) -> Optional[int]:
    """Coerce to int, or None for non-integral/bool values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def validate_models(models, *, require_priority: bool = False, allow_empty: bool = False,
                    name: str = "模型目录"):
    """Validate a model array's structure; raise InvalidCatalog on any violation.

    Shared by `apply` (merged catalog) and `restore` (model catalog backup). Rules:
      - `models` must be a list of objects; empty is rejected unless `allow_empty`
        (a custom source is legitimately initialised empty and may be restored to
        that "no custom models" state; a merged catalog is never empty)
      - every object has a non-empty string `slug`, unique across the array
      - known scalar fields have the right type when present (priority/context
        windows are integral numbers; display_name/description/visibility are
        strings; input_modalities is a list of strings)
      - context windows, if present, are positive and max >= current
      - reasoning levels, if present, are objects with a non-empty effort and the
        default level is one of them
      - unknown fields are PRESERVED (never rejected); we only check what we know.

    `require_priority` and `allow_empty` are independent, explicit catalog-kind
    switches: merged catalogs require an integer `priority` and a non-empty list,
    custom sources allow empty and carry no `priority` (it is assigned at merge).
    """
    from .errors import InvalidCatalog

    if not isinstance(models, list):
        raise InvalidCatalog(f"{name}的 models 必须是数组")
    if not models and not allow_empty:
        raise InvalidCatalog(f"{name}的 models 必须是非空数组")

    slugs: list = []
    for idx, model in enumerate(models):
        if not isinstance(model, dict):
            raise InvalidCatalog(f"{name}第 {idx + 1} 个模型必须是对象")

        slug = model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise InvalidCatalog(f"{name}第 {idx + 1} 个模型须包含非空 slug")
        if slug in slugs:
            raise InvalidCatalog(f"{name}包含重复 slug：{slug}")
        slugs.append(slug)

        priority = model.get("priority")
        if require_priority:
            if _int_or_none(priority) is None:
                raise InvalidCatalog(f"{name}模型 {slug} 缺少数字 priority")
        elif priority is not None and _int_or_none(priority) is None:
            raise InvalidCatalog(f"{name}模型 {slug} 的 priority 必须是数字")

        for field in ("context_window", "max_context_window"):
            raw = model.get(field)
            if raw is not None and (isinstance(raw, bool) or _int_or_none(raw) is None or _int_or_none(raw) <= 0):
                raise InvalidCatalog(f"{name}模型 {slug} 的 {field} 必须为正整数")

        cw = _int_or_none(model.get("context_window"))
        mcw = _int_or_none(model.get("max_context_window"))
        if cw is not None and mcw is not None and mcw < cw:
            raise InvalidCatalog(f"{name}模型 {slug} 的 max_context_window 不能小于 context_window")

        modalities = model.get("input_modalities")
        if modalities is not None:
            if not isinstance(modalities, list):
                raise InvalidCatalog(f"{name}模型 {slug} 的 input_modalities 必须是数组")
            if any(not isinstance(x, str) for x in modalities):
                raise InvalidCatalog(f"{name}模型 {slug} 的 input_modalities 元素必须是字符串")

        for field in ("display_name", "description", "visibility"):
            value = model.get(field)
            if value is not None and not isinstance(value, str):
                raise InvalidCatalog(f"{name}模型 {slug} 的 {field} 必须是字符串")

        levels = model.get("supported_reasoning_levels")
        if levels is not None:
            if not isinstance(levels, list):
                raise InvalidCatalog(f"{name}模型 {slug} 的 supported_reasoning_levels 必须是数组")
            efforts = []
            for level in levels:
                if not isinstance(level, dict) or not isinstance(level.get("effort"), str) or not level["effort"].strip():
                    raise InvalidCatalog(
                        f"{name}模型 {slug} 的 supported_reasoning_levels 元素须为含非空 effort 的对象")
                efforts.append(level["effort"])
            if len(set(efforts)) != len(efforts):
                raise InvalidCatalog(f"{name}模型 {slug} 的 reasoning efforts 存在重复")

        default = model.get("default_reasoning_level")
        if default is not None:
            if not isinstance(default, str):
                raise InvalidCatalog(f"{name}模型 {slug} 的 default_reasoning_level 必须是字符串")
            if levels is not None and isinstance(levels, list):
                efforts = [level["effort"] for level in levels
                           if isinstance(level, dict) and isinstance(level.get("effort"), str)]
                if default not in efforts:
                    raise InvalidCatalog(
                        f"{name}模型 {slug} 的 default_reasoning_level 须在 supported_reasoning_levels 中")

    return slugs


def validate_catalog_file(path: str, *, require_priority: bool = True,
                          allow_empty: bool = False, name: str = "model_catalog_json") -> bool:
    """True when the JSON file at `path` is a structurally valid model catalog.

    Structural violations raise InvalidCatalog (callers wrap to a bool/block as
    needed). A missing file or a non-JSON root with a non-list `models` returns
    False; a `models` array with illegal entries raises so the exact reason is
    available to the caller.

    `allow_empty` (like `require_priority`) is an explicit catalog-kind switch:
    custom sources may be empty, merged catalogs may not.
    """
    import json
    import os

    if not os.path.exists(path):
        return False
    try:
        root = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(root, dict) or not isinstance(root.get("models"), list):
        return False
    validate_models(root["models"], require_priority=require_priority,
                    allow_empty=allow_empty, name=name)
    return True


def _catalog_valid(merged_catalog: str) -> bool:
    """Bool form of validate_catalog_file for the merged catalog (priority required)."""
    try:
        return validate_catalog_file(merged_catalog, require_priority=True)
    except Exception:  # noqa: BLE001 - any structural error means "not writable"
        return False


def _canonical(path: str) -> str:
    """Absolute, '..'-free, symlink/junction-resolved form of a path."""
    import os

    return os.path.realpath(os.path.abspath(path))


def _normcase(path: str) -> str:
    import os

    return os.path.normcase(path)


def _is_within(sandbox: str, target: str) -> bool:
    """True when `target` (canonical) is `sandbox` or a descendant of it.

    Uses os.path.commonpath on the normalised forms so '..' is already gone
    (done by _canonical) and case-insensitivity is applied on Windows; a plain
    string prefix would mis-judge sibling paths and unresolved '..'.
    """
    import os

    try:
        return os.path.commonpath([_normcase(sandbox), _normcase(target)]) == _normcase(sandbox)
    except ValueError:
        return False  # different drives


def apply_gate(config, merged_catalog: str, requested_target: str, readonly: bool) -> ApplyGate:
    """Decide whether a write to `requested_target`/config.codexConfigPath is allowed.

    Shared by CLI `cmd_apply` and GUI `apply_to_codex`. Rules:
      - target comes ONLY from caller/config; never derived from the environment.
      - read-only preview is always allowed (no gates), but never writes.
      - real write requires: target is not a directory, runtime exists, catalog
        structure valid, and (demo mode) the canonical target is inside the app
        sandbox (the canonical dir holding merged_catalog), OR (real mode) verified
        Codex compatibility evidence. Without that evidence real mode stays
        read-only. Both check and write use the SAME canonical target (gate.target).
    """
    import os

    from .errors import InvalidConfiguration

    target = requested_target or config.codex_config_path
    if not target:
        raise InvalidConfiguration(
            "未指定目标 config.toml：请在应用配置设置 codexConfigPath，或用 --codex-config 显式指定。"
            "应用不会未经授权从环境推导目标路径。"
        )
    resolved_target = _canonical(target)
    if readonly:
        # preview only; never a candidate for write
        return ApplyGate(target=resolved_target, writable=False)

    # ---- real write path ----
    if os.path.isdir(resolved_target):
        return ApplyGate(
            target=resolved_target, writable=False,
            reason="目标 config.toml 是目录，拒绝写入。",
        )

    runtime = _resolve_runtime(config)
    if not runtime:
        return ApplyGate(
            target=resolved_target, writable=False,
            reason="Codex 运行时不存在或不可执行（已检查 codexRuntimePath / 本机运行时），不能写入。",
        )

    if not _catalog_valid(merged_catalog):
        return ApplyGate(
            target=resolved_target, writable=False,
            reason="model_catalog_json 目录无效：merged_catalog 不存在或 models 结构非法（非对象元素、"
            "缺失/重复 slug、字段类型错误或上下文/推理约束不满足），已拒绝写入。",
        )

    if config.is_demo:
        # Simulated apply is allowed ONLY inside the app sandbox: the canonical
        # directory that holds the merged_catalog. The candidate target is also
        # canonical (realpath), so '..', symlinks and junctions cannot escape it,
        # and a CLI --codex-config pointing outside is rejected.
        sandbox = _canonical(str(Path(merged_catalog).parent))
        if not _is_within(sandbox, resolved_target):
            return ApplyGate(
                target=resolved_target, writable=False,
                reason=f"演示 apply 仅能写入应用沙箱内（{sandbox}）的目标；目标越界，已拒绝：{resolved_target}",
            )
        return ApplyGate(target=resolved_target, writable=True)

    # Real mode: require verified compatibility evidence that still binds to the
    # current runtime. Demo evidence never enables a real write. A stored evidence
    # snapshot from a real probe is re-validated against the current runtime's
    # path / file SHA256 / version / distro / probe-scheme; only when all bindings
    # match is the write allowed. Without valid evidence, stay read-only.
    from .compat_probe import evidence_from_dict, evidence_valid
    from .errors import CodexModelError

    evidence = evidence_from_dict(config.compat_evidence)
    try:
        current_target = config.runtime_target()
    except CodexModelError:
        current_target = None
    if current_target is not None and evidence_valid(evidence, current_target):
        return ApplyGate(target=resolved_target, writable=True)
    return ApplyGate(
        target=resolved_target, writable=False,
        reason="未验证 Codex 兼容性：缺少绑定到当前运行时的有效证据。"
        "请先运行 `probe --verify`（验证兼容性）生成真实行为证据，成功后再 apply。"
        "--version 或 `debug models --bundled` 输出不作为证据。已保持只读预览，未写入。",
    )


def demo_write_block_reason(config, sandbox_source: str, target: str) -> str:
    """Reason a demo-mode write must be refused, or ``""`` when it is allowed.

    :func:`apply_gate` confines the catalog write to the demo sandbox. Every
    other writer — the local-bridge switch in both the CLI and the GUI — has to
    obey the same rule, otherwise demo mode could reach a real ``config.toml``
    through a second door.

    ``sandbox_source`` is any path whose parent directory defines the sandbox
    (callers pass the merged catalog, matching :func:`apply_gate`).
    """
    import os

    if not getattr(config, "is_demo", False):
        return ""
    if not target:
        return "未指定目标 config.toml。"
    resolved = _canonical(target)
    if os.path.isdir(resolved):
        return f"目标是目录，拒绝写入：{resolved}"
    sandbox = _canonical(str(Path(sandbox_source).parent))
    if not _is_within(sandbox, resolved):
        return (
            f"演示模式仅能写入应用沙箱内（{sandbox}）的目标；目标越界，已拒绝：{resolved}"
        )
    return ""


def os_path_sep() -> str:
    import os

    return os.sep