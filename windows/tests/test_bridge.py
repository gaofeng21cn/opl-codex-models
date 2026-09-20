from __future__ import annotations

import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from codex_model_manager.core.bridge import (
    CUSTOM,
    FUNCTION,
    REASON_AMBIGUOUS,
    REASON_NAMESPACE_UNREGISTERED,
    REASON_UNDECLARED,
    BridgeServer,
    ToolRegistry,
    conversation_key,
    ensure_loopback,
    translate_request,
    translate_response,
)
from codex_model_manager.core.config_editor import (
    read_provider_base_url,
    set_provider_base_url,
    undo_provider_base_url,
)


def _request(custom_call=None):
    items = [{
        "type": "additional_tools",
        "role": "developer",
        "tools": [{
            "type": "namespace", "name": "functions", "tools": [{
                "type": "custom", "name": "exec", "description": "run code",
            }],
        }],
    }, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
    if custom_call:
        items.append(custom_call)
    return {"model": "deepseek-v4.1-flash", "input": items, "tools": [], "stream": True}


def _declarations(payload: dict, inherited=None):
    """Run the request rewrite and return the registration table it produced."""
    return translate_request(payload, inherited)[1]


def _session(namespace: str, name: str, kind: str) -> dict:
    """A minimal request whose tool tree uses a distinct namespace path."""
    return {
        "model": "deepseek-v4.1-flash",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{
                    "type": "namespace",
                    "name": namespace,
                    "tools": [{"type": kind, "name": name, "description": "d"}],
                }],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
        ],
        "stream": False,
    }


def _single_call(name: str, arguments: str = '{"input":"pwd"}', call_id: str = "c1") -> dict:
    return {"output": [{"type": "function_call", "name": name, "call_id": call_id,
                        "arguments": arguments}]}


def test_translate_request_flattens_custom_exec_and_removes_envelope():
    payload, declarations = translate_request(_request())
    assert declarations.custom_names == {"exec"}
    assert [x["type"] for x in payload["input"]] == ["message"]
    assert len(payload["tools"]) == 1
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "exec"
    assert tool["parameters"]["required"] == ["input"]


def test_translate_request_maps_custom_followup_to_function_call():
    payload, declarations = translate_request(_request({
        "type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": "return 1",
    }))
    call = payload["input"][-1]
    assert declarations.custom_names == {"exec"}
    assert call["type"] == "function_call"
    assert json.loads(call["arguments"]) == {"input": "return 1"}


def test_translate_response_maps_only_custom_function_calls():
    declarations = _declarations(_session("functions", "exec", "custom"))
    translated = translate_response(_single_call("exec"), declarations)
    out = translated["output"]
    assert out[0]["type"] == "custom_tool_call"
    assert out[0]["input"] == "pwd"
    # ``wait`` was never declared, so it must stay a function_call.
    response = translate_response(_single_call("wait", '{"seconds":1}'), declarations)
    assert response["output"][0]["type"] == "function_call"


def test_provider_base_url_enable_and_undo_preserve_config(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '# keep me\nmodel_provider = "OpenAI"\n\n'
        '[model_providers.OpenAI]\nname = "Relay"\nbase_url = "https://upstream.example"\n',
        encoding="utf-8",
    )
    backup = tmp_path / "backups"
    previous = set_provider_base_url(str(config), "OpenAI", "http://127.0.0.1:8787", str(backup))
    assert previous == "https://upstream.example"
    assert "# keep me" in config.read_text(encoding="utf-8")
    result = undo_provider_base_url(
        str(config), "OpenAI", previous, "http://127.0.0.1:8787", str(backup)
    )
    assert "已恢复" in result
    assert "https://upstream.example" in config.read_text(encoding="utf-8")


