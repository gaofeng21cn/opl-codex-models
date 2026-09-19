"""Optional local Responses bridge for Codex custom tools.

Codex sends its ``exec`` tool as a Responses ``custom`` tool — either inside the
``additional_tools`` input item (``use_responses_lite=true``) or as a top-level
``tools[]`` entry (``use_responses_lite=false``).  Several OpenAI-compatible
relays accept only ordinary function tools and reject ``type=custom`` outright
(``Unsupported custom tool: 'exec'``).  This module keeps the bridge deliberately
small and local:

* requests: drop the ``additional_tools`` envelope and expose ``custom`` tools as
  ordinary function tools (leaf name unchanged — never ``functions__exec``); an
  empty ``tools: []`` is dropped rather than forwarded, because strict relays
  reject it;
* responses: turn a call back into a ``custom_tool_call`` when — and only when —
  its name resolves through an explicit registration, preserving ``call_id``;
* safety: the server binds loopback only and never stores credentials — it
  forwards the Authorization header Codex already supplied.

Translation is driven by an **explicit registration table**, never by string
heuristics.  Every tool the client declares produces a :class:`ToolRegistration`
recording the namespace path it was declared under, the leaf spelling sent
upstream, the spelling Codex must get back, and whether it is a ``custom`` or an
ordinary ``function`` tool.  From that table the bridge derives the exact set of
spellings the relay may legitimately return:

* the leaf name (``exec``) — what the bridge sent upstream;
* the namespace-joined name (``functions__exec``) — what a model actually emits
  when it qualifies a tool with the namespace it saw, as captured against the
  real relay.  Restoring it to ``custom_tool_call(name="exec")`` is the whole
  point: ``functions.exec`` is Codex's *internal* notation and ``exec`` is the
  callable name.

Anything else is refused and reported, never guessed:

* a spelling with no registration is left as-is and reported as a diagnostic;
* a spelling claimed by two different registrations is reported as ambiguous;
* a spelling registered as a plain ``function`` stays a ``function_call`` but is
  restored to the client-side leaf name, even if some other conversation
  registered the same letters as ``custom``.

Registrations are scoped to one conversation.  A request's own declarations are
authoritative and are all that is needed: a capture of Codex
0.155.0-alpha.9.2 against the real relay shows ``additional_tools`` re-sent on
*every* turn and ``previous_response_id`` never sent at all.  The
``previous_response_id`` continuation path is therefore kept as an explicitly
bounded fallback (LRU + idle TTL) and never as the primary mechanism.  History
items such as ``custom_tool_call`` are self-describing and are lowered on their
own — they deliberately grant **no** restoration rights, so a past call can never
authorise the current turn's response translation.

Model scope (``scoped_models``)
-------------------------------

A relay's ``base_url`` is a *provider-wide* setting, so putting the bridge in
front of it also puts every model on that provider behind the bridge.  A real
Codex client (verified against ``0.155.0-alpha.9.2``) picks the provider globally:
its model catalog carries no provider field, the app-server ``Model`` type has
none either, and the desktop composer's saved model choice is only
``{model, reasoningEffort, serviceTier}``.  There is therefore **no per-model
provider selection to lean on**, and the bridge does not pretend otherwise.

``scoped_models`` narrows the blast radius instead: when it is a non-empty set,
only those models are translated; every other model is relayed **verbatim** — the
exact request bytes the client sent are forwarded untouched (``stream`` flag and
all) and the upstream response bytes are streamed straight back, so a non-scoped
model sees precisely what it would have seen talking to the relay directly, and
nothing about it is written to the evidence sinks.  What this does *not* remove
is the extra loopback hop: while the switch is on, a stopped bridge breaks those
models too.  ``scoped_models=None`` (the default) keeps the original behaviour of
translating whatever arrives.

Nothing here claims to fix the relay.  A tool call that cannot be translated is
reported, never faked as success.
"""

from __future__ import annotations

import json
import http.client
import queue
import select
import socket
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

#: The only hosts the bridge may ever listen on.  Enforced in the data class and
#: in :func:`run_bridge` so no caller (CLI, GUI, tests, scripts) can bind wider.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: Separator Codex uses for its internal namespace notation (``functions.exec``
#: is spelled ``functions__exec`` on the wire by models that qualify a name).
NAMESPACE_MARKER = "__"

#: Client-supplied fields that identify a conversation, most specific first.
#: Only used to *scope* registrations; never used to authorise a tool.
SESSION_FIELDS = ("conversation_id", "session_id")

#: Tool kinds as they appear on the wire.
CUSTOM = "custom"
FUNCTION = "function"

