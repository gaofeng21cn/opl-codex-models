"""Read-only diagnostics for the Codex desktop tool path.

The manager cannot see the tool registry that the desktop app sends on a
particular turn.  This module therefore reports observable facts separately:
the configured provider/bridge, bridge reachability, configured runtime files,
and (where the host exposes ``/proc``) whether app-server and code-mode-host
processes are currently visible.  It never starts Codex, reads credentials, or
writes configuration.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional


def _check(state: str, detail: str, **extra: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {"state": state, "detail": detail}
    value.update(extra)
    return value


def _safe_url(url: Optional[str]) -> Optional[str]:
    """Return a provider URL for display without ever handling auth fields."""
    if not url:
        return None
    text = str(url).strip()
    # Config editor already rejects malformed URLs for bridge use.  Doctor is
    # deliberately non-invasive, so it only redacts credentials if an older
    # config contains them rather than trying to parse or contact the URL.
    try:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(text)
        # Always drop userinfo, query and fragment.  A provider URL should not
        # contain a key, but an old hand-written config might put one there;
        # diagnostics must remain safe even in that case.
        if not parsed.scheme or not parsed.netloc:
            return "<unparseable-url>"
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return "<unparseable-url>"


def _bridge_probe(host: str, port: int, timeout: float = 0.35) -> Dict[str, Any]:
    """Probe only a loopback listener; no request body or credential is sent."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return _check("invalid", "bridge 端口不是整数")
    if not (1 <= port <= 65535):
        return _check("invalid", "bridge 端口超出 1..65535")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return _check("not_checked", "为避免向远程地址发包，仅检查 loopback bridge")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return _check("reachable", f"{host}:{port} 可建立 TCP 连接", host=host, port=port)
    except OSError as exc:
        return _check("unreachable", f"{host}:{port} 不可连接（{type(exc).__name__}）",
                      host=host, port=port)


def _proc_snapshot() -> Dict[str, Any]:
    """Report component presence without exposing command lines or arguments."""
    proc = Path("/proc")
    if not proc.is_dir():
        return _check("unavailable", "当前系统没有可读的 /proc 进程视图")
    found = {"app_server": [], "code_mode_host": []}
    scanned = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        scanned += 1
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        name = comm.lower()
        if "code-mode-host" in name or "codex-code-mode-host" in name:
            found["code_mode_host"].append(int(entry.name))
        elif name in {"codex", "codex.exe"}:
            # There can be several codex processes; the manager only reports
            # the process id and role, never its command line.
            found["app_server"].append(int(entry.name))
    for value in found.values():
        value.sort()
    if found["app_server"] and not found["code_mode_host"]:
        state = "host_not_observed"
        detail = "观察到 codex 进程，但当前 /proc 未观察到 code-mode-host；这只是旁证，不能单独证明根因"
    elif found["app_server"] and found["code_mode_host"]:
        state = "components_observed"
        detail = "观察到 codex 与 code-mode-host 进程"
    elif found["code_mode_host"]:
        state = "host_without_app_server"
        detail = "观察到 code-mode-host，但未观察到 codex 进程"
    else:
        state = "none_observed"
        detail = "当前 /proc 未观察到 Codex 组件进程"
    return _check(state, detail, scanned=scanned, **found)


def _runtime_check(config: Any) -> Dict[str, Any]:
    path = getattr(config, "codex_runtime_path", None) if config is not None else None
    if not path:
        return _check("not_configured", "应用配置没有显式 codexRuntimePath；未启动探测或猜测运行时")
    p = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not p.is_file():
        return _check("missing", "显式运行时文件不存在", path=str(p))
    try:
        size = p.stat().st_size
    except OSError:
        size = None
    sibling_names = [p.with_name("codex-code-mode-host"),
                     p.with_name("codex-code-mode-host.exe")]
    siblings = {str(item): item.is_file() for item in sibling_names}
    return _check("present", "显式运行时文件存在（未执行 --version）", path=str(p),
                  size=size, code_mode_host_files=siblings)


