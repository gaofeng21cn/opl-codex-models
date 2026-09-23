"""tkinter GUI for the Windows Codex model manager.

Default behaviour is preview/sandbox: sync reads the bundled catalog into an
isolated CODEX_HOME and merges into a local models.json, but never touches the
real ~/.codex config. Writing to Codex requires an explicit "应用并写入 Codex"
action that shows the target path + diff, then backs up and writes atomically.

The module is import-safe without a display so tests can import it.
"""

from __future__ import annotations

import os
import copy
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core.app_config import AppConfiguration, sync_connection_config
from ..core.overrides import ModelFieldOverrides
from ..core.backup import backup_file, restore_file, classify_backup
from ..core.errors import (
    CodexModelError,
    ConfigurationNotFound,
    ConflictError,
    InvalidConfiguration,
    RuntimeNotFound,
)
from ..core.safe_preview import parse_error_note, preview, same_as_current
from ..services.catalog_data_service import CatalogDataService
from .model_page import ModelPage

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


class ContextOverrideDialog(tk.Toplevel):
    """Keep only checked context fields when the next official catalog arrives."""

    def __init__(self, master, model, current: ModelFieldOverrides, on_save):
        super().__init__(master)
        self.title(f"编辑上下文覆盖 - {model.slug}")
        self.resizable(False, False)
        self.grab_set()
        self.configure(padx=16, pady=12)
        self.on_save = on_save
        ttk.Label(self, text="只覆盖勾选字段，其他官方配置继续更新。", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.fields = []
        for row, (label, value, fallback) in enumerate((
            ("当前上下文", current.context_window, model.context_window),
            ("最大上下文", current.max_context_window, model.max_context_window),
        ), start=1):
            enabled = tk.BooleanVar(value=value is not None)
            entry_value = tk.StringVar(value=str(value if value is not None else fallback or ""))
            ttk.Checkbutton(self, text=f"覆盖{label}", variable=enabled).grid(
                row=row, column=0, sticky="w", padx=4, pady=4)
            ttk.Entry(self, textvariable=entry_value, justify="right", width=18).grid(
                row=row, column=1, sticky="e", padx=4, pady=4)
            self.fields.append((enabled, entry_value))
        ttk.Label(self, text="单位：token；384K = 393216。", style="Muted.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 10))
        ttk.Button(self, text="全部跟随官方", command=self._clear).grid(row=4, column=0, sticky="w")
        ttk.Button(self, text="保存并同步", command=self._save).grid(row=4, column=1, sticky="e")

    def _clear(self):
        for enabled, _ in self.fields:
            enabled.set(False)

    def _save(self):
        values = []
        for enabled, entry in self.fields:
            if not enabled.get():
                values.append(None)
                continue
            try:
                values.append(int(entry.get().strip()))
            except ValueError:
                messagebox.showerror("无效数值", "上下文必须是正整数。", parent=self)
                return
        override = ModelFieldOverrides(*values)
        try:
            override.validate()
        except InvalidConfiguration as exc:
            messagebox.showerror("无效配置", str(exc), parent=self)
            return
        if self.on_save(override):
            self.destroy()


class CodexModelManagerApp:
    def __init__(self, root, config_url: str | None = None):
        self.root = root
        self.root.title("Codex 模型管理器")
        self.config_url = config_url or AppConfiguration.default_url()
        self._config: AppConfiguration | None = None
        self.data: CatalogDataService | None = None
        self._config_error: str = ""
        self._runtime_error: str = ""
        self._syncing = False
        self._build()
        self._reload(build=True)
        self.bridge_panel.bind_configuration()
        # Populate the model page from the connection page's config: reading the
        # catalog needs a config path, not a runnable Codex.
        self.refresh()

    def _load_config(self) -> AppConfiguration:
        try:
            return AppConfiguration.load(self.config_url)
        except ConfigurationNotFound:
            cfg = AppConfiguration.recommended()
            # Keep portable distributions self-contained; merely opening the GUI
            # must not create model sources or change a real Codex configuration.
            home = Path(self.config_url).parent
            cfg.custom_source_path = str(home / 'custom-models.json')
            cfg.merged_catalog_path = str(home / 'models.json')
            cfg.sync_log_path = str(home / 'logs/sync.jsonl')
            cfg.error_log_path = str(home / 'logs/sync.error.log')
            cfg.backup_directory_path = str(home / 'backups')
            cfg.takeover_catalog_path = str(home / 'pending-models.json')
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

        if self._config is not None:
            # Repair the connection.json <-> codexConfigPath split at load time, so
            # an existing installation whose connection page works but whose app
            # config has no codexConfigPath stops reporting "接管状态不可用".
            try:
                sync_connection_config(self._config, self.config_url, save=True)
            except Exception:  # noqa: BLE001 - never block startup on a prefs repair
                pass

        self._runtime_error = ""
        if self._config is not None:
            try:
                self.data = CatalogDataService(self._config.resolved_paths())
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
                        base += "（运行时目录读取证据有效）"
                    else:
                        base += "（目录读取证据过期/不匹配，需重新验证）"
                else:
                    base += "（未验证运行时目录读取）"
            self.status.set(base)

    def _build(self):
        from .bridge_panel import BridgePanel
        self.root.geometry('1120x740')
        self.root.minsize(1000, 660)
        import sv_ttk
        import tkinter.font as tkfont
        sv_ttk.set_theme('light')
        for name in tkfont.names(self.root):
            # The theme owns separate fonts for entries, buttons and headings.
            # Configure those too, rather than only ttk's default style.
            tkfont.nametofont(name).configure(family='Microsoft YaHei UI')
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont', 'TkTooltipFont', 'TkFixedFont'):
            tkfont.nametofont(name).configure(size=10)
        icon = Path(__file__).with_name('assets') / 'app.png'
        if icon.exists():
            # Bind the image to THIS root explicitly: a PhotoImage created without a
            # master lands in tkinter's default root, which breaks a second window in
            # the same process (stale interpreter).
            self._app_icon = tk.PhotoImage(file=str(icon), master=self.root)
            self.root.iconphoto(True, self._app_icon)
        self.root.option_add('*TCombobox*Listbox.font', ('Microsoft YaHei UI', 11))
        style = ttk.Style(self.root)
        style.configure('.', font=('Microsoft YaHei UI', 11))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        style.configure('Heading.TLabel', font=('Microsoft YaHei UI', 13, 'bold'))
        style.configure('Muted.TLabel', foreground='#666666')
        style.configure('Section.TLabel', font=('Microsoft YaHei UI', 11, 'bold'))
        self.status = tk.StringVar(value='就绪')
        ttk.Label(self.root, textvariable=self.status, anchor='w').pack(
            side='bottom', fill='x', padx=14, pady=(0, 6))
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=12, pady=12)
        self.bridge_panel = BridgePanel(self.notebook, self.config_url, lambda: self._config,
                                        save_config=self._save_config)
        self.notebook.add(self.bridge_panel, text='连接与兼容')
        self.model_page = ModelPage(
            self.notebook,
            config_provider=self._model_config,
            save_config=self._save_config,
            config_url=self.config_url,
            data_provider=lambda: self.data,
            connection_settings=self.bridge_panel.settings,
            client_provider=lambda: self.bridge_panel.client,
            advanced_actions={
                "context_override": self.edit_sync_context,
                'sync': self.sync,
                'import_catalog': lambda: self.model_page.import_catalog(),
                'export_catalog': lambda: self.model_page.export_catalog(),
                'backup': self.backup,
                'restore': self.restore,
                'runtime_settings': self.choose_runtime,
                'verify': self.verify_compatibility,
                'doctor': self.doctor,
                'apply_config': self.apply_to_codex,
                'undo_apply': self.undo_apply,
                'undo_writeback': self.undo_writeback,
                'undo_first_apply': self.undo_first_apply,
                'takeover_details': self.show_takeover_details,
            },
            on_status=self._set_status)
        self.notebook.add(self.model_page, text='模型管理')
        self.notebook.bind('<<NotebookTabChanged>>', lambda _: self.refresh())

    def _save_config(self):
        if self._config is not None:
            self._config.save(self.config_url)

    def _model_config(self):
        """The app config, with the connection page's selected config adopted.

        The connection page is the single source of truth for "which config.toml is
        in use", so the model page reads exactly what it shows instead of a separate
        (possibly empty) codexConfigPath.
        """
        cfg = self._config
        if cfg is None:
            return None
        try:
            sync_connection_config(cfg, self.config_url, save=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            # The live widget value wins over a stale connection.json: the user may
            # have just typed/browsed a path without running an action yet.
            from ..core.app_config import load_connection_prefs, save_connection_prefs

            conn = self.bridge_panel.target.get().strip()
            if conn and (cfg.codex_config_path or '') != conn:
                cfg.codex_config_path = conn
                cfg.save(self.config_url)
                prefs = load_connection_prefs(self.config_url)
                prefs['config'] = conn
                save_connection_prefs(self.config_url, prefs)
        except Exception:  # noqa: BLE001
            pass
        return cfg

    def refresh(self):
        self.model_page.refresh()

    # ---- shared helpers used by the advanced actions ----

    def _current_config(self):
        return self._config

    def _set_status(self, text: str) -> None:
        self.status.set(text)

    def show_takeover_details(self):
        """Advanced: read-only raw view of the two files behind the edit copy."""
        from ..core import takeover

        config = self._model_config()
        if config is None:
            messagebox.showerror("不可查看", self._config_error or "配置不可用。")
            return
        try:
            text = takeover.render_working_state(takeover.working_state(config))
        except CodexModelError as exc:
            text = str(exc)
        messagebox.showinfo("编辑副本细节（只读）", text, parent=self.root)

    def undo_first_apply(self):
        """Advanced: reverse a first apply (config reference + published catalog)."""
        from ..core import takeover

        config = self._model_config()
        if config is None:
            messagebox.showerror("不可撤销", self._config_error or "配置不可用。")
            return
        record = config.last_first_apply
        if not record:
            messagebox.showwarning("无可撤销", "没有已记录的首次应用可撤销。")
            return
        if not messagebox.askyesno(
                "确认撤销首次应用",
                f"将撤销 config.toml 的引用：\n{record.get('configPath')}\n\n"
                f"model_catalog_json: {record.get('appliedValue')} -> "
                f"{record.get('previousValue') or '（删除该键）'}\n"
                f"生效目录：{record.get('targetCatalog')}"
                + ("（删除本次新建的文件）" if record.get("createdCatalog") else "（恢复为应用前内容）")
                + "\n\n任一文件被外部修改、或缺少必要备份时：一个文件都不会动，"
                  "撤销记录会保留，处理冲突后可重试。继续？",
                parent=self.root):
            return
        try:
            message = takeover.undo_first_apply(config, persist=self._save_config)
        except ConflictError as exc:
            messagebox.showwarning("未完成撤销（记录已保留）", str(exc), parent=self.root)
            self.refresh()
            return
        except CodexModelError as exc:
            messagebox.showerror("撤销失败", str(exc), parent=self.root)
            return
        self.refresh()
        self.status.set("已撤销首次应用")
        messagebox.showinfo("撤销完成", message, parent=self.root)

    def undo_writeback(self):
        """Advanced: reverse the last write-back with conflict detection."""
        from ..core import takeover

        config = self._model_config()
        if config is None:
            messagebox.showerror("不可撤销", self._config_error or "配置不可用。")
            return
        record = config.last_takeover
        if not record:
            messagebox.showwarning("无可撤销", "没有已记录的写回操作可撤销。")
            return
        if not messagebox.askyesno(
                "确认撤销写回",
                f"将恢复：\n{record.get('activePath')}\n\n"
                f"写回差异：{record.get('diffSummary') or '（未记录）'}\n"
                "仅当生效目录仍等于上次写回值时才恢复，否则报告冲突、不改动。继续？",
                parent=self.root):
            return
        try:
            message = takeover.undo_pending(config)
            self._save_config()
        except ConflictError as exc:
            messagebox.showwarning("未自动撤销", str(exc), parent=self.root)
            return
        except CodexModelError as exc:
            messagebox.showerror("撤销失败", str(exc), parent=self.root)
            return
        self.refresh()
        self.status.set("已撤销写回")
        messagebox.showinfo("撤销完成", message, parent=self.root)

    # The model page owns add/edit/delete now; these aliases keep older callers
    # (and muscle memory) working without duplicating the logic.
    def add_model(self):
        self.model_page.add_model()

    def edit_reasoning(self):
        self.model_page.edit_selected()

    def delete_model(self):
        self.model_page.delete_selected()

    def sync(self):
        if self._syncing:
            messagebox.showinfo("同步", "同步正在进行中，请稍候。")
            return
        if self.data is None:
            messagebox.showwarning("不可同步", "当前无可用 Codex 运行时。请先用“设置 Codex 运行时…”选择，或改用离线预览。")
            return

        captured = copy.deepcopy(self._config)

        class SyncService:
            def run_sync(self):
                return CatalogDataService(captured.resolved()).run_sync()
        service = SyncService()
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

    def _on_context_override(self, slug, override):
        if self._syncing or self._config is None:
            messagebox.showwarning("请稍候", "同步完成后再修改上下文。", parent=self.root)
            return False
        try:
            config = copy.deepcopy(self._config)
            if override.is_empty:
                config.model_overrides.pop(slug, None)
            else:
                config.model_overrides[slug] = override
            backup_file(self.config_url, config.resolved_paths().backup_directory, prefix="config")
            config.save(self.config_url)
            self._config = config
            self.sync()
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False


    def edit_sync_context(self):
        row = self.model_page._selected_row()
        if row is None or self._config is None:
            messagebox.showinfo("请选择模型", "请先选择要保留同步上下文设置的模型。")
            return
        model = row["model"]
        current = self._config.model_overrides.get(model.slug, ModelFieldOverrides())
        ContextOverrideDialog(self.root, model, current,
                              lambda override: self._on_context_override(model.slug, override))

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
        """Run the runtime's catalog-read behaviour proof and store evidence.

        Scope, stated plainly to the user: this proves the selected runtime loads
        ``model_catalog_json`` from an isolated CODEX_HOME. It makes NO model
        request, so it is neither a relay-capability check nor a proof that a
        given model / image input / reasoning level actually works.
        """
        from ..core.compat_probe import probe_compatibility, evidence_to_dict

        if self._config is None:
            messagebox.showerror("不可验证", self._config_error or "配置不可用。")
            return
        try:
            target = self._config.runtime_target()
        except CodexModelError as exc:
            messagebox.showerror("无法构建运行时目标", str(exc))
            return

        self.status.set("正在运行运行时目录读取证明……")
        self.root.update_idletasks()
        try:
            evidence = probe_compatibility(target)
        except Exception as exc:  # noqa: BLE001
            self.status.set("运行时目录读取证明失败")
            messagebox.showerror("运行时目录读取证明失败", str(exc))
            return

        lines = [
            "说明：本证明只验证所选运行时能读取隔离 CODEX_HOME 里的 model_catalog_json，"
            "不发起任何模型请求；它不代表中转提供某个模型，也不验证图片/推理档位是否可用。",
            "",
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
            self.status.set("运行时目录读取证据有效，已存储。")
            messagebox.showinfo("运行时目录读取证明通过", "\n".join(lines))
        else:
            self.status.set("运行时目录读取证明未通过")
            messagebox.showwarning("运行时目录读取证明未通过", "\n".join(lines))

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
    CodexModelManagerApp(root, config_url)
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
