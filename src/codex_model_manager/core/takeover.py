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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .backup import atomic_write, backup_file, restore_file
from .errors import ConflictError, InvalidCatalog, InvalidConfiguration

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

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    @property
    def removed_slugs(self) -> List[str]:
        return [c.slug for c in self.removed]

    def summary(self) -> str:
        return (f"新增 {len(self.added)}，修改 {len(self.modified)}，"
                f"删除 {len(self.removed)}")


def diff_catalogs(active_data: bytes, pending_data: bytes) -> CatalogDiff:
    """Compare two catalog documents, by slug, field by field.

    A model present on only one side is added (pending only) or removed (active
    only); a model present on both sides but with different content is modified,
    listing exactly which fields changed. Field names are the raw catalog keys, so
    unknown fields are diffed too rather than hidden.
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
            if json.dumps(old, **_CANONICAL) != json.dumps(new, **_CANONICAL):
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
    return diff


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


def active_catalog_path(config, explicit: Optional[str] = None) -> str:
    """Where the active catalog is: an explicit path, else the selected config.

    The fallback reads ONLY the top-level ``model_catalog_json`` of the configured
    ``codexConfigPath``; nothing else from the file is read, stored or printed. A
    relative value resolves against the config file's directory.
    """
    if explicit and explicit.strip():
        return str(Path(explicit.strip()).expanduser().absolute())
    config_path = (getattr(config, "codex_config_path", None) or "").strip()
    if not config_path:
        raise InvalidConfiguration(
            "未指定生效配置引用目录：请在应用配置里设置 codexConfigPath（连接页所选配置），"
            "或用 --active 显式给出 model_catalog_json 指向的文件。")
    from .safe_preview import preview

    info = preview(config_path, "")
    current = info["current"]
    if not current:
        raise InvalidConfiguration(
            f"所选配置未设置 model_catalog_json，没有可接管的目录：{config_path}")
    value = Path(str(current)).expanduser()
    if not value.is_absolute():
        value = Path(config_path).parent / value
    return str(value.absolute())


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