def test_provider_base_url_undo_refuses_conflict(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[model_providers.OpenAI]\nbase_url = "http://changed"\n', encoding="utf-8")
    result = undo_provider_base_url(
        str(config), "OpenAI", "https://old", "http://127.0.0.1:8787", str(tmp_path / "b")
    )
    assert result.startswith("冲突：")


# --------------------------------------------------------------------------- #
# Top-level tools path (use_responses_lite=false)
# --------------------------------------------------------------------------- #


def test_translate_request_lowers_top_level_custom_tool():
    payload = {
        "model": "deepseek-v4.1-flash",
        "input": [{"type": "message", "role": "user", "content": []}],
        "tools": [{"type": "custom", "name": "exec", "description": "run code"}],
        "stream": False,
    }
    out, declarations = translate_request(payload)
    assert declarations.custom_names == {"exec"}
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["name"] == "exec"
    assert out["tools"][0]["parameters"]["required"] == ["input"]
    # A top-level custom tool has no namespace, so only its leaf name is admissible.
    assert "exec" in declarations.lookup
    assert "functions__exec" not in declarations.lookup


def test_translate_request_dedupes_top_level_and_additional_tools():
    payload = _request()
    payload["tools"] = [{"type": "custom", "name": "exec", "description": "dup"}]
    out, declarations = translate_request(payload)
    assert declarations.custom_names == {"exec"}
    assert [t["name"] for t in out["tools"]] == ["exec"]


# --------------------------------------------------------------------------- #
# Cross-turn continuation is explicit: the request's own declarations win
# --------------------------------------------------------------------------- #


def test_translate_request_lowers_history_without_tool_declaration():
    payload = {
        "model": "deepseek-v4.1-flash",
        "input": [
            {"type": "custom_tool_call", "name": "exec", "call_id": "c9", "input": "x"},
            {"type": "custom_tool_call_output", "call_id": "c9", "output": "y"},
        ],
    }
    inherited = _declarations(_session("functions", "exec", "custom"))
    out, declarations = translate_request(payload, inherited)
    assert declarations.custom_names == {"exec"}
    assert [i["type"] for i in out["input"]] == ["function_call", "function_call_output"]
    assert json.loads(out["input"][0]["arguments"]) == {"input": "x"}
    assert out["input"][1]["call_id"] == "c9"
    # No tool declaration this turn -> the bridge must not invent an empty tools[].
    assert "tools" not in out


def test_history_custom_call_does_not_authorise_response_translation():
    """A past call is a record of what happened, not a licence for the answer."""
    payload = {
        "model": "deepseek-v4.1-flash",
        "input": [
            {"type": "custom_tool_call", "name": "exec", "call_id": "c9", "input": "x"},
            {"type": "custom_tool_call_output", "call_id": "c9", "output": "y"},
        ],
    }
    # No inherited table, no declaration: lowering history still happens ...
    out, declarations = translate_request(payload)
    assert out["input"][0]["type"] == "function_call"
    # ... but the table stays empty, so the relay's answer is NOT rewritten.
    assert declarations.lookup == {}
    diagnostics: list[dict] = []
    translated = translate_response(_single_call("exec"), declarations, diagnostics)
    assert translated["output"][0]["type"] == "function_call"
    assert diagnostics == [{"name": "exec", "reason": REASON_UNDECLARED}]


def test_translate_request_drops_empty_tools_array():
    payload = {
        "model": "deepseek-v4.1-flash",
        "input": [{"type": "message", "role": "user", "content": []}],
        "tools": [],
    }
    out, declarations = translate_request(payload)
    assert declarations.custom_names == set()
    assert "tools" not in out, "空 tools[] 不应透传给严格中转"


# --------------------------------------------------------------------------- #
# Explicit registration mapping: namespace-joined names are resolved, not guessed
#
# Captured against the real relay: Codex declares ``functions.exec`` as a custom
# tool inside ``additional_tools``; the model answers with
# ``function_call(name="functions__exec")``.  That spelling is the legitimate
# namespace-qualified product of a declaration, so it must be restored to
# ``custom_tool_call(name="exec")`` -- not rejected, and not passed through.
# --------------------------------------------------------------------------- #


def test_namespace_joined_name_is_restored_through_registration():
    declarations = _declarations(_session("functions", "exec", "custom"))
    assert set(declarations.lookup) == {"exec", "functions__exec"}

    diagnostics: list[dict] = []
    out = translate_response(
        _single_call("functions__exec", '{"input":"ls -la"}'), declarations, diagnostics
    )
    item = out["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "exec", "命名空间限定名必须还原为可调用名"
    assert item["input"] == "ls -la"
    assert item["call_id"] == "c1"
    assert diagnostics == []


def test_namespace_joined_name_of_a_plain_function_is_not_restored():
    declarations = _declarations(_session("functions", "wait", FUNCTION))
    assert "functions__wait" in declarations.lookup
    out = translate_response(_single_call("functions__wait", '{"seconds":1}'), declarations)
    item = out["output"][0]
    assert item["type"] == "function_call", "普通 function 工具没有 custom 语义可还原"
    # Namespace qualification is transport syntax; the client registry expects
    # the leaf name for ordinary functions too.  Only the type remains a regular
    # function_call (custom semantics are reserved for custom registrations).
    assert item["name"] == "wait"


def test_namespace_joined_plain_function_preserves_arguments_and_call_id():
    declarations = _declarations(_session("functions", "wait", FUNCTION))
    out = translate_response(
        _single_call("functions__wait", '{"seconds":2}', call_id="wait-1"),
        declarations,
    )
    item = out["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "wait"
    assert item["call_id"] == "wait-1"
    assert item["arguments"] == '{"seconds":2}'


def test_new_tool_declarations_replace_previous_conversation_mapping():
    first = _session("functions", "exec", CUSTOM)
    second = _session("other", "wait", FUNCTION)
    inherited = _declarations(first)
    _out, declarations = translate_request(second, inherited)
    diagnostics = []
    out = translate_response(_single_call("functions__exec"), declarations, diagnostics)
    assert out["output"][0]["type"] == "function_call"
    assert diagnostics == [{
        "name": "functions__exec",
        "reason": REASON_NAMESPACE_UNREGISTERED,
    }]


def test_namespace_joined_name_without_registration_is_refused():
    declarations = _declarations(_session("functions", "exec", "custom"))
    diagnostics: list[dict] = []
    out = translate_response(
        _single_call("functions__run", '{"input":"rm -rf /"}'), declarations, diagnostics
    )
    item = out["output"][0]
    assert item["type"] == "function_call", "无映射不得转换为可执行调用"
    assert diagnostics == [{"name": "functions__run", "reason": REASON_NAMESPACE_UNREGISTERED}]


def test_ambiguous_registration_is_refused():
    """One spelling claimed by two different tools must never be resolved."""
    ambiguity = _session("functions", "exec", CUSTOM)
    ambiguity["input"].append({
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "function", "name": "functions__exec",
                   "description": "a literal tool with the same spelling"}],
    })
    declarations = _declarations(ambiguity)
    assert "functions__exec" in declarations.ambiguous

    diagnostics: list[dict] = []
    out = translate_response(
        _single_call("functions__exec", '{"input":"x"}'), declarations, diagnostics
    )
    assert out["output"][0]["type"] == "function_call"
    assert diagnostics == [{"name": "functions__exec", "reason": REASON_AMBIGUOUS}]


def test_ambiguous_leaf_also_refuses_the_plain_spelling():
    """``exec`` declared as both custom and function is ambiguous at the leaf too."""
    payload = _session("functions", "exec", CUSTOM)
    payload["input"].append({
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "function", "name": "exec", "description": "same leaf, other kind"}],
    })
    declarations = _declarations(payload)
    assert "exec" in declarations.ambiguous
    diagnostics: list[dict] = []
    out = translate_response(_single_call("exec"), declarations, diagnostics)
    assert out["output"][0]["type"] == "function_call"
    assert diagnostics[0]["reason"] == REASON_AMBIGUOUS