REASON_AMBIGUOUS = "ambiguous_registration"
REASON_NAMESPACE_UNREGISTERED = "namespace_prefixed_without_registration"
REASON_UNDECLARED = "undeclared_in_this_conversation"


def ensure_loopback(host: str) -> str:
    """Return ``host`` when it is a loopback address, else raise ``ValueError``."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "本地桥只允许绑定 loopback 地址（%s），收到：%r"
            % ("/".join(LOOPBACK_HOSTS), host)
        )
    return host


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _custom_parameters() -> dict:
    return {
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": ["input"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class ToolRegistration:
    """One explicit registration produced while rewriting a request.

    ``upstream_name`` is the leaf spelling sent to the relay; ``namespace`` is the
    dotted namespace path it was declared under (``"functions"``), or ``None``.
    ``original_name`` is the spelling Codex declared and must get back.
    """

    upstream_name: str
    original_name: str
    kind: str
    namespace: Optional[str] = None

    @property
    def joined_name(self) -> Optional[str]:
        """The namespace-qualified spelling a model may emit (``functions__exec``)."""
        if not self.namespace:
            return None
        return self.namespace.replace(".", NAMESPACE_MARKER) + NAMESPACE_MARKER + self.upstream_name

    def admissible_spellings(self) -> Tuple[str, ...]:
        joined = self.joined_name
        if joined and joined != self.upstream_name:
            return (self.upstream_name, joined)
        return (self.upstream_name,)

    def to_json(self) -> dict:
        payload = {
            "upstream_name": self.upstream_name,
            "original_name": self.original_name,
            "kind": self.kind,
        }
        if self.namespace:
            payload["namespace"] = self.namespace
            payload["joined_name"] = self.joined_name
        return payload


@dataclass(frozen=True)
class ToolDeclarations:
    """Resolved registration table for answering one request.

    ``lookup`` maps each spelling the relay may legitimately return to its
    registration.  It is the *only* authority used when translating a response: a
    spelling absent from this table is never rewritten.
    """

    lookup: Dict[str, ToolRegistration]
    ambiguous: FrozenSet[str]
    declared_now: FrozenSet[str]
    inherited_from: Optional[str] = None
    tool_count: int = 0

    @property
    def custom_names(self) -> Set[str]:
        """Codex-side names of the custom tools that may be restored."""
        return {
            reg.original_name
            for reg in self.lookup.values()
            if reg.kind == CUSTOM
        }

    def resolve(self, name: str) -> Tuple[Optional[ToolRegistration], Optional[str]]:
        """Return ``(registration, reason)`` for an upstream call name.

        Exactly one of the two is non-``None``; ``reason`` explains a refusal.
        """
        if name in self.ambiguous:
            return None, REASON_AMBIGUOUS
        reg = self.lookup.get(name)
        if reg is not None:
            return reg, None
        if NAMESPACE_MARKER in name:
            return None, REASON_NAMESPACE_UNREGISTERED
        return None, REASON_UNDECLARED

    def describe(self) -> dict:
        return {
            "declared_now": sorted(self.declared_now),
            "admissible_spellings": sorted(self.lookup),
            "registered": [r.to_json() for r in self._unique_registrations()],
            "ambiguous": sorted(self.ambiguous),
            "tool_count": self.tool_count,
            "inherited_from_previous_response_id": self.inherited_from,
        }

    def _unique_registrations(self) -> List[ToolRegistration]:
        seen: Dict[tuple, ToolRegistration] = {}
        for reg in self.lookup.values():
            seen[(reg.upstream_name, reg.original_name, reg.kind, reg.namespace)] = reg
        return list(seen.values())


def _build_declarations(
    registrations: Iterable[ToolRegistration],
    declared_now: Iterable[str],
    ambiguous: Iterable[str],
    inherited_from: Optional[str],
    tool_count: int,
) -> ToolDeclarations:
    lookup: Dict[str, ToolRegistration] = {}
    conflicts: Set[str] = set(ambiguous)
    for reg in registrations:
        for spelling in reg.admissible_spellings():
            existing = lookup.get(spelling)
            if existing is None:
                lookup[spelling] = reg
            elif existing != reg:
                conflicts.add(spelling)
    for spelling in conflicts:
        # An ambiguous spelling is refused outright: guessing which tool a call
        # meant would be exactly the kind of silent mis-translation this bridge
        # exists to prevent.
        lookup.pop(spelling, None)
    return ToolDeclarations(
        lookup=lookup,
        ambiguous=frozenset(conflicts),
        declared_now=frozenset(declared_now),
        inherited_from=inherited_from,
        tool_count=tool_count,
    )


def _child_tools(node: dict) -> List[Any]:
    """Return the children of a namespace node across the shapes Codex uses."""
    inner = node.get("tools")
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        return [dict(spec, name=spec.get("name", key)) if isinstance(spec, dict) else spec
                for key, spec in inner.items()]
    # Bare mapping: any dict-valued key (excluding reserved ones) is a child.
    reserved = {"type", "name", "description", "parameters"}
    return [dict(spec, name=spec.get("name", key)) if isinstance(spec, dict) else spec
            for key, spec in node.items() if key not in reserved and isinstance(spec, dict)]


def _function_tool(
    tool: dict, namespace: Optional[str]
) -> Tuple[Optional[dict], Optional[ToolRegistration]]:
    kind = tool.get("type")
    name = tool.get("name")
    if not name:
        return None, None
    if kind == CUSTOM:
        registration = ToolRegistration(name, name, CUSTOM, namespace)
        return {
            "type": "function",
            "name": name,
            "description": str(tool.get("description") or "")[:12000],
            "parameters": _custom_parameters(),
            "strict": False,
        }, registration
    if kind == FUNCTION:
        registration = ToolRegistration(name, name, FUNCTION, namespace)
        result = dict(tool)
        result["type"] = "function"
        return result, registration
    return None, None


def _visit_tools(
    items: Iterable[Any],
    namespace: Optional[str],
    converted: List[dict],
    registrations: List[ToolRegistration],
    seen: Set[str],
) -> None:
    """Flatten a tool tree (namespace nodes are transparent) into function tools.

    The namespace path is preserved on each registration so the namespace-joined
    spelling (``functions__exec``) can later be resolved to exactly one tool.
    """
    for tool in items:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            node_name = tool.get("name")
            child_ns = (
                "%s.%s" % (namespace, node_name)
                if namespace and node_name
                else (node_name or namespace)
            )
            _visit_tools(_child_tools(tool), child_ns, converted, registrations, seen)
            continue
        out, registration = _function_tool(tool, namespace)
        if registration is None or out is None:
            continue
        registrations.append(registration)
        name = out.get("name")
        if name and name not in seen:
            seen.add(name)
            converted.append(out)


def _dedupe(registrations: Iterable[ToolRegistration]) -> List[ToolRegistration]:
    """Collapse exact duplicates, keeping the namespace part of the identity."""
    seen: "OrderedDict[Tuple[str, str, str, Optional[str]], ToolRegistration]" = OrderedDict()
    for reg in registrations:
        seen[(reg.upstream_name, reg.original_name, reg.kind, reg.namespace)] = reg
    return list(seen.values())


def collect_declarations(
    *sources: Optional[Iterable[Any]],
) -> Tuple[List[dict], List[ToolRegistration], int]:
    """Flatten one or more tool trees into function tools plus registrations."""
    tools: List[dict] = []
    registrations: List[ToolRegistration] = []
    seen: Set[str] = set()
    count = 0
    for source in sources:
        source_list = list(source or [])
        count += len(source_list)
        _visit_tools(source_list, None, tools, registrations, seen)
    return tools, _dedupe(registrations), count


def extract_tools(input_items: Iterable[dict]) -> Tuple[List[dict], List[ToolRegistration]]:
    """Flatten Codex ``additional_tools`` input items into function tools."""
    sources = [
        item.get("tools") or []
        for item in input_items
        if isinstance(item, dict) and item.get("type") == "additional_tools"
    ]
    tools, registrations, _count = collect_declarations(*sources)
    return tools, registrations


def convert_top_level_tools(
    tools: Optional[Iterable[Any]],
) -> Tuple[List[dict], List[ToolRegistration]]:
    """Convert top-level ``tools[]`` (the ``use_responses_lite=false`` path).

    Without this, a relay that rejects ``type=custom`` still fails, because Codex
    sends ``exec`` as a top-level custom tool in that mode.
    """
    converted, registrations, _count = collect_declarations(tools)
    return converted, registrations


def conversation_key(payload: dict) -> Optional[str]:
    """Derive the conversation scope for a request, or ``None`` when unscoped.

    ``previous_response_id`` wins because it names a response *this bridge*
    answered, which is the tightest possible continuation link.  Otherwise a
    client-supplied session field is used.  With neither, the request is answered
    from its own declarations only — the bridge never falls back to a global name
    set, so two concurrent clients cannot leak tools into each other.
    """
    previous = payload.get("previous_response_id")
    if isinstance(previous, str) and previous:
        return "prev:" + previous
    for field_name in SESSION_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return "client:%s:%s" % (field_name, value)
    meta = payload.get("client_metadata")
    if isinstance(meta, dict):
        for field_name in SESSION_FIELDS:
            value = meta.get(field_name)
            if isinstance(value, str) and value:
                return "meta:%s:%s" % (field_name, value)
    return None


def response_key(response_id: str) -> str:
    """Registry key under which a response's declarations are remembered."""
    return "prev:" + response_id


