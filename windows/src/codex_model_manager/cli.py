"""Command-line sync / management entry point (arg-parser, no shell).

Equivalent to Sources/CodexModelSync/main.swift but with structured subcommands
for management tasks. All child processes go through the argument-array runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core.app_config import AppConfiguration
from .core.backup import backup_file, restore_file
from .core.errors import CodexModelError
from .core.catalog_sync import CatalogSyncService
from .core.custom_models import NewModelDraft
from .core.reasoning import ReasoningSettings
from .services.catalog_data_service import CatalogDataService


def _config(args) -> AppConfiguration:
    url = args.config or AppConfiguration.default_url()
    if not os.path.exists(url):
        required = "{'" + "', '".join(
            ["customSourcePath", "mergedCatalogPath", "syncLogPath", "errorLogPath", "backupDirectoryPath"]
        ) + "'}"
        raise CodexModelError(
            f"尚未找到配置：{url}\n"
            f"请先运行 `python -m codex_model_manager init` 生成默认配置，或提供 --config。"
        )
    return AppConfiguration.load(url)


def cmd_init(args) -> int:
    config = AppConfiguration.recommended()
    if args.codex:
        config.codex_runtime_path = args.codex
    url = args.config or config.default_url()
    config.save(url)
    print(f"已生成默认配置：{url}")
    print("默认采用沙箱/预览模式：模型源与合并目录位于应用沙箱，不改动 Codex 实际读取的配置。")
    print("真实写入需显式 `apply`（先只读预览，再由你在配置中设定 codexConfigPath 目标并确认）。")
    return 0


def cmd_sync(args) -> int:
    paths = _config(args).resolved()
    service = CatalogSyncService(paths)
    service.ensure_initial_files()
    service.clear_error_log()
    result = service.sync()  # reading only; does not touch real ~/.codex
    record = json.loads(result.record_data)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if args.append_log:
        with open(paths.sync_log, "ab") as fh:
            fh.write(result.record_data + b"\n")
    return 0


def cmd_list(args) -> int:
    service = CatalogDataService.from_config(_config(args))
    snapshot = service.load_snapshot()
    for m in snapshot.models:
        if not m.matches(args.search or ""):
            continue
        print(f"[{m.source}] {m.slug}\t{m.display_name}\tctx={m.context_window}\tvis={m.visibility}")
    return 0


def cmd_add(args) -> int:
    service = CatalogDataService.from_config(_config(args))
    draft = NewModelDraft(
        slug=args.slug,
        display_name=args.name or args.slug,
        description=args.description or "",
        template_slug=args.template,
        context_window=args.context_window,
        supports_image=args.image,
    )
    service.add_custom_model(draft)
    print(f"已新增自定义模型：{args.slug}")
    return 0


def cmd_edit_reasoning(args) -> int:
    service = CatalogDataService.from_config(_config(args))
    supported = [e.strip() for e in args.efforts.split(",") if e.strip()]
    settings = ReasoningSettings(supported_efforts=supported, default_effort=args.default)
    service.update_reasoning(settings, args.slug)
    print(f"已更新模型 {args.slug} 的推理档位。")
    return 0


def cmd_backup(args) -> int:
    config = _config(args)
    paths = config.resolved_paths()
    targets = [paths.custom_source, paths.merged_catalog]
    backup_dir = args.backup_dir or paths.backup_directory
    for t in targets:
        p = backup_file(t, backup_dir, prefix=Path(t).stem or "file")
        print(f"{'已备份' if p else '不存在'} {t}" + (f" -> {p}" if p else ""))
    return 0


def cmd_restore(args) -> int:
    config = _config(args)
    paths = config.resolved_paths()
    from .core.backup import classify_backup, restore_file

    target_map = {
        "merged": paths.merged_catalog,
        "custom": paths.custom_source,
    }
    target = target_map.get(args.target)
    if target is None:
        # config target restores the codex config.toml identified by codexConfigPath.
        if not config.codex_config_path:
            raise CodexModelError("恢复 config 需要应用配置设置 codexConfigPath。")
        target = config.codex_config_path

    # Content-based validation: never let a config.toml backup overwrite a model
    # JSON (or vice versa). No filename guessing.
    expected = "toplevel_config" if args.target == "config" else "model_catalog_json"
    kind = classify_backup(args.backup_path)
    if kind == "unknown":
        raise CodexModelError(f"备份文件无法识别（既非模型目录 JSON 也非 TOML 配置）：{args.backup_path}")
    if kind != expected:
        raise CodexModelError(
            f"备份类型不匹配，已拒绝恢复：备份是 {kind}，目标需要 {expected}。"
            f"请选择正确的备份，避免类型混淆。"
        )

    # Structural validation for model catalogs: don't let a malformed catalog
    # (``models`` with non-object/duplicate/missing slugs, wrong field types) or a
    # corrupt backup overwrite a healthy model source. Merged catalogs require an
    # integer priority and a non-empty list; custom sources may be empty (they are
    # legitimately initialised empty) but carry no priority.
    if kind == "model_catalog_json":
        from .core.safe_preview import validate_catalog_file
        from .core.errors import InvalidCatalog

        require_priority = (args.target == "merged")
        allow_empty = (args.target == "custom")
        try:
            validate_catalog_file(
                args.backup_path, require_priority=require_priority,
                allow_empty=allow_empty, name="备份模型目录")
        except InvalidCatalog as exc:
            raise CodexModelError(f"备份模型目录结构无效，已拒绝恢复：{exc}")

    before = restore_file(args.backup_path, target, paths.backup_directory, prefix=Path(target).stem)
    print(f"已恢复 {args.backup_path} -> {target}")
    if before:
        print(f"恢复前状态已备份：{before}")
    return 0


# ---------------------------------------------------------------------------
# Takeover: bring the catalog Codex actually reads under management.
# ---------------------------------------------------------------------------

def _save(config, args) -> None:
    """Persist app state (import/apply/undo records) to the config we loaded."""
    config.save(args.config or AppConfiguration.default_url())


def cmd_takeover_import(args) -> int:
    """Copy the active catalog into the manager's pending catalog (preserving all)."""
    from .core import takeover

    config = _config(args)
    record = takeover.import_active(config, args.active, create_empty=args.create_empty)
    _save(config, args)
    print("已导入副本，接管进入“待应用”状态（尚未影响 Codex 读取的目录）。")
    print(f"  生效配置引用目录：{record['activePath']}")
    print(f"  待应用目录：{record['pendingPath']}")
    print(f"  模型数：{record['modelCount']}")
    for warning in record.get("structureWarnings") or []:
        print(f"  提示：{warning}")
    print("下一步：编辑待应用目录（edit-model / add），再用 takeover-diff 查看差异。")
    return 0


