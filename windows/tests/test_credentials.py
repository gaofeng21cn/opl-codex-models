"""The credential channel's security contract, made executable.

``secret-tool lookup`` does not use a side channel: it **prints the secret to its
own stdout**, exactly like ``cat``.  Every rule in ``scripts/l2_credentials.py``
follows from that one fact, and these tests exist so the rules cannot quietly
rot:

* the pipe is captured inside the program (``capture_output=True``);
* the value reaches the caller's memory and the process's stdout/stderr **never**;
* a failure path reports the exit code only — it must not quote the child's
  stdout/stderr, because that is precisely how a key ends up in a transcript;
* there is no command-line channel for a key at all;
* a key that has already been exposed is never read back, printed, or compared:
  the repository is scanned for anything shaped like a live secret.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import l2_credentials  # noqa: E402

#: Stands in for a live credential.  It deliberately does **not** carry a real key's
#: prefix or shape: the repository-wide invariant is "a grep for secret-shaped
#: literals comes back empty", and a fixture that tripped that grep would make the
#: invariant untrustworthy.  ``test_repository_holds_no_secret_shaped_literal``
#: scans the whole project, this file included.
SENTINEL = "SENTINEL-fake-credential-0123456789abcdef"

SECRET_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")

#: Directories that are not source and may legitimately contain anything.
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}


def _install_fake_secret_tool(tmp_path, monkeypatch, *, payload, exit_code=0):
    """Put a stand-in ``secret-tool`` behind the real subprocess machinery.

    The spy keeps the call genuine — a real child process runs, with pipes the
    parent must capture — while letting the test decide what the child writes and
    what it exits with.  It also asserts the one property that matters most: the
    child's stdout was piped, not inherited.
    """
    script = tmp_path / "fake_secret_tool.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write(%r)\n"
        "sys.stdout.flush()\n"
        "sys.exit(%d)\n" % (payload, exit_code),
        encoding="utf-8",
    )
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        assert cmd[0] == "secret-tool", cmd
        assert kwargs.get("capture_output") is True, (
            "秘密管道必须由程序捕获，否则 secret-tool 会把它写到终端"
        )
        return real_run([sys.executable, str(script)] + list(cmd[1:]), **kwargs)

    monkeypatch.setattr(l2_credentials.subprocess, "run", spy)
    return script


def _assert_clean(capsys):
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out, "秘密出现在进程 stdout"
    assert SENTINEL not in captured.err, "秘密出现在进程 stderr"
    return captured


def test_secret_is_captured_in_memory_and_never_printed(tmp_path, monkeypatch, capsys):
    _install_fake_secret_tool(
        tmp_path, monkeypatch, payload=json.dumps({"OPENAI_API_KEY": SENTINEL})
    )
    assert l2_credentials.from_secret_tool(codex_home="/tmp/whatever") == SENTINEL
    _assert_clean(capsys)


def test_failure_path_reports_exit_code_only(tmp_path, monkeypatch, capsys):
    """A child that fails *and* wrote secret bytes must not have them quoted."""
    _install_fake_secret_tool(
        tmp_path,
        monkeypatch,
        payload=json.dumps({"OPENAI_API_KEY": SENTINEL}),
        exit_code=1,
    )
    with pytest.raises(l2_credentials.CredentialError) as excinfo:
        l2_credentials.from_secret_tool(codex_home="/tmp/whatever")
    message = str(excinfo.value)
    assert SENTINEL not in message, "异常正文里带了秘密"
    assert "返回 1" in message
    _assert_clean(capsys)


def test_missing_field_names_the_field_not_the_value(tmp_path, monkeypatch, capsys):
    _install_fake_secret_tool(
        tmp_path, monkeypatch, payload=json.dumps({"SOME_OTHER_TOKEN": SENTINEL})
    )
    with pytest.raises(l2_credentials.CredentialError) as excinfo:
        l2_credentials.from_secret_tool(codex_home="/tmp/whatever")
    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message  # the key *name* is fine to report
    assert SENTINEL not in message
    _assert_clean(capsys)


def test_a_bare_token_is_accepted(tmp_path, monkeypatch, capsys):
    _install_fake_secret_tool(tmp_path, monkeypatch, payload=SENTINEL)
    assert l2_credentials.from_secret_tool(codex_home="/tmp/whatever") == SENTINEL
    _assert_clean(capsys)


def test_empty_output_is_an_error_not_an_empty_secret(tmp_path, monkeypatch, capsys):
    _install_fake_secret_tool(tmp_path, monkeypatch, payload="\n")
    with pytest.raises(l2_credentials.CredentialError):
        l2_credentials.from_secret_tool(codex_home="/tmp/whatever")
    _assert_clean(capsys)


def test_diagnostic_helpers_never_reveal_the_value():
    assert l2_credentials.describe(None) == "未提供（上游可能拒绝）"
    described = l2_credentials.describe(SENTINEL)
    assert SENTINEL not in described
    assert str(len(SENTINEL)) in described  # a length class is all it may say

    embedded = "上游说：%s 无效" % SENTINEL
    assert l2_credentials.redact(embedded, SENTINEL) == "上游说：[REDACTED] 无效"
    # A missing secret must not make redact() mangle anything.
    assert l2_credentials.redact(embedded, None) == embedded


def test_env_source_takes_a_name_not_a_value(monkeypatch):
    monkeypatch.setenv("L2_TEST_KEY_NAME", SENTINEL)
    assert l2_credentials.from_env("L2_TEST_KEY_NAME") == SENTINEL
    with pytest.raises(l2_credentials.CredentialError):
        l2_credentials.from_env("L2_TEST_ABSENT_NAME")
    assert l2_credentials.resolve("none") is None
    with pytest.raises(l2_credentials.CredentialError):
        l2_credentials.resolve("nonsense")


def test_stdout_capture_rule_is_documented_and_flagged():
    """The rule is part of the module, not folklore in a commit message."""
    assert l2_credentials.SECRET_TOOL_STDOUT_CARRIES_SECRET is True
    doc = l2_credentials.__doc__ or ""
    assert "stdout" in doc
    assert "capture_output" in doc
    assert "exception" in doc  # error messages are called out explicitly


def test_no_script_offers_a_command_line_key_channel():
    """No script may accept a credential as an argument — no exceptions."""
    offenders = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "key" in arg.value.lower():
                        offenders.append("%s: %s" % (path.name, arg.value))
    assert offenders == [], "存在命令行传 Key 的入口：%s" % offenders


def test_resolve_has_no_value_parameter():
    """The only credential-shaped parameter is a *variable name*."""
    import inspect

    params = set(inspect.signature(l2_credentials.resolve).parameters)
    assert params == {"source", "env_name", "codex_home", "secret_key"}
    assert "secret_key" in params  # the JSON field name, not the secret


def test_repository_holds_no_secret_shaped_literal():
    """An exposed key is never read back: nothing shaped like one may be committed.

    Scans the whole project — sources, scripts, tests, docs and the checked-in
    evidence — so the invariant is verified rather than assumed.
    """
    hits = []
    for path in sorted(PROJECT.rglob("*")):
        if not path.is_file() or SKIP_DIRS & set(path.parts):
            continue
        if path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in SECRET_SHAPE.finditer(text):
            hits.append("%s: %s…" % (path.relative_to(PROJECT), match.group(0)[:12]))
    assert hits == [], "仓库内出现疑似真实凭据：%s" % hits
