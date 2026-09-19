r"""Full integration flow: probe -> apply -> read -> undo, all in temp CODEX_HOME.

This script demonstrates the complete evidence-gated apply cycle against a real
WSL Codex runtime through the Windows -> wsl.exe adapter chain:

  1. probe_compatibility: behaviour proof with pos/neg controls -> evidence
  2. set_model_catalog: write model_catalog_json into an isolated temp config
  3. debug models (non-bundled): verify the runtime reads the applied catalog
  4. undo_model_catalog: restore the previous value, verify config reverted

All writes go to an isolated temp CODEX_HOME. No user real config is touched.
No credentials are read or emitted.

Usage (from the project root):
  set PYTHONPATH=outputs\opl-codex-models-windows\src
  py outputs\opl-codex-models-windows\integration-evidence\integration_flow.py
  py ...\integration_flow.py --runtime C:\path\to\codex --distro Ubuntu

Exit code 0 = all steps passed; non-zero = a step failed (see stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_model_manager.core.wsl_adapter import make_wsl_target, resolve_distro
from codex_model_manager.core.compat_probe import (
    probe_compatibility,
    evidence_valid,
    evidence_to_dict,
)
from codex_model_manager.core.config_editor import set_model_catalog, undo_model_catalog

DEFAULT_RUNTIME = r"C:\Users\MECHREVO\.codex\bin\wsl\385b74eb4db8c237\codex"
DEFAULT_DISTRO = "Ubuntu"


def _step(name: str) -> None:
    print(f"\n=== {name} ===", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}", file=sys.stderr)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME)
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if not os.path.isfile(args.runtime):
        _fail(f"运行时文件不存在：{args.runtime}")
        return 2

    try:
        distro = resolve_distro(args.distro)
    except Exception as exc:
        _fail(f"无法解析 WSL 发行版：{exc}")
        return 2

    target = make_wsl_target(
        executable=args.runtime, distro=distro, codex_home_win="", version=None)

    results: dict = {"steps": []}

    _step("1. 兼容性探测（行为证明 + 正反对照）")
    evidence = probe_compatibility(target, timeout=args.timeout)
    results["evidence"] = evidence_to_dict(evidence)
    if not evidence.ok:
        _fail(f"探测失败：{evidence.reason}")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1
    _ok(f"探测通过：version={evidence.runtime_version}, "
        f"bundled={evidence.bundled_model_count} models, "
        f"marker_loaded={evidence.marker_loaded}, "
        f"marker_absent={evidence.marker_absent_from_bundled}, "
        f"missing_exit={evidence.missing_catalog_exit_code}")

    _step("2. 证据有效性检查")
    if not evidence_valid(evidence, target):
        _fail("证据对当前运行时无效")
        return 1
    _ok("证据有效，允许 apply")

    _step("3. 在隔离 temp CODEX_HOME 中 apply model_catalog_json")
    with tempfile.TemporaryDirectory(prefix="codex-integration-flow-") as temp_home:
        config_path = os.path.join(temp_home, "config.toml")
        Path(config_path).write_text(
            'model = "gpt-5"\n# 用户注释\n', encoding="utf-8")

        catalog_file = os.path.join(temp_home, "catalog.json")
        bundled_res = target.run(["debug", "models", "--bundled"], timeout=args.timeout)
        bundled_root = json.loads(bundled_res.standard_output)
        template = dict(bundled_root["models"][0])
        template["slug"] = "integration-flow-test"
        template["display_name"] = "Integration flow test"
        template["visibility"] = "list"
        template["priority"] = 0
        Path(catalog_file).write_text(
            json.dumps({"models": [template]}, ensure_ascii=False), encoding="utf-8")

        catalog_runtime_path = target.to_runtime_path(catalog_file)
        backup_dir = os.path.join(temp_home, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        prev_value = set_model_catalog(catalog_runtime_path, config_path, backup_dir)
        results["steps"].append({
            "step": "apply",
            "previous_value": prev_value,
            "applied_value": catalog_runtime_path,
        })
        _ok(f"已写入 model_catalog_json = {catalog_runtime_path}")
        _ok(f"原值 = {prev_value!r}")

        _step("4. 通过 WSL 运行时读取验证 applied catalog")
        loaded = target.run(["debug", "models"], timeout=args.timeout,
                            codex_home_win=temp_home)
        if loaded.exit_code != 0:
            _fail(f"debug models 退出码 {loaded.exit_code}：{loaded.standard_error[:200]}")
            return 1
        loaded_root = json.loads(loaded.standard_output)
        slugs = [m.get("slug") for m in loaded_root.get("models", [])]
        if "integration-flow-test" not in slugs:
            _fail(f"运行时未返回测试 slug，实际 slugs={slugs}")
            return 1
        _ok(f"运行时读取成功，slugs={slugs}")

        _step("5. 撤销上次 apply")
        restored = undo_model_catalog(
            config_path, backup_dir, prev_value, catalog_runtime_path)
        results["steps"].append({"step": "undo", "restored_to": restored})
        _ok(f"已恢复 model_catalog_json = {restored!r}")

        _step("6. 验证 config 已恢复")
        content = Path(config_path).read_text(encoding="utf-8")
        if "model_catalog_json" in content:
            _fail("config 仍含 model_catalog_json，撤销未生效")
            return 1
        if 'model = "gpt-5"' not in content:
            _fail("config 丢失了无关字段 model = \"gpt-5\"")
            return 1
        if "# 用户注释" not in content:
            _fail("config 丢失了用户注释")
            return 1
        _ok("model_catalog_json 已移除，model 和注释保留")

    _step("完成")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