def cmd_takeover_diff(args) -> int:
    """Read-only: show both directories and the pending-vs-active difference."""
    from .core import takeover

    config = _config(args)
    state = takeover.inspect(config, args.active)
    print(takeover.render_state(state))
    print("（只读预览：未写入、未备份、未创建任何文件。）")
    return 0


def cmd_takeover_apply(args) -> int:
    """Explicitly write the pending catalog back over the active path.

    Gated by the same sandbox/compatibility checks as `apply`; refused when the
    active catalog changed after import, and refused when the write-back would drop
    existing models unless --confirm-removals is given.
    """
    from .core import takeover

    config = _config(args)
    state = takeover.inspect(config, args.active)
    print(takeover.render_state(state))
    if not state.ready:
        problems = [p for p in (state.pending_error, state.conflict, state.gate_reason) if p]
        print("！（只读）未写入：" + (problems[0] if problems else "接管尚未就绪（请先导入）。"))
        return 1
    result = takeover.apply_pending(
        config, active_path=args.active,
        confirm_removals=args.confirm_removals, allow_create=args.allow_create,
        persist=lambda: _save(config, args))
    print(result.message)
    if result.applied:
        print(f"  差异：{result.diff_summary}")
        if result.backup_path:
            print(f"  原文件已备份：{result.backup_path}")
        print("  可用 `takeover-undo` 撤销本次写回。")
    return 0


