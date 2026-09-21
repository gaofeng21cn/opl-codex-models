"""Opt-in request-copy recovery, independently selectable from tool translation.

Never edits a rollout, user message, tool call/result, reasoning item or file.
Only exact, known diagnostic-only assistant replies can be omitted. Unknown or
mixed work/diagnostic messages are kept. Evidence contains counts, not text.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

MODES = {"off": (False, False), "protocol": (True, False),
         "context": (False, True), "both": (True, True)}
MARKER = "[local-tool-context-recovery-v1]"
HINT = MARKER + "\n" + (
    "For this request, the client has declared an execution tool named exec in "
    "the current tool definitions. Earlier assistant claims that execution tools "
    "were unavailable describe earlier turns; they do not determine availability "
    "now. Follow the current tool definitions and their input format when a tool "
    "is needed for the user's request. Produce a real structured tool call, not "
    "a textual imitation. This note grants no additional permission: obey the "
    "user's scope, active restrictions and approval requirements. If a real tool "
    "call fails, report the actual failure without inventing success."
)


def normalize(text):
    text = text.replace("`", "").replace("**", "")
    text = re.sub(r"无法在 (?:/[^\r\n]+|[A-Za-z]:[\\/][^\r\n]+) 下执行 pwd", "无法在 <WORKDIR> 下执行 pwd", text)
    return re.sub(r"\s+", "", text)


# Intentionally exact. Do not broaden into a keyword filter over arbitrary work.
_KNOWN_LINES = frozenset(normalize(s) for s in (
    "本轮没有可调用的终端执行工具（exec 或 exec_command），因此无法实际执行 pwd。未执行任何命令，未修改文件。",
    "本轮实际可见的工具中没有命令执行入口，因此无法在 <WORKDIR> 下执行 pwd。这是依据本轮工具列表作出的判断，未尝试执行，也未修改文件。",
    "本轮可见工具名称为：",
    "其中 functions.functions__wait 只能等待已经启动的执行任务，不能启动命令。本轮没有 functions.exec、exec_command 或其他终端执行工具。",
    "我当前这轮也没有命令执行入口。",
    "当前这一轮暴露给我的工具里没有可用的命令执行或文件编辑入口。",
    "有 wait、用户提问和协作工具，但没有 exec、命令执行或文件编辑工具。",
    "我收到的工具清单中有 wait、提问和代理协作，却没有 exec，也没有文件读取/编辑工具。",
    "I can't run shell commands or access files with the tools available in this session, so I haven't created probe.txt.",
    "No shell execution tool is available in this session.",
))
_KNOWN_TOOL_LINES = frozenset((
    "functions.functions__wait", "functions.functions__request_user_input",
    "functions.functions__request_user_input_async", "functions.clock__sleep",
    "functions.collaboration__followup_task", "functions.collaboration__interrupt_agent",
    "functions.collaboration__list_agents", "functions.collaboration__send_message",
    "functions.collaboration__spawn_agent", "functions.collaboration__wait_agent",
))


def message_text(item):
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    if any(not isinstance(c, dict) or c.get("type") not in ("text", "input_text", "output_text")
           or not isinstance(c.get("text"), str) for c in content):
        return None
    return "\n".join(c["text"] for c in content)


def stale_diagnostic_only(item):
    if (not isinstance(item, dict) or item.get("role") != "assistant"
            or item.get("type", "message") != "message" or item.get("tool_calls")
            or item.get("function_call")):
        return False
    text = message_text(item)
    if not text or len(text) > 5000:
        return False
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    matched = False
    for line in lines:
        if line in ("```", "```text", "```markdown") or line in _KNOWN_TOOL_LINES:
            continue
        if normalize(line) not in _KNOWN_LINES:
            return False
        matched = True
    return matched


def recover_context(payload, declarations):
    """Use this request's declarations only, never a registry's inherited tools."""
    meta = {"enabled": True, "removed_messages": 0, "hint_added": False,
            "skip_reason": None, "version": 1}
    if payload.get("previous_response_id") or payload.get("conversation"):
        meta["skip_reason"] = "server_side_history"
        return payload, meta
    reg, reason = declarations.resolve("exec")
    if reason or not reg or "exec" not in declarations.declared_now:
        meta["skip_reason"] = "exec_not_declared_now"
        return payload, meta
    items = payload.get("input")
    if not isinstance(items, list):
        meta["skip_reason"] = "input_not_list"
        return payload, meta
    latest = next((i for i in range(len(items) - 1, -1, -1)
                   if isinstance(items[i], dict) and items[i].get("role") == "user"), None)
    if latest is None:
        meta["skip_reason"] = "no_user_boundary"
        return payload, meta
    cleaned = []
    for i, item in enumerate(items):
        if i < latest and stale_diagnostic_only(item):
            meta["removed_messages"] += 1
            continue
        # Idempotence: only remove our own exact developer note, never a quote.
        if (isinstance(item, dict) and item.get("role") == "developer"
                and message_text(item) == HINT):
            continue
        if i == latest:
            cleaned.append({"type": "message", "role": "developer",
                            "content": [{"type": "input_text", "text": HINT}]})
        cleaned.append(item)
    meta["hint_added"] = True
    return dict(payload, input=cleaned), meta


def policy(mode_file, protocol, context):
    if mode_file:
        with Path(mode_file).open("rb") as f:
            raw = f.read(4097)
        if len(raw) > 4096:
            raise ValueError("Experiment mode file is oversized")
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"mode"}
                or not isinstance(value["mode"], str) or value["mode"] not in MODES):
            raise ValueError("Invalid experiment mode file")
        return value["mode"], *MODES[value["mode"]]
    return next(name for name, flags in MODES.items() if flags == (protocol, context)), protocol, context