def call_ref(call_id: Any) -> Optional[str]:
    """Irreversible short reference for a ``call_id`` (never the raw value)."""
    import hashlib

    if not isinstance(call_id, str) or not call_id:
        return None
    return hashlib.sha256(call_id.encode("utf-8", "replace")).hexdigest()[:8]


def call_metadata(payload: Any) -> List[dict]:
    """Redacted call inventory: type, name, hashed id, argument length only.

    Used for evidence so a report can distinguish "what the relay returned" from
    "what the bridge emitted", without recording prompts, arguments or headers.
    """
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list):
        return []
    inventory: List[dict] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call", "custom_tool_call"):
            continue
        arguments = item.get("arguments")
        if arguments is None:
            arguments = item.get("input")
        inventory.append(
            {
                "type": item.get("type"),
                "name": item.get("name"),
                "call_ref": call_ref(item.get("call_id")),
                "payload_len": len(arguments) if isinstance(arguments, str) else None,
            }
        )
    return inventory


@dataclass
class ToolRegistry:
    """Conversation-scoped registration table with explicit expiry.

    Entries are written only when a client explicitly declared tools, and are
    looked up only for the conversation they were declared in.  Bounded by entry
    count (LRU) and by idle time, so a long-running bridge cannot accumulate
    stale mappings or answer a later, unrelated turn from an old registration.
    """

    max_entries: int = 512
    ttl_seconds: float = 3600.0

    def __post_init__(self) -> None:
        self._entries: "OrderedDict[str, Tuple[float, ToolDeclarations]]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: Optional[str], declarations: ToolDeclarations) -> None:
        if not key:
            return
        with self._lock:
            self._entries[key] = (time.monotonic(), declarations)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, key: Optional[str]) -> Optional[ToolDeclarations]:
        if not key:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stamp, declarations = entry
            if time.monotonic() - stamp >= self.ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return declarations

    def forget(self, key: Optional[str]) -> None:
        if not key:
            return
        with self._lock:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _custom_call_from_function(item: dict, reg: ToolRegistration) -> dict:
    name = item.get("name")
    args = item.get("arguments", "")
    try:
        parsed = json.loads(args) if isinstance(args, str) else args
    except (TypeError, ValueError):
        parsed = {"input": str(args)}
    if isinstance(parsed, dict) and "input" in parsed:
        value = parsed["input"]
    else:
        value = parsed
    result = dict(item)
    result["type"] = "custom_tool_call"
    result.pop("arguments", None)
    result["input"] = _json_text(value)
    # Restore the spelling Codex declared: the namespace-qualified form the model
    # emitted (``functions__exec``) must become the callable name (``exec``).
    if reg.original_name and reg.original_name != name:
        result["name"] = reg.original_name
    result.setdefault("status", "completed")
    return result