def cmd_takeover_undo(args) -> int:
    """Undo the last takeover write-back (with conflict detection)."""
    from .core import takeover

    config = _config(args)
    message = takeover.undo_pending(config)
    _save(config, args)
    print(message)
    return 0


def cmd_edit_model(args) -> int:
    """Safe edit of one existing model in a catalog (default: the pending catalog).

    Only the fields you pass are touched; unknown fields, other models and unknown
    top-level keys are preserved. Backed up and written atomically.
    """
    from .core import takeover
    from .core.backup import atomic_write, backup_file
    from .core.custom_models import ModelEdit, update_model

    config = _config(args)
    target = takeover.pending_write_path(config, args.catalog)
    settings = None
    if args.efforts or args.default:
        if not (args.efforts and args.default):
            raise CodexModelError("--efforts 与 --default 必须同时给出。")
        supported = [e.strip() for e in args.efforts.split(",") if e.strip()]
        settings = ReasoningSettings(supported_efforts=supported, default_effort=args.default)
    edit = ModelEdit(
        context_window=args.context_window,
        max_context_window=args.max_context_window,
        display_name=args.name,
        description=args.description,
        reasoning=settings,
    )
    if edit.is_empty:
        raise CodexModelError("没有要修改的字段：请至少给出 --context-window/--max-context-window/"
                              "--efforts+--default/--name/--description 之一。")
    source_data = Path(target).read_bytes()
    updated = update_model(edit, args.slug, source_data, name=Path(target).name)
    if updated == source_data:
        print(f"无变化：{args.slug}（未写入、未备份）。")
        return 0
    backup_file(target, config.resolved_paths().backup_directory, prefix="custom-models")
    atomic_write(target, updated)
    print(f"已更新 {args.slug} -> {target}")
    return 0


def cmd_apply(args) -> int:
    """Explicitly wire the merged catalog into a Codex config.toml.

    Semantics:
      - The TARGET config is taken only from --codex-config or the app config's
        codexConfigPath. In demo mode it must additionally lie inside the app
        sandbox. It is never re-derived from the environment, so a demo config can
        never point at the real ~/.codex.
      - --diff / --dry-run are STRICTLY READ-ONLY: they print only the
        model_catalog_json current vs proposed value (never a full-file diff that
        could expose unrelated config / secrets), do NOT backup/create dirs/write.
      - A real write is gated by apply_gate(): the runtime must exist/be runnable,
        the merged catalog must be structurally valid, and either the target is an
        in-sandbox demo write OR real Codex compatibility has verified evidence.
        Real mode without such evidence stays read-only ("未验证").
    """
    from .core.config_editor import set_model_catalog
    from .core.safe_preview import (
        apply_gate,
        parse_error_note,
        preview,
        render_preview,
        same_as_current,
    )

    config = _config(args)
    # Local-only: --diff/--dry-run must never require a discoverable/startable Codex
    # runtime. The real write gate (apply_gate) performs runtime validation itself.
    paths = config.resolved_paths()
    gate = apply_gate(config, paths.merged_catalog, args.codex_config or "", readonly=bool(args.diff or args.dry_run))

    if gate.reason:
        # blocked real write (no runtime / invalid catalog / demo out-of-sandbox /
        # real mode without verified compatibility): never reach the writer.
        print(render_preview(preview(gate.target, paths.merged_catalog)))
        print(f"！（只读）未写入：{gate.reason}")
        return 1

    info = preview(gate.target, paths.merged_catalog)
    print(render_preview(info))
    if not info["parse_ok"]:
        raise CodexModelError(parse_error_note(gate.target))

    if args.diff or args.dry_run:
        # Strictly read-only: nothing backed up, no dirs created, nothing written.
        if same_as_current(info):
            print("（model_catalog_json 已等于拟写入值，无变化）")
        print("--read-only 预览：未写入、未备份、未创建任何文件。")
        return 0

    if not gate.writable:
        print(f"！（只读）未写入：{gate.reason or '未验证 Codex 兼容性'}")
        return 0

    if same_as_current(info):
        print("model_catalog_json 已等于拟写入值，未做变更。")
        return 0

    # Convert the merged catalog path to the form the runtime reads.
    # For WSL targets this is a /mnt/c/... Linux path; for native it's unchanged.
    try:
        runtime_target = config.runtime_target()
        catalog_value = runtime_target.to_runtime_path(paths.merged_catalog)
    except CodexModelError:
        catalog_value = paths.merged_catalog

    prev_value = set_model_catalog(catalog_value, gate.target, paths.backup_directory)
    print("已备份原 config 并写入 model_catalog_json。")
    print(f"  写入值：{catalog_value}")
    if prev_value is not None:
        print(f"  原值：{prev_value}")
    # Record the apply for undo.
    config.last_apply = {
        "targetConfig": gate.target,
        "previousValue": prev_value,
        "appliedValue": catalog_value,
    }
    config_url = args.config or AppConfiguration.default_url()
    config.save(config_url)
    return 0


