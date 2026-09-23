"""App configuration for the Windows port.

Faithful to Sources/CodexModelCore/AppConfiguration.swift, adapted to Windows
user directories. The Codex directory honours CODEX_HOME (else ~/.codex).
The app's own settings live under the per-user AppData (LOCALAPPDATA on
Windows), and never inside the Codex config dir.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Dict, Optional

from .backup import atomic_write
from .errors import ConfigurationNotFound, InvalidConfiguration
from .overrides import ModelFieldOverrides

APP_FOLDER = "CodexModelManager"
LABEL = "com.onepersonlab.codex-model-manager.sync"


def _default_takeover_catalog_path(merged_catalog_path: str = "") -> str:
    """Place the pending takeover copy beside the manager-owned merged catalog.

    Older configuration files predate ``takeoverCatalogPath``.  Deriving the new
    path keeps those installations usable without rewriting their config merely
    because the GUI was opened.  ``PureWindowsPath`` preserves a Windows path when
    the config is inspected from WSL or another POSIX process.
    """
    raw = (merged_catalog_path or "").strip()
    if raw:
        win = PureWindowsPath(raw)
        if win.drive or "\\" in raw:
            return str(win.with_name("pending-models.json"))
        return str(Path(raw).expanduser().with_name("pending-models.json"))
    return str(_appdata_dir() / APP_FOLDER / "pending-models.json")


def _appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base)
    return Path(os.path.expanduser("~")) / "AppData" / "Local"


def connection_prefs_path(config_url: str) -> str:
    """The connection page's preferences file, beside the app config.

    The connection page has always stored its selected ``config.toml`` here. The
    model page must use the same source of truth, otherwise a path that is correct
    on the connection page leaves ``codexConfigPath`` empty and every catalog-based
    feature (including takeover state) reports "unavailable".
    """
    return str(Path(config_url).with_name("connection.json"))


def load_connection_prefs(config_url: str) -> dict:
    """Read the connection page prefs; a missing/corrupt file is an empty dict."""
    path = Path(connection_prefs_path(config_url))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_connection_prefs(config_url: str, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write(connection_prefs_path(config_url), payload)


def sync_connection_config(config: "AppConfiguration", config_url: str, *,
                           save: bool = True) -> bool:
    """Keep the connection page's selected config and the app config in lock-step.

    Adopts the connection page's ``config`` into ``codexConfigPath`` (and the
    backend/distro selection) when it differs, so the model page and every
    catalog-based feature use exactly what the connection page shows. If the
    connection file does not exist yet but the app config already names a config,
    the selection is written back so the reverse direction cannot drift either.

    Returns True when the app config changed (and was saved, when ``save``).
    """
    prefs = load_connection_prefs(config_url)
    conn = str(prefs.get("config") or "").strip()
    changed = False
    if conn:
        if (config.codex_config_path or "").strip() != conn:
            config.codex_config_path = conn
            changed = True
        backend = prefs.get("backend")
        if backend in ("auto", "native", "wsl") and (config.codex_backend or "auto") != backend:
            config.codex_backend = backend
            changed = True
        distro = str(prefs.get("distro") or "").strip()
        if distro and (config.codex_distro or "") != distro:
            config.codex_distro = distro
            changed = True
    elif (config.codex_config_path or "").strip():
        data = dict(prefs)
        data["config"] = config.codex_config_path
        data.setdefault("backend", config.codex_backend or "auto")
        if config.codex_distro:
            data.setdefault("distro", config.codex_distro)
        save_connection_prefs(config_url, data)
    if changed and save:
        config.save(config_url)
    return changed


@dataclass
class AppConfiguration:
    codex_runtime_path: Optional[str] = None
    custom_source_path: str = ""
    merged_catalog_path: str = ""
    sync_log_path: str = ""
    error_log_path: str = ""
    backup_directory_path: str = ""
    # Explicit target for `apply`: the absolute path of the Codex config.toml the
    # merged catalog should be wired into. NEVER re-derived from the environment at
    # apply time, so a demo config can never point at the real ~/.codex.
    codex_config_path: Optional[str] = None
    # Backend selection: 'auto' | 'native' | 'wsl'. 'auto' prefers native and
    # falls back to WSL. The distro is explicit for WSL (never rely on the default
    # distro happening to be correct).
    codex_backend: str = "auto"
    codex_distro: Optional[str] = None
    # The CODEX_HOME the selected runtime should read (Windows-accessible form).
    # Shown as a candidate after the real CODEX_HOME is verified; never
    # auto-written. For demo configs it stays inside the sandbox.
    target_codex_home: Optional[str] = None
    # Demo vs real mode. Demo configs (setup_demo / offline_demo) set this true:
    # simulated `apply` is then allowed ONLY inside the app sandbox and the mock
    # runtime. Real mode requires verified Codex compatibility evidence before any
    # write; without it, apply stays read-only ("未验证").
    is_demo: bool = False
    # Stored compatibility evidence from the last successful "验证兼容性" probe.
    # Bound to runtime path/SHA256/version/distro/probe-scheme; a runtime change
    # or update invalidates it. Only honoured in real (non-demo) mode. Never
    # auto-generated; the user runs the probe explicitly.
    compat_evidence: Optional[Dict] = None
    # Record of the last successful apply, for undo. Holds the target config
    # path, the previous and applied `model_catalog_json` values, and a timestamp.
    # Undo restores the previous value (or deletes the key) with conflict
    # detection against the current value.
    last_apply: Optional[Dict] = None
    # Takeover of an existing catalog: the manager's own *pending* (待应用) copy of
    # the catalog the selected config.toml references, plus the import record used
    # for conflict detection and for showing both directories side by side.
    takeover_catalog_path: Optional[str] = None
    takeover_import: Optional[Dict] = None
    # Undo record of the last takeover write-back (active path, backup, hashes).
    last_takeover: Optional[Dict] = None
    # Undo record of the last "first apply" (publish the edit copy as a catalog the
    # selected config.toml then references). Holds both file paths, both backups and
    # the hashes needed to refuse an undo after an external change.
    last_first_apply: Optional[Dict] = None
    # Optional local Responses compatibility bridge. Disabled by default; the
    # bridge forwards Codex's bearer header and never stores credentials.
    bridge_enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8787
    bridge_upstream_url: Optional[str] = None
    bridge_provider: Optional[str] = None
    last_bridge_apply: Optional[Dict] = None
    launch_agent_label: str = LABEL
    visibility_overrides: Dict[str, str] = field(default_factory=dict)
    model_overrides: Dict[str, ModelFieldOverrides] = field(default_factory=dict)

    @classmethod
    def default_url(cls) -> str:
        return str(_appdata_dir() / APP_FOLDER / "config.json")

    @classmethod
    def recommended(cls) -> "AppConfiguration":
        """Build the recommended layout without installing anything.

        All model sources, the merged catalog, logs and backups live inside the
        app's own per-user sandbox (%LOCALAPPDATA%\\CodexModelManager). We do NOT
        write custom-models.json or models.json into the real CODEX_HOME, so a
        default preview/sync never mutates anything Codex actually reads. The
        real Codex config.toml is only ever touched by an explicit `apply`, and
        gated by codex_config_path below.
        """
        app_dir = _appdata_dir() / APP_FOLDER
        log_dir = app_dir / "Logs"
        return cls(
            codex_runtime_path=None,
            custom_source_path=str(app_dir / "custom-models.json"),
            merged_catalog_path=str(app_dir / "models.json"),
            sync_log_path=str(log_dir / "sync.jsonl"),
            error_log_path=str(log_dir / "sync.error.log"),
            backup_directory_path=str(app_dir / "Backups"),
            codex_config_path=None,  # only set explicitly; never derived from env
            is_demo=False,  # default preview/real; demo configs set this true
            takeover_catalog_path=str(app_dir / "pending-models.json"),
        )

    def to_json(self) -> dict:
        return {
            "codexRuntimePath": self.codex_runtime_path,
            "customSourcePath": self.custom_source_path,
            "mergedCatalogPath": self.merged_catalog_path,
            "syncLogPath": self.sync_log_path,
            "errorLogPath": self.error_log_path,
            "backupDirectoryPath": self.backup_directory_path,
            "codexConfigPath": self.codex_config_path,
            "codexBackend": self.codex_backend,
            "codexDistro": self.codex_distro,
            "targetCodexHome": self.target_codex_home,
            "isDemo": self.is_demo,
            "compatEvidence": self.compat_evidence or None,
            "lastApply": self.last_apply or None,
            "takeoverCatalogPath": self.takeover_catalog_path,
            "takeoverImport": self.takeover_import or None,
            "lastTakeover": self.last_takeover or None,
            "lastFirstApply": self.last_first_apply or None,
            "bridgeEnabled": self.bridge_enabled,
            "bridgeHost": self.bridge_host,
            "bridgePort": self.bridge_port,
            "bridgeUpstreamUrl": self.bridge_upstream_url,
            "bridgeProvider": self.bridge_provider,
            "lastBridgeApply": self.last_bridge_apply or None,
            "launchAgentLabel": self.launch_agent_label,
            "visibilityOverrides": self.visibility_overrides or None,
            "modelOverrides": {
                k: v.to_json() for k, v in (self.model_overrides or {}).items()
            }
            or None,
        }

    @classmethod
    def from_json(cls, data: dict) -> "AppConfiguration":
        merged_catalog_path = data.get("mergedCatalogPath", "")
        takeover_catalog_path = data.get("takeoverCatalogPath")
        if not isinstance(takeover_catalog_path, str) or not takeover_catalog_path.strip():
            takeover_catalog_path = _default_takeover_catalog_path(merged_catalog_path)
        return cls(
            codex_runtime_path=data.get("codexRuntimePath"),
            custom_source_path=data.get("customSourcePath", ""),
            merged_catalog_path=merged_catalog_path,
            sync_log_path=data.get("syncLogPath", ""),
            error_log_path=data.get("errorLogPath", ""),
            backup_directory_path=data.get("backupDirectoryPath", ""),
            codex_config_path=data.get("codexConfigPath"),
            codex_backend=data.get("codexBackend", "auto") or "auto",
            codex_distro=data.get("codexDistro"),
            target_codex_home=data.get("targetCodexHome"),
            is_demo=bool(data.get("isDemo", False)),
            compat_evidence=data.get("compatEvidence"),
            last_apply=data.get("lastApply"),
            takeover_catalog_path=takeover_catalog_path,
            takeover_import=data.get("takeoverImport"),
            last_takeover=data.get("lastTakeover"),
            last_first_apply=data.get("lastFirstApply"),
            bridge_enabled=bool(data.get("bridgeEnabled", False)),
            bridge_host=data.get("bridgeHost", "127.0.0.1") or "127.0.0.1",
            bridge_port=int(data.get("bridgePort", 8787) or 8787),
            bridge_upstream_url=data.get("bridgeUpstreamUrl"),
            bridge_provider=data.get("bridgeProvider"),
            last_bridge_apply=data.get("lastBridgeApply"),
            launch_agent_label=data.get("launchAgentLabel", LABEL),
            visibility_overrides=data.get("visibilityOverrides") or {},
            model_overrides={
                k: ModelFieldOverrides.from_json(v)
                for k, v in (data.get("modelOverrides") or {}).items()
            },
        )

    def save(self, url: Optional[str] = None) -> None:
        url = url or self.default_url()
        data = json.dumps(self.to_json(), indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        atomic_write(url, data)

    @classmethod
    def load(cls, url: Optional[str] = None) -> "AppConfiguration":
        url = url or cls.default_url()
        if not os.path.exists(url):
            raise ConfigurationNotFound(url)
        raw = Path(url).read_bytes()
        for enc in ("utf-8-sig", "utf-8"):
            try:
                return cls.from_json(json.loads(raw.decode(enc)))
            except UnicodeDecodeError:
                continue
        try:
            return cls.from_json(json.loads(raw.decode("utf-8", errors="replace")))
        except (ValueError, TypeError) as exc:
            raise InvalidConfiguration(f"配置无法解析：{exc}") from exc

    def resolved_paths(self) -> "CatalogPaths":
        """Resolve filesystem paths WITHOUT discovering or requiring a runnable Codex.

        Used by local-only operations (apply --diff/--dry-run, preview, backup,
        restore) that must not fail merely because no native Codex was found. The
        `codex_runtime` field is filled from the explicit config path when present
        (no existence check here); the real write gate checks runtime separately.
        """
        from .catalog_sync import CatalogPaths

        if not self.custom_source_path or not self.merged_catalog_path:
            raise InvalidConfiguration("自定义模型源与合并目录不能为空")

        label = self.launch_agent_label.strip()
        if not label:
            raise InvalidConfiguration("launchAgentLabel 不能为空")

        runtime = ""
        if self.codex_runtime_path and self.codex_runtime_path.strip():
            runtime = _resolve_path(self.codex_runtime_path, "codexRuntimePath")

        return CatalogPaths(
            codex_runtime=runtime,
            custom_source=_resolve_path(self.custom_source_path, "customSourcePath"),
            merged_catalog=_resolve_path(self.merged_catalog_path, "mergedCatalogPath"),
            sync_log=_resolve_path(self.sync_log_path, "syncLogPath"),
            error_log=_resolve_path(self.error_log_path, "errorLogPath"),
            backup_directory=_resolve_path(self.backup_directory_path, "backupDirectoryPath"),
            visibility_overrides=self.visibility_overrides or {},
            model_overrides=self.model_overrides or {},
        )

    def resolved(self) -> "CatalogPaths":
        """Resolve paths AND ensure a usable Codex runtime is known.

        The runtime may be a native Windows executable OR a WSL-backed Linux
        binary (honouring codexBackend/codexDistro). Raises InvalidConfiguration
        for a missing explicit runtime path, or RuntimeNotFound when nothing is
        discovered and none is set.
        """
        from .errors import RuntimeNotFound
        from .runtime import classify, find_native, codex_home

        paths = self.resolved_paths()
        paths.codex_home_explicit = bool(self.target_codex_home)

        if self.codex_runtime_path and self.codex_runtime_path.strip():
            runtime = _resolve_path(self.codex_runtime_path, "codexRuntimePath")
            if not os.path.exists(runtime):
                raise InvalidConfiguration(
                    f"codexRuntimePath 指向的文件不存在：{runtime}"
                )
            paths.codex_runtime = runtime
            kind = classify(runtime)
            paths.runtime_kind = kind if kind != "unknown" else None
            paths.runtime_distro = self.codex_distro if kind == "wsl" else None
            paths.codex_home_win = self.target_codex_home or self._default_codex_home_win()
            return paths

        backend = (self.codex_backend or "auto").strip().lower()
        if backend == "native":
            discovered = find_native()
        elif backend == "wsl":
            runtimes = [r for r in _discover_wsl() if r.distro == self.codex_distro
                        or not self.codex_distro]
            discovered = runtimes[0] if runtimes else None
        else:
            discovered = find_native()
            if discovered is None:
                # 'auto': fall back to a WSL-backed runtime rather than failing.
                discovered = None
                for r in _discover_wsl():
                    if not self.codex_distro or r.distro == self.codex_distro:
                        discovered = r
                        break

        if discovered is None:
            raise RuntimeNotFound(
                "没有找到可用的 Codex 运行时。请设置 codexRuntimePath，或在设置中选择 WSL 后端与发行版。"
            )
        paths.codex_runtime = discovered.path
        paths.runtime_kind = discovered.kind
        paths.runtime_distro = discovered.distro
        paths.codex_home_win = self.target_codex_home or self._default_codex_home_win()
        return paths

    def runtime_target(self):
        """Return a unified RuntimeTarget for the configured backend.

        Native -> windows-native target; WSL -> wsl target carrying the explicit
        distro and the Linux-side CODEX_HOME the runtime must read. Raises
        RuntimeNotFound when nothing usable is configured/discoverable.
        """
        from . import wsl_adapter
        from .errors import RuntimeNotFound
        from .runtime import classify, find_native

        codex_home_win = self.target_codex_home or self._default_codex_home_win()
        backend = (self.codex_backend or "auto").strip().lower()

        if self.codex_runtime_path and self.codex_runtime_path.strip():
            runtime = _resolve_path(self.codex_runtime_path, "codexRuntimePath")
            if not os.path.exists(runtime):
                raise InvalidConfiguration(f"codexRuntimePath 指向的文件不存在：{runtime}")
            kind = classify(runtime)
            if kind == "wsl":
                return wsl_adapter.make_wsl_target(
                    runtime, self.codex_distro or "", codex_home_win)
            return wsl_adapter.make_native_target(runtime, codex_home_win)

        if backend == "native":
            found = find_native()
            if found is None:
                raise RuntimeNotFound("未找到原生 Windows Codex 运行时。")
            return wsl_adapter.make_native_target(found.path, codex_home_win)

        if backend == "wsl":
            return self._configured_wsl_target(codex_home_win)

        found = find_native()
        if found is not None:
            return wsl_adapter.make_native_target(found.path, codex_home_win)
        return self._configured_wsl_target(codex_home_win)

    def _configured_wsl_target(self, codex_home_win: str):
        from . import wsl_adapter
        from .errors import RuntimeNotFound

        for r in _discover_wsl():
            if not self.codex_distro or r.distro == self.codex_distro:
                return wsl_adapter.make_wsl_target(
                    r.path, r.distro or "", codex_home_win)
        raise RuntimeNotFound(
            "未找到 WSL 后端 Codex 运行时。请确认已安装 Codex，或设置 codexRuntimePath。"
        )

    def _default_codex_home_win(self) -> str:
        """The CODEX_HOME candidate for this config (verified CODEX_HOME, else sandbox).

        In demo mode we never point at the real Codex home: the sandbox holding the
        merged catalog is used instead.
        """
        from .runtime import codex_home

        if self.is_demo:
            base = self.merged_catalog_path or self.custom_source_path
            if base:
                return str(Path(base).parent)
        return str(codex_home())


def _discover_wsl():
    """Discover WSL-backed runtimes without raising."""
    try:
        from .runtime import discover
        return discover(include_wsl=True)
    except Exception:  # noqa: BLE001
        return []


def _resolve_path(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise InvalidConfiguration(f"{field_name} 不能为空")
    if value == "~":
        return str(Path.home())
    if value.startswith("~/"):
        return str(Path.home() / value[2:])
    p = Path(value)
    if not p.is_absolute():
        raise InvalidConfiguration(f"{field_name} 必须使用绝对路径或 ~/ 路径")
    return str(p)