def _safe_text(value: Any, limit: int = 64) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text).strip()
    if not text:
        return None
    return text[:limit] + ("..." if len(text) > limit else "")


def _count(value: Any) -> int:
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, 1000000))


def _observation_check(log_path: Optional[str]) -> Dict[str, Any]:
    """Summarise observer JSONL without dumping raw request/response lines."""
    if not log_path:
        return _check("not_configured", "未提供观察代理日志；无法比较正常/异常请求")
    path = Path(os.path.expandvars(os.path.expanduser(str(log_path))))
    if not path.is_file():
        return _check("missing", "观察代理日志不存在", path=str(path))

    requests: Dict[str, Dict[str, Any]] = {}
    responses: Dict[str, Dict[str, Any]] = {}
    invalid = 0
    lines_read = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                lines_read += 1
                if lines_read > 5000:
                    break
                try:
                    row = json.loads(raw)
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("req_id"), str):
                    invalid += 1
                    continue
                req_id = row["req_id"]
                if row.get("capture") == "request":
                    requests[req_id] = row
                elif row.get("capture") == "response":
                    responses[req_id] = row
    except OSError as exc:
        return _check("unreadable", f"观察代理日志无法读取（{type(exc).__name__}）", path=str(path))

    pairs = []
    for req_id, request in requests.items():
        response = responses.get(req_id)
        if response is None:
            continue
        tools = request.get("tools") if isinstance(request.get("tools"), dict) else {}
        additional = request.get("additional_tools") if isinstance(request.get("additional_tools"), dict) else {}
        tool_count = _count(tools.get("count", 0))
        additional_count = _count(additional.get("count", 0))
        calls = response.get("calls") if isinstance(response.get("calls"), list) else []
        pairs.append({
            "model": _safe_text(request.get("model")),
            "request_tools_state": _safe_text(tools.get("state")),
            "request_tools_count": tool_count,
            "request_additional_tools_state": _safe_text(additional.get("state")),
            "request_additional_tools_count": additional_count,
            "request_has_tool_declaration": tool_count > 0 or additional_count > 0,
            "response_call_count": len(calls),
            "response_call_types": sorted({
                _safe_text(call.get("type")) for call in calls
                if isinstance(call, dict) and _safe_text(call.get("type"))
            }),
            "response_call_names": sorted({
                _safe_text(call.get("name")) for call in calls
                if isinstance(call, dict) and _safe_text(call.get("name"))
            }),
        })
    if not pairs:
        return _check("no_pairs", "日志中没有可配对的 request/response 元数据",
                      path=str(path), lines_read=lines_read, invalid_lines=invalid,
                      request_count=len(requests), response_count=len(responses))
    latest = pairs[-1]
    if latest["request_has_tool_declaration"]:
        state = "request_declared_tools"
        detail = "最近一对请求包含工具声明；这只证明客户端发出了声明"
    else:
        state = "request_missing_tools"
        detail = "最近一对请求未包含可识别工具声明；这支持检查客户端装配层，但不单独证明根因"
    return _check(state, detail, path=str(path), lines_read=lines_read,
                  invalid_lines=invalid, request_count=len(requests),
                  response_count=len(responses), pair_count=len(pairs), latest=latest)


