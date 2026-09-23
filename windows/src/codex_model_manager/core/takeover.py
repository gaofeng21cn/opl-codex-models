"""Take over an existing Codex model catalog without silent data loss.

The manager could already *read* the catalog that a selected ``config.toml``
references, and it had its own editable directory, but nothing connected the two:
the catalog Codex actually reads stayed outside the manager. This module is that
connection, in four explicit steps.

  import   copy the *active* catalog (生效配置引用目录) into the manager's own
           *pending* catalog (待应用目录). Whole objects are carried over, so
           unknown top-level keys and unknown per-model fields survive; only the
           two files' JSON text ordering is normalised.
  diff     by-slug added / modified / removed comparison of active vs pending, so
           a removal is never implicit and never silent.
  apply    write the pending catalog back over the active path: gated by the
           shared write gate, refused when the active file changed after import,
           refused when removals were not explicitly confirmed, backed up first,
           written atomically.
  undo     restore the pre-apply bytes, refusing when the file moved under us.

Only model catalog JSON files are touched. No credentials, no ``config.toml``
rewrite, no bridge, no running Codex required: reading ``config.toml`` is limited
to its top-level ``model_catalog_json`` value, and even that only to locate the
active catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .backup import atomic_write, backup_file, restore_file
from .errors import CodexModelError, ConflictError, InvalidCatalog, InvalidConfiguration

EMPTY_SCHEMA = "codex_model_manager_custom_models.v1"

_CANONICAL = {"sort_keys": True, "ensure_ascii": False, "separators": (",", ":")}
_PRETTY = {"indent": 2, "sort_keys": True, "ensure_ascii": False}

MISSING = object()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(root: Any) -> str:
    """Content hash that ignores JSON formatting/whitespace/key order.

    Conflict detection compares this, so a re-formatted active file is not
    mistaken for someone changing which models Codex reads.
    """
    return sha256_hex(json.dumps(root, **_CANONICAL).encode("utf-8"))


def _pretty(root: Any) -> bytes:
    return json.dumps(root, **_PRETTY).encode("utf-8") + b"\n"


def parse_root(data: bytes, name: str) -> dict:
    try:
        root = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCatalog(f"{name}无法解析为 JSON：{exc}") from exc
    if not isinstance(root, dict):
        raise InvalidCatalog(f"{name}顶层必须是 JSON 对象")
    return root


def read_file(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise InvalidConfiguration(f"无法读取文件：{path}（{exc}）") from exc


def _index(root: dict, name: str) -> Dict[str, dict]:
    models = root.get("models")
    if not isinstance(models, list):
        raise InvalidCatalog(f"{name}的 models 必须是数组")
    indexed: Dict[str, dict] = {}
    for model in models:
        if isinstance(model, dict) and isinstance(model.get("slug"), str) and model["slug"]:
            indexed[model["slug"]] = model
    return indexed


@dataclass
class ModelChange:
    """One model that differs between the active and the pending catalog."""

    slug: str
    kind: str  # 'added' | 'modified' | 'removed'
    display_name: str = ""
    fields: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)


@dataclass
class CatalogDiff:
    added: List[ModelChange] = field(default_factory=list)
    modified: List[ModelChange] = field(default_factory=list)
    removed: List[ModelChange] = field(default_factory=list)
    # Pure id changes: (old_slug, new_slug) where the model object is identical
    # apart from its slug. Kept separate so a rename is never reported as a
    # destructive removal + addition.
    renamed: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.removed or self.renamed)

    @property
    def removed_slugs(self) -> List[str]:
        return [c.slug for c in self.removed]

    def summary(self) -> str:
        return (f"新增 {len(self.added)}，修改 {len(self.modified)}，"
                f"删除 {len(self.removed)}，重命名 {len(self.renamed)}")


def _signature_without_slug(model: dict) -> str:
    """Canonical content of a model with its slug removed (for rename detection)."""
    return json.dumps({k: v for k, v in model.items() if k != "slug"}, **_CANONICAL)


def _differs(a: Any, b: Any) -> bool:
    """JSON-aware inequality that tolerates the MISSING sentinel.

    A field present on one side only must report as changed; ``json.dumps`` cannot
    serialise the sentinel, so that case is handled explicitly (this was a latent
    crash whenever an edit added or removed a key).
    """
    if a is MISSING or b is MISSING:
        return a is not b
    return json.dumps(a, **_CANONICAL) != json.dumps(b, **_CANONICAL)


def diff_catalogs(active_data: bytes, pending_data: bytes) -> CatalogDiff:
    """Compare two catalog documents, by slug, field by field.

    A model present on only one side is added (pending only) or removed (active
    only); a model present on both sides but with different content is modified,
    listing exactly which fields changed. Field names are the raw catalog keys, so
    unknown fields are diffed too rather than hidden.

    A removed + added pair whose model objects are byte-identical apart from the
    ``slug`` key is reported as a rename, not a deletion plus an addition.
    """
    active = _index(parse_root(active_data, "活跃目录"), "活跃目录")
    pending = _index(parse_root(pending_data, "待应用目录"), "待应用目录")

    diff = CatalogDiff()
    for slug, model in pending.items():
        if slug not in active:
            diff.added.append(ModelChange(
                slug=slug, kind="added",
                display_name=str(model.get("display_name") or "")))
            continue
        before, after = active[slug], model
        if json.dumps(before, **_CANONICAL) == json.dumps(after, **_CANONICAL):
            continue
        changes: Dict[str, Tuple[Any, Any]] = {}
        for key in sorted(set(before) | set(after)):
            old = before.get(key, MISSING)
            new = after.get(key, MISSING)
            if _differs(old, new):
                changes[key] = (old, new)
        diff.modified.append(ModelChange(
            slug=slug, kind="modified",
            display_name=str(after.get("display_name") or before.get("display_name") or ""),
            fields=changes))
    for slug, model in active.items():
        if slug not in pending:
            diff.removed.append(ModelChange(
                slug=slug, kind="removed",
                display_name=str(model.get("display_name") or "")))

    _match_renames(active, pending, diff)
    return diff


def _match_renames(active: Dict[str, dict], pending: Dict[str, dict],
                   diff: CatalogDiff) -> None:
    """Move identical-apart-from-slug removed/added pairs into ``diff.renamed``."""
    if not diff.added or not diff.removed:
        return
    removed_sigs: Dict[str, List[str]] = {}
    for change in diff.removed:
        removed_sigs.setdefault(_signature_without_slug(active[change.slug]), []).append(change.slug)
    renamed: List[Tuple[str, str]] = []
    consumed_removed: set = set()
    remaining_added: List[ModelChange] = []
    for change in diff.added:
        sig = _signature_without_slug(pending[change.slug])
        candidates = [s for s in removed_sigs.get(sig, []) if s not in consumed_removed]
        if candidates:
            consumed_removed.add(candidates[0])
            renamed.append((candidates[0], change.slug))
        else:
            remaining_added.append(change)
    if renamed:
        diff.renamed.extend(renamed)
        diff.added = remaining_added
        diff.removed = [c for c in diff.removed if c.slug not in consumed_removed]


def _fmt(value: Any) -> str:
    if value is MISSING:
        return "（无）"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def render_diff(diff: CatalogDiff) -> str:
    """Render a diff for the CLI/GUI. Removals are called out as destructive."""
    lines: List[str] = []
    if not diff.has_changes:
        return "待应用目录与活跃目录一致：没有新增、修改或删除。"
    for change in diff.added:
        lines.append(f"  + {change.slug}" + (f" — {change.display_name}" if change.display_name else ""))
    if diff.added:
        lines.insert(0, f"新增 {len(diff.added)} 个模型：")
    if diff.modified:
        lines.append(f"修改 {len(diff.modified)} 个模型：")
        for change in diff.modified:
            lines.append(f"  ~ {change.slug}" + (f" — {change.display_name}" if change.display_name else ""))
            for key, (old, new) in change.fields.items():
                lines.append(f"      {key}: {_fmt(old)} -> {_fmt(new)}")
    if diff.removed:
        lines.append(f"删除 {len(diff.removed)} 个模型（写回会移除活跃目录中的这些已有模型，需显式确认）：")
        for change in diff.removed:
            lines.append(f"  - {change.slug}" + (f" — {change.display_name}" if change.display_name else ""))
    if diff.renamed:
        lines.append(f"重命名 {len(diff.renamed)} 个模型（仅 ID 变化，其余字段保持不变）：")
        for old_slug, new_slug in diff.renamed:
            lines.append(f"  = {old_slug} -> {new_slug}")
    return "\n".join(lines)


def pending_catalog_path(config) -> str:
    value = (getattr(config, "takeover_catalog_path", None) or "").strip()
    if not value:
        raise InvalidConfiguration(
            "未设置待应用目录（takeoverCatalogPath）。请先在设置中选择一个管理器内的目录文件。")
    return value


def pending_write_path(config, explicit: Optional[str] = None) -> str:
    """Return the only catalog ``edit-model`` and import may mutate.

    An explicit CLI path is accepted only when it resolves to the configured
    pending copy.  This keeps ``--catalog`` useful for scripting while preventing
    it from becoming an arbitrary file-write escape hatch.  Demo configurations
    are additionally confined to their sandbox, including through symlinks and
    junctions.
    """
    configured = pending_catalog_path(config)
    if explicit and explicit.strip() and not _same_file(configured, explicit.strip()):
        raise InvalidConfiguration(
            "--catalog 只能指向配置中的待应用目录（takeoverCatalogPath）；"
            "如需修改生效目录，请先导入副本，再通过接管写回。")
    from .safe_preview import demo_write_block_reason

    block = demo_write_block_reason(config, config.merged_catalog_path, configured)
    if block:
        raise InvalidConfiguration(block)
    return configured


def _windows_from_mnt(value: str) -> Optional[str]:
    """Map ``/mnt/c/...`` to ``C:\\...`` (pure, no wsl.exe required)."""
    match = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", value)
    if not match:
        return None
    drive = match.group(1).upper()
    tail = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:{tail}"


def _linux_from_drive(value: str) -> Optional[str]:
    """Map ``C:\\...`` / ``C:/...`` to ``/mnt/c/...`` (pure mapping)."""
    match = re.match(r"^([a-zA-Z]):[\\/](.*)$", value)
    if not match:
        return None
    return "/mnt/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/")


def resolve_catalog_value(value: str, config_path: str,
                          distro: Optional[str] = None) -> str:
    """Turn a ``model_catalog_json`` value into a path this process can read.

    Handles the three forms a real config uses:
      * a relative path -> resolved against the config file's directory;
      * a Windows path while running under Linux -> ``/mnt/<drive>/...``;
      * a Linux path (``/mnt/c/...`` or another absolute Linux path) while running
        on Windows -> the Windows path (``/mnt/c`` -> ``C:\\``; other absolute
        Linux paths are converted through the distro's ``wslpath`` when known).

    Raises :class:`InvalidConfiguration` with a clear, actionable message when a
    Linux path cannot be mapped to a Windows-readable location (for example a
    catalog inside a WSL-only home directory).
    """
    raw = str(value).strip()
    if not raw:
        raise InvalidConfiguration("model_catalog_json 为空，没有可接管的目录。")

    expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
    if os.name != "nt":
        if re.match(r"^[a-zA-Z]:[\\/]", expanded):
            mapped = _linux_from_drive(expanded)
            return str(Path(mapped).absolute()) if mapped else expanded
        path = Path(expanded)
        if not path.is_absolute():
            path = Path(config_path).parent / path
        return str(path.absolute())

    # Windows host.
    if expanded.startswith("/"):
        mapped = _windows_from_mnt(expanded)
        if mapped is None:
            from . import wsl_adapter

            mapped = wsl_adapter.to_windows_path(expanded, distro) if distro else None
        if not mapped:
            raise InvalidConfiguration(
                f"model_catalog_json 指向 Linux 路径，Windows 无法直接读取：{expanded}\n"
                "请确认 Codex 使用的 WSL 发行版，或让该目录位于 Windows 可访问的位置。")
        expanded = mapped
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(config_path).parent / path
    return str(path.absolute())


def active_catalog_path_from_config(config_path: str, distro: Optional[str] = None) -> str:
    """Resolve the catalog referenced by a ``config.toml`` (the connection page's target).

    Reads ONLY the top-level ``model_catalog_json``; nothing else from the file is
    read, stored or printed. Relative values resolve against the config directory,
    and Windows/WSL path forms are normalised by :func:`resolve_catalog_value`.
    """
    from .safe_preview import preview

    info = preview(config_path, "")
    current = info["current"]
    if not current:
        raise InvalidConfiguration(
            f"所选配置未设置 model_catalog_json，没有可接管的目录：{config_path}")
    return resolve_catalog_value(str(current), config_path, distro)


def active_catalog_path(config, explicit: Optional[str] = None) -> str:
    """Where the active catalog is: an explicit path, else the selected config.

    The fallback reads ONLY the top-level ``model_catalog_json`` of the configured
    ``codexConfigPath``; nothing else from the file is read, stored or printed. A
    relative value resolves against the config file's directory and Windows/WSL
    path forms are normalised. ``explicit`` is the *catalog* path itself.
    """
    if explicit and explicit.strip():
        return str(Path(explicit.strip()).expanduser().absolute())
    config_path = (getattr(config, "codex_config_path", None) or "").strip()
    if not config_path:
        raise InvalidConfiguration(
            "未指定生效配置引用目录：请在应用配置里设置 codexConfigPath（连接页所选配置），"
            "或用 --active 显式给出 model_catalog_json 指向的文件。")
    distro = (getattr(config, "codex_distro", None) or "").strip() or None
    return active_catalog_path_from_config(config_path, distro)


def _same_file(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.realpath(a) == os.path.realpath(b)


def _structure_warnings(models: List[dict]) -> List[str]:
    """Non-blocking structural notes about an imported catalog.

    Import copies what is there; it does not silently "fix" or reject it. Anything
    the shared write gate would refuse is reported so the user learns about it at
    import time rather than at apply time.
    """
    from .safe_preview import validate_models

    try:
        validate_models(models, require_priority=True, name="活跃目录")
    except Exception as exc:  # noqa: BLE001 - reported, never fatal for a read
        return [f"活跃目录未通过写回校验：{exc}"]
    return []


def import_active(config, active_path: Optional[str] = None, *,
                  create_empty: bool = False) -> dict:
    """Import a copy of the active catalog into the manager's pending catalog.

    Returns (and records on ``config.takeover_import``) the import record used for
    conflict detection and for showing both directories in the UI. Unknown fields
    are preserved because the whole document is carried over; only JSON formatting
    is normalised (sorted keys, two-space indent) so later edits produce readable,
    stable diffs.
    """
    active = active_catalog_path(config, active_path)
    pending = pending_write_path(config)
    backup_dir = config.resolved_paths().backup_directory

    if _same_file(active, pending):
        raise InvalidConfiguration(
            f"活跃目录与待应用目录是同一个文件，无需接管：{active}")
    if os.path.isdir(active):
        raise InvalidConfiguration(f"活跃目录是目录，不是文件：{active}")

    warnings: List[str] = []
    if os.path.exists(active):
        raw = read_file(active)
        root = parse_root(raw, "活跃目录")
        models = root.get("models")
        if not isinstance(models, list):
            raise InvalidCatalog("活跃目录的 models 必须是数组，无法接管。")
        warnings = _structure_warnings(models)
        active_raw_hash: Optional[str] = sha256_hex(raw)
        active_canonical = canonical_hash(root)
    elif create_empty:
        root = {"schema": EMPTY_SCHEMA, "models": []}
        active_raw_hash = None
        active_canonical = None
        warnings = []
    else:
        raise InvalidCatalog(
            f"活跃目录不存在，无法导入：{active}\n"
            "如确认要从一个空白目录开始接管，请使用 create_empty / --create-empty。")

    existed = os.path.exists(pending)
    if existed:
        backup_file(pending, backup_dir, prefix="pending-catalog")
    atomic_write(pending, _pretty(root))

    record = {
        "activePath": active,
        "pendingPath": pending,
        "importedAt": _iso_now(),
        "activeRawHash": active_raw_hash,
        "activeCanonicalHash": active_canonical,
        "modelCount": len([m for m in root.get("models", []) if isinstance(m, dict)]),
        "replacedExistingPending": existed,
        "structureWarnings": warnings,
    }
    config.takeover_catalog_path = pending
    config.takeover_import = record
    return record


@dataclass
class TakeoverState:
    """Everything needed to show and to gate one takeover, read-only."""

    active_path: str
    pending_path: str
    active_exists: bool
    pending_exists: bool
    diff: Optional[CatalogDiff] = None
    gate_reason: str = ""
    writable: bool = False
    conflict: str = ""
    pending_error: str = ""
    pending_ok: bool = False
    imported_at: str = ""
    structure_warnings: List[str] = field(default_factory=list)
    model_count: int = 0

    @property
    def ready(self) -> bool:
        return (self.pending_ok and self.writable and not self.conflict
                and self.pending_exists)


def inspect(config, active_path: Optional[str] = None) -> TakeoverState:
    """Read-only inspection: paths, diff, gate and conflict state. Writes nothing."""
    from .safe_preview import catalog_write_gate, validate_models

    active = active_catalog_path(config, active_path)
    pending = pending_catalog_path(config)
    state = TakeoverState(active_path=active, pending_path=pending,
                          active_exists=os.path.isfile(active),
                          pending_exists=os.path.isfile(pending))

    record = getattr(config, "takeover_import", None) or {}
    state.imported_at = str(record.get("importedAt") or "")
    state.structure_warnings = list(record.get("structureWarnings") or [])

    gate = catalog_write_gate(config, config.merged_catalog_path, active)
    state.writable = gate.writable
    state.gate_reason = gate.reason or ""

    active_data = read_file(active) if state.active_exists else b'{"models": []}'
    if state.pending_exists:
        pending_data = read_file(pending)
        try:
            pending_root = parse_root(pending_data, "待应用目录")
            models = pending_root.get("models")
            if not isinstance(models, list):
                raise InvalidCatalog("待应用目录的 models 必须是数组")
            validate_models(models, require_priority=True, name="待应用目录")
            state.pending_ok = True
            state.model_count = len([m for m in models if isinstance(m, dict)])
        except (InvalidCatalog, InvalidConfiguration) as exc:
            state.pending_error = str(exc)
        try:
            state.diff = diff_catalogs(active_data, pending_data)
        except (InvalidCatalog, InvalidConfiguration) as exc:
            # A malformed document cannot be diffed, but the state must still be
            # reportable (the write gate refuses it) rather than raising here.
            state.pending_error = state.pending_error or str(exc)
            state.diff = None

    if record.get("activePath") and not _same_file(active, str(record["activePath"])):
        state.conflict = (
            "冲突：本次目标与导入时的活跃目录不是同一个文件。"
            "请针对该目标重新导入后再写回。")
    if state.active_exists and record:
        imported = record.get("activeCanonicalHash")
        try:
            current = canonical_hash(parse_root(active_data, "活跃目录"))
        except InvalidCatalog as exc:
            # Someone replaced the live catalog with something unparseable: that is
            # exactly the case where a write must not proceed.
            current = None
            state.conflict = f"冲突：活跃目录无法解析，写回已拒绝：{exc}"
        if imported and current is not None and current != imported and not state.conflict:
            state.conflict = (
                "冲突：活跃目录在导入后被外部修改（内容哈希不一致），写回会覆盖别人的改动，已拒绝。"
                "请重新导入后再编辑。")
    elif state.active_exists and not record:
        state.conflict = "尚未从活跃目录导入副本；请先导入，再编辑并写回。"
    return state


def render_state(state: TakeoverState) -> str:
    """Human-readable summary of a takeover: both directories, then the diff."""
    lines = [
        f"生效配置引用目录（Codex 实际读取）: {state.active_path}"
        + ("" if state.active_exists else "（不存在）"),
        f"待应用目录（管理器编辑，未生效）: {state.pending_path}"
        + ("" if state.pending_exists else "（尚未导入）"),
    ]
    if state.imported_at:
        lines.append(f"导入时间: {state.imported_at}")
    if not state.pending_exists:
        lines.append("请先“从现有目录导入副本”，再编辑待应用目录。")
        return "\n".join(lines)
    if not state.pending_ok:
        lines.append(f"待应用目录结构无效，写回会被拒绝：{state.pending_error}")
    if not state.writable:
        lines.append(f"写入校验未通过（只读）：{state.gate_reason}")
    if state.conflict:
        lines.append(state.conflict)
    for warning in state.structure_warnings:
        lines.append(f"提示：{warning}")
    lines.append(f"待应用目录模型数: {state.model_count}")
    if state.diff is not None:
        lines.append(f"与活跃目录的差异（{state.diff.summary()}）:")
        lines.append(render_diff(state.diff))
    return "\n".join(lines)


@dataclass
class ApplyResult:
    applied: bool
    active_path: str
    pending_path: str
    message: str
    backup_path: str = ""
    before_raw_hash: str = ""
    after_canonical_hash: str = ""
    created_file: bool = False
    diff_summary: str = ""
    applied_at: str = ""


def apply_pending(config, *, active_path: Optional[str] = None,
                  confirm_removals: bool = False, allow_create: bool = False,
                  persist: Optional[Callable[[], None]] = None) -> ApplyResult:
    """Write the pending catalog back over the active path.

    Refusals, in order, all before anything is written:
      1. the shared write gate (demo sandbox / verified compatibility),
      2. pending catalog must be structurally valid (same rule set as ``apply``),
      3. the active file must not have changed since the import,
      4. removals of existing models need ``confirm_removals=True``.
    Then: back up the active file (when it exists) and write atomically. The undo
    record is stored on ``config.last_takeover``.
    """
    from .safe_preview import validate_models

    active = active_catalog_path(config, active_path)
    pending = pending_catalog_path(config)
    backup_dir = config.resolved_paths().backup_directory

    if not os.path.isfile(pending):
        raise InvalidCatalog(f"待应用目录不存在，无法写回：{pending}")
    if _same_file(active, pending):
        raise InvalidConfiguration(f"活跃目录与待应用目录是同一个文件：{active}")
    if os.path.isdir(active):
        raise InvalidConfiguration(f"活跃目录是目录，拒绝写入：{active}")

    from .safe_preview import catalog_write_gate

    gate = catalog_write_gate(config, config.merged_catalog_path, active)
    if not gate.writable:
        raise InvalidConfiguration(gate.reason or "写入校验未通过，已拒绝写回。")

    pending_data = read_file(pending)
    pending_root = parse_root(pending_data, "待应用目录")
    models = pending_root.get("models")
    if not isinstance(models, list):
        raise InvalidCatalog("待应用目录的 models 必须是数组")
    validate_models(models, require_priority=True, name="待应用目录")

    active_exists = os.path.isfile(active)
    if not active_exists and not allow_create:
        raise InvalidConfiguration(
            f"活跃目录不存在，拒绝创建新的 Codex 目录文件：{active}"
            "（如确认要新建，请显式允许创建。）")

    record = getattr(config, "takeover_import", None) or None
    active_data = read_file(active) if active_exists else b'{"models": []}'
    active_root = parse_root(active_data, "活跃目录")
    if active_exists:
        if not record:
            raise ConflictError(
                "尚未从活跃目录导入副本，写回已拒绝：无法确认当前内容是否是你要覆盖的对象。"
                "请先“从现有目录导入副本”。")
        imported_path = str(record.get("activePath") or "")
        if imported_path and not _same_file(active, imported_path):
            raise ConflictError(
                "冲突：本次目标与导入时的活跃目录不是同一个文件，写回已拒绝。"
                "请针对该目标重新导入后再编辑。")
        imported = record.get("activeCanonicalHash")
        if imported and canonical_hash(active_root) != imported:
            raise ConflictError(
                "冲突：活跃目录在导入后被外部修改，写回已拒绝，未写入任何内容。"
                "请重新导入以获取最新内容。")

    diff = diff_catalogs(active_data, pending_data)
    if diff.removed and not confirm_removals:
        raise ConflictError(
            "写回会删除活跃目录中已有的 " + str(len(diff.removed)) + " 个模型："
            + ", ".join(diff.removed_slugs)
            + "。为避免静默删除，需要显式确认（--confirm-removals / 界面确认）后才写入。")

    if not diff.has_changes:
        return ApplyResult(
            applied=False, active_path=active, pending_path=pending,
            message="待应用目录与活跃目录内容一致，未写入、未备份。",
            diff_summary=diff.summary())

    before_raw_hash = sha256_hex(active_data) if active_exists else ""
    backup_path = ""
    if active_exists:
        backup_path = backup_file(active, backup_dir, prefix="active-catalog") or ""

    # Re-read immediately before the write so an external edit inside the same
    # second is still seen; the write itself is atomic.
    current_bytes = read_file(active) if active_exists else b""
    if active_exists and canonical_hash(parse_root(current_bytes, "活跃目录")) != canonical_hash(active_root):
        raise ConflictError("冲突：活跃目录在写入前刚刚被外部修改，已中止写入（未写入）。")

    applied_at = _iso_now()
    previous_last_takeover = getattr(config, "last_takeover", None)
    config.last_takeover = {
        "activePath": active,
        "pendingPath": pending,
        "backupPath": backup_path or None,
        "beforeRawHash": before_raw_hash or None,
        "beforeCanonicalHash": canonical_hash(active_root) if active_exists else None,
        "afterCanonicalHash": canonical_hash(pending_root),
        "createdFile": not active_exists,
        "diffSummary": diff.summary(),
        "appliedAt": applied_at,
    }
    # Persist the complete undo record before touching the active catalog.  If
    # saving app state fails, the live bytes remain unchanged.  Once this save
    # succeeds, even a process crash immediately after os.replace leaves a durable
    # record containing the backup and both hashes.
    try:
        if persist is not None:
            persist()
    except BaseException:
        config.last_takeover = previous_last_takeover
        raise
    try:
        atomic_write(active, pending_data)
    except BaseException:
        config.last_takeover = previous_last_takeover
        if persist is not None:
            try:
                persist()
            except BaseException:
                pass
        raise
    # The write succeeded, so the edit copy and the live catalog are now the same
    # content: advance the import baseline. Without this the page would immediately
    # report "the live catalog changed externally" against its own, just-written
    # content (the baseline would still describe the pre-apply file).
    previous_import = getattr(config, "takeover_import", None)
    try:
        config.takeover_import = {
            **(previous_import or {}),
            "activePath": active,
            "pendingPath": pending,
            "activeRawHash": sha256_hex(pending_data),
            "activeCanonicalHash": config.last_takeover["afterCanonicalHash"],
            "modelCount": len([m for m in pending_root.get("models", []) if isinstance(m, dict)]),
            "rebaselinedByApplyAt": applied_at,
        }
        if persist is not None:
            persist()
    except BaseException:
        config.takeover_import = previous_import
    return ApplyResult(
        applied=True, active_path=active, pending_path=pending,
        message=f"已写回活跃目录：{active}", backup_path=backup_path,
        before_raw_hash=before_raw_hash,
        after_canonical_hash=config.last_takeover["afterCanonicalHash"],
        created_file=not active_exists, diff_summary=diff.summary(),
        applied_at=applied_at)


def undo_pending(config) -> str:
    """Restore the bytes the last :func:`apply_pending` replaced.

    Confinement is still enforced (a demo-mode undo can never reach outside the
    sandbox), but the compatibility-evidence requirement is not re-checked: undo
    only reverses a change this app just made, and refusing it because a proof
    expired could strand the user in a state they cannot revert. The active file
    must still hash to exactly what we wrote, otherwise this refuses as a conflict.
    """
    from .safe_preview import demo_write_block_reason

    record = getattr(config, "last_takeover", None) or None
    if not record:
        raise InvalidConfiguration("没有已记录的写回操作可撤销。")
    active = record.get("activePath") or ""
    if not active:
        raise InvalidConfiguration("写回记录缺少活跃目录路径，无法撤销。")
    if not os.path.isfile(active):
        return f"无需撤销：活跃目录不存在：{active}"

    backup_dir = config.resolved_paths().backup_directory
    block = demo_write_block_reason(config, config.merged_catalog_path, active)
    if block:
        raise InvalidConfiguration(block)

    current_data = read_file(active)
    current_canonical = canonical_hash(parse_root(current_data, "活跃目录"))
    if record.get("afterCanonicalHash") and current_canonical != record["afterCanonicalHash"]:
        raise ConflictError(
            "冲突：活跃目录当前内容与上次写回值不一致，可能被外部修改，未自动撤销。"
            "请先预览确认。")

    if record.get("createdFile") or not record.get("backupPath"):
        os.remove(active)
        config.last_takeover = None
        return f"已撤销：删除了本次写回新建的活跃目录文件（{active}）。"

    backup_path = record["backupPath"]
    if not os.path.isfile(backup_path):
        raise InvalidConfiguration(f"写回备份不存在，无法撤销：{backup_path}")
    before_hash = record.get("beforeRawHash")
    if before_hash and sha256_hex(Path(backup_path).read_bytes()) != before_hash:
        raise ConflictError("冲突：写回备份内容与记录不一致，未自动撤销。")

    kept = restore_file(backup_path, active, backup_dir, prefix="active-catalog.before-undo")
    config.last_takeover = None
    return (f"已撤销写回：活跃目录已恢复为应用前内容（{active}）。"
            + (f" 撤销前状态已备份：{kept}" if kept else ""))


# ---------------------------------------------------------------------------
# Working-copy lifecycle
#
# The user should not have to understand "takeover" before editing a model. This
# section hides the two-file machinery behind one working copy: the manager keeps
# an editable copy of the catalog Codex actually reads, every edit goes there, and
# a single "view and apply" writes it back with the same gates as apply_pending.
# All state derivation here is read-only; writes happen only in the explicit
# ensure/discard/apply helpers.
# ---------------------------------------------------------------------------

WORKING_UNCONFIGURED = "unconfigured"   # no manager working-copy path configured
WORKING_NO_ACTIVE = "no_active"         # Codex config has no readable catalog
WORKING_NO_COPY = "no_copy"             # active exists, no working copy yet
WORKING_STALE = "stale"                 # working copy baseline lost/mismatched
WORKING_EMPTY = "empty"                 # working copy exists and is empty
WORKING_CLEAN = "clean"                 # working copy matches the active catalog
WORKING_MODIFIED = "modified"           # working copy has unapplied edits


@dataclass
class WorkingState:
    """Read-only description of the manager's editable copy of the live catalog."""

    status: str = ""
    message: str = ""
    active_path: Optional[str] = None
    working_path: Optional[str] = None
    active_exists: bool = False
    working_exists: bool = False
    working_model_count: int = 0
    diff: Optional[CatalogDiff] = None
    conflict: str = ""
    gate_reason: str = ""
    writable: bool = False
    imported_at: str = ""
    structure_warnings: List[str] = field(default_factory=list)

    @property
    def has_unapplied(self) -> bool:
        return bool(self.diff is not None and self.diff.has_changes)

    @property
    def ready_to_apply(self) -> bool:
        return (self.working_exists and self.writable and not self.conflict
                and self.has_unapplied)

    @property
    def can_edit(self) -> bool:
        return self.status != WORKING_UNCONFIGURED and self.working_exists


def _read_models(path: str) -> List[dict]:
    root = parse_root(read_file(path), Path(path).name)
    models = root.get("models")
    if not isinstance(models, list):
        raise InvalidCatalog(f"{Path(path).name}的 models 必须是数组")
    return [m for m in models if isinstance(m, dict)]


def render_working_state(state: "WorkingState") -> str:
    """Human-readable working-copy report: both paths, status, then the diff.

    Model data only: this never reads or renders the config file's other keys.
    """
    lines: List[str] = []
    if state.active_path:
        lines.append("Codex 实际使用目录: " + state.active_path
                     + ("" if state.active_exists else "（不存在）"))
    else:
        lines.append("Codex 实际使用目录: 未配置（所选配置未设置 model_catalog_json）")
    if state.working_path:
        lines.append("可编辑副本: " + state.working_path
                     + ("" if state.working_exists else "（尚未创建）"))
    if state.imported_at:
        lines.append("副本创建时间: " + state.imported_at)
    if state.message:
        lines.append(state.message)
    if state.conflict:
        lines.append(state.conflict)
    if state.working_exists and not state.writable and state.gate_reason:
        lines.append("写入校验未通过（只读）：" + state.gate_reason)
    for warning in state.structure_warnings:
        lines.append("提示：" + warning)
    if state.diff is not None:
        lines.append(f"与 Codex 实际使用目录的差异（{state.diff.summary()}）:")
        lines.append(render_diff(state.diff))
    return "\n".join(lines)


def working_state(config, active_path: Optional[str] = None) -> WorkingState:
    """Read-only working-copy state. Never writes, never raises for a bad setup."""
    from .errors import CodexModelError
    from .safe_preview import catalog_write_gate, validate_models

    try:
        working = pending_catalog_path(config)
    except CodexModelError as exc:
        return WorkingState(status=WORKING_UNCONFIGURED, message=str(exc))
    state = WorkingState(working_path=working)

    try:
        state.active_path = active_catalog_path(config, active_path)
    except CodexModelError as exc:
        state.active_path = None
        state.message = str(exc)

    record = getattr(config, "takeover_import", None) or {}
    state.imported_at = str(record.get("importedAt") or "")
    state.structure_warnings = list(record.get("structureWarnings") or [])
    state.active_exists = bool(state.active_path and os.path.isfile(state.active_path))
    state.working_exists = os.path.isfile(working)

    active_data = b'{"models": []}'
    if state.active_exists:
        active_data = read_file(state.active_path)
        try:
            parse_root(active_data, "活跃目录")
        except InvalidCatalog as exc:
            state.conflict = f"生效目录无法解析：{exc}"

    # The write gate is evaluated even when the live catalog does not exist yet:
    # creating it from an empty edit copy is a first write and must pass exactly the
    # same checks (demo sandbox / verified compatibility). Skipping it here made the
    # explicit "create the file" confirmation unreachable.
    if state.active_path:
        try:
            gate = catalog_write_gate(config, config.merged_catalog_path, state.active_path)
            state.writable = gate.writable
            state.gate_reason = gate.reason or ""
        except CodexModelError as exc:
            state.gate_reason = str(exc)

    working_root = None
    if state.working_exists:
        try:
            working_data = read_file(working)
            working_root = parse_root(working_data, "编辑副本")
            models = working_root.get("models")
            if not isinstance(models, list):
                raise InvalidCatalog("编辑副本的 models 必须是数组")
            # An empty list is a legitimate starting point (created from scratch so
            # the first model can be added); only a non-empty catalog is validated
            # against the structural rules here. Writing an empty catalog back is
            # still refused by apply_pending, which is the right place for it.
            if models:
                validate_models(models, require_priority=True, name="编辑副本")
            state.working_model_count = len([m for m in models if isinstance(m, dict)])
        except (InvalidCatalog, InvalidConfiguration) as exc:
            state.conflict = state.conflict or f"编辑副本结构无效：{exc}"
            working_root = None
        if working_root is not None:
            try:
                state.diff = diff_catalogs(active_data, read_file(working))
            except (InvalidCatalog, InvalidConfiguration) as exc:
                state.conflict = state.conflict or str(exc)

    if not state.working_exists:
        state.status = WORKING_NO_ACTIVE if not state.active_exists else WORKING_NO_COPY
        if not state.message:
            if state.active_exists:
                state.message = "尚未创建可编辑副本：编辑任意模型时会从当前 Codex 目录自动创建。"
            elif state.active_path:
                state.message = (f"Codex 配置引用的模型目录不存在：{state.active_path}。"
                                 "可以先创建一个空的编辑副本。")
            else:
                state.message = ("Codex 配置尚未指向模型目录（model_catalog_json）："
                                 "添加模型后点“查看并应用修改”，会创建生效目录并写好引用。")
        return state

    if state.active_exists:
        imported_path = str(record.get("activePath") or "")
        if imported_path and not _same_file(state.active_path, imported_path):
            state.conflict = state.conflict or (
                "冲突：当前生效目录与创建编辑副本时的目录不是同一个文件。"
                "请重新创建编辑副本后再写回。")
        elif not imported_path:
            state.conflict = state.conflict or (
                "尚未从生效目录创建编辑副本；请重新创建编辑副本，再编辑并写回。")
        if not state.conflict and record:
            imported = record.get("activeCanonicalHash")
            try:
                current = canonical_hash(parse_root(active_data, "活跃目录"))
            except InvalidCatalog as exc:
                state.conflict = f"冲突：生效目录无法解析：{exc}"
            else:
                if imported and current != imported:
                    state.conflict = (
                        "外部修改：生效目录在创建编辑副本后被改动，写回会覆盖别人的修改，已暂停。"
                        "可“重新载入最新目录”放弃本地修改，或“保留我的修改”显式以最新目录为新基线。")

    if state.conflict:
        state.status = WORKING_STALE
    elif state.working_model_count == 0 and not state.active_exists:
        state.status = WORKING_EMPTY
        state.message = "编辑副本为空，可以直接添加模型。"
    elif state.has_unapplied:
        state.status = WORKING_MODIFIED
        state.message = "有未应用的修改（尚未写回 Codex）。"
    else:
        state.status = WORKING_CLEAN
        state.message = "编辑副本与生效目录一致。"
    return state


def _empty_working_document() -> bytes:
    return _pretty({"schema": EMPTY_SCHEMA, "models": []})


def _write_empty_working(config) -> str:
    """Write (or back up and replace) a valid empty working copy; return its path."""
    backup_dir = config.resolved_paths().backup_directory
    working = pending_write_path(config)
    if os.path.exists(working):
        backup_file(working, backup_dir, prefix="pending-catalog")
    atomic_write(working, _empty_working_document())
    config.takeover_catalog_path = working
    config.takeover_import = {
        "activePath": None,
        "pendingPath": working,
        "importedAt": _iso_now(),
        "activeRawHash": None,
        "activeCanonicalHash": None,
        "modelCount": 0,
        "replacedExistingPending": False,
        "structureWarnings": [],
    }
    return working


def ensure_working_copy(config, active_path: Optional[str] = None, *,
                        create_empty: bool = True,
                        persist: Optional[Callable[[], None]] = None) -> WorkingState:
    """Materialise the editable copy, if it does not already exist.

    - an existing working copy is returned untouched (never silently overwritten,
      even when its baseline is stale);
    - active catalog readable -> copy it verbatim (unknown fields preserved);
    - no active catalog yet and ``create_empty`` -> start from a valid empty file;
    - otherwise report the concrete reason instead of writing.
    """
    state = working_state(config, active_path)
    if state.status == WORKING_UNCONFIGURED:
        raise InvalidConfiguration(state.message)
    if state.working_exists:
        return state
    try:
        active = active_catalog_path(config, active_path)
    except Exception:  # noqa: BLE001 - "no active catalog" is an expected setup
        active = None

    if active and os.path.isfile(active):
        import_active(config, active)
    elif create_empty:
        _write_empty_working(config)
    else:
        raise InvalidConfiguration(state.message or "没有可创建编辑副本的生效目录。")
    if persist is not None:
        persist()
    return working_state(config, active_path)


def reload_from_active(config, active_path: Optional[str] = None, *,
                       persist: Optional[Callable[[], None]] = None) -> WorkingState:
    """Discard local edits: recreate the working copy from the current live catalog.

    This is the recovery path for "the live file changed underneath us" and the
    button behind "放弃修改". The replaced working copy is backed up first; when
    there is no live catalog the working copy is reset to a valid empty document.
    """
    from .errors import CodexModelError

    try:
        active = active_catalog_path(config, active_path)
    except CodexModelError:
        active = None
    if active and os.path.isfile(active):
        import_active(config, active)
    else:
        _write_empty_working(config)
    if persist is not None:
        persist()
    return working_state(config, active_path)


def rebind_baseline(config, active_path: Optional[str] = None, *,
                    persist: Optional[Callable[[], None]] = None) -> WorkingState:
    """Keep local edits but accept the current live catalog as the new baseline.

    This is the explicit recovery for "someone else changed the live catalog": the
    user has seen the warning and chooses to write their edits on top of the newest
    content. Nothing is written to the live catalog here; only the import record's
    baseline hashes advance.
    """
    state = working_state(config, active_path)
    if not state.working_exists:
        raise InvalidConfiguration("还没有可编辑副本。")
    if state.active_path is None or not os.path.isfile(state.active_path):
        raise InvalidConfiguration("当前没有可用的生效目录，无法登记新基线。")
    raw = read_file(state.active_path)
    root = parse_root(raw, "活跃目录")
    record = dict(getattr(config, "takeover_import", None) or {})
    record.update({
        "activePath": state.active_path,
        "pendingPath": state.working_path,
        "activeRawHash": sha256_hex(raw),
        "activeCanonicalHash": canonical_hash(root),
        "rebasedAt": _iso_now(),
    })
    config.takeover_catalog_path = state.working_path
    config.takeover_import = record
    if persist is not None:
        persist()
    return working_state(config, active_path)


def discard_working(config, active_path: Optional[str] = None, *,
                    persist: Optional[Callable[[], None]] = None) -> WorkingState:
    """Give up unapplied edits and restore the working copy to the live catalog.

    Unlike :func:`reload_from_active`, a missing live catalog resets the working
    copy to a valid empty document rather than raising, so "放弃修改" always leaves a
    clean, valid state.
    """
    return reload_from_active(config, active_path, persist=persist)


def record_import_without_active(config, *, model_count: int = 0,
                                 persist: Optional[Callable[[], None]] = None) -> WorkingState:
    """Record a working copy imported from a file when no live catalog exists.

    Used by "导入模型目录到编辑副本": the user picked a valid catalog, but the
    selected config points at no catalog yet, so there is no live file to bind to.
    The copy is still fully editable; applying it needs a live target first.
    """
    working = pending_write_path(config)
    config.takeover_catalog_path = working
    config.takeover_import = {
        "activePath": None,
        "pendingPath": working,
        "importedAt": _iso_now(),
        "activeRawHash": None,
        "activeCanonicalHash": None,
        "modelCount": int(model_count),
        "replacedExistingPending": True,
        "structureWarnings": [],
    }
    if persist is not None:
        persist()
    return working_state(config)


# First apply: a config.toml with no model_catalog_json yet
#
# The working-copy flow assumes Codex already references a catalog. When it does
# not, "apply" has to change TWO files: publish the edit copy into a real catalog
# file, and point the selected config.toml at that file. Two files cannot be
# replaced atomically, so this is implemented as a small recoverable transaction:
#
#   * the preview returns a STATE SNAPSHOT (existence + content hashes of the
#     config, the target catalog and the edit copy). apply only accepts a matching
#     snapshot, so anything that changed while the confirmation dialog was open is
#     refused instead of silently overwritten (a value appearing where there was
#     none counts as a change);
#   * before any write, the target catalog and the config are backed up and a
#     PHASE RECORD is persisted (planned -> catalog_written -> applied), so an
#     interrupted run is recoverable after a restart;
#   * if the config write fails after the catalog was replaced, the catalog is
#     compensated: the original bytes are restored (or the created file removed)
#     unless someone else changed it in the meantime, in which case nothing is
#     overwritten and the record is kept so the user can retry;
#   * undo refuses before touching anything when either file moved on, and never
#     claims success while a restore is still outstanding.
#
# The published file is deliberately NOT the edit copy - keeping them separate is
# what makes the later "edit -> preview -> apply" cycle safe.
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = ("config_path", "config_exists", "config_raw_hash",
                   "target_catalog", "target_exists", "target_raw_hash",
                   "edit_copy", "edit_copy_raw_hash", "edit_copy_canonical_hash",
                   "model_count")
_SNAPSHOT_LABELS = {
    "config_path": "config.toml 路径",
    "config_exists": "config.toml 是否存在",
    "config_raw_hash": "config.toml 内容",
    "target_catalog": "生效目录路径",
    "target_exists": "生效目录是否存在",
    "target_raw_hash": "生效目录内容",
    "edit_copy": "编辑副本路径",
    "edit_copy_raw_hash": "编辑副本内容",
    "edit_copy_canonical_hash": "编辑副本内容",
    "model_count": "模型数",
}


@dataclass
class FirstApplyResult:
    applied: bool
    edit_copy: str
    target_catalog: str
    config_path: str
    previous_value: Optional[str]
    applied_value: str
    catalog_backup: str = ""
    model_count: int = 0
    created_catalog: bool = False
    message: str = ""


@dataclass
class FirstApplyRecovery:
    """Outcome of a compensation/undo attempt for a first apply."""

    ok: bool
    report: str
    config_restored: bool = False
    catalog_restored: bool = False
    record_cleared: bool = False
    conflicts: List[str] = field(default_factory=list)


def _runtime_catalog_value(config, target: str) -> str:
    """The string the runtime should read for `target` (Linux path for WSL)."""
    try:
        return config.runtime_target().to_runtime_path(target)
    except Exception:  # noqa: BLE001 - preview must not need a runnable runtime
        return target


def _raw_hash(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        if os.path.isfile(path):
            return sha256_hex(Path(path).read_bytes())
    except OSError:
        return None
    return None


def _canonical_hash_or_none(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        if os.path.isfile(path):
            return canonical_hash(parse_root(read_file(path), Path(path).name))
    except (InvalidCatalog, InvalidConfiguration):
        return None
    return None


def _catalog_snapshot(config, target: str, pending: str) -> dict:
    """Existence + content hashes of everything the confirmation is bound to."""
    model_count = 0
    if pending and os.path.isfile(pending):
        try:
            root = parse_root(read_file(pending), "编辑副本")
            models = root.get("models")
            if isinstance(models, list):
                model_count = len([m for m in models if isinstance(m, dict)])
        except InvalidCatalog:
            model_count = 0
    config_path = (getattr(config, "codex_config_path", None) or "").strip()
    return {
        "config_path": config_path,
        "config_exists": bool(config_path) and os.path.isfile(config_path),
        "config_raw_hash": _raw_hash(config_path),
        "target_catalog": target,
        "target_exists": bool(target) and os.path.isfile(target),
        "target_raw_hash": _raw_hash(target),
        "edit_copy": pending,
        "edit_copy_raw_hash": _raw_hash(pending),
        "edit_copy_canonical_hash": _canonical_hash_or_none(pending),
        "model_count": model_count,
    }


def first_apply_preview(config, target_catalog: Optional[str] = None) -> dict:
    """Read-only description of the first apply, plus the state snapshot.

    Writes nothing. The returned ``snapshot`` must be handed back to
    :func:`apply_first_time`; apply refuses when the files no longer match it.
    """
    from .safe_preview import preview

    pending = pending_catalog_path(config)
    target = str(target_catalog or getattr(config, "merged_catalog_path", "") or "").strip()
    config_path = (getattr(config, "codex_config_path", None) or "").strip()
    snapshot = _catalog_snapshot(config, target, pending)
    info = preview(config_path, target) if config_path else {
        "config_path": "", "current": None, "parse_ok": False}
    writable, reason = False, ""
    if config_path and target and pending and os.path.isfile(pending):
        from .safe_preview import apply_gate, catalog_write_gate

        try:
            config_gate = apply_gate(config, pending, config_path, readonly=False)
            catalog_gate = catalog_write_gate(config, pending, target)
            writable = bool(config_gate.writable and catalog_gate.writable)
            reason = config_gate.reason or catalog_gate.reason or ""
        except CodexModelError as exc:
            reason = str(exc)
    elif not os.path.isfile(pending):
        reason = "还没有可编辑副本，无法首次应用。"
    return {
        "edit_copy": pending,
        "target_catalog": target,
        "config_path": config_path,
        "config_exists": snapshot["config_exists"],
        "config_parse_ok": bool(info.get("parse_ok")),
        "current_value": info.get("current"),
        "proposed_value": _runtime_catalog_value(config, target) if target else "",
        "target_exists": snapshot["target_exists"],
        "target_is_edit_copy": bool(target) and _same_file(target, pending),
        "model_count": snapshot["model_count"],
        "writable": writable,
        "gate_reason": reason,
        "snapshot": snapshot,
    }


def _verify_snapshot(expected, current: dict) -> None:
    """Refuse when anything the confirmation was based on has changed.

    Comparison is against the snapshot produced by :func:`first_apply_preview`,
    not against a value re-sampled only at apply time: a key appearing where the
    preview saw none is a change and is refused.
    """
    if not isinstance(expected, dict) or not expected.get("edit_copy"):
        raise InvalidConfiguration(
            "首次应用必须先预览并确认：请重新点“查看并应用修改…”生成预览。")
    changed = [_SNAPSHOT_LABELS[field] for field in SNAPSHOT_FIELDS
               if expected.get(field) != current.get(field)]
    if changed:
        raise ConflictError(
            "预览后以下内容发生变化：" + "、".join(dict.fromkeys(changed))
            + "。为避免覆盖他人的改动，本次未写入任何内容；请重新预览并确认。")


def apply_first_time(config, *, expected, target_catalog: Optional[str] = None,
                     persist: Optional[Callable[[], None]] = None) -> FirstApplyResult:
    """Publish the edit copy and make the selected config.toml reference it.

    Refusals, in order, all before anything is written:
      1. the edit copy must exist, parse and be a valid non-empty catalog;
      2. a target catalog path must be resolvable, must not be the edit copy and
         must not be a directory;
      3. the shared config gate (runtime + demo sandbox / verified evidence) and
         the shared catalog write gate must both allow the write;
      4. the config.toml must parse, so a broken file is never rewritten;
      5. the CURRENT state must still equal the ``expected`` preview snapshot.

    Then the writes happen as a recoverable two-file transaction: backups and a
    durable phase record first, catalog second, config reference last. A failure
    after the catalog write is compensated (original bytes restored / created file
    removed) unless the file was changed by someone else meanwhile, in which case
    nothing is overwritten and the record is kept for a retry. The edit copy is
    never modified.
    """
    from .config_editor import set_model_catalog
    from .safe_preview import apply_gate, catalog_write_gate, parse_error_note, preview, validate_models

    pending = pending_catalog_path(config)
    if not os.path.isfile(pending):
        raise InvalidConfiguration("还没有可编辑副本，无法首次应用。请先添加模型。")
    pending_data = read_file(pending)
    pending_root = parse_root(pending_data, "编辑副本")
    models = pending_root.get("models")
    if not isinstance(models, list) or not models:
        raise InvalidCatalog("编辑副本为空，没有可应用的模型。请先添加至少一个模型。")
    validate_models(models, require_priority=True, name="编辑副本")

    config_path = (getattr(config, "codex_config_path", None) or "").strip()
    if not config_path:
        raise InvalidConfiguration(
            "未设置 config.toml（codexConfigPath）：请先在连接页选择正在使用的 Codex 配置。")
    target = str(target_catalog or getattr(config, "merged_catalog_path", "") or "").strip()
    if not target:
        raise InvalidConfiguration("未配置生效目录路径（mergedCatalogPath）。")
    if os.path.isdir(target):
        raise InvalidConfiguration(f"生效目录是目录，拒绝写入：{target}")
    if _same_file(target, pending):
        raise InvalidConfiguration(
            "生效目录不能与编辑副本是同一个文件：那样之后的编辑会立即影响 Codex，"
            "也会绕过写回确认。请为生效目录选择另一个文件。")

    config_gate = apply_gate(config, pending, config_path, readonly=False)
    if not config_gate.writable:
        raise InvalidConfiguration(config_gate.reason or "写入校验未通过，已拒绝首次应用。")
    catalog_gate = catalog_write_gate(config, pending, target)
    if not catalog_gate.writable:
        raise InvalidConfiguration(catalog_gate.reason or "生效目录写入校验未通过，已拒绝。")

    info = preview(config_path, target)
    if not info["parse_ok"]:
        raise InvalidConfiguration(parse_error_note(config_path))

    # Bind the write to the previewed state (including "an absent file appeared").
    _verify_snapshot(expected, _catalog_snapshot(config, target, pending))

    backup_dir = config.resolved_paths().backup_directory
    target_exists = os.path.isfile(target)
    target_backup = (backup_file(target, backup_dir, prefix="first-apply-catalog")
                     if target_exists else "") or ""
    config_exists = os.path.isfile(config_gate.target)
    config_backup = (backup_file(config_gate.target, backup_dir, prefix="config.toml.first-apply")
                     if config_exists else "") or ""
    applied_value = _runtime_catalog_value(config, target)
    before_root = None
    if target_exists:
        try:
            before_root = parse_root(read_file(target), "生效目录")
        except (InvalidCatalog, InvalidConfiguration):
            before_root = None

    record = {
        "phase": "planned",
        "editCopy": pending,
        "targetCatalog": target,
        "configPath": config_gate.target,
        "previousValue": info["current"],
        "appliedValue": applied_value,
        "catalogBackup": target_backup or None,
        "configBackup": config_backup or None,
        "createdCatalog": not target_exists,
        "configExisted": config_exists,
        "targetBeforeRawHash": _raw_hash(target) if target_exists else None,
        "targetBeforeCanonicalHash": canonical_hash(before_root) if before_root else None,
        "configBeforeRawHash": _raw_hash(config_gate.target) if config_exists else None,
        "afterCanonicalHash": canonical_hash(pending_root),
        "modelCount": len([m for m in models if isinstance(m, dict)]),
        "appliedAt": _iso_now(),
    }
    previous_record = getattr(config, "last_first_apply", None)
    previous_import = getattr(config, "takeover_import", None)
    config.last_first_apply = record
    if persist is not None:
        persist()

    # ---- write 1/2: the catalog -------------------------------------------
    try:
        atomic_write(target, pending_data)
    except Exception as exc:  # noqa: BLE001 - nothing was replaced
        config.last_first_apply = previous_record
        if persist is not None:
            persist()
        raise InvalidConfiguration(
            f"首次应用失败：写入生效目录时出错，未替换任何文件（{exc}）。") from exc
    record["phase"] = "catalog_written"
    config.last_first_apply = record
    if persist is not None:
        persist()

    # ---- write 2/2: the config reference ----------------------------------
    try:
        set_model_catalog(applied_value, config_gate.target, backup_dir)
    except Exception as exc:  # noqa: BLE001 - compensate the catalog
        outcome = _recover_first_apply(config, record, persist=persist)
        if outcome.ok:
            detail = ("首次应用失败：config.toml 未改写，生效目录已恢复到应用前状态"
                      f"（{exc}）。\n{outcome.report}")
        else:
            detail = ("首次应用失败：config.toml 未改写，但生效目录未能自动恢复"
                      f"（{exc}）。\n{outcome.report}")
        raise InvalidConfiguration(detail) from exc

    record["phase"] = "applied"
    record["afterConfigRawHash"] = _raw_hash(config_gate.target)
    config.last_first_apply = record
    config.takeover_catalog_path = pending
    config.takeover_import = {
        "activePath": target,
        "pendingPath": pending,
        "importedAt": _iso_now(),
        "activeRawHash": sha256_hex(pending_data),
        "activeCanonicalHash": canonical_hash(pending_root),
        "modelCount": record["modelCount"],
        "replacedExistingPending": False,
        "structureWarnings": [],
        "createdByFirstApply": record["createdCatalog"],
    }
    try:
        if persist is not None:
            persist()
    except BaseException:
        config.takeover_import = previous_import
    return FirstApplyResult(
        applied=True, edit_copy=pending, target_catalog=target,
        config_path=config_gate.target, previous_value=info["current"],
        applied_value=applied_value, catalog_backup=target_backup,
        model_count=record["modelCount"], created_catalog=record["createdCatalog"],
        message=f"已首次应用：{record['modelCount']} 个模型 -> {target}，"
                f"并让 config.toml 引用它。")


def _recover_first_apply(config, record, *,
                         persist: Optional[Callable[[], None]] = None) -> FirstApplyRecovery:
    """Restore the two files of a first apply, or report exactly what blocks it.

    Analysis happens for BOTH files before anything is touched. Nothing is
    overwritten when the current content is not what this transaction wrote, and
    the record is only cleared when everything is back to the pre-apply state.
    """
    from .config_editor import undo_model_catalog

    config_path = record.get("configPath") or ""
    target = record.get("targetCatalog") or ""
    applied_value = record.get("appliedValue") or ""
    previous_value = record.get("previousValue")
    backup_dir = config.resolved_paths().backup_directory
    conflicts: List[str] = []
    notes: List[str] = []

    # ---- analyse config.toml ----
    if not config_path:
        config_state = "previous"
    elif not os.path.isfile(config_path):
        if record.get("configExisted", True):
            config_state = "conflict"
            conflicts.append(f"config.toml 不存在：{config_path}")
        else:
            config_state = "previous"          # it was never created
    else:
        try:
            import tomlkit

            parsed = tomlkit.parse(Path(config_path).read_text(encoding="utf-8"))
            value = parsed.get("model_catalog_json")
            current_value = None if value is None else str(value)
            if current_value == applied_value:
                config_state = "applied"
            elif previous_value is None and value is None:
                config_state = "previous"
            elif previous_value is not None and current_value == previous_value:
                config_state = "previous"
            else:
                config_state = "conflict"
                conflicts.append(
                    f"config.toml 的 model_catalog_json 已被外部修改（当前={current_value!r}），"
                    "未自动还原。")
        except Exception as exc:  # noqa: BLE001
            config_state = "conflict"
            conflicts.append(f"config.toml 无法解析：{exc}")

    # ---- analyse the published catalog ----
    after_hash = record.get("afterCanonicalHash")
    # A crash can occur after os.replace but before catalog_written is saved.
    # Even planned records must inspect the file against both recorded hashes.
    wrote_catalog = bool(after_hash) and record.get("phase") in (
        "planned", "catalog_written", "applied", "compensating")
    catalog_state = "not_written"
    if wrote_catalog:
        if not target or not os.path.isfile(target):
            if record.get("createdCatalog"):
                catalog_state = "gone"
            else:
                catalog_state = "conflict"
                conflicts.append(f"生效目录不存在：{target}")
        else:
            current_hash = _canonical_hash_or_none(target)
            if current_hash == after_hash:
                catalog_state = "ours"
            elif (record.get("targetBeforeRawHash") and
                  _raw_hash(target) == record.get("targetBeforeRawHash")) or (
                    record.get("targetBeforeCanonicalHash") and
                    current_hash == record.get("targetBeforeCanonicalHash")):
                catalog_state = "original"      # our write never landed
            else:
                catalog_state = "conflict"
                conflicts.append(f"生效目录已被外部修改，未自动覆盖：{target}")

    if conflicts:
        config.last_first_apply = record
        if persist is not None:
            persist()
        return FirstApplyRecovery(
            ok=False,
            report="未做任何恢复（检测到外部改动或文件缺失），记录已保留；"
                   "请先处理以下内容再重试撤销：\n- " + "\n- ".join(conflicts),
            conflicts=conflicts)

    # ---- restore the catalog ----
    catalog_restored = False
    if catalog_state == "ours":
        if record.get("createdCatalog"):
            os.remove(target)
            catalog_restored = True
            notes.append(f"已删除本次新建的生效目录：{target}")
        else:
            backup = record.get("catalogBackup") or ""
            before_raw = record.get("targetBeforeRawHash")
            if not backup or not os.path.isfile(backup):
                config.last_first_apply = record
                if persist is not None:
                    persist()
                return FirstApplyRecovery(
                    ok=False,
                    report=f"生效目录缺少可用备份，未做任何恢复，记录已保留：{target}",
                    conflicts=["缺少生效目录备份"])
            if before_raw and sha256_hex(Path(backup).read_bytes()) != before_raw:
                config.last_first_apply = record
                if persist is not None:
                    persist()
                return FirstApplyRecovery(
                    ok=False,
                    report=f"生效目录备份内容校验失败，未做任何恢复，记录已保留：{backup}",
                    conflicts=["生效目录备份校验失败"])
            atomic_write(target, Path(backup).read_bytes())
            catalog_restored = True
            notes.append(f"生效目录已恢复为应用前内容：{target}")
    elif catalog_state == "gone":
        notes.append("生效目录已不存在，无需恢复。")
    elif catalog_state == "original":
        notes.append("生效目录仍是应用前内容，无需恢复。")

    # ---- restore config.toml ----
    config_restored = False
    if config_state == "applied":
        byte_exact = bool(record.get("afterConfigRawHash")) and \
            _raw_hash(config_path) == record.get("afterConfigRawHash")
        config_backup = record.get("configBackup") or ""
        backup_ok = bool(config_backup) and os.path.isfile(config_backup) and (
            not record.get("configBeforeRawHash")
            or _raw_hash(config_backup) == record.get("configBeforeRawHash"))
        if byte_exact and backup_ok:
            atomic_write(config_path, Path(config_backup).read_bytes())
            notes.append("config.toml 已恢复为应用前内容（字节级）。")
        else:
            notes.append("config.toml：" + undo_model_catalog(
                config_path, backup_dir, previous_value, applied_value))
        config_restored = True
    elif config_state == "previous":
        notes.append("config.toml 仍是应用前的值，无需恢复。")

    config.last_first_apply = None
    config.takeover_import = {
        "activePath": None,
        "pendingPath": record.get("editCopy") or pending_catalog_path(config),
        "importedAt": _iso_now(),
        "activeRawHash": None,
        "activeCanonicalHash": None,
        "modelCount": 0,
        "replacedExistingPending": False,
        "structureWarnings": [],
        "revertedByUndo": True,
    }
    if persist is not None:
        persist()
    return FirstApplyRecovery(
        ok=True, report="；".join(notes) or "没有需要恢复的内容。",
        config_restored=config_restored, catalog_restored=catalog_restored,
        record_cleared=True)


def undo_first_apply(config, *, persist: Optional[Callable[[], None]] = None) -> str:
    """Undo :func:`apply_first_time` (also recovers an interrupted run).

    Both files are analysed before anything is touched: if either was changed by
    someone else, or a needed backup is missing, nothing is overwritten and the
    record is kept. The record is cleared only once every restore is complete.
    """
    record = getattr(config, "last_first_apply", None) or None
    if not record:
        raise InvalidConfiguration("没有已记录的首次应用可撤销。")
    if not record.get("configPath") or not record.get("targetCatalog"):
        raise InvalidConfiguration("首次应用记录缺少路径，无法撤销。")
    outcome = _recover_first_apply(config, record, persist=persist)
    if not outcome.ok:
        raise ConflictError(outcome.report)
    return outcome.report


def pending_first_apply(config) -> Optional[dict]:
    """Summary of an unfinished first apply (for the UI), or None."""
    record = getattr(config, "last_first_apply", None) or None
    if not record:
        return None
    phase = str(record.get("phase") or "applied")
    if phase == "applied":
        return None
    return {
        "phase": phase,
        "target": record.get("targetCatalog"),
        "config": record.get("configPath"),
    }
