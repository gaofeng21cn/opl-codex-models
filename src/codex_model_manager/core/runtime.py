"""Codex runtime discovery for Windows.

Mirrors Sources/CodexModelCore/AppConfiguration.swift (CodexRuntimeLocator) but
for Windows. Distinguishes a native Windows Codex from a Linux/WSL binary so a
WSL-only run is never reported as a native Windows success.

Runtime classification is based on the executable header:
  - 'MZ'  -> native Windows executable (this is the one we treat as a real
             Windows integration).
  - 0x7F 'ELF' -> a Linux binary meant to run under WSL.
```

Config-directory resolution honours CODEX_HOME (mirroring how Codex itself
behaves) and falls back to ~/.codex. The machine is Windows, so WSL also
maintains its own config inside the Linux filesystem; this module never guesses
that a WSL config is the Windows config.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .process_runner import run_process
from .errors import RuntimeNotFound
from . import wsl_adapter

WSL_CODEX_HINT = "$USERPROFILE\\.codex\\bin\\wsl"


@dataclass(frozen=True)
class CodexRuntime:
    path: str
    version: str
    kind: str  # 'windows-native' or 'wsl'
    distro: Optional[str] = None  # WSL distro name (wsl kind only)

    @property
    def is_native(self) -> bool:
        return self.kind == "windows-native"



def classify(path: str) -> str:
    """Classify an executable as 'windows-native' or 'wsl' by its header."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return "unknown"
    if magic[:2] == b"MZ":
        return "windows-native"
    if magic[:4] == b"\x7fELF":
        return "wsl"
    return "unknown"


def _version(path: str) -> Optional[str]:
    try:
        result = run_process(path, ["--version"], timeout=20.0)
    except Exception:
        return None
    if result.exit_code != 0:
        return None
    return result.standard_output.strip() or None


def _wsl_version(windows_path: str, distro: Optional[str] = None) -> Optional[str]:
    """Read --version from a Linux ELF binary through the WSL adapter.

    Only for classification/probing; a WSL result is never a native-Windows
    verification. Returns version or None.
    """
    try:
        target = _target_for(windows_path, kind="wsl", distro=distro,
                             codex_home_win=os.environ.get("CODEX_HOME") or "")
        result = target.run(["--version"], timeout=30.0,
                            codex_home_win=target.codex_home_win)
    except Exception:
        return None
    if result.exit_code != 0:
        return None
    return result.standard_output.strip() or None


def _target_for(path: str, *, kind: Optional[str] = None,
                distro: Optional[str] = None, codex_home_win: str = "",
                version: Optional[str] = None) -> "wsl_adapter.RuntimeTarget":
    """Build the execution target for `path` (classifying when needed)."""
    kind = kind or classify(path)
    if kind == "wsl":
        resolved_distro = distro
        if not resolved_distro:
            try:
                resolved_distro = wsl_adapter.resolve_distro(None)
            except Exception:
                resolved_distro = None
        return wsl_adapter.make_wsl_target(path, resolved_distro or "", codex_home_win,
                                           version=version)
    return wsl_adapter.make_native_target(path, codex_home_win, version=version)


def build_target(path: str, *, kind: Optional[str] = None,
                 distro: Optional[str] = None, codex_home_win: str = "",
                 version: Optional[str] = None) -> "wsl_adapter.RuntimeTarget":
    """Public form of `_target_for` so sync/CLI share one execution target."""
    return _target_for(path, kind=kind, distro=distro,
                       codex_home_win=codex_home_win, version=version)


def _to_wsl_path(windows_path: str, distro: Optional[str] = None) -> Optional[str]:
    """Windows path -> Linux path via the distro's wslpath (adapter-backed)."""
    if distro:
        try:
            return wsl_adapter.to_linux_path(windows_path, distro)
        except Exception:
            pass
    return wsl_adapter._fallback_to_linux(windows_path)


