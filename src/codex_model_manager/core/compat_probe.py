"""Compatibility probe: a behaviour proof that the selected runtime reads
`model_catalog_json` from config.toml.

This replaces the constant-refusal real-mode apply gate with an evidence-gated
one. The probe:

  1. runs `--version` and `debug models --bundled` through the adapter, parsing
     real JSON;
  2. in an isolated temp CODEX_HOME, writes config.toml pointing at a catalog
     that contains a UNIQUE RANDOM test slug copied from a bundled template, then
     runs `debug models` (non-bundled) and asserts the slug is returned;
  3. negative controls: the bundled catalog must NOT contain the test slug, and a
     config pointing at a missing file must exit non-zero;
  4. returns bounded, desensitised evidence bound to the runtime path / file
     SHA256 / version / distro / PROBE_SCHEME_VERSION, so a runtime change,
     update, or distro change invalidates it;
  5. cleans the temp directory.

No static passed=true, no mock substitution, no string-existence check. Demo
evidence never enables a real write (the apply gate only honours evidence in
real, non-demo mode).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import wsl_adapter
from .wsl_adapter import RuntimeTarget, PROBE_SCHEME_VERSION


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CompatEvidence:
    """Desensitised compatibility evidence captured by a real probe run.

    `ok` is True only when every step of the behaviour proof succeeded. The
    remaining fields bind the evidence to a specific runtime so a change,
    update, or distro switch invalidates it (see `evidence_valid`).
    """

    ok: bool
    reason: str
    runtime_path: str
    runtime_sha256: str
    runtime_version: str
    runtime_kind: str
    distro: Optional[str]
    probe_scheme: str
    probe_slug: str
    bundled_model_count: int
    marker_loaded: bool
    marker_absent_from_bundled: bool
    missing_catalog_exit_code: int
    probed_at: str


def _runtime_catalog_path(target: RuntimeTarget, temp_home: str) -> str:
    """The catalog path as the runtime sees it, inside the temp CODEX_HOME."""
    home_runtime = target.runtime_home(temp_home)
    if target.is_wsl:
        return home_runtime.rstrip("/") + "/catalog.json"
    return os.path.join(home_runtime, "catalog.json")


def _runtime_config_path(target: RuntimeTarget, temp_home: str) -> str:
    """The config.toml path as the runtime sees it (for diagnostics only)."""
    home_runtime = target.runtime_home(temp_home)
    if target.is_wsl:
        return home_runtime.rstrip("/") + "/config.toml"
    return os.path.join(home_runtime, "config.toml")


def _write_config(temp_home: str, catalog_runtime_path: str) -> None:
    """Write config.toml with `model_catalog_json = <catalog_runtime_path>`."""
    text = "model_catalog_json = " + json.dumps(catalog_runtime_path) + "\n"
    Path(os.path.join(temp_home, "config.toml")).write_text(text, encoding="utf-8")


def probe_compatibility(target: RuntimeTarget, *, timeout: float = 60.0) -> CompatEvidence:
    """Run the behaviour proof and return desensitised evidence.

    All real runs go through `target.run` (adapter), with an isolated temp
    CODEX_HOME. Nothing is written outside the temp dir, which is removed on
    return. A failure at any step yields `ok=False` with a concrete `reason`.
    """
    runtime_path = os.path.realpath(os.path.abspath(target.executable))
    runtime_sha = _sha256_file(target.executable) or ""
    distro = target.distro
    kind = target.kind
    probe_slug = "compat-probe-" + uuid.uuid4().hex[:12]
    probed_at = _iso_now()

    def fail(reason: str, **extra) -> CompatEvidence:
        return CompatEvidence(
            ok=False, reason=reason, runtime_path=runtime_path,
            runtime_sha256=runtime_sha, runtime_version=target.version or "",
            runtime_kind=kind, distro=distro, probe_scheme=PROBE_SCHEME_VERSION,
            probe_slug=probe_slug, bundled_model_count=extra.get("bundled_model_count", 0),
            marker_loaded=extra.get("marker_loaded", False),
            marker_absent_from_bundled=extra.get("marker_absent_from_bundled", False),
            missing_catalog_exit_code=extra.get("missing_catalog_exit_code", -1),
            probed_at=probed_at,
        )

    try:
        version_res = target.run(["--version"], timeout=30.0)
    except Exception as exc:
        return fail(f"--version 调用失败：{exc}")
    if version_res.exit_code != 0:
        return fail(f"--version 退出码 {version_res.exit_code}")
    version = version_res.standard_output.strip()
    if not version:
        return fail("--version 输出为空")

    try:
        bundled_res = target.run(["debug", "models", "--bundled"], timeout=timeout)
    except Exception as exc:
        return fail(f"debug models --bundled 调用失败：{exc}")
    if bundled_res.exit_code != 0:
        detail = bundled_res.standard_error.strip()[:200] or f"exit {bundled_res.exit_code}"
        return fail(f"debug models --bundled 失败：{detail}")
    try:
        bundled_root = json.loads(bundled_res.standard_output)
    except ValueError as exc:
        return fail(f"bundled 输出非 JSON：{exc}")
    if not isinstance(bundled_root, dict) or not isinstance(bundled_root.get("models"), list):
        return fail("bundled 输出不是 {\"models\": [...]} 结构")
    bundled_models = bundled_root["models"]
    if not bundled_models:
        return fail("bundled 模型列表为空，无法构造测试模板")

    marker_absent = all(m.get("slug") != probe_slug for m in bundled_models)
    if not marker_absent:
        return fail("随机测试 slug 意外出现在 bundled 目录（碰撞）",
                    bundled_model_count=len(bundled_models), marker_absent_from_bundled=False)

    template = dict(bundled_models[0])
    template["slug"] = probe_slug
    template["display_name"] = "Compatibility probe marker"
    template["visibility"] = "list"
    template["priority"] = 0
    catalog_obj = {"models": [template]}

    with tempfile.TemporaryDirectory(prefix="codex-compat-probe-") as temp_home:
        catalog_file = os.path.join(temp_home, "catalog.json")
        Path(catalog_file).write_text(
            json.dumps(catalog_obj, ensure_ascii=False), encoding="utf-8")
        catalog_runtime_path = _runtime_catalog_path(target, temp_home)
        _write_config(temp_home, catalog_runtime_path)

        try:
            loaded = target.run(["debug", "models"], timeout=timeout,
                                codex_home_win=temp_home)
        except Exception as exc:
            return fail(f"debug models（非 bundled）调用失败：{exc}",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent)
        if loaded.exit_code != 0:
            detail = loaded.standard_error.strip()[:200] or f"exit {loaded.exit_code}"
            return fail(f"debug models 退出码 {loaded.exit_code}：{detail}",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent)
        try:
            loaded_root = json.loads(loaded.standard_output)
        except ValueError as exc:
            return fail(f"非 bundled 输出非 JSON：{exc}",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent)
        loaded_models = loaded_root.get("models") if isinstance(loaded_root, dict) else None
        marker_loaded = isinstance(loaded_models, list) and any(
            m.get("slug") == probe_slug for m in loaded_models)
        if not marker_loaded:
            return fail("非 bundled 输出未包含测试 slug，运行时未读取 model_catalog_json 配置",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent,
                        marker_loaded=False)

        missing_runtime = _runtime_catalog_path(target, temp_home).rsplit("/catalog.json", 1)[0]
        missing_runtime = missing_runtime.rstrip("/") + "/missing-catalog.json"
        _write_config(temp_home, missing_runtime)
        try:
            negative = target.run(["debug", "models"], timeout=timeout,
                                  codex_home_win=temp_home)
        except Exception as exc:
            return fail(f"负对照调用失败：{exc}",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent,
                        marker_loaded=True)
        missing_exit = negative.exit_code
        if missing_exit == 0:
            return fail("负对照：指向不存在的 catalog 仍退出 0，未按预期失败",
                        bundled_model_count=len(bundled_models),
                        marker_absent_from_bundled=marker_absent,
                        marker_loaded=True, missing_catalog_exit_code=0)

    return CompatEvidence(
        ok=True, reason="", runtime_path=runtime_path,
        runtime_sha256=runtime_sha, runtime_version=version,
        runtime_kind=kind, distro=distro, probe_scheme=PROBE_SCHEME_VERSION,
        probe_slug=probe_slug, bundled_model_count=len(bundled_models),
        marker_loaded=True, marker_absent_from_bundled=True,
        missing_catalog_exit_code=missing_exit, probed_at=probed_at,
    )


def evidence_valid(evidence: Optional[CompatEvidence], target: RuntimeTarget) -> bool:
    """True when stored evidence still binds to the current runtime target.

    A runtime change (path), update (file SHA256), kind/distro switch, or probe
    scheme bump invalidates the evidence. The boolean outcomes must all hold.
    """
    if evidence is None or not evidence.ok:
        return False
    if evidence.probe_scheme != PROBE_SCHEME_VERSION:
        return False
    if evidence.runtime_kind != target.kind:
        return False
    if target.is_wsl and evidence.distro != target.distro:
        return False
    current_path = os.path.realpath(os.path.abspath(target.executable))
    if evidence.runtime_path != current_path:
        return False
    current_sha = _sha256_file(target.executable)
    if current_sha is None or evidence.runtime_sha256 != current_sha:
        return False
    if target.version and evidence.runtime_version and evidence.runtime_version != target.version:
        return False
    if not evidence.marker_loaded:
        return False
    if not evidence.marker_absent_from_bundled:
        return False
    if evidence.missing_catalog_exit_code == 0:
        return False
    return True


def evidence_to_dict(evidence: CompatEvidence) -> dict:
    return {
        "ok": evidence.ok,
        "reason": evidence.reason,
        "runtimePath": evidence.runtime_path,
        "runtimeSha256": evidence.runtime_sha256,
        "runtimeVersion": evidence.runtime_version,
        "runtimeKind": evidence.runtime_kind,
        "distro": evidence.distro,
        "probeScheme": evidence.probe_scheme,
        "probeSlug": evidence.probe_slug,
        "bundledModelCount": evidence.bundled_model_count,
        "markerLoaded": evidence.marker_loaded,
        "markerAbsentFromBundled": evidence.marker_absent_from_bundled,
        "missingCatalogExitCode": evidence.missing_catalog_exit_code,
        "probedAt": evidence.probed_at,
    }


def evidence_from_dict(data: Optional[dict]) -> Optional[CompatEvidence]:
    if not data or not isinstance(data, dict):
        return None
    try:
        return CompatEvidence(
            ok=bool(data.get("ok", False)),
            reason=str(data.get("reason", "")),
            runtime_path=str(data.get("runtimePath", "")),
            runtime_sha256=str(data.get("runtimeSha256", "")),
            runtime_version=str(data.get("runtimeVersion", "")),
            runtime_kind=str(data.get("runtimeKind", "")),
            distro=data.get("distro"),
            probe_scheme=str(data.get("probeScheme", "")),
            probe_slug=str(data.get("probeSlug", "")),
            bundled_model_count=int(data.get("bundledModelCount", 0)),
            marker_loaded=bool(data.get("markerLoaded", False)),
            marker_absent_from_bundled=bool(data.get("markerAbsentFromBundled", False)),
            missing_catalog_exit_code=int(data.get("missingCatalogExitCode", -1)),
            probed_at=str(data.get("probedAt", "")),
        )
    except (TypeError, ValueError):
        return None
