import json
import socket
import threading

from codex_model_manager.core.doctor import collect_doctor, format_doctor


class _Config:
    bridge_enabled = False
    bridge_host = "127.0.0.1"
    bridge_port = 8787
    bridge_upstream_url = "https://example.invalid/"
    bridge_provider = "OpenAI"
    codex_config_path = None
    codex_runtime_path = None


def test_doctor_is_read_only_and_marks_tool_registry_unobservable():
    report = collect_doctor(_Config())
    assert report["read_only"] is True
    assert report["checks"]["tool_registry"]["state"] == "unobservable"
    assert "sk-" not in format_doctor(report)
    json.dumps(report)


def test_doctor_drops_provider_url_userinfo_query_and_fragment():
    cfg = _Config()
    cfg.bridge_upstream_url = "https://user:secret@example.invalid/v1?api_key=sk-secret#x"
    report = collect_doctor(cfg)
    assert report["checks"]["bridge"]["upstream"] == "https://example.invalid/v1"
    assert "secret" not in json.dumps(report)


def test_doctor_summarises_observer_request_response_pair(tmp_path):
    path = tmp_path / "obs.jsonl"
    rows = [
        {"capture": "request", "req_id": "a", "model": "gpt-6-astra",
         "tools": {"state": "absent", "count": 0, "items": []},
         "additional_tools": {"state": "absent", "count": 0, "items": []}},
        {"capture": "response", "req_id": "a", "call_count": 0, "calls": []},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = collect_doctor(_Config(), observe_log=str(path))
    observation = report["checks"]["observation"]
    assert observation["state"] == "request_missing_tools"
    assert observation["pair_count"] == 1
    assert "client_sent_no_tool_declaration" in report["warnings"]


def test_doctor_checks_loopback_bridge_without_sending_credentials():
    ready = threading.Event()
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        ready.set()
        listener.settimeout(1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                # The test closes the listener during teardown. Windows raises
                # WSAENOTSOCK (10038) in the accept loop; that is normal cleanup,
                # not an assertion failure. Preserve unexpected socket errors.
                if getattr(exc, "winerror", None) == 10038 or listener.fileno() == -1:
                    break
                raise
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(1)
    cfg = _Config()
    cfg.bridge_enabled = True
    cfg.bridge_port = port
    try:
        report = collect_doctor(cfg)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)
    assert report["checks"]["bridge"]["probe"]["state"] == "reachable"