def probe_bundled(runtime: "CodexRuntime", timeout: float = 30.0):
    """Run `codex debug models --bundled` and report (ok: bool, detail: str).

    Only classifies the *bundled interface* (did the binary answer). It says
    nothing about whether `model_catalog_json` config is read — that is a separate,
    stricter capability not claimed here. WSL binaries are routed through the
    adapter (wsl.exe argument array, explicit distro), never `bash -lc`.
    """
    try:
        target = _target_for(runtime.path, kind=runtime.kind,
                             distro=getattr(runtime, "distro", None))
        result = target.run(["debug", "models", "--bundled"], timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"调用失败 {exc}"
    if result.exit_code != 0:
        return False, (result.standard_error.strip()[:120] or f"exit {result.exit_code}")
    return True, ""


def _candidates_for_path(value: Optional[str]) -> List[str]:
    if not value:
        return []
    p = Path(value).expanduser()
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        # A directory was given: look for codex / codex.exe inside.
        return [str(p / n) for n in ("codex.exe", "codex", "codex.cmd")]
    return [value]


def discover(
    user_path: Optional[str] = None,
    include_wsl: bool = False,
) -> List[CodexRuntime]:
    """Discover usable Codex runtimes.

    Order:
      1. explicit user path (must be a real Windows-native file or a dir)
      2. PATH lookup for `codex`/`codex.exe` (native)
      3. optional WSL locations (only when include_wsl is requested; these are
         CLI binaries and are labelled wsl, never reported as native).
    """
    seen = set()
    found: List[CodexRuntime] = []

    def add(path: str, force_wsl: bool = False, distro: Optional[str] = None):
        path = os.path.abspath(path)
        if path in seen or not os.path.isfile(path):
            return
        kind = classify(path)
        if kind == "wsl" or force_wsl:
            # A native Windows runner cannot execute a Linux ELF directly; probe
            # it through the WSL adapter purely for classification/versioning.
            resolved_distro = distro
            if resolved_distro is None:
                try:
                    resolved_distro = wsl_adapter.resolve_distro(None)
                except Exception:
                    resolved_distro = None
            ver = _wsl_version(path, resolved_distro)
        else:
            ver = _version(path)
            resolved_distro = None
        if ver is None or kind == "unknown":
            return
        seen.add(path)
        found.append(CodexRuntime(path=path, version=ver, kind=kind, distro=resolved_distro))

    for candidate in _candidates_for_path(user_path):
        add(candidate)

    for name in ("codex", "codex.exe"):
        which = shutil.which(name)
        if which:
            add(which)

    if include_wsl:
        home = os.path.expanduser("~")
        base = os.path.join(home, ".codex", "bin", "wsl")
        if os.path.isdir(base):
            for sub in sorted(os.listdir(base)):
                add(os.path.join(base, sub, "codex"), force_wsl=True)

    return found


def find_native(user_path: Optional[str] = None) -> Optional[CodexRuntime]:
    """Find the best *native Windows* runtime (prefer the newest version)."""
    runtimes = discover(user_path=user_path, include_wsl=False)
    native = [r for r in runtimes if r.is_native]
    if not native:
        return None
    return max(native, key=lambda r: _natural_key(r.version))


def _natural_key(version: str):
    import re

    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"[.-]", version)]


def codex_home() -> Path:
    """Resolve the Codex config directory.

    CODEX_HOME (if set) wins; otherwise ~/.codex. Returned as a Windows path.
    """
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~")) / ".codex"


def find_any(user_path: Optional[str] = None) -> Optional[CodexRuntime]:
    """Find any usable runtime (native first, then WSL labelled)."""
    native = find_native(user_path)
    if native:
        return native
    runtimes = discover(user_path=user_path, include_wsl=True)
    if runtimes:
        return max(runtimes, key=lambda r: _natural_key(r.version))
    raise RuntimeNotFound(
        "没有找到可用的 Codex 运行时。请先安装 Codex，或在设置中选择 codex 可执行文件。"
    )