def cmd_probe(args) -> int:
    """Probe the local Windows runtime and report evidence, read-only.

    We do NOT claim `model_catalog_json` config support on the strength of a
    --version or a bundled listing: a bundled catalog is data, not a config
    schema, and only a successful real config read can evidence that feature.
    Output distinguishes what was actually found / verified.

    With --verify, run the full compatibility behaviour proof against the
    configured runtime and store the desensitised evidence (enables real apply).
    """
    if getattr(args, "verify", False):
        return _cmd_probe_verify(args)

    from .core.runtime import discover, probe_bundled

    runtimes = discover(user_path=args.codex, include_wsl=args.wsl)
    if not runtimes:
        print("在已检查的位置未找到 Codex 运行时。")
        print("  已检查：显式 --codex 路径、PATH 中的 codex/codex.exe；" +
              ("& ~/.codex/bin/wsl/...（--wsl）" if args.wsl else "（未扫描 WSL，可加 --wsl）"))
        if os.name == "nt" and not args.wsl:
            print("提示：本机仅有 WSL 下的 Linux 二进制时，请加 --wsl 以便单独列出（仍不算原生 Windows 验证）。")
        return 1
    for r in runtimes:
        print(f"kind={r.kind}\tpath={r.path}")
        print(f"  version:      {r.version if r.version else '未验证'}")
        ok, detail = probe_bundled(r)
        print(f"  bundled 接口: {'已验证（exit 0）' if ok else '未验证 / ' + detail}")
    # model_catalog_json config feature: honest default = not verified here.
    print("model_catalog_json 配置读取能力：未验证（需真实运行时成功读取 config 后另行证明；"
          "bundled 输出不是配置 schema）。")
    print("提示：运行 `probe --verify` 执行完整兼容性行为证明并存储证据，成功后可 apply。")
    return 0


def _cmd_probe_verify(args) -> int:
    """Run the real compatibility behaviour proof and store desensitised evidence.

    The proof creates an isolated temp CODEX_HOME with a unique random slug in
    model_catalog_json, runs `debug models` (non-bundled), and asserts the slug
    is returned. A negative control (missing catalog) must exit non-zero. No
    writes touch the user's real config.toml / auth.json.
    """
    from .core.compat_probe import probe_compatibility, evidence_to_dict
    from .core.errors import CodexModelError

    config = _config(args)
    try:
        target = config.runtime_target()
    except CodexModelError as exc:
        print(f"无法构建运行时目标：{exc}", file=sys.stderr)
        return 1

    print(f"运行兼容性行为证明：kind={target.kind} path={target.executable}")
    if target.is_wsl:
        print(f"  distro={target.distro}")
    evidence = probe_compatibility(target)
    print(f"  结果：{'通过' if evidence.ok else '失败'} — {evidence.reason}")
    print(f"  runtime_version={evidence.runtime_version}")
    print(f"  runtime_sha256={evidence.runtime_sha256[:16]}…（已脱敏）")
    print(f"  bundled_model_count={evidence.bundled_model_count}")
    print(f"  marker_loaded={evidence.marker_loaded}")
    print(f"  marker_absent_from_bundled={evidence.marker_absent_from_bundled}")
    print(f"  missing_catalog_exit_code={evidence.missing_catalog_exit_code}")
    print(f"  probe_scheme={evidence.probe_scheme} probe_slug={evidence.probe_slug}")

    if not evidence.ok:
        print("！（只读）兼容性证明未通过，未存储证据；apply 仍将拒绝真实写入。", file=sys.stderr)
        return 1

    config.compat_evidence = evidence_to_dict(evidence)
    config_url = args.config or AppConfiguration.default_url()
    config.save(config_url)
    print(f"已存储脱敏证据到应用配置：{config_url}")
    print("现在可以运行 `apply` 将 model_catalog_json 写入 Codex config.toml。")
    return 0


