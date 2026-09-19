#!/usr/bin/env python
"""L1 end-to-end check for the local Responses bridge.

Codex shape in -> local bridge -> relay -> SSE back out.  The point is to prove
the *wire* contract, not to prove the relay is fixed.

Default run uses a built-in fake relay: no network, no API key, and no real
Codex config is read or written.

    python scripts/bridge_l1_e2e.py

To capture evidence against a real relay (L2), point it at the upstream.  This
still does not touch any real ``config.toml``, and the credential is supplied
through a source that keeps it out of the command line (see
``l2_credentials.py``) — there is deliberately no ``--api-key`` flag:

    python scripts/bridge_l1_e2e.py --upstream https://relay.example/v1 \
        --credential env --credential-env MY_RELAY_KEY

Exit code is non-zero when any assertion fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import l2_credentials  # noqa: E402
from codex_model_manager.core.bridge import BridgeServer  # noqa: E402

EXEC_TOOL = {
    "type": "namespace",
    "name": "functions",
    "tools": [
        {
            "type": "custom",
            "name": "exec",
            "description": "Run JavaScript in the Codex sandbox to read files, run commands.",
        }
    ],
}


def codex_request(
    *,
    with_tools: bool,
    call_id: Optional[str] = None,
    with_output: bool = False,
) -> dict:
    """Build a request shaped the way Codex sends it.

    Captured from codex-cli 0.155.0-alpha.9.2 against the real relay: the
    ``additional_tools`` developer item is re-sent on *every* turn, and
    ``previous_response_id`` is never sent.  Turn 2 therefore keeps the
    declaration rather than relying on server-side memory.
    """
    items: list[dict] = []
    if with_tools:
        items.append({"type": "additional_tools", "role": "developer", "tools": [EXEC_TOOL]})
    items.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "列出当前目录"}],
        }
    )
    if call_id:
        items.append(
            {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": "ls -la"}
        )
        if with_output:
            items.append(
                {"type": "custom_tool_call_output", "call_id": call_id, "output": "total 0"}
            )
    return {
        "model": "deepseek-v4.1-flash",
        "instructions": "",
        "input": items,
        "tools": [],
        "stream": True,
    }


def relay_reply(index: int) -> dict:
    """A DeepSeek-style reply: it answers with a bare ``function_call``.

    The name is the namespace-qualified spelling the real relay actually
    returns; the bridge must resolve it back to ``exec`` through its explicit
    registration rather than pass it through.
    """
    return {
        "id": "resp_%d" % index,
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_%d" % index,
                "summary": [{"type": "summary_text", "text": "用户想列目录。"}],
            },
            {
                "type": "function_call",
                "id": "fc_%d" % index,
                "name": "functions__exec",
                "call_id": "call_%d" % index,
                "arguments": json.dumps({"input": "const r = await tools.execCommand('ls -la');"}),
                "status": "completed",
            },
        ],
    }


class FakeRelay:
    """Minimal stand-in for a relay that only understands ordinary functions."""

    def __init__(self, reply_factory):
        self.requests: list[dict] = []
        self._reply_factory = reply_factory
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                relay.requests.append(json.loads(self.rfile.read(length)))
                raw = json.dumps(relay._reply_factory(len(relay.requests))).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def post(url: str, payload: dict, credential: Optional[str]) -> tuple[int, str, bytes]:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer %s" % credential
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def sse_events(raw: bytes) -> list[dict]:
    events = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def summarise_tools(tools: Any) -> str:
    if not isinstance(tools, list):
        return "(none)"
    return ", ".join("%s:%s" % (t.get("type"), t.get("name")) for t in tools) or "(empty)"


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        print("  %s %s%s" % ("PASS" if ok else "FAIL", label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label)


def run(args) -> int:
    report = Report()
    relay: Optional[FakeRelay] = None
    # Already resolved in main() from env / secret-tool / stdin; never from argv.
    credential = getattr(args, "credential", None)
    if getattr(args, "upstream", None):
        print("凭据：%s" % l2_credentials.describe(credential))

    if args.upstream:
        upstream = args.upstream
        print("模式：真实中转取证 -> %s" % upstream)
    else:
        relay = FakeRelay(relay_reply)
        relay.start()
        upstream = relay.url
        print("模式：内置假中转（零外部依赖）-> %s" % upstream)

    bridge = BridgeServer(upstream_url=upstream, port=args.port)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    if not bridge.wait_ready():
        print("桥未能启动")
        return 2
    endpoint = "http://127.0.0.1:%d/responses" % bridge.bound_port
    print("本地桥：%s" % endpoint)
    print("监听地址仅限 loopback，未写入任何真实 Codex 配置。")
    print()

    try:
        # ---------------- turn 1: declares the custom tool ------------------ #
        print("[第 1 轮] 携带 additional_tools 声明 exec")
        status, content_type, raw = post(endpoint, codex_request(with_tools=True), credential)
        report.check(status == 200, "HTTP 200", "status=%s" % status)
        if relay is not None:
            sent = relay.requests[0]
            types = [item.get("type") for item in sent["input"]]
            report.check(
                "additional_tools" not in types,
                "additional_tools 信封已剥离",
                "input=%s" % types,
            )
            report.check(sent.get("stream") is False, "上游收到 stream=false（桥内聚合）")
            report.check(
                isinstance(sent.get("tools"), list)
                and sent["tools"]
                and sent["tools"][0]["type"] == "function",
                "exec 已降级为 function 工具",
                summarise_tools(sent.get("tools")),
            )
            report.check(
                sent["tools"][0]["name"] == "exec",
                "工具名保持 exec（未加 functions__ 前缀）",
                str(sent["tools"][0].get("name")),
            )
        report.check(
            content_type.startswith("text/event-stream") or args.upstream,
            "客户端收到 SSE",
            content_type,
        )

        events = sse_events(raw)
        if events:
            print("  SSE 事件：%s" % ", ".join(e.get("type", "?") for e in events))
            completed = events[-1]
            report.check(completed.get("type") == "response.completed", "末事件为 response.completed")
            output = (completed.get("response") or {}).get("output") or []
            calls = [i for i in output if i.get("type") == "custom_tool_call"]
            report.check(bool(calls), "输出中含 custom_tool_call")
            if calls:
                report.check(
                    calls[0].get("name") == "exec",
                    "custom_tool_call 名为 exec",
                    str(calls[0].get("name")),
                )
                report.check(
                    isinstance(calls[0].get("input"), str) and "execCommand" in calls[0]["input"],
                    "arguments 已还原为 custom input 字符串",
                    repr(calls[0].get("input"))[:70],
                )
                turn1_call_id = calls[0].get("call_id")
                print("  call_id = %s" % turn1_call_id)
        else:
            report.check(False, "解析 SSE 事件")
            turn1_call_id = "call_1"
        print()

        # ---------------- turn 2: declarations re-sent + history ------------ #
        print("[第 2 轮] 按真实抓包重发 additional_tools 并携带历史")
        followup = codex_request(with_tools=True, call_id=turn1_call_id, with_output=True)
        status2, _ct2, raw2 = post(endpoint, followup, credential)
        report.check(status2 == 200, "HTTP 200", "status=%s" % status2)
        if relay is not None:
            sent2 = relay.requests[1]
            types2 = [item.get("type") for item in sent2["input"]]
            report.check(
                types2[:3] == ["message", "function_call", "function_call_output"],
                "历史被降级为 function_call / function_call_output",
                "input=%s" % types2,
            )
            call_args = sent2["input"][1].get("arguments") if len(sent2["input"]) > 1 else None
            report.check(
                isinstance(call_args, str) and json.loads(call_args).get("input") == "ls -la",
                "历史 custom input 还原进 arguments",
                repr(call_args)[:70],
            )
            report.check(
                sent2["input"][2].get("call_id") == turn1_call_id,
                "call_id 字节级配对",
                str(sent2["input"][2].get("call_id")),
            )
            report.check(
                isinstance(sent2.get("tools"), list)
                and sent2["tools"]
                and sent2["tools"][0]["type"] == "function",
                "本轮声明仍被降级为 function（上游不存在 custom）",
                summarise_tools(sent2.get("tools")),
            )
        events2 = sse_events(raw2)
        if events2:
            output2 = (events2[-1].get("response") or {}).get("output") or []
            calls2 = [i for i in output2 if i.get("type") == "custom_tool_call"]
            report.check(bool(calls2), "第 2 轮仍返回 custom_tool_call（未退化为 function_call）")
            if calls2:
                report.check(
                    calls2[0].get("name") == "exec",
                    "命名空间限定名已还原为可调用名 exec",
                    str(calls2[0].get("name")),
                )
    finally:
        bridge.stop()
        if relay is not None:
            relay.stop()

    print()
    if report.failures:
        print("结果：%d 项失败 -> %s" % (len(report.failures), "; ".join(report.failures)))
        return 1
    print(
        "结果：全部通过。本脚本用内置假中转只取证「桥自身」的 wire 转换，"
        "不对真实中转或真实 Codex 客户端作任何结论；真实联调结果见"
        " integration-evidence/ 中的脱敏证据。"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地桥 L1 端到端验证")
    parser.add_argument("--upstream", help="真实中转地址；省略则使用内置假中转")
    parser.add_argument("--credential", default="none",
                        choices=list(l2_credentials.SOURCES),
                        help="凭据来源；不接受把 Key 直接写在命令行上")
    parser.add_argument("--credential-env", help="--credential env 时的变量名")
    parser.add_argument("--credential-codex-home",
                        default=l2_credentials.DEFAULT_CODEX_HOME)
    parser.add_argument("--port", type=int, default=0, help="本地桥端口（默认随机）")
    args = parser.parse_args()
    args.credential_value = l2_credentials.resolve(
        args.credential,
        env_name=args.credential_env,
        codex_home=args.credential_codex_home,
    )
    args.credential = args.credential_value
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
