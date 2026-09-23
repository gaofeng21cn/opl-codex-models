"""Catalog sync service.

Faithful port of Sources/CodexModelCore/CatalogSyncService.swift.

Flow:
  1. validate overrides
  2. ensure runtime is executable, ensure initial files
  3. create an isolated temp CODEX_HOME with the selected runtime's credentials
  4. refresh the official catalog, falling back to the bundled catalog
  5. `codex --version`
  6. merge official + custom (custom always appended with higher priority,
     never guessed), applying visibility overrides and field overrides
  7. if nothing changed -> no_change; else backup + atomic write to merged.json
  8. append a JSONL sync record
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .errors import CodexModelError, InvalidCatalog, ProcessFailed
from .overrides import ModelFieldOverrides
from .backup import atomic_write, backup_file

JSON_OPTIONS = {"sort_keys": True, "ensure_ascii": False}

DEFAULT_SUPPORT_FIELDS = {"supports_reasoning_summaries": True, "supports_parallel_tool_calls": True}


@dataclass
class CatalogPaths:
    codex_runtime: str
    custom_source: str
    merged_catalog: str
    sync_log: str
    error_log: str
    backup_directory: str
    visibility_overrides: Dict[str, str] = field(default_factory=dict)
    model_overrides: Dict[str, ModelFieldOverrides] = field(default_factory=dict)
    # Backend metadata so sync shares the SAME execution target as probes/apply.
    # When absent the runtime is classified from its header.
    runtime_kind: Optional[str] = None
    runtime_distro: Optional[str] = None
    codex_home_win: Optional[str] = None


@dataclass
class CatalogSyncResult:
    status: str
    record_data: bytes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, **_canonical_kwargs()).encode("utf-8")


def _canonical_kwargs() -> dict:
    return {"sort_keys": True, "ensure_ascii": False, "separators": (",", ":")}


def _pretty(obj) -> bytes:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_root(data: bytes, name: str) -> dict:
    try:
        root = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCatalog(f"{name} 无法解析：{exc}") from exc
    if not isinstance(root, dict):
        raise InvalidCatalog(f"{name} 顶层必须是 JSON 对象")
    return root


def _validate_slugs(models: List[dict], name: str) -> List[str]:
    slugs = []
    for m in models:
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug:
            raise InvalidCatalog(f"{name}模型须包含非空 slug")
        slugs.append(slug)
    if len(set(slugs)) != len(slugs):
        raise InvalidCatalog(f"{name}包含重复 slug")
    return slugs


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class CatalogSyncService:
    def __init__(self, paths: CatalogPaths):
        self.paths = paths

    def ensure_initial_files(self) -> None:
        for directory in [
            os.path.dirname(self.paths.custom_source),
            os.path.dirname(self.paths.merged_catalog),
            os.path.dirname(self.paths.sync_log),
            os.path.dirname(self.paths.error_log),
            self.paths.backup_directory,
        ]:
            os.makedirs(directory, exist_ok=True)

        if not os.path.exists(self.paths.custom_source):
            initial = {"schema": "codex_model_manager_custom_models.v1", "models": []}
            atomic_write(self.paths.custom_source, _pretty(initial))
        for log in (self.paths.sync_log, self.paths.error_log):
            if not os.path.exists(log):
                atomic_write(log, b"")

    def _is_executable(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        if os.name == "nt":
            return True  # Windows tracks executability via extension/PATHEXT.
        return os.access(path, os.X_OK)

    def _target(self):
        """The unified execution target for this runtime (native or WSL).

        All sync subprocess calls go through this adapter, so an isolated
        CODEX_HOME reaches WSL as a Linux path and arguments are never joined
        into a shell string.
        """
        from .runtime import build_target

        return build_target(
            self.paths.codex_runtime,
            kind=self.paths.runtime_kind,
            distro=self.paths.runtime_distro,
            codex_home_win=self.paths.codex_home_win or "",
        )

    def sync(self) -> CatalogSyncResult:
        for override in self.paths.model_overrides.values():
            override.validate()

        if not self._is_executable(self.paths.codex_runtime):
            raise ProcessFailed(f"Codex 运行时不可执行：{self.paths.codex_runtime}")
        self.ensure_initial_files()

        target = self._target()

        with tempfile.TemporaryDirectory(
            prefix="codex-model-manager-", dir=_temp_parent()
        ) as isolated:
            source = "bundled"
            try:
                auth_source = self._auth_source(target)
                has_auth = bool(auth_source and auth_source.is_file())
                if has_auth:
                    shutil.copyfile(auth_source, Path(isolated) / "auth.json")
                refreshed = target.run(["debug", "models"], timeout=120.0,
                                       codex_home_win=isolated)
            except (OSError, CodexModelError):
                refreshed = None
            official = refreshed if refreshed and refreshed.exit_code == 0 and refreshed.standard_output.strip() else None
            if official is not None:
                source = "refresh"
            else:
                official = target.run(["debug", "models", "--bundled"],
                                      timeout=120.0, codex_home_win=isolated)
            if official.exit_code != 0:
                detail = official.standard_error.strip()
                message = detail or f"读取 Codex 官方模型失败，退出码 {official.exit_code}。"
                raise ProcessFailed(message)

            version = target.run(["--version"], timeout=30.0, codex_home_win=isolated)
            app_version = version.standard_output.strip() if version.exit_code == 0 else "unknown"

            custom_bytes = Path(self.paths.custom_source).read_bytes()
            bundled_bytes = official.standard_output.encode("utf-8")

            custom_root = parse_root(custom_bytes, "自定义模型源")
            bundled_root = parse_root(bundled_bytes, "Codex 官方模型")

            custom_models = custom_root.get("models")
            bundled_models = bundled_root.get("models")
            if (
                not isinstance(custom_models, list)
                or not isinstance(bundled_models, list)
                or not bundled_models
            ):
                raise InvalidCatalog("models 必须是数组，且官方目录不能为空")

            bundled_slugs = _validate_slugs(bundled_models, "官方模型")
            custom_slugs = _validate_slugs(custom_models, "自定义模型")
            overlap = set(bundled_slugs) & set(custom_slugs)
            if overlap:
                raise InvalidCatalog(
                    "自定义模型与官方模型重名：" + ", ".join(sorted(overlap))
                )

            priorities = [_as_int(m.get("priority")) for m in bundled_models]
            if any(p is None for p in priorities):
                raise InvalidCatalog("官方模型缺少数字 priority")
            priority_ceiling = max(priorities)

            applied_overrides: Dict[str, Dict[str, int]] = {}
            merged_official: List[dict] = []
            for model in bundled_models:
                copy = dict(model)
                for f in DEFAULT_SUPPORT_FIELDS:
                    if copy.get(f) is None:
                        copy[f] = True
                slug = copy.get("slug")
                if isinstance(slug, str) and slug in self.paths.visibility_overrides:
                    copy["visibility"] = self.paths.visibility_overrides[slug]
                if isinstance(slug, str) and slug in self.paths.model_overrides:
                    copy = self.paths.model_overrides[slug].applying(copy)
                    if not self.paths.model_overrides[slug].is_empty:
                        applied_overrides[slug] = self.paths.model_overrides[slug].fields
                merged_official.append(copy)

            merged_custom: List[dict] = []
            for index, model in enumerate(custom_models):
                copy = dict(model)
                copy["priority"] = priority_ceiling + index + 1
                for f in DEFAULT_SUPPORT_FIELDS:
                    if copy.get(f) is None:
                        copy[f] = True
                slug = copy.get("slug")
                if isinstance(slug, str) and slug in self.paths.visibility_overrides:
                    copy["visibility"] = self.paths.visibility_overrides[slug]
                merged_custom.append(copy)

            bundled_root["models"] = merged_official + merged_custom
            desired_data = _pretty(bundled_root)
            desired_canonical = _canonical(bundled_root)
            desired_hash = _sha256(desired_canonical)
            custom_hash = _sha256(_canonical(custom_root))

            current_hash: Optional[str] = None
            if os.path.exists(self.paths.merged_catalog):
                try:
                    current_obj = parse_root(
                        Path(self.paths.merged_catalog).read_bytes(), "当前合并目录"
                    )
                    current_hash = _sha256(_canonical(current_obj))
                except InvalidCatalog:
                    current_hash = None

            status: str
            backup_path: Optional[str] = None
            if current_hash == desired_hash:
                status = "no_change"
            else:
                status = "updated"
                if os.path.exists(self.paths.merged_catalog):
                    backup_path = backup_file(
                        self.paths.merged_catalog, self.paths.backup_directory, prefix="models"
                    )
                atomic_write(self.paths.merged_catalog, desired_data)

            inactive = sorted(
                slug for slug in self.paths.model_overrides if slug not in bundled_slugs
            )
            record = {
                "recorded_at": _iso_timestamp(),
                "status": status,
                "app_codex": self.paths.codex_runtime,
                "app_version": app_version,
                "custom_models": self.paths.custom_source,
                "custom_hash": custom_hash,
                "catalog": self.paths.merged_catalog,
                "official_source": source,
                "bundled_count": len(merged_official),
                "custom_count": len(merged_custom),
                "applied_model_overrides": applied_overrides,
                "inactive_model_overrides": inactive,
                "current_hash": current_hash,
                "desired_hash": desired_hash,
                "backup_path": backup_path,
            }
            record = {k: v for k, v in record.items() if v is not None}
            record_data = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
            return CatalogSyncResult(status=status, record_data=record_data)

    def _auth_source(self, target) -> Optional[Path]:
        """Use the selected runtime's identity without importing its config.toml."""
        if not target.is_wsl:
            return Path(self.paths.codex_home_win or Path.home() / ".codex") / "auth.json"
        from .wsl_adapter import filtered_windows_env, run_process, to_windows_path, wsl_exe

        exe = wsl_exe()
        if not exe or not target.distro:
            return None
        result = run_process(
            exe, ["--distribution", target.distro, "--exec", "printenv", "HOME"],
            timeout=20.0, environment=filtered_windows_env(), replace_env=True,
        )
        if result.exit_code != 0:
            return None
        linux_home = result.standard_output.strip()
        if not linux_home.startswith("/"):
            return None
        win_path = to_windows_path(linux_home.rstrip("/") + "/.codex/auth.json", target.distro)
        return Path(win_path) if win_path else None

    def clear_error_log(self) -> None:
        self.ensure_initial_files()
        with open(self.paths.error_log, "wb") as fh:
            fh.truncate(0)

    def sync_and_append_log(self) -> CatalogSyncResult:
        result = self.sync()
        with open(self.paths.sync_log, "ab") as fh:
            fh.write(result.record_data + b"\n")
        return result


def _temp_parent() -> str:
    return tempfile.gettempdir()
