#!/usr/bin/env python
"""L2: real-relay protocol closed loop through the local bridge.

This is the step that L1 deliberately does not take.  L1 proves the bridge's
*wire* contract against a fake relay; L2 drives the real relay with the request
shape Codex actually sends and checks whether DeepSeek can complete a tool call
end to end through the bridge.

Fidelity rules (nothing here is a self-invented simplification):

* transport is ``POST /responses`` with ``stream: true`` and an SSE reply, which
  is what Codex uses;
* tool declarations use Codex's own tree — a ``namespace`` node named
  ``functions`` holding a ``custom`` tool — for the ``additional_tools`` path,
  and a top-level ``custom`` tool for the ``use_responses_lite=false`` path;
* turn 2 re-sends the declarations *and* the history, matching the captured
  behaviour of codex-cli 0.155.0-alpha.9.2 (``previous_response_id`` is never
  sent by that build);
* only the whitelisted ``probe_echo`` tool is ever executed.  Anything else the
  model asks for is reported and refused — the probe never runs model-authored
  commands.

Credentials stay in memory (see ``l2_credentials.py``); TLS verification is left
at the library default and is never disabled.

    # in WSL, reusing the credential Codex itself stores:
    python3 scripts/bridge_l2_probe.py --upstream https://gflabtoken.cn \
        --credential secret-tool --record /tmp/l2/evidence.jsonl

    # offline self-check of the probe itself (no network, no credential):
    python3 scripts/bridge_l2_probe.py --fake

Exit code is non-zero when the closed loop did not hold.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import l2_credentials  # noqa: E402
from codex_model_manager.core.bridge import BridgeServer  # noqa: E402

#: The synthetic, side-effect-free tool.  Its only job is to return a fixed
#: prefix plus whatever it was handed, so a model answer that repeats the value
#: proves the tool result really travelled back through the bridge.
PROBE_TOOL = "probe_echo"
PROBE_PREFIX = "PROBE_ECHO_OK:"
PROBE_INPUT = "PING-L2"

#: The whitelist.  A tool call naming anything else is never executed.
WHITELIST: Dict[str, Callable[[str], str]] = {
    PROBE_TOOL: lambda value: PROBE_PREFIX + value,
}

#: Paths under test, and how each one declares a custom tool.
ADDITIONAL_TOOLS = "additional_tools"
TOP_LEVEL_TOOLS = "top_level_tools"


def probe_echo(value: str) -> str:
    """The only executable the probe will ever run."""
    return PROBE_PREFIX + value


WHITELIST[PROBE_TOOL] = probe_echo


# --------------------------------------------------------------------------- #
# Request construction (Codex-shaped)
# --------------------------------------------------------------------------- #


def _functions_namespace(custom_name: str, description: str) -> dict:
    """The namespace node exactly as Codex builds it for a custom tool."""
    return {
        "type": "namespace",
        "name": "functions",
        "tools": [
            {"type": "custom", "name": custom_name, "description": description},
        ],
    }


def declared_tools(path: str, description: str) -> Tuple[list, list, Optional[list]]:
    """Return ``(additional_tools_sources, top_level_tools, input_prefix)``."""
    if path == ADDITIONAL_TOOLS:
        source = [_functions_namespace(PROBE_TOOL, description)]
        return source, [], [{"type": "additional_tools", "role": "developer", "tools": source}]
    if path == TOP_LEVEL_TOOLS:
        return [], [{"type": "custom", "name": PROBE_TOOL, "description": description}], []
    raise ValueError("未知的工具声明路径：%r" % path)


def build_turn(
    *,
    path: str,
    model: str,
    effort: str,
    prompt: str,
    call_id: Optional[str] = None,
    tool_output: Optional[str] = None,
) -> Dict[str, Any]:
    """One Codex-shaped request body."""
    description = (
        "Echoes back the exact string you pass in the `input` field, prefixed "
        "with %s. It has no side effects and is the only tool available."
        % PROBE_PREFIX
    )
    additional, top_level, envelope = declared_tools(path, description)
    items: List[dict] = list(envelope)
    items.append(
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}
    )
    if call_id:
        items.append(
            {"type": "custom_tool_call", "name": PROBE_TOOL, "call_id": call_id,
             "input": PROBE_INPUT}
        )
        items.append(
            {"type": "custom_tool_call_output", "call_id": call_id,
             "output": tool_output if tool_output is not None else ""}
        )
    body: Dict[str, Any] = {
        "model": model,
        "instructions": "You are a protocol probe. Follow the user instruction exactly.",
        "input": items,
        "stream": True,
        "store": False,
        "reasoning": {"effort": effort},
    }
    if top_level:
        body["tools"] = top_level
    return body


# --------------------------------------------------------------------------- #
# HTTP + SSE
# --------------------------------------------------------------------------- #


def post(endpoint: str, payload: dict, credential: Optional[str], timeout: float = 180.0):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if credential:
        headers["Authorization"] = "Bearer %s" % credential
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()
    except URLError as exc:
        return 0, "", json.dumps({"error": {"message": str(exc)}}).encode("utf-8")


def sse_final_output(raw: bytes) -> Tuple[List[str], List[dict]]:
    """Return ``(event_types, output_items)`` from an SSE body."""
    event_types: List[str] = []
    output: List[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[len("data: "):])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        event_types.append(str(payload.get("type")))
        response = payload.get("response")
        if payload.get("type") == "response.completed" and isinstance(response, dict):
            output = [i for i in (response.get("output") or []) if isinstance(i, dict)]
    if not output:
        # Fall back to the per-item announcements when no completion arrived.
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[len("data: "):])
            except ValueError:
                continue
            item = payload.get("item") if isinstance(payload, dict) else None
            if isinstance(item, dict):
                output.append(item)
    return event_types, output


def output_text(items: List[dict]) -> str:
    chunks: List[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks)


def first_call(items: List[dict], wanted: str) -> Optional[dict]:
    for item in items:
        if item.get("type") == "custom_tool_call" and item.get("name") == wanted:
            return item
    return None


def custom_value(item: dict) -> str:
    """The custom tool's string input, whatever shape the relay returned it in."""
    value = item.get("input")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, dict) and isinstance(decoded.get("input"), str):
            return decoded["input"]
        if isinstance(decoded, str):
            return decoded
        return value
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Offline fake relay (self-check of the probe, no network / no credential)
# --------------------------------------------------------------------------- #


