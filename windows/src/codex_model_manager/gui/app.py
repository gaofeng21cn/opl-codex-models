"""tkinter GUI for the Windows Codex model manager.

Default behaviour is preview/sandbox: sync reads the bundled catalog into an
isolated CODEX_HOME and merges into a local models.json, but never touches the
real ~/.codex config. Writing to Codex requires an explicit "应用并写入 Codex"
action that shows the target path + diff, then backs up and writes atomically.

The module is import-safe without a display so tests can import it.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core.app_config import AppConfiguration
from ..core.backup import backup_file, restore_file, classify_backup
from ..core.custom_models import NewModelDraft
from ..core.errors import CodexModelError, ConfigurationNotFound, InvalidConfiguration, RuntimeNotFound
from ..core.reasoning import ReasoningSettings
from ..core.safe_preview import parse_error_note, preview, render_preview, same_as_current
from ..services.catalog_data_service import CatalogDataService, CatalogSnapshot

KNOWN_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def run_sync_in_background(service, schedule, on_done, on_error) -> None:
    """Run service.run_sync() off the UI thread and hand back a SINGLE completion or
    error callback via `schedule(fn)` (the UI thread decides when to run `fn`, e.g.
    root.after).

    Two correctness requirements this satisfies:
      - The exception text is captured synchronously INSIDE the except block
        (wrapped in a default arg), so the scheduled callback never reads a cleared
        frame variable. The old code's `except exc: root.after(0, lambda: f(exc))`
        failed with NameError after the except block ended.
      - `service` is captured at start, so a user swapping the runtime mid-flight
        cannot make the running task write with different config.
    `schedule` may raise if the window was destroyed; we swallow that so the worker
    thread never crashes on close.
    """
    import sys
    import threading

    def work() -> None:
        try:
            result = service.run_sync()
            status = result.status
        except Exception:
            exc = sys.exc_info()[1]
            msg = str(exc) if exc is not None else "未知同步错误"
            try:
                schedule(lambda text=msg: on_error(text))
            except Exception:  # window closed / no UI
                pass
        else:
            try:
                schedule(lambda s=status: on_done(s))
            except Exception:  # window closed / no UI
                pass

    threading.Thread(target=work, name="codex-model-sync", daemon=True).start()


class ReasoningDialog(tk.Toplevel):
    """Pick supported reasoning efforts + a default effort."""

    def __init__(self, master, slug, current: ReasoningSettings, on_save):
        super().__init__(master)
        self.title(f"编辑推理档位 - {slug}")
        self.resizable(False, False)
        self.grab_set()
        self.configure(padx=12, pady=12)
        self.vars = {}
        choices = current.available_efforts if current.available_efforts else KNOWN_EFFORTS
        lbl = ttk.Label(self, text="支持的档位（默认档位必须在其中）")
        lbl.grid(row=0, column=0, columnspan=2, sticky="w")
        self.default_var = tk.StringVar(value=current.default_effort)
        for i, effort in enumerate(choices):
            var = tk.BooleanVar(value=effort in current.supported_efforts)
            self.vars[effort] = var
            row = i + 1
            ttk.Checkbutton(self, text=effort, variable=var,
                            command=lambda e=effort: self._sync_default(e)).grid(
                row=row, column=0, sticky="w", padx=6)
            ttk.Radiobutton(self, text="默认", variable=self.default_var, value=effort).grid(
                row=row, column=1, sticky="w")
        row = len(choices) + 1
        btn = ttk.Button(self, text="保存", command=self._save)
        btn.grid(row=row, column=0, columnspan=2, pady=10)
        self.on_save = on_save

    def _sync_default(self, changed):
        selected = [e for e, v in self.vars.items() if v.get()]
        if self.default_var.get() not in selected:
            self.default_var.set(selected[-1] if selected else "")

    def _save(self):
        supported = [e for e, v in self.vars.items() if v.get()]
        default = self.default_var.get()
        settings = ReasoningSettings(supported_efforts=supported, default_effort=default)
        if not settings.is_valid:
            messagebox.showerror("无效配置", "请至少选择一个档位，并将默认档位设为其中之一。", parent=self)
            return
        self.on_save(settings)
        self.destroy()


class AddModelDialog(tk.Toplevel):
    def __init__(self, master, catalog, existing, on_submit):
        super().__init__(master)
        self.title("新增自定义模型")
        self.resizable(False, False)
        self.grab_set()
        self.configure(padx=12, pady=12)
        self.catalog = catalog
        self.existing = existing
        self.on_submit = on_submit
        self.vars = {
            "slug": tk.StringVar(), "name": tk.StringVar(), "description": tk.StringVar(),
            "template": tk.StringVar(value=catalog[0].slug if catalog else ""),
            "context": tk.StringVar(value="128000"),
            "image": tk.BooleanVar(value=False),
            "image_original": tk.BooleanVar(value=False),
            "reasoning_efforts": tk.StringVar(value="low,high,max"),
            "reasoning_default": tk.StringVar(value="high"),
        }
        rows = [
            ("模板模型", ttk.Combobox(self, textvariable=self.vars["template"],
                                      values=[m.slug for m in catalog], state="readonly")),
            ("标识 (slug)", ttk.Entry(self, textvariable=self.vars["slug"])),
            ("名称", ttk.Entry(self, textvariable=self.vars["name"])),
            ("描述", ttk.Entry(self, textvariable=self.vars["description"])),
            ("上下文窗口", ttk.Entry(self, textvariable=self.vars["context"])),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=4)
            widget.grid(row=r, column=1, sticky="we", padx=4, pady=4)
        ttk.Checkbutton(self, text="支持图像", variable=self.vars["image"]).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(self, text="支持原始图像细节", variable=self.vars["image_original"]).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Label(self, text="推理档位 (逗号分隔)").grid(row=len(rows) + 2, column=0, sticky="w", padx=4)
        ttk.Entry(self, textvariable=self.vars["reasoning_efforts"]).grid(
            row=len(rows) + 2, column=1, sticky="we", padx=4)
        ttk.Label(self, text="默认档位").grid(row=len(rows) + 3, column=0, sticky="w", padx=4)
        ttk.Entry(self, textvariable=self.vars["reasoning_default"]).grid(
            row=len(rows) + 3, column=1, sticky="we", padx=4)
        ttk.Button(self, text="保存", command=self._save).grid(
            row=len(rows) + 4, column=0, columnspan=2, pady=10)

    def _save(self):
        try:
            context = int(self.vars["context"].get())
        except ValueError:
            messagebox.showerror("无效数值", "上下文窗口必须是正整数。", parent=self)
            return
        draft = NewModelDraft(
            slug=self.vars["slug"].get(),
            display_name=self.vars["name"].get(),
            description=self.vars["description"].get(),
            template_slug=self.vars["template"].get(),
            context_window=context,
            supports_image=self.vars["image"].get(),
            supports_original_image_detail=self.vars["image_original"].get(),
            reasoning=self._reasoning(),
        )
        draft.reasoning = self._reasoning()
        self.on_submit(draft)
        self.destroy()

    def _reasoning(self):
        supported = [e.strip() for e in self.vars["reasoning_efforts"].get().split(",") if e.strip()]
        return ReasoningSettings(supported_efforts=supported, default_effort=self.vars["reasoning_default"].get())


class SettingsDialog(tk.Toplevel):
    """Settings dialog for backend, distro, runtime path, CODEX_HOME, and target config."""

    def __init__(self, master, config: AppConfiguration, on_save):
        super().__init__(master)
        self.title("Codex 运行时设置")
        self.resizable(True, False)
        self.grab_set()
        self.configure(padx=12, pady=12)
        self.config = config
        self.on_save = on_save

        self.backend_var = tk.StringVar(value=config.codex_backend or "auto")
        self.distro_var = tk.StringVar(value=config.codex_distro or "")
        self.runtime_var = tk.StringVar(value=config.codex_runtime_path or "")
        self.codex_home_var = tk.StringVar(value=config.target_codex_home or "")
        self.config_path_var = tk.StringVar(value=config.codex_config_path or "")

        row = 0
        ttk.Label(self, text="后端").grid(row=row, column=0, sticky="w", padx=4, pady=4)
        backend_cb = ttk.Combobox(self, textvariable=self.backend_var,
                                  values=["auto", "native", "wsl"], state="readonly")
        backend_cb.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        backend_cb.bind("<<ComboboxSelected>>", lambda _: self._update_distro_choices())

        row += 1
        ttk.Label(self, text="WSL 发行版").grid(row=row, column=0, sticky="w", padx=4, pady=4)
        self.distro_cb = ttk.Combobox(self, textvariable=self.distro_var)
        self.distro_cb.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        self._update_distro_choices()

        row += 1
        ttk.Label(self, text="运行时路径").grid(row=row, column=0, sticky="w", padx=4, pady=4)
        rt_frame = ttk.Frame(self)
        rt_frame.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        ttk.Entry(rt_frame, textvariable=self.runtime_var).pack(side="left", fill="x", expand=True)
        ttk.Button(rt_frame, text="…", width=3, command=self._browse_runtime).pack(side="left")

        row += 1
        ttk.Label(self, text="CODEX_HOME").grid(row=row, column=0, sticky="w", padx=4, pady=4)
        home_frame = ttk.Frame(self)
        home_frame.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        ttk.Entry(home_frame, textvariable=self.codex_home_var).pack(side="left", fill="x", expand=True)
        ttk.Button(home_frame, text="…", width=3, command=self._browse_codex_home).pack(side="left")

        row += 1
        ttk.Label(self, text="目标 config.toml").grid(row=row, column=0, sticky="w", padx=4, pady=4)
        cfg_frame = ttk.Frame(self)
        cfg_frame.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        ttk.Entry(cfg_frame, textvariable=self.config_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(cfg_frame, text="…", width=3, command=self._browse_config_path).pack(side="left")

        row += 1
        ttk.Button(self, text="保存", command=self._save).grid(
            row=row, column=0, columnspan=2, pady=10)

        self.columnconfigure(1, weight=1)

    def _update_distro_choices(self):
        if self.backend_var.get() == "wsl":
            try:
                from ..core.runtime import discover
                distros = sorted({r.distro for r in discover(include_wsl=True) if r.distro})
                self.distro_cb.configure(values=distros)
            except Exception:
                self.distro_cb.configure(values=[])
        else:
            self.distro_cb.configure(values=[])

    def _browse_runtime(self):
        path = filedialog.askopenfilename(title="选择 Codex 可执行文件")
        if path:
            self.runtime_var.set(path)

    def _browse_codex_home(self):
        path = filedialog.askdirectory(title="选择 CODEX_HOME 目录")
        if path:
            self.codex_home_var.set(path)

    def _browse_config_path(self):
        path = filedialog.askopenfilename(title="选择目标 config.toml")
        if path:
            self.config_path_var.set(path)

    def _save(self):
        self.config.codex_backend = self.backend_var.get()
        self.config.codex_distro = self.distro_var.get().strip() or None
        self.config.codex_runtime_path = self.runtime_var.get().strip() or None
        self.config.target_codex_home = self.codex_home_var.get().strip() or None
        self.config.codex_config_path = self.config_path_var.get().strip() or None
        self.on_save(self.config)
        self.destroy()


class CodexModelManagerApp:
    def __init__(self, root, config_url: str | None = None):
        self.root = root
        self.root.title("Codex 模型管理器（Windows）")
        self.config_url = config_url or AppConfiguration.default_url()
        self._config: AppConfiguration | None = None
        self.data: CatalogDataService | None = None
        self._snapshot: CatalogSnapshot | None = None
        self._last_err: str = ""
        self._config_error: str = ""
        self._runtime_error: str = ""
        self._syncing = False
        self._build()
        self._reload(build=True)
        # populate the list once a working runtime/config is available
        if self.data is not None:
            self.refresh()

    def _load_config(self) -> AppConfiguration:
        try:
            return AppConfiguration.load(self.config_url)
        except ConfigurationNotFound:
            cfg = AppConfiguration.recommended()
            cfg.save(self.config_url)
            return cfg

    def _reload(self, build: bool = False) -> None:
        """Reload config + data so the UI can recover after a fix (offline/runtime)."""
        try:
            self._config = self._load_config()
            self._config_error = ""
        except CodexModelError as exc:
            # Corrupt/invalid config: keep the file, don't auto-overwrite, go offline.
            self._config = None
            self._config_error = f"配置无效（已保留原文件）：{exc}"
        except Exception as exc:  # noqa: BLE001
            self._config = None
            self._config_error = f"读取配置失败：{exc}"

        self._runtime_error = ""
        if self._config is not None:
            try:
                self.data = CatalogDataService(self._config.resolved())
            except RuntimeNotFound as exc:
                self.data = None
                self._runtime_error = str(exc)
            except CodexModelError as exc:
                self.data = None
                self._runtime_error = f"配置解析失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                self.data = None
                self._runtime_error = f"初始化失败：{exc}"
        else:
            self.data = None

        self._update_status()
        if build:
            return
        self.refresh()

    def _update_status(self) -> None:
        if self._config_error:
            self.status.set(f"启动受限：{self._config_error}")
        elif self._runtime_error:
            self.status.set(f"无可用 Codex 运行时：{self._runtime_error}")
        elif self._syncing:
            self.status.set("同步中……")
        else:
            base = "就绪（离线预览）" if self.data is None else "就绪"
            # Append evidence state when a config is loaded.
            if self._config is not None:
                from ..core.compat_probe import evidence_from_dict, evidence_valid
                evidence = evidence_from_dict(self._config.compat_evidence)
                if evidence is not None:
                    try:
                        target = self._config.runtime_target()
                    except CodexModelError:
                        target = None
                    if target is not None and evidence_valid(evidence, target):
                        base += "（兼容性已验证）"
                    else:
                        base += "（证据过期/不匹配，需重新验证）"
                else:
                    base += "（未验证兼容性）"
            self.status.set(base)

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x")
        ttk.Button(top, text="同步（预览，不写 Codex）", command=self.sync).pack(
            side="left", **pad)
        ttk.Button(top, text="新增模型", command=self.add_model).pack(side="left", **pad)
        ttk.Button(top, text="编辑推理档位", command=self.edit_reasoning).pack(side="left", **pad)
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="left", **pad)
        ttk.Button(top, text="备份", command=self.backup).pack(side="left", **pad)
        ttk.Button(top, text="恢复…", command=self.restore).pack(side="left", **pad)
        ttk.Button(top, text="设置 Codex 运行时…", command=self.choose_runtime).pack(
            side="left", **pad)
        ttk.Button(top, text="验证兼容性", command=self.verify_compatibility).pack(
            side="left", **pad)
        ttk.Button(top, text="诊断工具链", command=self.doctor).pack(
            side="left", **pad)
        ttk.Button(top, text="应用并写入 Codex…", command=self.apply_to_codex).pack(
            side="left", **pad)
        ttk.Button(top, text="撤销 apply", command=self.undo_apply).pack(
            side="left", **pad)
        ttk.Button(top, text="启用本地桥…", command=self.bridge_enable).pack(
            side="left", **pad)
        ttk.Button(top, text="关闭本地桥", command=self.bridge_disable).pack(
            side="left", **pad)

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True)
        self.search_var = tk.StringVar()
        ttk.Label(mid, text="搜索").pack(side="left", padx=8)
        ttk.Entry(mid, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=8)
        self.search_var.trace_add("write", lambda *_: self._render_list())

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        self.listbox = tk.Listbox(left, height=20)
        sb = ttk.Scrollbar(left, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelection>>", lambda _: self.show_detail())

        self.detail = tk.Text(right, height=20, state="disabled")
        self.detail.pack(fill="both", expand=True)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x")
        self.status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(side="bottom", fill="x", padx=8, pady=4)
        self._models_cache = []

    def refresh(self):
        try:
            self._snapshot = self.data.load_snapshot()
            self._last_err = ""
        except Exception as exc:  # noqa: BLE001
            self._snapshot = None
            self._last_err = f"{exc}"
            self.status.set(f"加载失败：{exc}")
        self._render_list()

    def _visible_models(self):
        if not self._snapshot:
            return []
        needle = self.search_var.get()
        return [m for m in self._snapshot.models if m.matches(needle)]

    def _render_list(self):
        self.listbox.delete(0, tk.END)
        self._models_cache = self._visible_models()
        for m in self._models_cache:
            tag = "自定义" if m.source == "custom" else "官方"
            self.listbox.insert(tk.END, f"[{tag}] {m.slug} — {m.display_name}")

    def show_detail(self):
        sel = self.listbox.curselection()
        if not sel or not self._models_cache:
            return
        m = self._models_cache[sel[0]]
        text = (
            f"slug: {m.slug}\n"
            f"来源: {'自定义' if m.source == 'custom' else '官方'}\n"
            f"名称: {m.display_name}\n"
            f"描述: {m.description}\n"
            f"当前上下文: {m.context_window}\n"
            f"最大上下文: {m.max_context_window}\n"
            f"可见性: {m.visibility}\n"
            f"输入模态: {', '.join(m.input_modalities)}\n"
            f"推理档位: {', '.join(m.reasoning.supported_efforts)}\n"
            f"默认档位: {m.reasoning.default_effort}\n"
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", tk.END)
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def sync(self):
        if self._syncing:
            messagebox.showinfo("同步", "同步正在进行中，请稍候。")
            return
        if self.data is None:
            messagebox.showwarning("不可同步", "当前无可用 Codex 运行时。请先用“设置 Codex 运行时…”选择，或改用离线预览。")
            return

        service = self.data  # capture now; a runtime swap must not affect this run
        self._syncing = True
        self._update_status()
        run_sync_in_background(
            service,
            schedule=lambda fn: self.root.after(0, fn),
            on_done=self._on_sync_done,
            on_error=self._on_sync_error,
        )

    def _on_sync_done(self, status: str) -> None:
        self._syncing = False
        try:
            self.refresh()
        finally:
            self.status.set(f"同步完成：{status}")

    def _on_sync_error(self, message: str) -> None:
        self._syncing = False
        self.status.set("同步失败")
        messagebox.showerror("同步失败", message)

    def choose_runtime(self) -> None:
        """Open a settings dialog for backend, distro, runtime path, CODEX_HOME,
        and the target Codex config.toml path."""
        if self._config is None:
            messagebox.showerror("不可设置", self._config_error or "配置不可用。")
            return
        SettingsDialog(self.root, self._config, self._on_settings_saved)

    def _on_settings_saved(self, config: AppConfiguration) -> None:
        config.save(self.config_url)
        self._config = config
        self._reload()
        self.status.set("已保存运行时设置。")

    def add_model(self):
        if not self._snapshot or not self._snapshot.models:
            messagebox.showwarning("暂无模型", "请先同步获取模型目录。")
            return
        AddModelDialog(
            self.root, self._snapshot.models,
            {m.slug for m in self._snapshot.models}, self._on_add_model)

    def _on_add_model(self, draft: NewModelDraft):
        try:
            self.data.add_custom_model(draft)
            self.refresh()
            self.status.set(f"已新增 {draft.normalized_slug}，请同步后生效")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("新增失败", str(exc))

    def edit_reasoning(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请在列表中选择一个自定义模型。")
            return
        m = self._models_cache[sel[0]]
        if m.source != "custom":
            messagebox.showinfo("提示", "官方模型的推理档位随目录同步，详情中只读。")
            return
        ReasoningDialog(self.root, m.slug, m.reasoning, lambda s: self._on_reasoning(m.slug, s))

    def _on_reasoning(self, slug, settings):
        try:
            self.data.update_reasoning(settings, slug)
            self.refresh()
            self.status.set(f"已更新 {slug} 推理档位")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("更新失败", str(exc))

    def backup(self):
        try:
            paths = self._config.resolved_paths()
            p1 = backup_file(paths.custom_source, paths.backup_directory, prefix="custom-models")
            p2 = backup_file(paths.merged_catalog, paths.backup_directory, prefix="models")
            self.status.set(f"备份完成：{p1 or p2 or '（无文件需备份）'}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("备份失败", str(exc))

    def restore(self):
        if self._config is None:
            messagebox.showerror("不可恢复", self._config_error or "配置不可用。")
            return
        paths = self._config.resolved_paths()
        backup_dir = paths.backup_directory
        if not os.path.isdir(backup_dir):
            messagebox.showwarning("无备份", "备份目录不存在。")
            return
        path = filedialog.askopenfilename(
            title="选择备份文件", initialdir=backup_dir,
            filetypes=[("备份文件", "*.bak")])
        if not path:
            return

        # Classify by content, then let the user pick one of the compatible targets.
        kind = classify_backup(path)
        if kind == "unknown":
            messagebox.showerror("无法识别", "该备份既非模型目录 JSON 也非 TOML 配置，已拒绝恢复。")
            return
        if kind == "toplevel_config" and not self._config.codex_config_path:
            messagebox.showerror("不可恢复", "恢复 config 需要应用配置设置 codexConfigPath。")
            return

        if kind == "model_catalog_json":
            options = ["merged", "custom"]
        else:
            options = ["config"]
        from tkinter import simpledialog
        choice = simpledialog.askstring(
            "选择恢复目标",
            f"备份类型：{kind}\n恢复目标（{', '.join(options)}）：",
            initialvalue=options[0])
        if choice not in options:
            return
        target = paths.merged_catalog if choice == "merged" else (
            paths.custom_source if choice == "custom" else self._config.codex_config_path)

        if not messagebox.askyesno("确认恢复", f"将用备份覆盖：\n{target}\n当前文件会先自动备份。继续？"):
            return
        try:
            if kind == "model_catalog_json":
                from ..core.safe_preview import validate_catalog_file
                validate_catalog_file(path, require_priority=(choice == "merged"),
                                      allow_empty=(choice == "custom"), name="备份模型目录")
            before = restore_file(path, target, paths.backup_directory, prefix=os.path.splitext(os.path.basename(target))[0])
            self.refresh()
            self.status.set(f"已恢复 {os.path.basename(path)}" + (f"（原文件已备份：{os.path.basename(before)}）" if before else ""))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("恢复失败", str(exc))

    def apply_to_codex(self):
        """Explicit, gated write. Target comes ONLY from config.codexConfigPath
        (never re-derived from the environment), and is validated by the shared
        apply_gate(): demo writes only inside the app sandbox, real mode only with
        verified Codex compatibility (else read-only). Shows only the
        model_catalog_json change (safe)."""
        from ..core.config_editor import set_model_catalog
        from ..core.safe_preview import apply_gate, render_preview

        if self._config is None:
            messagebox.showerror("不可写入", self._config_error or "配置不可用。")
            return
        # Local-only path resolution: the shared apply_gate performs the runtime
        # check for a real write, so previewing must not require a runnable Codex.
        paths = self._config.resolved_paths()
        gate = apply_gate(self._config, paths.merged_catalog,
                          self._config.codex_config_path or "", readonly=False)
        if gate.reason:
            # blocked real write: show the safe preview + the honest reason, write nothing.
            info = preview(gate.target, paths.merged_catalog)
            text = render_preview(info) + f"\n\n未写入：{gate.reason}"
            messagebox.showwarning("未写入（只读）", text)
            self.status.set("未写入：未通过校验")
            return

        info = preview(gate.target, paths.merged_catalog)
        if not info["parse_ok"]:
            messagebox.showerror("config 无法解析", parse_error_note(gate.target))
            return
        detail = render_preview(info)
        detail += "\n\n此预览仅显示 model_catalog_json 这一项，不读取或输出其他任何配置/密钥。确认写入？"
        if not messagebox.askyesno("应用到 Codex（显式写入）", detail):
            self.status.set("已取消写入")
            return
        if same_as_current(info):
            self.status.set("model_catalog_json 已等于拟写入值，未变更。")
            return
        try:
            # Convert the merged catalog path to the form the runtime reads.
            # For WSL targets this is a /mnt/c/... Linux path; for native it's unchanged.
            try:
                runtime_target = self._config.runtime_target()
                catalog_value = runtime_target.to_runtime_path(paths.merged_catalog)
            except CodexModelError:
                catalog_value = paths.merged_catalog

            prev_value = set_model_catalog(catalog_value, gate.target, paths.backup_directory)
            self._config.last_apply = {
                "targetConfig": gate.target,
                "previousValue": prev_value,
                "appliedValue": catalog_value,
            }
            self._config.save(self.config_url)
            self.status.set(f"已备份原 config 并写入 model_catalog_json：{catalog_value}")
        except InvalidConfiguration as exc:
            messagebox.showerror("写入失败（已保留原文件）", str(exc))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("写入失败", str(exc))

    def verify_compatibility(self):
        """Run the real compatibility behaviour proof and store evidence."""
        from ..core.compat_probe import probe_compatibility, evidence_to_dict, evidence_from_dict, evidence_valid

        if self._config is None:
            messagebox.showerror("不可验证", self._config_error or "配置不可用。")
            return
        try:
            target = self._config.runtime_target()
        except CodexModelError as exc:
            messagebox.showerror("无法构建运行时目标", str(exc))
            return

        self.status.set("正在运行兼容性行为证明……")
        self.root.update_idletasks()
        try:
            evidence = probe_compatibility(target)
        except Exception as exc:  # noqa: BLE001
            self.status.set("兼容性证明失败")
            messagebox.showerror("兼容性证明失败", str(exc))
            return

        lines = [
            f"结果：{'通过' if evidence.ok else '失败'} — {evidence.reason}",
            f"runtime_version={evidence.runtime_version}",
            f"runtime_sha256={evidence.runtime_sha256[:16]}…（已脱敏）",
            f"bundled_model_count={evidence.bundled_model_count}",
            f"marker_loaded={evidence.marker_loaded}",
            f"marker_absent_from_bundled={evidence.marker_absent_from_bundled}",
            f"missing_catalog_exit_code={evidence.missing_catalog_exit_code}",
            f"probe_scheme={evidence.probe_scheme}",
        ]
        if evidence.ok:
            self._config.compat_evidence = evidence_to_dict(evidence)
            self._config.save(self.config_url)
            self.status.set("兼容性证明通过，已存储证据。")
            messagebox.showinfo("兼容性证明通过", "\n".join(lines))
        else:
            self.status.set("兼容性证明未通过")
            messagebox.showwarning("兼容性证明未通过", "\n".join(lines))

    def doctor(self):
        """Show read-only evidence about the desktop tool path."""
        from ..core.doctor import collect_doctor, format_doctor

        try:
            report = collect_doctor(
                self._config,
                codex_config=(self._config.codex_config_path if self._config else None),
            )
            text = format_doctor(report)
            self.status.set("只读诊断完成；当前回合工具清单仍需实际命令确认。")
            if report.get("warnings"):
                messagebox.showwarning("Codex 工具链诊断", text, parent=self.root)
            else:
                messagebox.showinfo("Codex 工具链诊断", text, parent=self.root)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("诊断失败", str(exc), parent=self.root)

    def undo_apply(self):
        """Undo the last apply of model_catalog_json, with conflict detection."""
        from ..core.config_editor import undo_model_catalog

        if self._config is None:
            messagebox.showerror("不可撤销", self._config_error or "配置不可用。")
            return
        record = self._config.last_apply
        if not record:
            messagebox.showwarning("无可撤销", "没有已记录的 apply 操作可撤销。")
            return

        target_config = record.get("targetConfig")
        prev_value = record.get("previousValue")
        applied_value = record.get("appliedValue")
        if not target_config:
            messagebox.showerror("不可撤销", "apply 记录缺少 targetConfig。")
            return

        if not messagebox.askyesno(
            "确认撤销",
            f"将撤销对以下 config 的 apply：\n{target_config}\n"
            f"写入值：{applied_value}\n恢复为：{prev_value or '（删除键）'}\n\n继续？",
        ):
            return

        paths = self._config.resolved_paths()
        try:
            message = undo_model_catalog(
                target_config, paths.backup_directory, prev_value, applied_value,
            )
            self._config.last_apply = None
            self._config.save(self.config_url)
            self.status.set("已撤销 apply。")
            messagebox.showinfo("撤销完成", message)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("撤销失败", str(exc))

    def bridge_enable(self):
        """Point the active provider at the local bridge. Explicit, previewed write.

        Only ``model_providers.<provider>.base_url`` is touched, and only after the
        diff is shown and confirmed. The bridge itself stays off until the user
        runs ``bridge start``; this is a switch, not an autostart.
        """
        from ..core.bridge import ensure_loopback
        from ..core.config_editor import read_provider_base_url, set_provider_base_url
        from ..core.safe_preview import demo_write_block_reason

        if self._config is None:
            messagebox.showerror("不可启用", self._config_error or "配置不可用。")
            return
        target = self._config.codex_config_path
        if not target:
            messagebox.showwarning(
                "未设置目标 config",
                "请先在「设置」里填写 Codex config.toml 路径。为避免误写真实配置，此处不会自动推断。",
            )
            return
        block = demo_write_block_reason(
            self._config, self._config.merged_catalog_path, target
        )
        if block:
            messagebox.showerror("不可启用本地桥", block)
            return
        try:
            provider, current_url = read_provider_base_url(target)
            host = ensure_loopback(self._config.bridge_host or "127.0.0.1")
        except ValueError as exc:
            messagebox.showerror("不可启用本地桥", str(exc))
            return
        port = int(self._config.bridge_port or 8787)
        local_url = f"http://{host}:{port}"
        existing = self._config.last_bridge_apply or {}
        same_target = (
            existing.get("targetConfig") == target
            and existing.get("provider") == provider
            and existing.get("appliedUrl") == local_url
        )
        if existing and current_url == local_url and same_target:
            self._config.bridge_enabled = True
            self._config.bridge_upstream_url = existing.get("previousUrl") or current_url
            self._config.bridge_provider = provider
            self._config.save(self.config_url)
            self.status.set("本地桥已经启用，保留原始回退地址；未重复写入。")
            messagebox.showinfo("本地桥已启用", "当前地址已经是桥地址，保留原始回退地址，未重复写入。")
            return
        if existing and current_url == existing.get("appliedUrl") and not same_target:
            messagebox.showerror("不可启用本地桥", "已有另一项本地桥配置处于启用状态，请先关闭。")
            return
        if not messagebox.askyesno(
            "启用本地桥（显式写入）",
            f"将改写：\n{target}\n\n"
            f"model_providers.{provider}.base_url\n"
            f"  {current_url}\n-> {local_url}\n\n"
            "只改这一项，其余配置与注释保持不变；写入前会自动备份。\n"
            "API Key 不会写入本地桥，仅透传 Codex 已有的请求头。\n\n继续？",
        ):
            self.status.set("已取消启用本地桥")
            return
        paths = self._config.resolved_paths()
        try:
            previous = set_provider_base_url(
                target, provider, local_url, paths.backup_directory
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启用失败（已保留原文件）", str(exc))
            return
        self._config.bridge_enabled = True
        self._config.bridge_host = host
        self._config.bridge_port = port
        self._config.bridge_upstream_url = current_url
        self._config.bridge_provider = provider
        self._config.last_bridge_apply = {
            "targetConfig": target,
            "provider": provider,
            "previousUrl": previous,
            "appliedUrl": local_url,
        }
        self._config.save(self.config_url)
        self.status.set(f"本地桥已启用：{provider}.base_url -> {local_url}")
        messagebox.showinfo(
            "本地桥已启用（还没运行）",
            f"model_providers.{provider}.base_url -> {local_url}\n"
            f"上游：{current_url}\n\n"
            "这是配置开关。桥进程需要另开终端启动：\n"
            "  python -m codex_model_manager bridge start\n\n"
            "停止：在该终端按 Ctrl+C，再点「关闭本地桥」恢复地址。",
        )

    def bridge_disable(self):
        """Restore the provider URL recorded by :meth:`bridge_enable`."""
        from ..core.config_editor import undo_provider_base_url

        if self._config is None:
            messagebox.showerror("不可关闭", self._config_error or "配置不可用。")
            return
        record = self._config.last_bridge_apply
        if not record:
            messagebox.showwarning("无可关闭", "没有已记录的本地桥配置，无需关闭。")
            return
        if not messagebox.askyesno(
            "关闭本地桥",
            f"将恢复：\n{record.get('targetConfig')}\n\n"
            f"model_providers.{record.get('provider')}.base_url\n"
            f"  {record.get('appliedUrl')}\n"
            f"-> {record.get('previousUrl') or '（删除该键）'}\n\n"
            "只在当前值仍是桥接写入值时才恢复，否则报告冲突、不擅自改动。\n\n继续？",
        ):
            return
        try:
            message = undo_provider_base_url(
                record["targetConfig"],
                record["provider"],
                record.get("previousUrl"),
                record["appliedUrl"],
                self._config.resolved_paths().backup_directory,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("关闭失败", str(exc))
            return
        if message.startswith("冲突："):
            messagebox.showwarning("未自动关闭", message)
            self.status.set("未自动关闭：检测到冲突")
            return
        self._config.bridge_enabled = False
        self._config.last_bridge_apply = None
        self._config.save(self.config_url)
        self.status.set("本地桥已关闭。")
        messagebox.showinfo(
            "本地桥已关闭", f"{message}\n\n如有 bridge start 进程，请在其终端按 Ctrl+C 停止。"
        )


def launch(config_url: str | None = None) -> int:
    root = tk.Tk()
    app = CodexModelManagerApp(root, config_url)
    root.mainloop()
    return 0


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="codex-model-manager-gui")
    parser.add_argument("--config", help="app 配置路径（默认使用 LOCALAPPDATA\\CodexModelManager\\config.json）")
    args = parser.parse_args()
    return launch(args.config)


if __name__ == "__main__":
    raise SystemExit(_main())