def cmd_undo(args) -> int:
    """Undo the last apply of model_catalog_json, with conflict detection.

    Reads the last_apply record (target config path, previous value, applied
    value) from the app configuration. If the current value no longer matches
    the applied value, the undo is refused (the user has changed things since).
    On success the last_apply record is cleared.
    """
    from .core.config_editor import undo_model_catalog

    config = _config(args)
    record = config.last_apply
    if not record:
        print("没有已记录的 apply 操作可撤销。", file=sys.stderr)
        return 1

    target_config = record.get("targetConfig")
    prev_value = record.get("previousValue")
    applied_value = record.get("appliedValue")
    if not target_config:
        print("apply 记录缺少 targetConfig，无法撤销。", file=sys.stderr)
        return 1

    paths = config.resolved_paths()
    message = undo_model_catalog(
        target_config, paths.backup_directory, prev_value, applied_value,
    )
    print(message)
    config.last_apply = None
    config_url = args.config or AppConfiguration.default_url()
    config.save(config_url)
    print("已清除 apply 记录。")
    return 0


def _bridge_target(config: AppConfiguration, explicit: str | None) -> str:
    target = explicit or config.codex_config_path
    if not target:
        raise CodexModelError("启用本地桥需要 --codex-config 或应用配置中的 codexConfigPath。")
    return target


def _provider_from_config(path: str, provider: str | None) -> tuple[str, str]:
    from .core.config_editor import read_provider_base_url

    try:
        return read_provider_base_url(path, provider)
    except ValueError as exc:
        raise CodexModelError(str(exc)) from exc


def cmd_bridge_enable(args) -> int:
    from .core.bridge import ensure_loopback
    from .core.config_editor import set_provider_base_url
    from .core.safe_preview import demo_write_block_reason

    config = _config(args)
    target = _bridge_target(config, args.codex_config)
    # Same sandbox guarantee as `apply`: demo mode must not reach a real config.
    block = demo_write_block_reason(config, config.merged_catalog_path, target)
    if block:
        raise CodexModelError(block)
    provider, current_url = _provider_from_config(target, args.provider)
    upstream = args.upstream or current_url
    try:
        host = ensure_loopback(args.host or "127.0.0.1")
    except ValueError as exc:
        raise CodexModelError(str(exc)) from exc
    port = int(args.port or 8787)
    local_url = f"http://{host}:{port}"
    existing = config.last_bridge_apply or {}
    if existing:
        same_target = (
            existing.get("targetConfig") == target
            and existing.get("provider") == provider
            and existing.get("appliedUrl") == local_url
        )
        if same_target and current_url == local_url:
            # Idempotent enable must preserve the original URL recorded for
            # disable; treating the loopback URL as the new previous value would
            # make a second enable impossible to undo correctly.
            config.bridge_enabled = True
            config.bridge_host = host
            config.bridge_port = port
            config.bridge_upstream_url = existing.get("previousUrl") or upstream
            config.bridge_provider = provider
            config.save(args.config or AppConfiguration.default_url())
            print("本地桥已经启用，保留原始回退地址；未重复写入。")
            return 0
        if current_url == existing.get("appliedUrl") and not same_target:
            raise CodexModelError("已有另一项本地桥配置处于启用状态，请先运行 bridge disable。")
    paths = config.resolved_paths()
    previous = set_provider_base_url(target, provider, local_url, paths.backup_directory)
    config.bridge_enabled = True
    config.bridge_host = host
    config.bridge_port = port
    config.bridge_upstream_url = upstream
    config.bridge_provider = provider
    config.last_bridge_apply = {
        "targetConfig": target, "provider": provider,
        "previousUrl": previous, "appliedUrl": local_url,
    }
    config.save(args.config or AppConfiguration.default_url())
    print(f"已启用本地桥：model_providers.{provider}.base_url -> {local_url}")
    print(f"上游地址已保存到应用配置（未保存 API Key）：{upstream}")
    print("另开终端运行：python -m codex_model_manager bridge start")
    return 0


