#!/usr/bin/env python
"""Replay a captured upstream response through the bridge's own SSE framing.

Why this exists: the SSE framing the bridge emits has to satisfy a *strict real
client*, and iterating on it against a live relay costs a model call per attempt.
``--capture`` on ``bridge_l2_serve.py`` saves the raw upstream body, so this
script can replay it through ``_sse_response`` — the very same function the bridge
uses — and a real Codex can be pointed at it with no network at all.

Turn 1 replays the captured tool call.  Later turns answer with a synthetic final
message, so a client that correctly executes the tool can still finish the loop
instead of calling it forever.

    python scripts/bridge_replay.py --capture /tmp/l2codex/capture --port 8798
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_model_manager.core.bridge import _sse_event_names, _sse_response  # noqa: E402
from codex_model_manager.core.bridge import ensure_loopback  # noqa: E402

DONE_BODY = {
    "id": "resp_replay_done",
    "object": "response",
    "status": "completed",
    "model": "deepseek-v4.1-flash",
    "output": [
        {
            "type": "message",
            "id": "msg_replay_done",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "REPLAY_DONE"}],
        }
    ],
}


def load_captures(directory: str) -> List[dict]:
    """Every ``upstream-*.json`` in the capture directory, in order."""
    root = Path(directory)
    files = sorted(root.glob("upstream-*.json"), key=lambda p: p.stat().st_mtime)
    bodies = []
    for path in files:
        bodies.append(json.loads(path.read_text(encoding="utf-8")))
    return bodies


def build_handler(captures: List[dict], log: List[dict]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("", "/health"):
                body = b'{"ok":true,"replay":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(length))
            index = len(log) + 1
            if index <= len(captures):
                body = captures[index - 1]
                source = "capture-%d" % index
            else:
                body = DONE_BODY
                source = "synthetic-final"
            log.append({"index": index, "source": source})
            sys.stderr.write("REPLAY_REQ %d %s\n" % (index, source))
            sys.stderr.flush()

            output = body.get("output") or []
            items = [
                (i.get("type"), i.get("name"))
                for i in output
                if isinstance(i, dict)
                and i.get("type") in ("function_call", "custom_tool_call", "message")
            ]
            sys.stderr.write("REPLAY_ITEMS %s\n" % json.dumps(items, ensure_ascii=False))
            sys.stderr.flush()

            raw = _sse_response(body)
            sys.stderr.write(
                "REPLAY_EVENTS %s\n" % json.dumps(_sse_event_names(raw), ensure_ascii=False)
            )
            sys.stderr.flush()

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线回放上游响应，验证 SSE 帧格式")
    parser.add_argument("--capture", required=True)
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    ensure_loopback(args.host)
    captures = load_captures(args.capture)
    if not captures:
        print("没有找到 upstream-*.json，先跑一次带 --capture 的桥。", file=sys.stderr)
        return 2

    log: List[dict] = []
    server = ThreadingHTTPServer(
        (args.host, args.port), build_handler(captures, log)
    )
    print("REPLAY_READY http://%s:%d (%d 份捕获)" % (args.host, args.port, len(captures)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("REPLAY_STOPPED requests=%d" % len(log), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