# --------------------------------------------------------------------------- #
# Per-conversation isolation: the same spelling may mean different things
# --------------------------------------------------------------------------- #


def test_two_conversations_same_name_different_kind_are_isolated():
    """Session A declares ``exec`` as custom; session B declares it as function.

    The registry is keyed per conversation, so A's registration must not leak
    into B: B's ``function_call(name="exec")`` is a genuine function call.
    """
    registry = ToolRegistry()
    key_a = conversation_key({"conversation_id": "session-A"})
    key_b = conversation_key({"conversation_id": "session-B"})
    assert key_a != key_b

    _out_a, declarations_a = translate_request(
        _session("functions", "exec", CUSTOM), registry.get(key_a)
    )
    registry.put(key_a, declarations_a)
    _out_b, declarations_b = translate_request(
        _session("alpha", "exec", FUNCTION), registry.get(key_b)
    )
    registry.put(key_b, declarations_b)

    # A: resolved through its own conversation table.
    out_a = translate_response(_single_call("exec"), registry.get(key_a))
    assert out_a["output"][0]["type"] == "custom_tool_call"
    assert out_a["output"][0]["name"] == "exec"

    # B: same letters, different tool -- must stay a function call.
    diagnostics: list[dict] = []
    out_b = translate_response(_single_call("exec"), registry.get(key_b), diagnostics)
    assert out_b["output"][0]["type"] == "function_call"
    assert diagnostics == []

    # And the namespace-joined spelling exists only in A's world.
    assert "functions__exec" in registry.get(key_a).lookup
    assert "functions__exec" not in registry.get(key_b).lookup
    assert "alpha__exec" in registry.get(key_b).lookup


def test_registry_does_not_serve_a_conversation_that_never_declared_tools():
    registry = ToolRegistry()
    registry.put(
        conversation_key({"conversation_id": "A"}),
        _declarations(_session("functions", "exec", CUSTOM)),
    )
    assert registry.get(conversation_key({"conversation_id": "B"})) is None
    assert registry.get(None) is None


def test_registry_expires_idle_entries():
    registry = ToolRegistry(ttl_seconds=0.0)
    registry.put("k", _declarations(_session("functions", "exec", CUSTOM)))
    assert registry.get("k") is None, "过期条目不得再参与还原"


def test_registry_is_bounded():
    registry = ToolRegistry(max_entries=2)
    declarations = _declarations(_session("functions", "exec", CUSTOM))
    for index in range(5):
        registry.put("k%d" % index, declarations)
    assert len(registry) == 2
    assert registry.get("k0") is None, "最先写入的条目应被 LRU 淘汰"


def test_previous_response_id_continuation_is_explicit_and_bounded():
    """A chained follow-up inherits only the response the bridge itself answered."""
    registry = ToolRegistry()
    first = _session("functions", "exec", CUSTOM)
    _out, declarations = translate_request(first, registry.get(conversation_key(first)))
    registry.put("prev:resp_001", declarations)

    followup = {
        "model": "deepseek-v4.1-flash",
        "previous_response_id": "resp_001",
        "input": [{"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": "x"}],
        "stream": False,
    }
    assert conversation_key(followup) == "prev:resp_001"
    _out2, declarations2 = translate_request(followup, registry.get(conversation_key(followup)))
    out = translate_response(_single_call("exec"), declarations2)
    assert out["output"][0]["type"] == "custom_tool_call"

    # An unknown response id carries no authority at all.
    orphan = dict(followup, previous_response_id="resp_unknown")
    _out3, declarations3 = translate_request(orphan, registry.get(conversation_key(orphan)))
    assert declarations3.custom_names == set()
    assert translate_response(
        _single_call("exec"), declarations3
    )["output"][0]["type"] == "function_call"


# --------------------------------------------------------------------------- #
# Loopback enforcement is in the data model, not only in the CLI
# --------------------------------------------------------------------------- #


def test_ensure_loopback_rejects_non_loopback_hosts():
    for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
        raised = False
        try:
            ensure_loopback(host)
        except ValueError:
            raised = True
        assert raised, "应当拒绝非 loopback 主机：%r" % host
    assert ensure_loopback("127.0.0.1") == "127.0.0.1"
    assert ensure_loopback("localhost") == "localhost"
    assert ensure_loopback("::1") == "::1"


def test_bridge_server_refuses_public_bind():
    raised = False
    try:
        BridgeServer(upstream_url="http://127.0.0.1:1", host="0.0.0.0")
    except ValueError:
        raised = True
    assert raised, "BridgeServer 不应接受 0.0.0.0"


def test_run_bridge_refuses_public_bind():
    from codex_model_manager.core.bridge import run_bridge

    raised = False
    try:
        run_bridge("http://127.0.0.1:1", host="0.0.0.0", port=0)
    except ValueError:
        raised = True
    assert raised, "run_bridge 不应接受 0.0.0.0"


# --------------------------------------------------------------------------- #
# L1 end-to-end: Codex-shaped request -> bridge -> fake relay -> SSE back
# --------------------------------------------------------------------------- #


