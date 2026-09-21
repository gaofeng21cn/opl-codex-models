#!/usr/bin/env python
"""Acquire a relay credential without ever putting the secret on a command line.

Threat model: a command line is readable by other processes on the machine and
lands in shell history; a file on disk leaks and outlives the run.  So this
module offers only sources that keep the secret in memory for the lifetime of the
process:

``env``
    Read the value from a *named* environment variable.  The caller passes the
    variable's name, never its value.
``secret-tool``
    Ask the freedesktop Secret Service for the entry the Codex CLI itself uses.
    This is how the WSL Codex stores its OpenAI auth, so it reuses the existing
    authorised mechanism instead of inventing a new one.  The lookup is done in a
    child process and only the resulting value is read back into memory.
``stdin``
    Read one line piped in, for interactive use.

There is deliberately **no** "pass the key as an argument" option, and no source
that writes the secret anywhere.  Nothing here disables TLS verification: the
callers use ``urllib`` defaults / ``ssl.create_default_context()``.

SECURITY — ``secret-tool lookup`` writes the secret to its own stdout
--------------------------------------------------------------------

``secret-tool lookup`` does not hand the secret back through a special channel: it
**prints it to stdout**, exactly like ``cat``.  That single fact drives the rules
below, and they are absolute:

* the pipe must be captured *inside the program* (``subprocess.run(...,
  capture_output=True)``), never inherited from the terminal and never piped into
  a command whose output is being read, echoed or logged by a human or a tool;
* the bytes must never reach tool output, a log file, a console, an evidence
  record, a shell history entry, or an **exception message** — error paths in this
  module deliberately report only the exit code and a truncated account hash, and
  never echo stdout or stderr of the child;
* a diagnostic that wants to "check the entry" must call :func:`describe`, which
  returns a length class, never the value;
* anything already exposed is **not** read back or printed again.  A leaked key is
  rotated through the secure entry point and the old value is treated as dead —
  this module has no "compare with the old key" path by design.

Anything that wants to show partial content (an error body, a preview) must run it
through :func:`redact` first, so the value cannot survive by being embedded in a
larger string.

Account naming (matches codex's own convention)::

    account = "cli|" + sha256(str(CODEX_HOME))[:16]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Callable, Optional

#: Codex's normal CODEX_HOME in the current environment.  Callers may still
#: override it explicitly when probing another installation.
DEFAULT_CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")

#: The JSON key Codex stores the OpenAI credential under.
DEFAULT_SECRET_KEY = "OPENAI_API_KEY"

SOURCES = ("none", "env", "secret-tool", "stdin")

#: Greppable marker for the rule that governs every `secret-tool` call site:
#: ``secret-tool lookup`` writes the secret to **its own stdout**, so that pipe has
#: to be captured by the program and must never be forwarded to a terminal, a log,
#: an evidence file, or an exception message.  Tests assert this stays true.
SECRET_TOOL_STDOUT_CARRIES_SECRET = True


class CredentialError(RuntimeError):
    """Raised when a credential is required but cannot be obtained."""


def codex_auth_account(codex_home: str = DEFAULT_CODEX_HOME) -> str:
    """The Secret Service account Codex uses for ``codex_home``."""
    digest = hashlib.sha256(str(codex_home).encode("utf-8")).hexdigest()[:16]
    return "cli|" + digest


def from_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise CredentialError("环境变量 %s 为空或未设置。" % name)
    return value


def from_stdin(_unused: Optional[str] = None) -> str:
    line = sys.stdin.readline()
    value = line.strip()
    if not value:
        raise CredentialError("标准输入没有读到凭据。")
    return value


def from_secret_tool(
    codex_home: str = DEFAULT_CODEX_HOME, secret_key: str = DEFAULT_SECRET_KEY
) -> str:
    """Look the credential up in the Secret Service via ``secret-tool``.

    Returns the secret itself to the caller's memory; it is never echoed, logged
    or written.  ``secret-tool lookup`` prints the secret to its stdout, so that
    stream is captured here (``capture_output=True``) and never forwarded: the
    error paths below report the exit code and a truncated account hash only —
    neither stdout nor stderr of the child is ever quoted, because quoting it is
    exactly how the secret escapes into a tool transcript.

    Raises :class:`CredentialError` with an actionable message when the tool is
    missing, the keyring is locked, or the entry does not exist.
    """
    account = codex_auth_account(codex_home)
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", "Codex Auth", "username", account],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise CredentialError(
            "找不到 secret-tool：不在 Secret Service 环境内。改用 --credential env。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(
            "secret-tool 超时：密钥环可能已锁定或无人解锁。"
        ) from exc
    if result.returncode != 0:
        # Note what is *not* here: result.stdout / result.stderr.  On a failure the
        # child may still have written partial secret material, so it is dropped
        # rather than surfaced.
        raise CredentialError(
            "secret-tool 返回 %d：条目不存在或密钥环未解锁（account=%s）。"
            % (result.returncode, account[:6] + "…")
        )
    raw = result.stdout.strip()
    if not raw:
        raise CredentialError("secret-tool 没有返回内容（account=%s）。" % (account[:6] + "…"))
    try:
        payload = json.loads(raw)
    except ValueError:
        # Some setups store the bare token rather than a JSON blob.
        return raw
    if not isinstance(payload, dict) or not payload.get(secret_key):
        # The field name is safe to name; the value that may be present is not.
        raise CredentialError("凭据条目缺少字段 %s。" % secret_key)
    return str(payload[secret_key])


def resolve(
    source: str = "none",
    *,
    env_name: Optional[str] = None,
    codex_home: str = DEFAULT_CODEX_HOME,
    secret_key: str = DEFAULT_SECRET_KEY,
) -> Optional[str]:
    """Return the credential for ``source``, or ``None`` when none is needed."""
    if source == "none":
        return None
    if source == "env":
        if not env_name:
            raise CredentialError("--credential env 需要 --credential-env 指定变量名。")
        return from_env(env_name)
    if source == "secret-tool":
        return from_secret_tool(codex_home, secret_key)
    if source == "stdin":
        return from_stdin()
    raise CredentialError("未知凭据来源：%r（可选：%s）" % (source, "/".join(SOURCES)))


def redact(text: str, secret: Optional[str]) -> str:
    """Replace any occurrence of ``secret`` so it can never reach a log."""
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


def describe(secret: Optional[str]) -> str:
    """A fingerprint safe to print: length class only, never the value itself."""
    if secret is None:
        return "未提供（上游可能拒绝）"
    return "已载入内存（长度 %d，类别 %s，未落盘）" % (
        len(secret),
        "long" if len(secret) > 40 else "short",
    )


__all__ = [
    "CredentialError",
    "DEFAULT_CODEX_HOME",
    "DEFAULT_SECRET_KEY",
    "SECRET_TOOL_STDOUT_CARRIES_SECRET",
    "SOURCES",
    "codex_auth_account",
    "describe",
    "from_env",
    "from_secret_tool",
    "from_stdin",
    "redact",
    "resolve",
]