def _function_call_from_custom(item: dict) -> dict:
    result = dict(item)
    result["type"] = "function_call"
    value = result.pop("input", "")
    result["arguments"] = json.dumps(
        {"input": value if isinstance(value, str) else _json_text(value)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result.setdefault("status", "completed")
    return result


def _transform_response(
    value: Any, declarations: ToolDeclarations, diagnostics: List[dict]
) -> Any:
    if isinstance(value, list):
        return [_transform_response(v, declarations, diagnostics) for v in value]
    if not isinstance(value, dict):
        return value
    result = {
        k: _transform_response(v, declarations, diagnostics) for k, v in value.items()
    }
    if result.get("type") == "custom_tool_call":
        return result  # already in Codex's shape
    if result.get("type") != "function_call":
        return result
    name = result.get("name")
    if not isinstance(name, str) or not name:
        return result
    registration, reason = declarations.resolve(name)
    if registration is None:
        diagnostics.append({"name": name, "reason": reason})
        return result
    if registration.kind != CUSTOM:
        # Ordinary functions (wait, questions, etc.) need name restoration too.
        # Keep their type and arguments unchanged.
        result["name"] = registration.original_name
        return result
    return _custom_call_from_function(result, registration)


def translate_request(
    payload: dict, inherited: Optional[ToolDeclarations] = None
) -> Tuple[dict, ToolDeclarations]:
    """Return an upstream-compatible request and its resolved declarations.

    ``inherited`` is the table remembered for the response named by this request's
    ``previous_response_id`` (``None`` when there is none).  Codex
    0.155.0-alpha.9.2 re-declares ``additional_tools`` on every turn and never
    sends ``previous_response_id``, so this path is a bounded fallback rather than
    the mechanism the bridge depends on.
    """
    result = dict(payload)
    raw_input = payload.get("input")
    items = raw_input if isinstance(raw_input, list) else []

    input_sources = [
        item.get("tools") or []
        for item in items
        if isinstance(item, dict) and item.get("type") == "additional_tools"
    ]
    extracted, from_input, count_in = collect_declarations(*input_sources)
    top_tools, from_top, count_top = collect_declarations(payload.get("tools"))

    registrations: List[ToolRegistration] = []
    has_declarations = "tools" in payload or any(
        isinstance(item, dict) and item.get("type") == "additional_tools"
        for item in items
    )
    if inherited is not None and not has_declarations:
        registrations.extend(inherited._unique_registrations())
    registrations.extend(from_top)
    registrations.extend(from_input)

    previous = payload.get("previous_response_id")
    declarations = _build_declarations(
        registrations=registrations,
        declared_now=set(r.upstream_name for r in from_input + from_top),
        ambiguous=[],
        inherited_from=previous if isinstance(previous, str) and previous else None,
        tool_count=count_in + count_top,
    )

    cleaned: List[Any] = []
    for item in items:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        kind = item.get("type")
        if kind == "additional_tools":
            continue
        if kind == "custom_tool_call":
            # Self-describing history: lowering it is always safe, and it is NOT
            # added to the registration table because a past call is a record of
            # what happened, not an authorisation for the current turn.
            cleaned.append(_function_call_from_custom(item))
        elif kind == "custom_tool_call_output":
            output = dict(item)
            output["type"] = "function_call_output"
            cleaned.append(output)
        else:
            cleaned.append(item)
    if isinstance(raw_input, list):
        result["input"] = cleaned

    names = {t.get("name") for t in top_tools if t.get("name")}
    merged_tools = list(top_tools)
    for tool in extracted:
        tool_name = tool.get("name")
        if tool_name and tool_name not in names:
            names.add(tool_name)
            merged_tools.append(tool)
    if merged_tools:
        result["tools"] = merged_tools
    else:
        # An explicit ``tools: []`` is forwarded by Codex but means "no tools".
        # Several strict relays 400 on an empty tools array, so drop the key
        # entirely rather than pass the client's empty placeholder through.
        result.pop("tools", None)
    return result, declarations


def translate_response(
    payload: dict,
    declarations: ToolDeclarations,
    diagnostics: Optional[List[dict]] = None,
) -> dict:
    """Translate upstream calls back into Codex custom tool calls.

    ``diagnostics`` (optional) collects ``{"name", "reason"}`` entries for calls
    that were deliberately *not* translated.
    """
    return _transform_response(
        payload, declarations, diagnostics if diagnostics is not None else []
    )


def _sse_response(payload: dict) -> bytes:
    """Wrap a non-streaming Responses object in the SSE sequence Codex expects.

    The framing matters as much as the payload.  Codex's client assembles output
    items from ``response.output_item.done``: the literal
    ``response.output_item.added`` does not occur anywhere in the
    0.155.0-alpha.9.2 binary, so a stream that announces items only with
    ``added`` is consumed and then silently discarded — the turn completes with
    no items at all.  Each event therefore carries a ``sequence_number``, and
    every item is both announced and *completed*.
    """
    response = (
        payload.get("response") if isinstance(payload.get("response"), dict) else payload
    )
    events: List[dict] = []
    counter = {"n": 0}

    def emit(event: dict) -> None:
        counter["n"] += 1
        event["sequence_number"] = counter["n"]
        events.append(event)

    created = dict(response, output=[], status="in_progress")
    emit({"type": "response.created", "response": created})
    emit({"type": "response.in_progress", "response": created})
    for index, item in enumerate(response.get("output") or []):
        emit({"type": "response.output_item.added", "output_index": index, "item": item})
        emit({"type": "response.output_item.done", "output_index": index, "item": item})
    status = response.get("status", "completed")
    terminal = status if status in ("failed", "incomplete") else "completed"
    emit({"type": "response." + terminal, "response": response})
    return "".join(
        "event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event, ensure_ascii=False))
        for event in events
    ).encode("utf-8")


def _sse_event_names(raw: bytes) -> List[str]:
    """The event names inside an SSE byte string, for redacted evidence."""
    names: List[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event: "):
            names.append(line[7:].strip())
    return names


@dataclass
class BridgeServer:
    upstream_url: str
    host: str = "127.0.0.1"
    port: int = 8787
    timeout: float = 600.0
    heartbeat_interval: float = 10.0
    #: Optional JSONL evidence sink.  When set, one redacted metadata line is
    #: appended per ``/responses`` request.  Never enabled by default, and it
    #: records only tool names/types, hashed call ids and lengths — no prompts,
    #: no arguments, no headers, no credentials.
    record_path: Optional[str] = None
    #: Optional directory for offline-replay evidence.  When set, every request
    #: saves the raw upstream body plus the exact bytes returned to the client, so
    #: a strict real client can be replayed against this framing without spending
    #: another model call.  Never enabled by default (the flag is opt-in and the
    #: normal start command does not pass it).  **The captured bodies are model
    #: output and may be sensitive**: they are written to disk and are never
    #: printed to the console, never summarised into the log, and never deleted by
    #: this program.  Headers, prompts sent by the client and credentials are
    #: never written.  Non-scoped models are never captured at all.
    capture_dir: Optional[str] = None
    #: Models whose traffic the bridge is allowed to translate.  ``None`` (the
    #: default) keeps the historical behaviour — translate anything that arrives.
    #: A non-empty set makes the bridge *model-scoped*: everything outside the set
    #: is relayed byte-for-byte in both directions.  An empty set is rejected: it
    #: would make the bridge a no-op that still adds a hop.
    scoped_models: Optional[FrozenSet[str]] = None

    def __post_init__(self) -> None:
        ensure_loopback(self.host)
        parsed = urlsplit(self.upstream_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("上游须为不含凭据、查询参数或片段的 HTTP(S) base_url")
        if not 0 <= self.port <= 65535 or self.timeout <= 0 or self.heartbeat_interval <= 0:
            raise ValueError("端口或超时无效")
        if parsed.hostname in LOOPBACK_HOSTS and (parsed.port or (443 if parsed.scheme == "https" else 80)) == self.port:
            raise ValueError("上游不能指向本地桥自身")
        if self.scoped_models is not None and not self.scoped_models:
            raise ValueError(
                "scoped_models 不能为空集：那会让桥成为只增加一跳的空壳。"
                "要么不给（翻译全部），要么给出确切模型名。"
            )
        self.registry = ToolRegistry()
        self._server: Optional[ThreadingHTTPServer] = None
        self._ready = threading.Event()
        self._capture_index = 0

    def translates(self, model: object) -> bool:
        """Whether traffic for ``model`` goes through protocol translation.

        ``True`` when no scope was configured.  Otherwise only an exact string
        match is translated; anything else (including a missing/odd ``model``
        field) is passed through untouched, because a request the bridge does not
        recognise must not be rewritten.
        """
        if self.scoped_models is None:
            return True
        return isinstance(model, str) and model in self.scoped_models

    def record(self, entry: dict) -> None:
        """Append one redacted evidence line, if a sink was configured."""
        if not self.record_path:
            return
        try:
            with open(self.record_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass  # Evidence collection must never break a live request.

    def capture(self, upstream_body: str, client_sse: str, meta: dict) -> None:
        """Persist one offline-replay bundle, if a capture directory was set."""
        if not self.capture_dir:
            return
        try:
            import os

            os.makedirs(self.capture_dir, exist_ok=True)
            self._capture_index += 1
            index = self._capture_index
            with open(
                os.path.join(self.capture_dir, "upstream-%d.json" % index),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(upstream_body)
            with open(
                os.path.join(self.capture_dir, "client-%d.sse" % index),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(client_sse)
            with open(
                os.path.join(self.capture_dir, "index.jsonl"), "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(dict(meta, index=index), ensure_ascii=False, sort_keys=True)
                    + "\n"
                )
        except OSError:
            pass  # Evidence collection must never break a live request.

    def stop(self) -> None:
        """Stop a running server (used by tests and by graceful shutdown)."""
        server = self._server
        if server is not None:
            server.shutdown()

    @property
    def bound_port(self) -> Optional[int]:
        """The effective port, which is useful when constructed with ``port=0``."""
        server = self._server
        return server.server_address[1] if server is not None else None

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Block until the listener is accepting connections."""
        return self._ready.wait(timeout)

    def declarations_for(self, payload: dict) -> ToolDeclarations:
        """Resolve the registration table that will answer ``payload``."""
        inherited = self.registry.get(conversation_key(payload))
        _outgoing, declarations = translate_request(payload, inherited)
        return declarations

    def serve_forever(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in ("", "/health"):
                    body = json.dumps({"ok": True, "bridge": "codex-responses",
                                       "upstream": bridge.upstream_url,
                                       "models": sorted(bridge.scoped_models) if bridge.scoped_models else None}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            protocol_version = "HTTP/1.1"

            def _headers(self) -> dict:
                # Preserve routing/session headers, but never hop-by-hop headers.
                blocked = {"host", "content-length", "transfer-encoding", "connection",
                           "keep-alive", "proxy-authenticate", "proxy-authorization",
                           "te", "trailer", "upgrade", "accept-encoding"}
                blocked.update(x.strip().lower() for x in self.headers.get("Connection", "").split(","))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in blocked}
                headers["Accept-Encoding"] = "identity"
                return headers

            def _connection(self):
                upstream = urlsplit(bridge.upstream_url)
                factory = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
                connection = factory(upstream.hostname, upstream.port, timeout=bridge.timeout)
                route = urlsplit(self.path)
                base = upstream.path.rstrip("/")
                path = route.path
                if base.endswith("/v1") and path.startswith("/v1/"):
                    path = path[3:]
                return connection, urlunsplit(("", "", base + path, route.query, ""))

            def _send_bytes(self, status, data, content_type="application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.close_connection = True
                for name in ("Retry-After", "X-Request-Id"):
                    if headers and headers.get(name):
                        self.send_header(name, headers[name])
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()

            def _start_stream(self, status=200, content_type="text/event-stream", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                if headers:
                    for key in ("Content-Encoding", "Retry-After", "X-Request-Id"):
                        if headers.get(key):
                            self.send_header(key, headers[key])
                self.end_headers()
                self.close_connection = True
                self._stream_started = True

            def _watch_disconnect(self, connection, finished):
                # A cancelled desktop request must release a pending upstream call,
                # including before the upstream has sent response headers.
                while not finished.wait(0.1):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            self._disconnected.set()
                            if connection.sock:
                                try:
                                    connection.sock.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                            connection.close()
                            return
                    except (OSError, ValueError):
                        return

            def _relay_verbatim(self, raw_body, incoming):
                connection, path = self._connection()
                finished = threading.Event()
                threading.Thread(target=self._watch_disconnect, args=(connection, finished), daemon=True).start()
                caught = None
                try:
                    connection.request("POST", path, raw_body, self._headers())
                    response = connection.getresponse()
                    content_type = response.getheader("Content-Type", "application/json")
                    if bool(incoming.get("stream")):
                        self._start_stream(response.status, content_type, response.headers)
                        while not self._disconnected.is_set():
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        # Keep non-stream passthrough a normal, length-delimited
                        # response; only SSE requests use connection-close framing.
                        self._send_bytes(response.status, response.read(), content_type, response.headers)
                except (OSError, http.client.HTTPException) as exc:
                    caught = type(exc).__name__
                    if not self._stream_started and not self._disconnected.is_set():
                        self._send_bytes(502, b'{"error":{"type":"bridge_error","message":"Upstream connection failed"}}')
                finally:
                    finished.set()
                    connection.close()
                    bridge.record({"route": urlsplit(self.path).path, "model": incoming.get("model"),
                                   "mode": "passthrough", "client_stream": bool(incoming.get("stream")),
                                   "error": caught})

            def _translated(self, incoming):
                scope = conversation_key(incoming)
                # Do not allow different authenticated clients/models to share the
                # optional continuation cache. Hash credentials only in memory.
                import hashlib
                owner = hashlib.sha256((self.headers.get("Authorization", "") + "\0" + str(incoming.get("model"))).encode()).hexdigest()
                key = owner + ":" + scope if scope else None
                outgoing, declarations = translate_request(incoming, bridge.registry.get(key))
                bridge.registry.put(key, declarations)
                outgoing["stream"] = False
                connection, path = self._connection()
                finished = threading.Event()
                result = queue.Queue(maxsize=1)

                def fetch():
                    try:
                        connection.request("POST", path, json.dumps(outgoing, ensure_ascii=False).encode(), self._headers())
                        response = connection.getresponse()
                        result.put((response.status, response.headers, response.read()))
                    except (OSError, http.client.HTTPException) as exc:
                        result.put(exc)
                    finally:
                        connection.close()

                threading.Thread(target=self._watch_disconnect, args=(connection, finished), daemon=True).start()
                threading.Thread(target=fetch, daemon=True).start()
                try:
                    heartbeat_at = time.monotonic() + bridge.heartbeat_interval
                    while True:
                        if self._disconnected.is_set():
                            return
                        try:
                            response_result = result.get(timeout=0.1)
                            break
                        except queue.Empty:
                            if incoming.get("stream") and time.monotonic() >= heartbeat_at:
                                if not self._stream_started:
                                    self._start_stream()
                                self.wfile.write(b": bridge waiting for upstream\n\n")
                                self.wfile.flush()
                                heartbeat_at = time.monotonic() + bridge.heartbeat_interval
                    if isinstance(response_result, Exception):
                        raise ValueError("Upstream connection failed") from response_result
                    status, headers, raw = response_result
                    # Never follow redirects (especially with Authorization).
                    if not 200 <= status < 300:
                        if not self._stream_started:
                            self._send_bytes(status, raw, headers.get("Content-Type", "application/json"), headers)
                        else:
                            self._stream_error("upstream_http_error", status)
                        return
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("Upstream response must be an object")
                    response_id = body.get("id")
                    if isinstance(response_id, str) and response_id:
                        bridge.registry.put(owner + ":" + response_key(response_id), declarations)
                    upstream_calls = call_metadata(body)
                    diagnostics = []
                    body = translate_response(body, declarations, diagnostics)
                    raw_out = _sse_response(body) if incoming.get("stream") else json.dumps(body, ensure_ascii=False).encode()
                    meta = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                            "route": urlsplit(self.path).path, "model": incoming.get("model"),
                            "mode": "translate", "client_stream": bool(incoming.get("stream")),
                            "declared": declarations.describe(),
                            "declared_sources": [source for source, present in (
                                ("additional_tools", any(isinstance(x, dict) and x.get("type") == "additional_tools" for x in incoming.get("input", []) if isinstance(incoming.get("input"), list))),
                                ("top_level_tools", bool(incoming.get("tools")))) if present],
                            "upstream_calls": upstream_calls, "translated_calls": call_metadata(body),
                            "client_events": _sse_event_names(raw_out) if incoming.get("stream") else [],
                            "diagnostics": diagnostics, "upstream_status": status}
                    bridge.record(meta)
                    bridge.capture(raw.decode("utf-8", "replace"), raw_out.decode("utf-8", "replace"), meta)
                    if self._stream_started:
                        self.wfile.write(raw_out)
                        self.wfile.flush()
                    else:
                        self._send_bytes(status, raw_out, "text/event-stream" if incoming.get("stream") else "application/json")
                finally:
                    finished.set()
                    # shutdown interrupts a fetch still waiting after cancellation.
                    if self._disconnected.is_set() and connection.sock:
                        try:
                            connection.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    connection.close()

            def _stream_error(self, kind, status=None):
                event = {"type": "error", "code": kind,
                         "message": "Local bridge request failed", "param": None}
                if status is not None:
                    event["http_status"] = status
                self.wfile.write(("event: error\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

            def do_POST(self):  # noqa: N802
                self._stream_started = False
                self._disconnected = threading.Event()
                try:
                    if self.headers.get("Upgrade"):
                        self._send_bytes(501, b'{"error":"WebSocket is not supported"}')
                        return
                    if urlsplit(self.path).path not in ("/responses", "/v1/responses"):
                        self._send_bytes(404, b'{"error":"Unsupported route"}')
                        return
                    if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
                        self._send_bytes(415, b'{"error":"Unsupported request encoding"}')
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32 * 1024 * 1024:
                        self._send_bytes(413, b'{"error":"Invalid or oversized request"}')
                        return
                    self.connection.settimeout(bridge.timeout)
                    raw_body = self.rfile.read(length)
                    incoming = json.loads(raw_body)
                    if not isinstance(incoming, dict):
                        raise ValueError("Request must be an object")
                    if bridge.translates(incoming.get("model")):
                        self._translated(incoming)
                    else:
                        self._relay_verbatim(raw_body, incoming)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True
                except (OSError, ValueError, TypeError, http.client.HTTPException):
                    # Never render exception text: it may contain upstream bodies,
                    # URLs, arguments or credentials.
                    try:
                        if self._stream_started:
                            self._stream_error("bridge_error")
                        else:
                            self._send_bytes(502, b'{"error":{"type":"bridge_error","message":"Invalid request or upstream failure"}}')
                    except OSError:
                        pass


        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if bridge.host == "::1" else socket.AF_INET
            daemon_threads = True

            def handle_error(self, request, client_address):
                # Avoid default traceback output with untrusted request content.
                return

        server = Server((self.host, self.port), Handler)
        self._server = server
        self._ready.set()
        try:
            server.serve_forever()
        finally:
            server.server_close()
            self._server = None
            self._ready.clear()


def run_bridge(
    upstream_url: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    scoped_models: Optional[FrozenSet[str]] = None,
) -> None:
    if not upstream_url or not upstream_url.startswith(("http://", "https://")):
        raise ValueError("bridge upstream_url 必须是 http:// 或 https:// 地址")
    ensure_loopback(host)
    BridgeServer(
        upstream_url=upstream_url, host=host, port=port, scoped_models=scoped_models
    ).serve_forever()