class _FakeRelay:
    """Stands in for a DeepSeek-style relay that understands only functions.

    Records every request body it receives so the test can assert on the exact
    wire shape the bridge produced.
    """

    def __init__(self, reply_factory, status: int = 200):
        self.requests: list[dict] = []
        self._reply_factory = reply_factory
        self._status = status
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                relay.requests.append(json.loads(self.rfile.read(length)))
                raw = json.dumps(relay._reply_factory(len(relay.requests))).encode("utf-8")
                self.send_response(relay._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


def _post(url: str, payload: dict) -> tuple[int, str, bytes]:
    """POST and return (status, content-type, body), treating 4xx/5xx as data."""
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _sse_events(raw: bytes) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in raw.decode("utf-8").splitlines()
        if line.startswith("data: ")
    ]


def _relay_reply(index: int) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_%d" % index,
            "output": [
                {"type": "function_call", "name": "exec", "call_id": "call_%d" % index,
                 "arguments": '{"input":"print(%d)"}' % index}
            ],
        },
    }


def test_bridge_end_to_end_lowers_custom_tool_and_emits_sse():
    with _FakeRelay(_relay_reply) as relay:
        bridge = BridgeServer(upstream_url=relay.url, port=0)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            status, content_type, raw = _post(
                "http://127.0.0.1:%d/responses" % bridge.bound_port, _request()
            )
        finally:
            bridge.stop()

    # Upstream saw a plain function tool, no additional_tools envelope, no stream.
    sent = relay.requests[0]
    assert [item["type"] for item in sent["input"]] == ["message"]
    assert [t["type"] for t in sent["tools"]] == ["function"]
    assert sent["tools"][0]["name"] == "exec"
    assert sent["stream"] is False

    # Codex got SSE whose custom call preserves call_id byte-for-byte.
    assert status == 200
    assert content_type.startswith("text/event-stream")
    events = _sse_events(raw)
    assert events[0]["type"] == "response.created"
    assert events[0]["response"]["output"] == []
    assert events[0]["response"]["status"] == "in_progress"
    # Regression: a real Codex client assembles output items from
    # ``response.output_item.done``.  The literal ``response.output_item.added``
    # does not occur in the 0.155.0-alpha.9.2 binary, so announcing an item only
    # with ``added`` made Codex silently drop the tool call and end the turn.
    names = [event["type"] for event in events]
    assert "response.output_item.done" in names
    done = events[names.index("response.output_item.done")]
    assert done["item"]["type"] == "custom_tool_call"
    assert done["item"]["name"] == "exec"
    assert done["item"]["call_id"] == "call_1"
    assert done["item"]["input"] == "print(1)"
    # Every event carries the sequence number the client parses.
    assert [event["sequence_number"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert events[-1]["type"] == "response.completed"
    item = events[-1]["response"]["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "exec"
    assert item["call_id"] == "call_1"
    assert item["input"] == "print(1)"


def _followup_turn(namespace: str, kind: str, call_id: str) -> dict:
    """A second turn as Codex actually sends it: declarations re-sent + history.

    Captured behaviour (codex-cli 0.155.0-alpha.9.2): ``additional_tools`` is
    present on *every* turn and ``previous_response_id`` is never sent, so the
    declaration -- not a server-side memory -- is what must carry the semantics.
    """
    return {
        "model": "deepseek-v4.1-flash",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{
                    "type": "namespace", "name": namespace,
                    "tools": [{"type": kind, "name": "exec", "description": "run code"}],
                }],
            },
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "go"}]},
            {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
             "input": "print(1)"},
            {"type": "custom_tool_call_output", "call_id": call_id, "output": "1"},
        ],
        "stream": True,
    }