def _fake_relay() -> Tuple[ThreadingHTTPServer, str]:
    state = {"turn": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            state["turn"] += 1
            turn = state["turn"]
            # The bridge lowers history before forwarding, so the relay sees
            # ``function_call`` / ``function_call_output``, never the custom forms.
            asked = [
                i for i in body.get("input", [])
                if i.get("type") in ("custom_tool_call", "function_call")
            ]
            outputs = [
                i.get("output") for i in body.get("input", [])
                if i.get("type") in ("custom_tool_call_output", "function_call_output")
            ]
            has_tools = bool(body.get("tools"))
            declared = [
                t.get("name") for t in (body.get("tools") or []) if isinstance(t, dict)
            ]
            if asked:
                output = [{
                    "type": "message", "role": "assistant", "id": "m%d" % turn,
                    "content": [{"type": "output_text",
                                 "text": "The tool returned %s" % (outputs[-1] if outputs else "")}],
                }]
            else:
                output = [{
                    "type": "function_call", "id": "fc%d" % turn, "name": PROBE_TOOL,
                    "call_id": "call_fake_%d" % turn,
                    "arguments": json.dumps({"input": PROBE_INPUT}), "status": "completed",
                }]
            response = {"id": "resp_fake_%d" % turn, "status": "completed", "output": output,
                        "fake_declared_tools": declared, "fake_saw_top_level_tools": has_tools}
            raw = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, "http://127.0.0.1:%d" % server.server_address[1]


# --------------------------------------------------------------------------- #
# Scenario
# --------------------------------------------------------------------------- #


def run_scenario(
    *,
    endpoint: str,
    path: str,
    model: str,
    effort: str,
    credential: Optional[str],
) -> dict:
    record: Dict[str, Any] = {"path": path, "effort": effort}

    # ---- turn 1: declare the custom tool and ask for it --------------------
    prompt = (
        "Call the `%s` tool now, passing exactly the input value %r, then tell me "
        "the exact string it returned. You must call the tool; do not answer from "
        "memory." % (PROBE_TOOL, PROBE_INPUT)
    )
    turn1 = build_turn(path=path, model=model, effort=effort, prompt=prompt)
    status, _ctype, raw = post(endpoint, turn1, credential)
    event_types, items = sse_final_output(raw)
    record["turn1"] = {
        "http_status": status,
        "sse_events": event_types,
        "client_stream_requested": turn1["stream"],
    }
    if status != 200:
        record["turn1"]["error_preview"] = l2_credentials.redact(
            raw.decode("utf-8", "replace")[:400], credential
        )
        record["closed_loop"] = False
        record["stopped_at"] = "turn1_http"
        return record

    call = first_call(items, PROBE_TOOL)
    if call is None:
        record["turn1"]["calls_seen"] = [
            {"type": i.get("type"), "name": i.get("name")}
            for i in items if i.get("type") in ("function_call", "custom_tool_call")
        ]
        record["closed_loop"] = False
        record["stopped_at"] = "turn1_no_custom_tool_call"
        record["turn1"]["text_preview"] = l2_credentials.redact(output_text(items)[:300], credential)
        return record

    value = custom_value(call)
    call_id = call.get("call_id")
    record["turn1"]["bridge_call"] = {"type": call.get("type"), "name": call.get("name")}
    record["turn1"]["call_id_present"] = isinstance(call_id, str) and bool(call_id)
    record["turn1"]["argument_matches"] = value == PROBE_INPUT

    # ---- execute only the whitelisted tool ---------------------------------
    executor = WHITELIST.get(str(call.get("name")))
    if executor is None:
        record["execution"] = {"executed": False, "reason": "not_whitelisted",
                               "requested": call.get("name")}
        record["closed_loop"] = False
        record["stopped_at"] = "tool_not_whitelisted"
        return record
    result = executor(value)
    record["execution"] = {
        "executed": True,
        "tool": call.get("name"),
        "ok": result == PROBE_PREFIX + PROBE_INPUT,
    }

    # ---- turn 2: history + re-declaration, expecting a reasoned answer ------
    followup_prompt = "What exact string did the tool return? Quote it verbatim."
    turn2 = build_turn(
        path=path, model=model, effort=effort, prompt=followup_prompt,
        call_id=call_id, tool_output=result,
    )
    status2, _ctype2, raw2 = post(endpoint, turn2, credential)
    types2, items2 = sse_final_output(raw2)
    text2 = output_text(items2)
    record["turn2"] = {
        "http_status": status2,
        "sse_events": types2,
        "history_items_sent": [
            i.get("type") for i in turn2["input"] if i.get("type", "").startswith("custom_tool")
        ],
        "echo_value_seen": PROBE_PREFIX + PROBE_INPUT in text2,
        "text_preview": l2_credentials.redact(text2[:300], credential),
    }
    if status2 != 200:
        record["turn2"]["error_preview"] = l2_credentials.redact(
            raw2.decode("utf-8", "replace")[:400], credential
        )

    record["closed_loop"] = bool(
        status == 200
        and record["turn1"]["argument_matches"]
        and record["turn1"]["call_id_present"]
        and record["execution"]["ok"]
        and status2 == 200
        and record["turn2"]["echo_value_seen"]
    )
    if not record["closed_loop"]:
        record["stopped_at"] = "turn2_no_echo" if status2 == 200 else "turn2_http"
    return record


def summarise(record: dict) -> None:
    print("  路径：%s / 档位：%s" % (record["path"], record["effort"]))
    turn1 = record.get("turn1", {})
    print("    turn1 HTTP %s，SSE 事件：%s" % (turn1.get("http_status"),
                                          ", ".join(turn1.get("sse_events") or []) or "(空)"))
    if "bridge_call" in turn1:
        print("    桥返回调用：type=%s name=%s call_id=%s 参数匹配=%s"
              % (turn1["bridge_call"]["type"], turn1["bridge_call"]["name"],
                 turn1.get("call_id_present"), turn1.get("argument_matches")))
    if "execution" in record:
        print("    白名单执行：%s" % json.dumps(record["execution"], ensure_ascii=False))
    if "turn2" in record:
        print("    turn2 HTTP %s，回显命中=%s" % (record["turn2"].get("http_status"),
                                              record["turn2"].get("echo_value_seen")))
    if record.get("closed_loop"):
        print("    => 协议闭环成立")
    else:
        print("    => 未闭环，停在：%s" % record.get("stopped_at"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="L2 真实中转协议闭环（经本地桥）")
    parser.add_argument("--upstream", help="真实中转 base_url，如 https://gflabtoken.cn")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--effort", default="low",
                        help="主测试推理档位（DeepSeek V4.1 Flash：low/high/max）")
    parser.add_argument("--extra-effort", default="high",
                        help="单独另测的档位，不混入主测试变量；留空则跳过")
    parser.add_argument("--path", default="both",
                        choices=[ADDITIONAL_TOOLS, TOP_LEVEL_TOOLS, "both"])
    parser.add_argument("--credential", default="none",
                        choices=list(l2_credentials.SOURCES))
    parser.add_argument("--credential-env", help="--credential env 时的变量名")
    parser.add_argument("--credential-codex-home", default=l2_credentials.DEFAULT_CODEX_HOME)
    parser.add_argument("--record", help="桥的脱敏取证 JSONL 输出路径")
    parser.add_argument("--out", help="本轮证据 JSON 输出路径")
    parser.add_argument("--port", type=int, default=0, help="本地桥端口（默认随机）")
    parser.add_argument("--fake", action="store_true",
                        help="使用内置假中转自检探针本身（不需要网络与凭据）")
    args = parser.parse_args(argv)

    credential = None if args.fake else l2_credentials.resolve(
        args.credential,
        env_name=args.credential_env,
        codex_home=args.credential_codex_home,
    )

    fake_server = None
    if args.fake:
        fake_server, relay_url = _fake_relay()
        threading.Thread(target=fake_server.serve_forever, daemon=True).start()
        upstream = relay_url
        print("模式：内置假中转自检 -> %s" % upstream)
    else:
        upstream = (args.upstream or "").rstrip("/")
        if not upstream.startswith(("http://", "https://")):
            parser.error("需要 --upstream http(s)://... 或使用 --fake")
        print("模式：真实中转 -> %s" % upstream)
    print("凭据：%s" % l2_credentials.describe(credential))
    print("TLS 校验：保持默认开启（未做任何关闭）")

    bridge = BridgeServer(
        upstream_url=upstream, port=args.port, record_path=args.record
    )
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    if not bridge.wait_ready():
        print("桥未能启动")
        return 2
    endpoint = "http://127.0.0.1:%d/responses" % bridge.bound_port
    print("本地桥：%s（仅 loopback）" % endpoint)
    print()

    paths = [ADDITIONAL_TOOLS, TOP_LEVEL_TOOLS] if args.path == "both" else [args.path]
    scenarios: List[dict] = []
    try:
        for path in paths:
            print("[主测试] 路径=%s 档位=%s" % (path, args.effort))
            record = run_scenario(endpoint=endpoint, path=path, model=args.model,
                                  effort=args.effort, credential=credential)
            summarise(record)
            scenarios.append(record)
            print()

        if args.extra_effort:
            print("[另测] 档位=%s（独立变量，改变档位）" % args.extra_effort)
            record = run_scenario(endpoint=endpoint, path=ADDITIONAL_TOOLS, model=args.model,
                                  effort=args.extra_effort, credential=credential)
            record["is_extra_effort"] = True
            summarise(record)
            scenarios.append(record)
            print()
    finally:
        bridge.stop()
        if fake_server is not None:
            fake_server.shutdown()
            fake_server.server_close()

    evidence = {
        "schema": "l2-protocol-loop/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "mode": "fake-relay-selfcheck" if args.fake else "real-relay",
        "upstream_host": urlsplit(upstream).hostname,
        "model": args.model,
        "credential_source": "none" if args.fake else args.credential,
        "credential_loaded": credential is not None,
        "tls_verification": "enabled",
        "bridge_record_path": args.record,
        "scenarios": scenarios,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("证据已写入：%s" % args.out)

    main_failed = [s for s in scenarios if not s.get("is_extra_effort") and not s.get("closed_loop")]
    if main_failed:
        print("结果：主测试 %d 条未闭环。" % len(main_failed))
        return 1
    print("结果：主测试全部闭环。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
