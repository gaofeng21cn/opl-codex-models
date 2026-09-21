"""Runtime execution adapter for native Windows and WSL-backed Codex.

All Codex invocations (version probe, bundled catalog, non-bundled verification,
sync) go through ONE adapter so the two backends share identical argument-array
semantics and path handling.

Windows -> WSL invocation rules (deliberately strict):
  * arguments are passed as an ARRAY to `wsl.exe`; a user-controlled path is
    NEVER interpolated into a `bash -lc` string, so spaces, quotes, `$`, `;`,
    backticks and CJK characters cannot inject shell syntax;
  * the distribution is named explicitly (`--distribution <distro>`) rather than
    relying on the default distro happening to be correct;
  * the target CODEX_HOME is exported inside WSL with `env CODEX_HOME=<linux>`
    so the real override is visible to the Linux process;
  * Windows<->Linux path conversion uses the distro's own `wslpath` and falls
    back to a pure drive-letter mapping when `wslpath` is unavailable.

Nothing here reads or copies credentials; probes run with a caller-supplied
CODEX_HOME and a filtered Windows environment.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import CodexModelError
from .process_runner import ProcessResult, run_process

# Bump when the probe/execution scheme changes: evidence bound to an older value
# must be treated as invalid.
PROBE_SCHEME_VERSION = "wsl-adapter-v1"

WSL_KIND = "wsl"
NATIVE_KIND = "windows-native"

# Windows environment variables that may carry credentials. They are stripped
# before launching any child (probe or sync) and excluded from WSLENV so they are
# not forwarded into WSL. We never read their values.
_AUTH_ENV_MARKERS = (
    "OPENAI", "ANTHROPIC", "API_KEY", "APIKEY", "ACCESS_TOKEN", "AUTH",
    "SECRET", "PASSWORD", "CREDENTIAL", "CODEX_KEY", "BEARER",
)

# Env vars that tunnel Windows variables into WSL; cleared so credentials are
# never forwarded across the boundary.
_ENV_TUNNEL_KEYS = ("WSLENV",)


class WslUnavailable(CodexModelError):
    """WSL cannot be used on this machine (recoverable, actionable)."""


class DistroNotFound(CodexModelError):
    """The requested WSL distribution does not exist (recoverable)."""


def wsl_exe() -> Optional[str]:
    """Return the absolute path to wsl.exe, or None when WSL is unavailable."""
    found = shutil.which("wsl.exe") or shutil.which("wsl")
    if found:
        return found
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if root:
        candidate = os.path.join(root, "System32", "wsl.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _clean_wsl_text(text: str) -> str:
    # wsl.exe output is UTF-16 and may carry NULs / stray CR; normalise it.
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def list_distros(timeout: float = 30.0) -> List[str]:
    """List installed WSL distributions (read-only). Raises WslUnavailable."""
    exe = wsl_exe()
    if not exe:
        raise WslUnavailable(
            "未找到 wsl.exe：本机未安装/未启用 WSL。可安装 WSL 或在设置中改用原生 Windows Codex。"
        )
    result = run_process(exe, ["--list", "--quiet"], timeout=timeout)
    if result.exit_code != 0:
        detail = _clean_wsl_text(result.standard_error).strip() or f"exit {result.exit_code}"
        raise WslUnavailable(f"无法列出 WSL 发行版：{detail}")
    out = _clean_wsl_text(result.standard_output)
    distros: List[str] = []
    for line in out.split("\n"):
        name = line.strip().strip("*").strip()
        if not name or name.lower().startswith("windows subsystem"):
            continue
        distros.append(name)
    return distros


def resolve_distro(requested: Optional[str]) -> str:
    """Return the distro to use, validating it exists.

    When `requested` is empty and exactly one distro is installed it is used;
    otherwise the user must choose explicitly (we do not silently trust the
    default distro).
    """
    distros = list_distros()
    if not distros:
        raise DistroNotFound("未找到任何 WSL 发行版。请先安装（wsl --install -d <distro>）。")
    if requested and requested.strip():
        name = requested.strip()
        if name not in distros:
            raise DistroNotFound(
                f"指定的 WSL 发行版不存在：{name}。可用发行版：{', '.join(distros)}"
            )
        return name
    if len(distros) == 1:
        return distros[0]
    raise DistroNotFound(
        "未指定 WSL 发行版，且本机有多个可用发行版："
        + ", ".join(distros)
        + "。请显式选择发行版。"
    )


def _fallback_to_linux(windows_path: str) -> Optional[str]:
    norm = os.path.abspath(windows_path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(.*)$", norm)
    if not m:
        return None
    return f"/mnt/{m.group(1).lower()}{m.group(2)}"


def to_linux_path(windows_path: str, distro: str, timeout: float = 20.0) -> str:
    """Convert a Windows path to the path the given distro sees.

    Uses the distro's `wslpath -a -u` so exotic mount layouts are handled; falls
    back to the standard `/mnt/<drive>` mapping. The path is passed as a single
    argv element (no shell), so special characters are preserved literally.
    """
    exe = wsl_exe()
    if exe:
        try:
            result = run_process(
                exe,
                ["--distribution", distro, "--exec", "wslpath", "-a", "-u", windows_path],
                timeout=timeout,
            )
            if result.exit_code == 0:
                value = _clean_wsl_text(result.standard_output).strip()
                if value:
                    return value
        except CodexModelError:
            pass
    fallback = _fallback_to_linux(windows_path)
    if fallback:
        return fallback
    raise DistroNotFound(f"无法将 Windows 路径转换为 WSL 路径：{windows_path}")


def to_windows_path(linux_path: str, distro: str, timeout: float = 20.0) -> Optional[str]:
    """Convert a Linux path back to a Windows-visible path (best effort)."""
    exe = wsl_exe()
    if not exe:
        return None
    try:
        result = run_process(
            exe,
            ["--distribution", distro, "--exec", "wslpath", "-a", "-w", linux_path],
            timeout=timeout,
        )
    except CodexModelError:
        return None
    if result.exit_code != 0:
        return None
    value = _clean_wsl_text(result.standard_output).strip()
    return value or None


def filtered_windows_env() -> Dict[str, str]:
    """A copy of the Windows environment with credential-bearing keys removed.

    This is a denylist, not an allowlist: everything except keys that look like a
    token/key (and the WSL tunnelling var WSLENV) is preserved, so the child keeps
    a normal, startable environment while a probe can never forward the user's
    credentials. We never read or emit the removed values.
    """
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in _AUTH_ENV_MARKERS):
            continue
        if upper in _ENV_TUNNEL_KEYS:
            continue
        env[key] = value
    return env


@dataclass(frozen=True)
class RuntimeTarget:
    """Unified execution target for a Codex runtime.

    kind            : 'windows-native' or 'wsl'
    executable      : Windows-visible path to the codex executable on disk
    distro          : WSL distro name (wsl only)
    codex_home_win  : Windows-accessible CODEX_HOME
    codex_home_runtime : CODEX_HOME as the runtime sees it (Linux path for wsl)
    version         : probed version string, or None when unknown
    """

    kind: str
    executable: str
    codex_home_win: str
    codex_home_runtime: str
    distro: Optional[str] = None
    version: Optional[str] = None
    probe_scheme: str = PROBE_SCHEME_VERSION

    @property
    def is_wsl(self) -> bool:
        return self.kind == WSL_KIND

    @property
    def is_native(self) -> bool:
        return self.kind == NATIVE_KIND

    def to_runtime_path(self, windows_path: str) -> str:
        """Convert a Windows-visible path to the form the runtime reads.

        For WSL this is a Linux path (via the distro's wslpath); for native the
        Windows path is used unchanged. Used both by the compatibility probe
        (catalog inside CODEX_HOME) and by apply (`model_catalog_json` value that
        points at the merged catalog on the Windows filesystem).
        """
        if self.is_wsl:
            return to_linux_path(windows_path, self.distro)
        return windows_path

    def runtime_home(self, codex_home_win: Optional[str] = None) -> str:
        """The CODEX_HOME as the runtime sees it, for a given Windows-side home.

        For WSL the Windows home is converted with the distro's `wslpath`; for
        native the Windows home is used unchanged. When no override is supplied
        the home bound at construction is used.
        """
        if not self.is_wsl:
            return codex_home_win if codex_home_win is not None else self.codex_home_win
        home_win = codex_home_win if codex_home_win is not None else self.codex_home_win
        if not home_win:
            return self.codex_home_runtime
        return to_linux_path(home_win, self.distro)

    def build_command(self, arguments: List[str],
                      codex_home_win: Optional[str] = None) -> List[str]:
        """Return the full argv (executable + arguments) for this target.

        Arguments are never joined into a shell string.
        """
        if self.is_wsl:
            exe = wsl_exe()
            if not exe:
                raise WslUnavailable("未找到 wsl.exe，无法执行 WSL 后端。")
            if not self.distro:
                raise DistroNotFound("WSL 后端缺少发行版名称。")
            linux_exe = to_linux_path(self.executable, self.distro)
            argv = [exe, "--distribution", self.distro, "--exec", "env"]
            home = self.runtime_home(codex_home_win)
            if home:
                argv.append(f"CODEX_HOME={home}")
            argv.append(linux_exe)
            argv.extend(arguments)
            return argv
        return [self.executable, *arguments]

    def run(self, arguments: List[str], timeout: float = 60.0,
            codex_home_win: Optional[str] = None) -> ProcessResult:
        """Execute `arguments` on the target, isolating CODEX_HOME.

        For WSL the CODEX_HOME override is exported inside the distro (as a Linux
        path); for native it is set in the (filtered, replaced) child environment
        so the override genuinely reaches the runtime.
        """
        if self.is_wsl:
            argv = self.build_command(arguments, codex_home_win=codex_home_win)
            return run_process(argv[0], argv[1:], timeout=timeout,
                               environment=filtered_windows_env(), replace_env=True)
        home = codex_home_win if codex_home_win is not None else self.codex_home_win
        env = filtered_windows_env()
        if home:
            env["CODEX_HOME"] = home
        return run_process(self.executable, arguments, timeout=timeout,
                           environment=env, replace_env=True)


def make_native_target(executable: str, codex_home_win: str,
                       version: Optional[str] = None) -> RuntimeTarget:
    return RuntimeTarget(
        kind=NATIVE_KIND, executable=executable,
        codex_home_win=codex_home_win, codex_home_runtime=codex_home_win,
        distro=None, version=version,
    )


def make_wsl_target(executable: str, distro: str, codex_home_win: str = "",
                    version: Optional[str] = None,
                    codex_home_runtime: Optional[str] = None) -> RuntimeTarget:
    runtime_home = codex_home_runtime
    if runtime_home is None:
        runtime_home = to_linux_path(codex_home_win, distro) if codex_home_win else ""
    return RuntimeTarget(
        kind=WSL_KIND, executable=executable,
        codex_home_win=codex_home_win,
        codex_home_runtime=runtime_home,
        distro=distro, version=version,
    )