def cmd_bridge_disable(args) -> int:
    from .core.config_editor import undo_provider_base_url

    config = _config(args)
    record = config.last_bridge_apply
    if not record:
        print("没有已记录的本地桥配置可撤销。", file=sys.stderr)
        return 1
    message = undo_provider_base_url(
        record["targetConfig"], record["provider"], record.get("previousUrl"),
        record["appliedUrl"], config.resolved_paths().backup_directory,
    )
    if message.startswith("冲突："):
        print(message, file=sys.stderr)
        return 1
    config.bridge_enabled = False
    config.last_bridge_apply = None
    config.save(args.config or AppConfiguration.default_url())
    print(message)
    print("本地桥已关闭；如有 bridge start 进程，请在其终端按 Ctrl+C 停止。")
    return 0


def cmd_bridge_start(args) -> int:
    from .core.bridge import BridgeServer, ensure_loopback, run_bridge
    from .core.tool_recovery import policy

    config = _config(args)
    upstream = args.upstream or config.bridge_upstream_url
    if not upstream:
        raise CodexModelError("没有上游地址，请先运行 bridge enable 或提供 --upstream。")
    # Validate *before* announcing anything: printing "运行中" and then failing
    # would be misleading, and a rejected host must never look like it started.
    try:
        host = ensure_loopback(args.host or config.bridge_host or "127.0.0.1")
    except ValueError as exc:
        raise CodexModelError(str(exc)) from exc
    port = int(args.port or config.bridge_port or 8787)
    scoped = frozenset(args.only_model) if getattr(args, "only_model", None) else None
    if not upstream.startswith(("http://", "https://")):
        raise CodexModelError("bridge upstream_url 必须是 http:// 或 https:// 地址")
    try:
        BridgeServer(upstream_url=upstream, host=host, port=port, scoped_models=scoped,
                     protocol_translation=args.protocol == "function", context_recovery=args.context_recovery,
                     experiment_mode_file=args.experiment_mode_file, record_path=args.record)
        mode, _, _ = policy(args.experiment_mode_file, args.protocol == "function", args.context_recovery)
    except (ValueError, OSError) as exc:
        raise CodexModelError(str(exc) if isinstance(exc, ValueError) else "试验模式文件不可读取") from exc
    print(f"本地桥运行中：http://{host}:{port} -> {upstream}")
    print(f"补丁模式：{mode}（off=关闭，protocol=协议，context=上下文，both=两者）")
    print(
        "翻译范围：%s"
        % (", ".join(sorted(scoped)) if scoped else "全部（未启用 --only-model）")
    )
    if scoped:
        print(
            "注意：provider 的 base_url 是全局的，桥仍挡在所有模型前面；"
            "范围外的模型请求/响应原样直通，但桥停止时它们也会断。"
        )
    print("按 Ctrl+C 停止；API Key 仅透传 Codex 请求头，不写入本地桥。")
    try:
        run_bridge(upstream, host, port, scoped_models=scoped,
                   protocol_translation=args.protocol == "function", context_recovery=args.context_recovery,
                   experiment_mode_file=args.experiment_mode_file, record_path=args.record)
    except ValueError as exc:  # upstream URL validation
        raise CodexModelError(str(exc)) from exc
    return 0


