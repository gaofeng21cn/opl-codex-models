"""Subprocess runner.

Mirrors Sources/CodexModelCore/ProcessRunner.swift. All child processes use an
argument list (never a shell string), a timeout, and an explicit exit-code
check. No shell interpolation, no credentials touched.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import ProcessFailed


@dataclass
class ProcessResult:
    exit_code: int
    standard_output: str
    standard_error: str


def _decode(data: Optional[bytes]) -> str:
    if data is None:
        return ""
    # wsl.exe emits UTF-16LE by default (e.g. `wsl --list --quiet`). Detect the
    # BOM or a NUL byte in the first block and decode accordingly, so WSL output
    # is not mangled into cp1252 garbage.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    elif b"\x00" in data[:64] and len(data) % 2 == 0:
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    # Prefer UTF-8, fall back to the platform default without raising.
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _creation_flags() -> int:
    """Avoid spawning a console window when launched from a GUI on Windows."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run_process(
    executable: str,
    arguments: List[str],
    environment: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
    replace_env: bool = False,
) -> ProcessResult:
    if not os.path.isfile(executable):
        raise ProcessFailed(f"可执行文件不存在：{executable}")
    # replace_env=True is used for probes/sync so an explicit (filtered) child
    # environment is used verbatim: the inherited os.environ (which may carry the
    # user's credentials) is never merged in when isolation is requested.
    env = {} if replace_env else dict(os.environ)
    if environment:
        env.update(environment)
    try:
        proc = subprocess.Popen(
            [executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise ProcessFailed(f"无法启动进程 {executable}：{exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.communicate()
        raise ProcessFailed(
            f"进程 {executable} 在 {timeout:.0f} 秒内未完成，已终止。"
        ) from None
    if proc.returncode is None:
        # Should not happen after communicate(timeout=...).
        raise ProcessFailed(f"进程 {executable} 异常终止。")
    return ProcessResult(
        exit_code=proc.returncode,
        standard_output=_decode(out),
        standard_error=_decode(err),
    )


def require_zero(result: ProcessResult, detail: str) -> str:
    """Raise ProcessFailed when exit code is non-zero.

    Returns stdout trimmed when the process succeeded.
    """
    if result.exit_code != 0:
        message = detail
        stderr = result.standard_error.strip()
        if stderr:
            message = f"{detail}：{stderr}"
        raise ProcessFailed(message)
    return result.standard_output.strip()