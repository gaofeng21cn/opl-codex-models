#!/usr/bin/env python
"""Run the local bridge in the foreground so a real Codex can point at it.

Kept separate from ``bridge_l2_probe.py`` on purpose: the probe owns the client
side of the wire, this owns the server side, and a Codex acceptance run needs the
bridge to outlive each individual request.

Loopback only (enforced in the data model), no credential handling at all — the
bridge forwards the Authorization header Codex already sends.

    python3 scripts/bridge_l2_serve.py --upstream https://gflabtoken.cn \
        --port 8799 --record /tmp/l2codex/bridge.jsonl

Everything here is off by default.  ``--record`` writes redacted metadata only
(tool names/types, hashed ids, lengths).  ``--capture`` is **not** part of a
normal start and is deliberately opt-in because what it writes is model output
that may be sensitive: it is saved to disk, never printed, and never deleted by
this program — see the flag help.

``--only-model`` turns the bridge into the *model-scoped* variant: the listed
models are translated, everything else is relayed byte-for-byte.  Remember that a
relay's ``base_url`` is provider-wide, so the bridge still sits in front of every
model on that provider; scoping removes the rewriting, not the extra hop.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_model_manager.core.bridge import BridgeServer  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="前台运行本地 Responses 桥（仅 loopback）")
    parser.add_argument("--upstream", required=True, help="真实中转 base_url")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--record", help="脱敏取证 JSONL 输出路径（仅元数据）")
    parser.add_argument("--protocol", choices=("function", "native"), default="function",
                        help="function=协议兼容；native=保留原工具协议，可单独测试上下文纠偏")
    parser.add_argument("--context-recovery", action="store_true",
                        help="在请求副本中纠正旧无工具诊断；原任务历史不变，须指定 --only-model")
    parser.add_argument("--experiment-mode-file", help="本地四组试验模式文件，按请求读取，不自动重试")
    parser.add_argument(
        "--capture",
        help="离线回放取证目录（原始上游响应 + 实际发出的 SSE）。"
             "默认关闭；内容为模型输出、可能敏感，本程序只写不打印、不自动删除。",
    )
    parser.add_argument(
        "--only-model",
        action="append",
        default=None,
        metavar="MODEL",
        help="只翻译这些模型（可重复）；其余模型请求/响应原样直通，不翻译也不捕获。"
             "不给则该桥翻译一切到达的请求。",
    )
    args = parser.parse_args(argv)

    bridge = BridgeServer(
        upstream_url=args.upstream.rstrip("/"),
        host=args.host,
        port=args.port,
        record_path=args.record,
        capture_dir=args.capture,
        scoped_models=frozenset(args.only_model) if args.only_model else None,
        protocol_translation=args.protocol == "function",
        context_recovery=args.context_recovery,
        experiment_mode_file=args.experiment_mode_file,
    )
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    if not bridge.wait_ready():
        print("bridge failed to start", file=sys.stderr)
        return 2

    endpoint = "http://%s:%d" % (args.host, bridge.bound_port)
    print("BRIDGE_READY %s -> %s" % (endpoint, args.upstream), flush=True)
    print(
        "BRIDGE_SCOPE %s"
        % (",".join(sorted(bridge.scoped_models)) if bridge.scoped_models else "all"),
        flush=True,
    )
    if args.record:
        print("BRIDGE_RECORD %s" % args.record, flush=True)
    if args.capture:
        print(
            "BRIDGE_CAPTURE %s（内容可能敏感：只写不打印，本程序不删除）" % args.capture,
            flush=True,
        )

    stop = threading.Event()

    def _handle(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    # A health probe so a caller can wait deterministically instead of sleeping.
    try:
        with urlopen(endpoint + "/health", timeout=5) as response:
            if response.status != 200:
                print("health check returned %s" % response.status, file=sys.stderr)
    except URLError as exc:
        print("health check failed: %s" % exc, file=sys.stderr)
    while not stop.is_set():
        time.sleep(0.2)
    bridge.stop()
    print("BRIDGE_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