def cmd_bridge_status(args) -> int:
    config = _config(args)
    print(f"本地桥开关：{'已启用' if config.bridge_enabled else '关闭（默认）'}")
    print(f"监听地址：http://{config.bridge_host}:{config.bridge_port}")
    print(f"上游地址：{config.bridge_upstream_url or '（未设置）'}")
    print(f"provider：{config.bridge_provider or '（未设置）'}")
    record = config.last_bridge_apply or {}
    print(f"应用记录：{'有（可 disable 回退）' if record else '无'}")
    target = record.get("targetConfig") or config.codex_config_path
    if target and Path(target).is_file() and config.bridge_provider:
        try:
            name, current = _provider_from_config(target, config.bridge_provider)
            print(f"model_providers.{name}.base_url = {current}")
        except CodexModelError as exc:
            print(f"（无法读取当前 provider：{exc}）")
    else:
        print("目标 config.toml：未设置或不存在（不会写入真实配置）")
    return 0


def cmd_doctor(args) -> int:
    """Print read-only local evidence; never attempts recovery."""
    from .core.doctor import collect_doctor, format_doctor

    config = None
    config_url = args.config or AppConfiguration.default_url()
    if os.path.isfile(config_url):
        try:
            config = AppConfiguration.load(config_url)
        except Exception as exc:  # keep diagnostics useful even with bad app config
            print(f"应用配置无法读取（{type(exc).__name__}）；继续执行只读环境检查。", file=sys.stderr)
    report = collect_doctor(config, codex_config=args.codex_config,
                            provider=args.provider, observe_log=args.observe_log)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_doctor(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex-model-manager", description="Windows Codex 模型管理器")
    p.add_argument("--config", help="显式 app 配置文件路径（默认在 LOCALAPPDATA\\CodexModelManager\\config.json）")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="生成默认配置")
    s.add_argument("--codex", help="显式 Codex 运行时路径")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("sync", help="同步合并官方与自定义模型（只读，不写真实配置）")
    s.add_argument("--append-log", action="store_true", help="将结果追加到 sync.jsonl")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("list", help="列出模型")
    s.add_argument("--search", help="按 slug/名称/描述过滤")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("add", help="从模板新增自定义模型")
    s.add_argument("slug")
    s.add_argument("--name")
    s.add_argument("--description", default="")
    s.add_argument("--template", required=True)
    s.add_argument("--context-window", type=int, required=True)
    s.add_argument("--image", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("reasoning", help="编辑推理档位")
    s.add_argument("slug")
    s.add_argument("--efforts", required=True, help="支持档位，逗号分隔，如 low,high,max")
    s.add_argument("--default", required=True)
    s.set_defaults(func=cmd_edit_reasoning)

    s = sub.add_parser("backup", help="备份自定义源与合并目录")
    s.add_argument("--backup-dir")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="从备份恢复（按内容类型校验目标）")
    s.add_argument("backup_path")
    s.add_argument("--target", choices=["merged", "custom", "config"], default="merged")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("apply", help="将 model_catalog_json 显式写入 Codex config.toml")
    s.add_argument("--codex-config", help="目标 config.toml 路径（否则用应用配置 codexConfigPath；不会从环境推导）")
    s.add_argument("--diff", action="store_true", help="只读预览 model_catalog_json 差异（不写入、不备份）")
    s.add_argument("--dry-run", action="store_true", help="只读预览，不写入、不备份")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser(
        "takeover-import",
        help="接管：把 Codex 当前引用的模型目录复制到管理器待应用目录（保留未知字段）")
    s.add_argument("--active", help="生效配置引用目录的模型 JSON；默认读取 codexConfigPath 的 model_catalog_json")
    s.add_argument("--create-empty", action="store_true",
                   help="活跃目录不存在时，改为创建一个空白的待应用目录")
    s.set_defaults(func=cmd_takeover_import)

    s = sub.add_parser("takeover-diff", help="接管：只读显示生效目录与待应用目录的差异")
    s.add_argument("--active", help="生效配置引用目录的模型 JSON")
    s.set_defaults(func=cmd_takeover_diff)

    s = sub.add_parser("takeover-apply", help="接管：将待应用目录显式写回生效目录（带确认与备份）")
    s.add_argument("--active", help="生效配置引用目录的模型 JSON")
    s.add_argument("--confirm-removals", action="store_true",
                   help="显式确认：允许本次写回删除活跃目录中已有的模型")
    s.add_argument("--allow-create", action="store_true", help="允许在活跃目录不存在时新建文件")
    s.set_defaults(func=cmd_takeover_apply)

    s = sub.add_parser("takeover-undo", help="接管：撤销最近一次写回（带冲突检测）")
    s.set_defaults(func=cmd_takeover_undo)

    s = sub.add_parser("edit-model", help="安全编辑已有模型（默认编辑待应用目录）")
    s.add_argument("slug")
    s.add_argument("--catalog", help="待应用目录 JSON；如提供，必须与配置中的 takeoverCatalogPath 相同")
    s.add_argument("--context-window", type=int)
    s.add_argument("--max-context-window", type=int)
    s.add_argument("--efforts", help="支持的推理档位，逗号分隔，如 low,high,max")
    s.add_argument("--default", help="默认推理档位（必须在 --efforts 中）")
    s.add_argument("--name")
    s.add_argument("--description")
    s.set_defaults(func=cmd_edit_model)

    s = sub.add_parser("probe", help="探测本地运行时")
    s.add_argument("--codex")
    s.add_argument("--wsl", action="store_true", help="同时列出 WSL 下的 Linux 二进制（不影响原生判定）")
    s.add_argument("--verify", action="store_true", help="运行完整兼容性行为证明并存储证据（启用真实 apply）")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("undo", help="撤销最近一次 apply（带冲突检测）")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("doctor", help="只读诊断 Codex 工具链（不重启、不改配置、不读取凭据）")
    s.add_argument("--codex-config", help="目标 config.toml 路径；只读取 provider/base_url")
    s.add_argument("--provider", help="provider 名称（默认使用 config.toml 的 model_provider）")
    s.add_argument("--observe-log", help="观察代理白名单 JSONL；只汇总 request/response 工具元数据")
    s.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("bridge", help="可选的本地 Responses/custom-tool 兼容桥")
    bridge_sub = s.add_subparsers(dest="bridge_command", required=True)
    e = bridge_sub.add_parser("enable", help="将指定 provider 临时指向本地桥")
    e.add_argument("--codex-config", help="目标 config.toml（否则用应用配置 codexConfigPath）")
    e.add_argument("--provider", help="provider 名称（默认读取顶层 model_provider）")
    e.add_argument("--upstream", help="上游 Responses base_url（默认读取原 provider base_url）")
    e.add_argument("--host", default="127.0.0.1")
    e.add_argument("--port", type=int, default=8787)
    e.set_defaults(func=cmd_bridge_enable)
    d = bridge_sub.add_parser("disable", help="恢复 bridge enable 前的 provider 地址")
    d.set_defaults(func=cmd_bridge_disable)
    r = bridge_sub.add_parser("start", help="启动本地桥（前台运行）")
    r.add_argument("--upstream")
    r.add_argument("--host")
    r.add_argument("--port", type=int)
    r.add_argument("--protocol", choices=("function", "native"), default="function",
                   help="function=协议兼容，native=保留原工具协议")
    r.add_argument("--context-recovery", action="store_true", help="只纠偏发给模型的历史副本，须配合 --only-model")
    r.add_argument("--experiment-mode-file", help="按请求读取四组试验模式文件")
    r.add_argument("--record", help="只记录工具/试验元数据的 JSONL")
    r.add_argument(
        "--only-model",
        action="append",
        metavar="MODEL",
        help="只翻译这些模型（可重复）；其余模型原样直通。不给则翻译一切。",
    )
    r.set_defaults(func=cmd_bridge_start)
    st = bridge_sub.add_parser("status", help="显示本地桥开关/监听/上游与当前 provider 地址")
    st.set_defaults(func=cmd_bridge_status)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CodexModelError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # guard: never crash with a traceback for the user
        print(f"未预期错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