def test_bridge_end_to_end_keeps_custom_semantics_on_second_turn():
    with _FakeRelay(_relay_reply) as relay:
        bridge = BridgeServer(upstream_url=relay.url, port=0)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            base = "http://127.0.0.1:%d/responses" % bridge.bound_port
            _post(base, _request())  # turn 1 declares the custom tool
            status, _content_type, raw = _post(
                base, _followup_turn("functions", "custom", "call_1")
            )
        finally:
            bridge.stop()

    second = relay.requests[1]
    assert [item["type"] for item in second["input"]] == [
        "message", "function_call", "function_call_output",
    ]
    assert json.loads(second["input"][1]["arguments"]) == {"input": "print(1)"}
    assert second["input"][2]["call_id"] == "call_1"

    # The relay answers with a bare function_call; the bridge must turn it back
    # into a custom call using this turn's own declaration.
    assert status == 200
    item = _sse_events(raw)[-1]["response"]["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["call_id"] == "call_2"


def test_bridge_end_to_end_isolates_two_sessions_using_exec_differently():
    """Two live sessions through one bridge: same name, different tool kinds."""
    # The relay answers both sessions with the same bare function_call(name=exec).
    with _FakeRelay(_relay_reply) as relay:
        bridge = BridgeServer(upstream_url=relay.url, port=0)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            base = "http://127.0.0.1:%d/responses" % bridge.bound_port
            status_a, _ct, raw_a = _post(
                base, dict(_followup_turn("functions", "custom", "call_a"),
                           conversation_id="session-A")
            )
            status_b, _ct, raw_b = _post(
                base, dict(_followup_turn("alpha", "function", "call_b"),
                           conversation_id="session-B")
            )
        finally:
            bridge.stop()

    assert status_a == 200 and status_b == 200
    item_a = _sse_events(raw_a)[-1]["response"]["output"][0]
    item_b = _sse_events(raw_b)[-1]["response"]["output"][0]
    assert item_a["type"] == "custom_tool_call", "A 会话的 exec 是 custom 工具，应还原"
    assert item_b["type"] == "function_call", "B 会话的 exec 是普通 function，不得被 A 的注册影响"

    # Both upstream requests still saw a plain function tool named exec.
    for sent in (relay.requests[0], relay.requests[1]):
        assert sent["tools"][0]["name"] == "exec"
        assert sent["tools"][0]["type"] == "function"


def test_bridge_end_to_end_restores_namespace_joined_call():
    """The real relay returned ``functions__exec``; that must reach Codex as exec."""
    def reply(_index: int) -> dict:
        return {
            "type": "response.completed",
            "response": {
                "id": "resp_ns",
                "output": [
                    {"type": "function_call", "name": "functions__exec", "call_id": "call_ns",
                     "arguments": '{"input":"ls -la"}'}
                ],
            },
        }

    with _FakeRelay(reply) as relay:
        bridge = BridgeServer(upstream_url=relay.url, port=0)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            status, _ct, raw = _post(
                "http://127.0.0.1:%d/responses" % bridge.bound_port,
                _followup_turn("functions", "custom", "call_1"),
            )
        finally:
            bridge.stop()

    assert status == 200
    item = _sse_events(raw)[-1]["response"]["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "exec"
    assert item["input"] == "ls -la"
    assert item["call_id"] == "call_ns"


def test_bridge_end_to_end_surfaces_relay_error_without_faking_success():
    def _error(_index: int) -> dict:
        return {"error": {"message": "Unsupported custom tool: 'exec'"}}

    with _FakeRelay(_error, status=400) as relay:
        bridge = BridgeServer(upstream_url=relay.url, port=0)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            status, _ct, raw = _post(
                "http://127.0.0.1:%d/responses" % bridge.bound_port, _request()
            )
        finally:
            bridge.stop()

    # A rejected tool must surface as an error, never as a successful no-op.
    assert status == 400
    assert b"Unsupported custom tool" in raw


# --------------------------------------------------------------------------- #
# CLI switch: off by default, enable, status, disable (sandboxed config only)
# --------------------------------------------------------------------------- #


def _codex_config(tmp_path: Path) -> Path:
    path = tmp_path / "codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# keep this comment\nmodel_provider = "DeepSeek"\n\n'
        '[model_providers.DeepSeek]\nname = "Relay"\n'
        'base_url = "https://relay.example/v1"\nwire_api = "responses"\n',
        encoding="utf-8",
    )
    return path


def _app_config(tmp_path: Path, codex_config: Path) -> Path:
    app = tmp_path / "app.json"
    app.write_text(
        json.dumps(
            {
                "customSourcePath": str(tmp_path / "custom-models.json"),
                "mergedCatalogPath": str(tmp_path / "models.json"),
                "syncLogPath": str(tmp_path / "logs" / "sync.jsonl"),
                "errorLogPath": str(tmp_path / "logs" / "sync.error.log"),
                "backupDirectoryPath": str(tmp_path / "backups"),
                "codexConfigPath": str(codex_config),
            }
        ),
        encoding="utf-8",
    )
    return app


def test_cli_bridge_is_off_by_default_and_round_trips(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    codex_config = _codex_config(tmp_path)
    app = _app_config(tmp_path, codex_config)
    original = codex_config.read_text(encoding="utf-8")

    assert main(["--config", str(app), "bridge", "status"]) == 0
    assert "关闭（默认）" in capsys.readouterr().out

    assert main(["--config", str(app), "bridge", "enable"]) == 0
    assert "已启用本地桥" in capsys.readouterr().out
    after_enable = codex_config.read_text(encoding="utf-8")
    assert "base_url = \"http://127.0.0.1:8787\"" in after_enable
    assert "# keep this comment" in after_enable, "启用不得丢注释"
    assert list((tmp_path / "backups").glob("*.bak")), "启用前必须留备份"

    # A second enable must not replace the original upstream URL in the undo
    # record with the already-applied loopback URL.
    assert main(["--config", str(app), "bridge", "enable"]) == 0
    assert "未重复写入" in capsys.readouterr().out
    assert main(["--config", str(app), "bridge", "disable"]) == 0
    assert codex_config.read_text(encoding="utf-8") == original
    assert "已恢复" in capsys.readouterr().out


def test_cli_bridge_disable_without_record_fails_cleanly(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    app = _app_config(tmp_path, _codex_config(tmp_path))
    assert main(["--config", str(app), "bridge", "disable"]) == 1
    assert "没有已记录的本地桥配置" in capsys.readouterr().err


def test_cli_bridge_start_refuses_public_host_before_announcing(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    app = _app_config(tmp_path, _codex_config(tmp_path))
    rc = main(
        [
            "--config", str(app), "bridge", "start",
            "--upstream", "https://relay.example/v1", "--host", "0.0.0.0",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "运行中" not in captured.out, "被拒绝的绑定不得先宣告为已启动"
    assert "loopback" in captured.err


def test_cli_bridge_start_rejects_non_http_upstream(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    app = _app_config(tmp_path, _codex_config(tmp_path))
    rc = main(
        ["--config", str(app), "bridge", "start", "--upstream", "relay.example/v1"]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "http://" in captured.err


# --------------------------------------------------------------------------- #
# Shared provider resolution (CLI + GUI must not drift apart)
# --------------------------------------------------------------------------- #


def test_read_provider_base_url_uses_toplevel_provider(tmp_path: Path):
    config = _codex_config(tmp_path)
    assert read_provider_base_url(str(config)) == ("DeepSeek", "https://relay.example/v1")
    assert read_provider_base_url(str(config), "DeepSeek") == (
        "DeepSeek",
        "https://relay.example/v1",
    )


def test_read_provider_base_url_reports_missing_provider(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model_provider = "Ghost"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        read_provider_base_url(str(config))


# --------------------------------------------------------------------------- #
# Demo sandbox: the switch must not become a second door to a real config
# --------------------------------------------------------------------------- #


def _demo_app_config(tmp_path: Path, target_config: Path) -> Path:
    """App config whose sandbox is `<tmp>/app` but whose target lives outside it."""
    sandbox = tmp_path / "app"
    sandbox.mkdir(exist_ok=True)
    app = tmp_path / "app.json"
    app.write_text(
        json.dumps(
            {
                "customSourcePath": str(sandbox / "custom-models.json"),
                "mergedCatalogPath": str(sandbox / "models.json"),
                "syncLogPath": str(sandbox / "Logs" / "sync.jsonl"),
                "errorLogPath": str(sandbox / "Logs" / "sync.error.log"),
                "backupDirectoryPath": str(sandbox / "Backups"),
                "codexConfigPath": str(target_config),
                "isDemo": True,
            }
        ),
        encoding="utf-8",
    )
    return app


def test_cli_bridge_enable_refuses_to_escape_demo_sandbox(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    target = tmp_path / "real" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text(
        'model_provider = "DeepSeek"\n\n'
        '[model_providers.DeepSeek]\nbase_url = "https://relay.example/v1"\n',
        encoding="utf-8",
    )
    original = target.read_text(encoding="utf-8")
    app = _demo_app_config(tmp_path, target)

    rc = main(["--config", str(app), "bridge", "enable"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "越界" in captured.err or "沙箱" in captured.err
    assert target.read_text(encoding="utf-8") == original, "越界目标不得被改写"


def test_cli_bridge_enable_allows_target_inside_demo_sandbox(tmp_path: Path, capsys):
    from codex_model_manager.cli import main

    sandbox = tmp_path / "app"
    sandbox.mkdir()
    target = sandbox / "target_config.toml"
    target.write_text(
        'model_provider = "DeepSeek"\n\n'
        '[model_providers.DeepSeek]\nbase_url = "https://relay.example/v1"\n',
        encoding="utf-8",
    )
    app = tmp_path / "app.json"
    app.write_text(
        json.dumps(
            {
                "customSourcePath": str(sandbox / "custom-models.json"),
                "mergedCatalogPath": str(sandbox / "models.json"),
                "syncLogPath": str(sandbox / "Logs" / "sync.jsonl"),
                "errorLogPath": str(sandbox / "Logs" / "sync.error.log"),
                "backupDirectoryPath": str(sandbox / "Backups"),
                "codexConfigPath": str(target),
                "isDemo": True,
            }
        ),
        encoding="utf-8",
    )

    assert main(["--config", str(app), "bridge", "enable"]) == 0
    assert 'base_url = "http://127.0.0.1:8787"' in target.read_text(encoding="utf-8")
    assert list((sandbox / "Backups").glob("*.bak")), "沙箱内写入也必须先备份"


def test_gui_bridge_enable_refuses_to_escape_demo_sandbox(tmp_path: Path, monkeypatch):
    import codex_model_manager.gui.app as gui_app
    from codex_model_manager.core.app_config import AppConfiguration

    target = tmp_path / "real" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text(
        'model_provider = "DeepSeek"\n\n'
        '[model_providers.DeepSeek]\nbase_url = "https://relay.example/v1"\n',
        encoding="utf-8",
    )
    original = target.read_text(encoding="utf-8")
    app_path = _demo_app_config(tmp_path, target)
    config = AppConfiguration.from_json(json.loads(app_path.read_text(encoding="utf-8")))

    stub = _StubMessagebox(answer=True)
    monkeypatch.setattr(gui_app, "messagebox", stub)

    gui_app.CodexModelManagerApp.bridge_enable(_gui_app(config, app_path))
    assert stub.dialogs("showerror"), "越界时必须明确报错"
    assert config.bridge_enabled is False
    assert target.read_text(encoding="utf-8") == original, "越界目标不得被改写"


# --------------------------------------------------------------------------- #
# GUI switch (display-free: the methods are called on a stub object)
# --------------------------------------------------------------------------- #


class _StubStatus:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value) -> None:
        self.value = value


class _StubMessagebox:
    """Answers every dialog and records what the user would have seen."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.shown: list[dict] = []

    def _record(self, kind: str, title: str, message: str) -> str:
        self.shown.append({"kind": kind, "title": title, "message": message})
        return message

    def askyesno(self, title, message, **_kw) -> bool:
        self._record("askyesno", title, message)
        return self.answer

    def showinfo(self, title, message, **_kw) -> str:
        return self._record("showinfo", title, message)

    def showwarning(self, title, message, **_kw) -> str:
        return self._record("showwarning", title, message)

    def showerror(self, title, message, **_kw) -> str:
        return self._record("showerror", title, message)

    def dialogs(self, kind: str) -> list[str]:
        return [d["message"] for d in self.shown if d["kind"] == kind]


def _gui_app(config, app_config_path: Path):
    return types.SimpleNamespace(
        _config=config,
        _config_error=None,
        config_url=str(app_config_path),
        status=_StubStatus(),
    )


def _load_app_config(tmp_path: Path, codex_config: Path):
    from codex_model_manager.core.app_config import AppConfiguration

    app_path = _app_config(tmp_path, codex_config)
    return AppConfiguration.from_json(json.loads(app_path.read_text(encoding="utf-8"))), app_path


def test_gui_bridge_enable_previews_then_disable_restores(tmp_path: Path, monkeypatch):
    import codex_model_manager.gui.app as gui_app

    codex_config = _codex_config(tmp_path)
    config, app_path = _load_app_config(tmp_path, codex_config)
    original = codex_config.read_text(encoding="utf-8")

    stub = _StubMessagebox(answer=True)
    monkeypatch.setattr(gui_app, "messagebox", stub)
    app = _gui_app(config, app_path)

    gui_app.CodexModelManagerApp.bridge_enable(app)
    preview = stub.dialogs("askyesno")
    assert preview and "-> http://127.0.0.1:8787" in preview[0], "写入前必须先展示 diff"
    assert config.bridge_enabled is True
    assert config.bridge_provider == "DeepSeek"
    assert config.bridge_upstream_url == "https://relay.example/v1"
    after_enable = codex_config.read_text(encoding="utf-8")
    assert 'base_url = "http://127.0.0.1:8787"' in after_enable
    assert "# keep this comment" in after_enable

    gui_app.CodexModelManagerApp.bridge_disable(app)
    assert config.bridge_enabled is False
    assert codex_config.read_text(encoding="utf-8") == original


def test_gui_bridge_enable_declined_writes_nothing(tmp_path: Path, monkeypatch):
    import codex_model_manager.gui.app as gui_app

    codex_config = _codex_config(tmp_path)
    config, app_path = _load_app_config(tmp_path, codex_config)
    original = codex_config.read_text(encoding="utf-8")

    stub = _StubMessagebox(answer=False)
    monkeypatch.setattr(gui_app, "messagebox", stub)

    gui_app.CodexModelManagerApp.bridge_enable(_gui_app(config, app_path))
    assert config.bridge_enabled is False
    assert codex_config.read_text(encoding="utf-8") == original, "取消后不得有任何写入"


def test_gui_bridge_enable_without_target_config_refuses(tmp_path: Path, monkeypatch):
    import codex_model_manager.gui.app as gui_app
    from codex_model_manager.core.app_config import AppConfiguration

    config = AppConfiguration.from_json({"codexConfigPath": None})
    stub = _StubMessagebox(answer=True)
    monkeypatch.setattr(gui_app, "messagebox", stub)

    gui_app.CodexModelManagerApp.bridge_enable(_gui_app(config, tmp_path / "app.json"))
    assert stub.dialogs("showwarning"), "没有目标 config 时必须明确拒绝"
    assert config.bridge_enabled is False


def test_gui_bridge_disable_without_record_warns(tmp_path: Path, monkeypatch):
    import codex_model_manager.gui.app as gui_app

    codex_config = _codex_config(tmp_path)
    config, app_path = _load_app_config(tmp_path, codex_config)
    before = codex_config.read_text(encoding="utf-8")
    stub = _StubMessagebox(answer=True)
    monkeypatch.setattr(gui_app, "messagebox", stub)

    gui_app.CodexModelManagerApp.bridge_disable(_gui_app(config, app_path))
    assert stub.dialogs("showwarning")
    assert codex_config.read_text(encoding="utf-8") == before, "无记录时不得改动 config"


# ---------------------------------------------------------------------------
# Model scoping: a relay's base_url is provider-wide, so the bridge cannot be
# made per-model by configuration.  What it *can* do is decline to touch models
# it was not pointed at — byte-for-byte, in both directions.
# ---------------------------------------------------------------------------


class _RawRelay:
    """A relay that keeps the exact request bytes and answers with fixed bytes."""

    def __init__(self, reply: bytes, content_type: str = "application/json", status: int = 200):
        self.bodies: list[bytes] = []
        self._reply = reply
        self._content_type = content_type
        self._status = status
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                relay.bodies.append(self.rfile.read(length))
                self.send_response(relay._status)
                self.send_header("Content-Type", relay._content_type)
                self.send_header("Content-Length", str(len(relay._reply)))
                self.end_headers()
                self.wfile.write(relay._reply)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


def _post_exact(url: str, raw: bytes, content_type: str = "application/json"):
    """POST bytes as-is (no re-serialisation) and return (status, ctype, body)."""
    request = Request(
        url,
        data=raw,
        headers={"Content-Type": content_type, "Authorization": "Bearer sk-test"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


@pytest.mark.parametrize("compacting", [False, True])
@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "gpt-6-astra"])
def test_history_item_ids_are_type_local_and_call_ids_survive(tmp_path, compacting, model):
    payload = _request()
    payload["model"] = model
    payload["stream"] = False
    history = [
        {"type": "custom_tool_call", "id": "ctc_probe", "name": "exec",
         "call_id": "call_probe", "input": "return 1"},
        {"type": "custom_tool_call_output", "id": "ctco_probe",
         "call_id": "call_probe", "output": [{"type": "input_text", "text": "1"}]},
        {"type": "function_call_output", "id": "fco_keep",
         "call_id": "call_other", "output": "unchanged"},
    ]
    payload["input"].extend(history)
    if compacting:
        payload["input"].append({"type": "compaction_trigger"})
    before = json.dumps(payload)
    relay, result, _, _ = _run_scope_case(
        tmp_path, scoped=frozenset({"deepseek-v4.1-flash"}), payload=payload,
        reply=b'{"id":"resp_probe","output":[],"status":"completed"}',
    )
    assert result[0] == 200
    sent = json.loads(relay.bodies[0])
    assert json.dumps(payload) == before
    if model == "gpt-6-astra" or compacting:
        assert relay.bodies[0] == before.encode("utf-8")
        return
    call, output, ordinary = sent["input"][1:4]
    assert call["type"] == "function_call" and "id" not in call
    assert output["type"] == "function_call_output" and "id" not in output
    assert call["call_id"] == output["call_id"] == "call_probe"
    assert json.loads(call["arguments"]) == {"input": "return 1"}
    assert output["output"] == history[1]["output"]
    assert ordinary == history[2]


def _gpt_payload() -> dict:
    """A non-DeepSeek request carrying custom tools — the shape GPT customers send."""
    return {
        "model": "gpt-6-astra",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "namespace", "name": "functions", "tools": [
                    {"type": "custom", "name": "exec", "description": "run code"}]}],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "tools": [],
        "stream": True,
    }


def _run_scope_case(tmp_path: Path, *, scoped, payload, reply=b'{"ok":true}',
                    content_type: str = "application/json"):
    """Send ``payload`` through a bridge and return (relay, result, record lines)."""
    record = tmp_path / "bridge.jsonl"
    capture = tmp_path / "capture"
    with _RawRelay(reply, content_type=content_type) as relay:
        bridge = BridgeServer(
            upstream_url=relay.url,
            port=0,
            record_path=str(record),
            capture_dir=str(capture),
            scoped_models=scoped,
        )
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        assert bridge.wait_ready()
        try:
            raw = json.dumps(payload).encode("utf-8")
            result = _post_exact("http://127.0.0.1:%d/responses" % bridge.bound_port, raw)
        finally:
            bridge.stop()
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    return relay, result, lines, capture


def test_scoped_bridge_passes_other_models_through_byte_for_byte(tmp_path: Path):
    """Out of scope: the relay sees the client's own bytes and so does the client."""
    reply = b'data: {"type":"response.completed"}\n\n'
    relay, (status, content_type, body), lines, capture = _run_scope_case(
        tmp_path,
        scoped=frozenset({"deepseek-v4.1-flash"}),
        payload=_gpt_payload(),
        reply=reply,
        content_type="text/event-stream",
    )
    sent = json.dumps(_gpt_payload()).encode("utf-8")
    # Byte-identical outbound: no additional_tools stripping, no stream rewrite,
    # not even a re-serialisation of the JSON.
    assert relay.bodies == [sent]
    assert b'"stream": true' in relay.bodies[0]
    assert b"additional_tools" in relay.bodies[0]
    # Byte-identical inbound: the relay's SSE framing reaches the client untouched.
    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert body == reply
    # Bookkeeping says what happened, without quoting the payload or capturing it.
    assert [line["mode"] for line in lines] == ["passthrough"]
    assert lines[0]["model"] == "gpt-6-astra"
    assert not capture.exists() or not any(capture.iterdir()), "越界模型不得被捕获"


def test_scoped_bridge_still_translates_the_named_model(tmp_path: Path):
    relay, (status, content_type, body), lines, _capture = _run_scope_case(
        tmp_path,
        scoped=frozenset({"deepseek-v4.1-flash"}),
        payload=_request(),
        reply=json.dumps(_relay_reply(1)).encode("utf-8"),
    )
    sent = json.loads(relay.bodies[0])
    assert [item["type"] for item in sent["input"]] == ["message"]
    assert sent["tools"][0]["type"] == "function"
    assert sent["stream"] is False
    assert status == 200 and content_type.startswith("text/event-stream")
    assert lines[0].get("mode") != "passthrough"
    # SSE still framed for Codex, with the custom call restored.
    events = _sse_events(body)
    assert events and events[-1]["type"] == "response.completed"


def test_scoped_bridge_passes_through_an_unrecognised_model(tmp_path: Path):
    """A model the scope does not name — including a missing one — is never rewritten."""
    payload = _gpt_payload()
    payload.pop("model")
    payload["stream"] = False
    relay, (_status, _ctype, body), lines, _capture = _run_scope_case(
        tmp_path,
        scoped=frozenset({"deepseek-v4.1-flash"}),
        payload=payload,
        reply=b'{"id":"resp_x"}',
    )
    assert relay.bodies == [json.dumps(payload).encode("utf-8")]
    assert body == b'{"id":"resp_x"}'
    assert lines[0]["mode"] == "passthrough"


def test_unscoped_bridge_keeps_translating_everything(tmp_path: Path):
    """``scoped_models=None`` is the historical behaviour: translate what arrives."""
    relay, (_status, _ctype, _body), lines, _capture = _run_scope_case(
        tmp_path,
        scoped=None,
        payload=_gpt_payload(),
        reply=json.dumps(_relay_reply(1)).encode("utf-8"),
    )
    sent = json.loads(relay.bodies[0])
    assert [t["type"] for t in sent["tools"]] == ["function"], "默认仍然翻译一切"
    assert not [line for line in lines if line.get("mode") == "passthrough"]


def test_empty_scoped_models_is_refused():
    """An empty scope would be a bridge that only adds a hop."""
    with pytest.raises(ValueError):
        BridgeServer(upstream_url="http://127.0.0.1:1", port=0, scoped_models=frozenset())
    assert BridgeServer(upstream_url="http://127.0.0.1:1", port=0).translates("gpt-6-astra")
    scoped = BridgeServer(
        upstream_url="http://127.0.0.1:1", port=0, scoped_models=frozenset({"deepseek-v4.1-flash"})
    )
    assert scoped.translates("deepseek-v4.1-flash")
    assert not scoped.translates("gpt-6-astra")
    assert not scoped.translates(None)
