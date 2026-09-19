"""GUI smoke test: build the tkinter app against a config and exercise basic
ops (render list, search, select a model, populate detail) without any real
Codex. Requires a display; marks PASS/FAIL and exits 0 on success.

Usage:
  python scripts/gui_smoke.py --config <demo config.json>
  python scripts/gui_smoke.py --config <demo config.json> --bridge

With ``--bridge`` the local-bridge switch is also driven through the real GUI
methods (enable -> verify the provider URL was rewritten -> disable -> verify
byte-for-byte restore).  The bridge check always works on a throwaway
``config.toml`` inside the system temp directory and restores the app config
afterwards, so no real Codex config is ever read or written.

This is a headless-ish verification of the GUI wiring. A human-driven visual
inspection is described in HANDOFF.md (not automated here).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path


class StubMessagebox:
    """Auto-answers dialogs and records them, so no window can block the run."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.shown: list[tuple[str, str]] = []

    def _record(self, kind: str, title: str):
        self.shown.append((kind, title))

    def askyesno(self, title, message, **_kw) -> bool:
        self._record("askyesno", title)
        return self.answer

    def showinfo(self, title, message, **_kw) -> None:
        self._record("showinfo", title)

    def showwarning(self, title, message, **_kw) -> None:
        self._record("showwarning", title)

    def showerror(self, title, message, **_kw) -> None:
        self._record("showerror", title)


def bridge_round_trip(app, failures: list[str]) -> None:
    """Drive enable/disable through the real GUI methods on a throwaway config."""
    from codex_model_manager.gui import app as gui_app

    config_url = app.config_url
    saved_config = Path(config_url).read_bytes() if Path(config_url).is_file() else None
    sandbox = Path(tempfile.mkdtemp(prefix="bridge-gui-smoke-"))
    original_messagebox = gui_app.messagebox
    gui_app.messagebox = StubMessagebox(answer=True)
    try:
        target = sandbox / "config.toml"
        target.write_text(
            '# keep this comment\nmodel_provider = "DeepSeek"\n\n'
            '[model_providers.DeepSeek]\nname = "Relay"\n'
            'base_url = "https://relay.example/v1"\nwire_api = "responses"\n',
            encoding="utf-8",
        )
        before = target.read_text(encoding="utf-8")
        app._config.codex_config_path = str(target)

        app.bridge_enable()
        after_enable = target.read_text(encoding="utf-8")
        if "base_url = \"http://127.0.0.1:8787\"" not in after_enable:
            failures.append("bridge enable 未把 base_url 指向本地桥")
        if "# keep this comment" not in after_enable:
            failures.append("bridge enable 丢失了注释")
        if not app._config.bridge_enabled or app._config.bridge_provider != "DeepSeek":
            failures.append("bridge enable 未记录开关状态/provider")

        app.bridge_disable()
        if target.read_text(encoding="utf-8") != before:
            failures.append("bridge disable 未逐字节还原 config.toml")
        if app._config.bridge_enabled:
            failures.append("bridge disable 未把开关置回关闭")
    finally:
        gui_app.messagebox = original_messagebox
        if saved_config is not None:
            Path(config_url).write_bytes(saved_config)
        shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="同时验证本地桥的启用/关闭开关（使用临时 config.toml）",
    )
    args = parser.parse_args()
    root = tk.Tk()
    from codex_model_manager.gui.app import CodexModelManagerApp

    app = CodexModelManagerApp(root, args.config)
    root.update()
    # list rendered
    n = app.listbox.size()
    if n == 0:
        print("FAIL: model list empty")
        root.destroy()
        return 1
    # search filter works
    app.search_var.set("gpt-6")
    root.update()
    filtered = app.listbox.size()
    if filtered == 0:
        print("FAIL: search returned nothing")
        root.destroy()
        return 1
    # select first visible model and populate detail
    app.search_var.set("")
    root.update()
    app.listbox.selection_set(0)
    root.update()
    app.show_detail()
    detail = app.detail.get("1.0", "end")
    if not detail.strip():
        print("FAIL: detail empty")
        root.destroy()
        return 1
    print(f"PASS: basic GUI ops ok (total={n}, search_hits={filtered})")

    if args.bridge:
        failures: list[str] = []
        bridge_round_trip(app, failures)
        if failures:
            for item in failures:
                print(f"FAIL: {item}")
            root.destroy()
            return 1
        print("PASS: 本地桥开关 enable/disable 往返正常（临时 config，未触碰真实配置）")

    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