def collect_doctor(config: Any = None, *, codex_config: Optional[str] = None,
                   provider: Optional[str] = None,
                   observe_log: Optional[str] = None) -> Dict[str, Any]:
    """Collect a JSON-serialisable, read-only diagnostic report."""
    checks: Dict[str, Any] = {}
    config_path = codex_config
    bridge_enabled = bool(getattr(config, "bridge_enabled", False)) if config else False
    bridge_host = getattr(config, "bridge_host", "127.0.0.1") if config else "127.0.0.1"
    bridge_port = getattr(config, "bridge_port", 8787) if config else 8787
    bridge_upstream = getattr(config, "bridge_upstream_url", None) if config else None
    bridge_provider = getattr(config, "bridge_provider", None) if config else None
    if not config_path and config is not None:
        config_path = getattr(config, "codex_config_path", None)

    if config is None:
        checks["app_config"] = _check("not_loaded", "未提供应用配置；仍可查看进程与桥状态")
    else:
        checks["app_config"] = _check("loaded", "应用配置已读取（只读）")

    if config_path:
        path = Path(os.path.expandvars(os.path.expanduser(str(config_path))))
        if not path.is_file():
            checks["codex_config"] = _check("missing", "目标 config.toml 不存在", path=str(path))
        else:
            try:
                from .config_editor import read_provider_base_url

                name, url = read_provider_base_url(str(path), provider or bridge_provider)
                checks["codex_config"] = _check(
                    "readable", "目标 config.toml 可读取；未读取凭据", path=str(path),
                    provider=name, base_url=_safe_url(url),
                )
            except Exception as exc:  # read-only diagnostic; preserve exact file
                checks["codex_config"] = _check(
                    "invalid", f"目标 config.toml 无法读取 provider（{type(exc).__name__}）",
                    path=str(path),
                )
    else:
        checks["codex_config"] = _check("not_configured", "未提供目标 config.toml 路径")

    checks["runtime"] = _runtime_check(config)
    checks["processes"] = _proc_snapshot()
    checks["bridge"] = {
        "enabled": bridge_enabled,
        "host": str(bridge_host),
        "port": bridge_port,
        "upstream": _safe_url(bridge_upstream),
        "probe": _bridge_probe(str(bridge_host), bridge_port) if bridge_enabled
                 else _check("disabled", "本地桥开关关闭；未发起连接"),
    }
    # The desktop's per-turn tool list is intentionally not persisted by the
    # manager.  Making that uncertainty explicit prevents a healthy process or
    # bridge check from being misreported as proof that GPT has tools.
    checks["tool_registry"] = _check(
        "unobservable",
        "管理器无法读取桌面当前回合的 tools/additional_tools；请在新 GPT 回合执行一条明确命令取证",
    )
    checks["observation"] = _observation_check(observe_log)

    warnings = []
    if checks["processes"]["state"] == "host_not_observed":
        warnings.append("host_not_observed")
    if checks["bridge"]["probe"]["state"] == "unreachable":
        warnings.append("bridge_unreachable")
    if checks["codex_config"]["state"] in {"missing", "invalid"}:
        warnings.append("codex_config_unavailable")
    if checks["observation"]["state"] == "request_missing_tools":
        warnings.append("client_sent_no_tool_declaration")
    return {
        "schema": "codex-model-manager-doctor-v1",
        "read_only": True,
        "checks": checks,
        "warnings": warnings,
        "next_step": "tool_registry 状态只能通过桌面新回合的实际命令调用确认；本报告不自动修复",
    }


def format_doctor(report: Dict[str, Any]) -> str:
    """Human-readable output for the CLI without dumping raw process commands."""
    checks = report.get("checks", {})
    lines = ["Codex 工具链只读诊断", "- 不修改配置、不重启、不读取 API Key"]
    for key in ("app_config", "codex_config", "runtime", "processes", "bridge", "tool_registry", "observation"):
        value = checks.get(key, {})
        if key == "bridge":
            probe = value.get("probe", {})
            lines.append(f"- bridge: {'启用' if value.get('enabled') else '关闭'}；"
                         f"监听 {value.get('host')}:{value.get('port')}；{probe.get('detail', '')}")
        else:
            lines.append(f"- {key}: {value.get('state', 'unknown')}；{value.get('detail', '')}")
    if report.get("warnings"):
        lines.append("- 需要关注：" + ", ".join(report["warnings"]))
    lines.append("- 下一步：" + str(report.get("next_step", "")))
    return "\n".join(lines)
