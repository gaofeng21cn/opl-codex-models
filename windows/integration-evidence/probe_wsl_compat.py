r"""Rerunnable Windows -> wsl.exe -> Ubuntu -> codex compatibility probe.

This script tests the FULL Windows call chain through our RuntimeTarget adapter,
NOT a direct WSL execution. It uses the same probe_compatibility() that the CLI
`probe --verify` and GUI "验证兼容性" button use.

Prerequisites:
  - wsl.exe available on PATH
  - WSL distro "Ubuntu" installed
  - Codex runtime at the path below (or pass --runtime / --distro)

Usage (from the project root):
  py -3.12 -m pip install -r outputs\opl-codex-models-windows\requirements.txt
  set PYTHONPATH=outputs\opl-codex-models-windows\src
  py outputs\opl-codex-models-windows\integration-evidence\probe_wsl_compat.py
  py ...\probe_wsl_compat.py --runtime C:\path\to\codex --distro Ubuntu

Output: prints desensitised evidence JSON to stdout and writes it to
  outputs\opl-codex-models-windows\integration-evidence\wsl-chain-evidence.json

No credentials are read or emitted. All runs use an isolated temp CODEX_HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_model_manager.core.wsl_adapter import make_wsl_target, resolve_distro
from codex_model_manager.core.compat_probe import probe_compatibility, evidence_to_dict


DEFAULT_RUNTIME = r"C:\Users\MECHREVO\.codex\bin\wsl\385b74eb4db8c237\codex"
DEFAULT_DISTRO = "Ubuntu"
OUTPUT_FILE = HERE / "wsl-chain-evidence.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME,
                        help="Windows-visible path to the codex executable")
    parser.add_argument("--distro", default=DEFAULT_DISTRO,
                        help="WSL distribution name (default: Ubuntu)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Per-step timeout in seconds")
    args = parser.parse_args()

    if not os.path.isfile(args.runtime):
        print(f"错误：运行时文件不存在：{args.runtime}", file=sys.stderr)
        return 2

    try:
        distro = resolve_distro(args.distro)
    except Exception as exc:
        print(f"错误：无法解析 WSL 发行版：{exc}", file=sys.stderr)
        return 2

    target = make_wsl_target(
        executable=args.runtime,
        distro=distro,
        codex_home_win="",
        version=None,
    )

    print(f"探测 Windows -> wsl.exe -> {distro} -> {args.runtime}", file=sys.stderr)
    evidence = probe_compatibility(target, timeout=args.timeout)
    data = evidence_to_dict(evidence)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    print(text)
    OUTPUT_FILE.write_text(text + "\n", encoding="utf-8")
    print(f"\n证据已写入：{OUTPUT_FILE}", file=sys.stderr)

    return 0 if evidence.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
