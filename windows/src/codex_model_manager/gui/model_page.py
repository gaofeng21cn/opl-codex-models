"""The model management page: add / edit / delete, with an auto-managed edit copy.

The page the user sees is deliberately small:

  * the list is the catalog Codex actually reads (resolved from the connection
    page's selected ``config.toml``), plus any staged additions/deletions;
  * the primary actions are 添加模型 / 编辑 / 删除;
  * one "查看并应用修改" writes the staged edits back with the existing gates
    (backup, atomic replace, conflict detection, explicit removal confirmation),
    and one "放弃修改" throws them away;
  * everything else (official sync, import/export, backup/restore, runtime
    verification, raw takeover steps) lives behind the 高级操作 toggle.

The editable copy is materialised automatically the first time the user edits
something, so "接管" is an implementation detail rather than a prerequisite.
Safety is unchanged: nothing is written to the live catalog without the explicit,
previewed apply, and no write happens without a backup and the compatibility gate.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..core import takeover
from ..core.app_config import load_connection_prefs
from ..core.custom_models import (
    ModelEdit,
    NewModelDraft,
    describe_edit,
    is_valid_slug,
    normalize_slug,
)
from ..core.errors import CodexModelError, ConflictError
from ..core.reasoning import ReasoningSettings
from ..parser import CatalogModel, parse_catalog_models

IMAGE_MARK = "图片"
NO_IMAGE_MARK = "—"

_STATUS_ORDER = {"已修改": 0, "新增": 1, "待删除": 2, "未修改": 3}


def _reasoning_label(model: CatalogModel) -> str:
    settings = model.reasoning
    if not settings.supported_efforts:
        return "—"
    levels = "/".join(settings.supported_efforts)
    default = settings.default_effort or (settings.supported_efforts[0] if settings.supported_efforts else "")
    return f"{levels}（默认 {default}）" if default else levels


def _image_label(model: CatalogModel) -> str:
    if "image" not in (model.input_modalities or []):
        return NO_IMAGE_MARK
    return IMAGE_MARK + ("(原始)" if model.supports_original_image_detail else "")


class PreviewDialog(tk.Toplevel):
    """Read-only preview of exactly what is about to be written.

    ``ask`` adds an 应用 button and makes the dialog modal, so a preview can also be
    the confirmation step. No file is touched by this dialog.
    """

    def __init__(self, master, title, body, ask=False, confirm_label="应用", note=""):
        super().__init__(master)
        self.title(title)
        self.resizable(True, True)
        self.configure(padx=12, pady=12)
        self.result = False
        text = tk.Text(self, width=100, height=24, wrap="none")
        text.insert("1.0", body)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        if note:
            ttk.Label(self, text=note, style="Muted.TLabel", wraplength=760,
                      justify="left").pack(fill="x", pady=(8, 0))
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(10, 0))
        if ask:
            ttk.Button(row, text=confirm_label, style="Accent.TButton",
                       command=self._confirm).pack(side="right", padx=6)
        ttk.Button(row, text="关闭" if not ask else "取消",
                   command=self.destroy).pack(side="right")
        if ask:
            self.transient(master)
            self.grab_set()

    def _confirm(self):
        self.result = True
        self.destroy()


class AddModelDialog(tk.Toplevel):
    """Add a model by id, copying the full configuration of an existing model.

    The copied entry keeps every field (including unknown provider fields) of the
    chosen source; the user then adjusts id, display name and capabilities. The
    preview shows the exact object that will be added, and the dialog states plainly
    that a catalog entry is not proof that the relay serves the model.
    """

    def __init__(self, master, catalog, existing, on_submit, on_preview):
        super().__init__(master)
        self.title("添加模型")
        self.resizable(False, False)
        self.grab_set()
        self.configure(padx=14, pady=12)
        self.catalog = list(catalog)
        self.existing = set(existing)
        self.on_submit = on_submit
        self.on_preview = on_preview
        first = self.catalog[0].slug if self.catalog else ""
        self.vars = {
            "slug": tk.StringVar(),
            "template": tk.StringVar(value=first),
            "name": tk.StringVar(),
            "description": tk.StringVar(),
            "context": tk.StringVar(value="" if self.catalog else "128000"),
            "image": tk.BooleanVar(value=False),
            "image_original": tk.BooleanVar(value=False),
            "efforts": tk.StringVar(),
            "default": tk.StringVar(),
        }
        values = [m.slug for m in self.catalog]
        rows = [
            ("模型 ID（真实 ID，必填）", ttk.Entry(self, textvariable=self.vars["slug"], width=44)),
            ("完整复制自", ttk.Combobox(self, textvariable=self.vars["template"],
                                       values=values, state="readonly", width=42)),
            ("显示名（留空则等于 ID）", ttk.Entry(self, textvariable=self.vars["name"], width=44)),
            ("描述", ttk.Entry(self, textvariable=self.vars["description"], width=44)),
            ("上下文窗口", ttk.Entry(self, textvariable=self.vars["context"], width=44)),
            ("推理档位（逗号分隔）", ttk.Entry(self, textvariable=self.vars["efforts"], width=44)),
            ("默认档位", ttk.Entry(self, textvariable=self.vars["default"], width=44)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=4)
            widget.grid(row=r, column=1, sticky="we", padx=4, pady=4)
        base = len(rows)
        ttk.Checkbutton(self, text="支持图片输入", variable=self.vars["image"],
                        command=self._sync_image).grid(row=base, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(self, text="支持原始图片细节", variable=self.vars["image_original"]).grid(
            row=base + 1, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Label(self, text="条目只写入模型目录；是否真的可用取决于中转/上游，请在“高级操作 → 运行时验证”中确认。",
                  style="Muted.TLabel", wraplength=560, justify="left").grid(
            row=base + 2, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 0))
        buttons = ttk.Frame(self)
        buttons.grid(row=base + 3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="预览…", command=self._preview).pack(side="left", padx=6)
        ttk.Button(buttons, text="应用", style="Accent.TButton",
                   command=self._save).pack(side="left")
        self.vars["template"].trace_add("write", lambda *_: self._load_template())
        self._load_template()

    # ---- template prefill ----

    def _template_model(self):
        slug = self.vars["template"].get()
        return next((m for m in self.catalog if m.slug == slug), None)

    def _load_template(self):
        """Fill the copied fields from the selected source model.

        Changing the source reloads its configuration (context, reasoning, image
        capability, description); the id and display name are the user's own input
        and are never overwritten.
        """
        model = self._template_model()
        if model is None:
            return
        self.vars["context"].set(str(model.context_window or 128000))
        self.vars["description"].set(model.description or f"Copied from {model.slug}")
        self.vars["efforts"].set(",".join(model.reasoning.supported_efforts))
        self.vars["default"].set(model.reasoning.default_effort or "")
        self.vars["image"].set("image" in (model.input_modalities or []))
        self.vars["image_original"].set(bool(model.supports_original_image_detail))

    def _sync_image(self):
        if not self.vars["image"].get():
            self.vars["image_original"].set(False)

    # ---- draft ----

    def _draft(self):
        slug = normalize_slug(self.vars["slug"].get())
        if not is_valid_slug(slug):
            messagebox.showerror("无效的模型 ID",
                                 "模型 ID 只能包含小写字母、数字、点、下划线和连字符，且以字母或数字开头。",
                                 parent=self)
            return None
        if slug in self.existing:
            messagebox.showerror("模型 ID 已存在",
                                 f"目录中已经有 {slug}。若要调整它，请使用“编辑”。", parent=self)
            return None
        try:
            context = int(self.vars["context"].get().strip() or "0")
        except ValueError:
            messagebox.showerror("无效数值", "上下文窗口必须是正整数。", parent=self)
            return None
        if context <= 0:
            messagebox.showerror("无效数值", "上下文窗口必须是正整数。", parent=self)
            return None
        efforts = [e.strip() for e in self.vars["efforts"].get().split(",") if e.strip()]
        default = self.vars["default"].get().strip()
        reasoning = ReasoningSettings(supported_efforts=efforts, default_effort=default)
        if efforts or default:
            if not reasoning.is_valid:
                messagebox.showerror("无效配置", "请至少选择一个推理档位，并将默认档位设为其中之一。",
                                     parent=self)
                return None
        else:
            source = self._template_model()
            reasoning = source.reasoning if source is not None else None
        return NewModelDraft(
            slug=slug,
            display_name=self.vars["name"].get().strip() or slug,
            description=self.vars["description"].get().strip() or (
                f"Copied from {self.vars['template'].get()}"
                if self.vars["template"].get() else f"Added {slug}"),
            template_slug=self.vars["template"].get(),
            context_window=context,
            supports_image=bool(self.vars["image"].get()),
            supports_original_image_detail=bool(self.vars["image"].get()
                                                and self.vars["image_original"].get()),
            reasoning=reasoning,
        )

    def _preview(self):
        draft = self._draft()
        if draft is None:
            return
        self.on_preview(draft)

    def _save(self):
        draft = self._draft()
        if draft is None:
            return
        self.on_submit(draft)
        self.destroy()


class EditModelDialog(tk.Toplevel):
    """Edit one model, including its id (slug), preserving every other field.

    ``new_slug`` is a real rename: the model keeps its position and all unknown
    fields; a duplicate id is refused. Saving with no change is a no-op.
    """

    def __init__(self, master, model, on_submit, on_preview):
        super().__init__(master)
        self.title(f"编辑模型 - {model.slug}")
        self.resizable(False, False)
        self.grab_set()
        self.configure(padx=14, pady=12)
        self.on_submit = on_submit
        self.on_preview = on_preview
        self.original_slug = model.slug
        reasoning = model.reasoning
        self.vars = {
            "slug": tk.StringVar(value=model.slug),
            "name": tk.StringVar(value=model.display_name or ""),
            "description": tk.StringVar(value=model.description or ""),
            "context": tk.StringVar(value="" if model.context_window is None else str(model.context_window)),
            "max_context": tk.StringVar(value="" if model.max_context_window is None else str(model.max_context_window)),
            "image": tk.BooleanVar(value="image" in (model.input_modalities or [])),
            "image_original": tk.BooleanVar(value=bool(model.supports_original_image_detail)),
            "efforts": tk.StringVar(value=",".join(reasoning.supported_efforts)),
            "default": tk.StringVar(value=reasoning.default_effort or ""),
        }
        rows = [
            ("模型 ID", ttk.Entry(self, textvariable=self.vars["slug"], width=44)),
            ("显示名", ttk.Entry(self, textvariable=self.vars["name"], width=44)),
            ("描述", ttk.Entry(self, textvariable=self.vars["description"], width=44)),
            ("当前上下文", ttk.Entry(self, textvariable=self.vars["context"], width=44)),
            ("最大上下文", ttk.Entry(self, textvariable=self.vars["max_context"], width=44)),
            ("推理档位（逗号分隔）", ttk.Entry(self, textvariable=self.vars["efforts"], width=44)),
            ("默认档位", ttk.Entry(self, textvariable=self.vars["default"], width=44)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=4)
            widget.grid(row=r, column=1, sticky="we", padx=4, pady=4)
        base = len(rows)
        ttk.Checkbutton(self, text="支持图片输入", variable=self.vars["image"]).grid(
            row=base, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(self, text="支持原始图片细节", variable=self.vars["image_original"]).grid(
            row=base + 1, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Label(self, text="未知字段与其他模型保持不变；修改 ID 会检测重复。",
                  style="Muted.TLabel").grid(row=base + 2, column=0, columnspan=2,
                                             sticky="w", padx=4, pady=(6, 0))
        buttons = ttk.Frame(self)
        buttons.grid(row=base + 3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="预览…", command=self._preview).pack(side="left", padx=6)
        ttk.Button(buttons, text="保存", style="Accent.TButton",
                   command=self._save).pack(side="left")

    def _int_or_none(self, key, label):
        raw = self.vars[key].get().strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            messagebox.showerror("无效数值", f"{label}必须是正整数。", parent=self)
            raise ValueError(label) from None
        if value <= 0:
            messagebox.showerror("无效数值", f"{label}必须是正整数。", parent=self)
            raise ValueError(label)
        return value

    def _edit(self):
        slug = normalize_slug(self.vars["slug"].get())
        if not is_valid_slug(slug):
            messagebox.showerror("无效的模型 ID",
                                 "模型 ID 只能包含小写字母、数字、点、下划线和连字符，且以字母或数字开头。",
                                 parent=self)
            return None
        efforts = [e.strip() for e in self.vars["efforts"].get().split(",") if e.strip()]
        default = self.vars["default"].get().strip()
        reasoning = None
        if efforts or default:
            reasoning = ReasoningSettings(supported_efforts=efforts, default_effort=default)
            if not reasoning.is_valid:
                messagebox.showerror("无效配置", "请至少选择一个推理档位，并将默认档位设为其中之一。",
                                     parent=self)
                return None
        new_slug = slug if slug != self.original_slug else None
        try:
            return ModelEdit(
                context_window=self._int_or_none("context", "当前上下文"),
                max_context_window=self._int_or_none("max_context", "最大上下文"),
                display_name=self.vars["name"].get().strip() or None,
                description=self.vars["description"].get(),
                reasoning=reasoning,
                new_slug=new_slug,
            )
        except ValueError:
            return None

    def _preview(self):
        edit = self._edit()
        if edit is None:
            return
        self.on_preview(edit, self.original_slug)

    def _save(self):
        edit = self._edit()
        if edit is None:
            return
        if edit.is_empty:
            messagebox.showinfo("没有修改", "所有字段都与当前值相同，未做修改。", parent=self)
            return
        self.on_submit(edit)
        self.destroy()


class ModelPage(ttk.Frame):
    """Model management page: catalog list, staged edits, advanced operations."""

    def __init__(self, master, *, config_provider, save_config, config_url,
                 data_provider, connection_settings=None, client_provider=None,
                 advanced_actions=None, on_status=None):
        super().__init__(master, padding=12)
        self.config_provider = config_provider
        self.save_config = save_config
        self.config_url = config_url
        self.data_provider = data_provider
        self.connection_settings = connection_settings or (lambda: {})
        self.client_provider = client_provider
        self.advanced_actions = advanced_actions or {}
        self.set_status = on_status or (lambda text: None)
        self.state: takeover.WorkingState | None = None
        self._active_models: list[CatalogModel] = []
        self._working_models: list[CatalogModel] = []
        self._active_raw: dict[str, dict] = {}
        self._working_raw: dict[str, dict] = {}
        self._rows: list[dict] = []
        self._filter = ""
        self._async_generation = 0
        self._build()

    # ------------------------------------------------------------------ widgets

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="模型管理", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="刷新", command=self.refresh).pack(side="right")

        self.source_var = tk.StringVar(value="正在读取 Codex 实际使用的模型目录…")
        ttk.Label(self, textvariable=self.source_var, style="Muted.TLabel",
                  wraplength=980, justify="left").pack(fill="x", pady=(8, 2))
        self.state_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.state_var, style="Muted.TLabel",
                  wraplength=980, justify="left").pack(fill="x", pady=(0, 6))
        ttk.Label(self, text="目录条目只表示模型定义，不代表中转/上游一定提供该模型。"
                             "“验证兼容性”只证明本机 Codex 能读取隔离的模型目录（不发起模型调用），"
                             "不验证中转能力，也不验证图片或推理档位是否真的可用。",
                  style="Muted.TLabel", wraplength=980, justify="left").pack(fill="x", pady=(0, 8))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        pad = {"padx": 4, "pady": 2}
        self.add_button = ttk.Button(toolbar, text="添加模型", style="Accent.TButton",
                                     command=self.add_model)
        self.add_button.pack(side="left", **pad)
        self.edit_button = ttk.Button(toolbar, text="编辑", command=self.edit_selected)
        self.edit_button.pack(side="left", **pad)
        self.delete_button = ttk.Button(toolbar, text="删除", command=self.delete_selected)
        self.delete_button.pack(side="left", **pad)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=10, pady=2)
        self.apply_button = ttk.Button(toolbar, text="查看并应用修改…", command=self.view_and_apply)
        self.apply_button.pack(side="left", **pad)
        self.discard_button = ttk.Button(toolbar, text="放弃修改", command=self.discard_changes)
        self.discard_button.pack(side="left", **pad)

        search = ttk.Frame(self)
        search.pack(fill="x", pady=(8, 4))
        ttk.Label(search, text="搜索").pack(side="left", padx=(4, 8))
        self.search_var = tk.StringVar()
        ttk.Entry(search, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        self.search_var.trace_add("write", lambda *_: self._render_rows())

        # List first (full width), details underneath: all five columns stay visible
        # without squeezing, and the detail JSON has room to breathe.
        body = ttk.Panedwindow(self, orient="vertical")
        body.pack(fill="both", expand=True, pady=(4, 6))
        top = ttk.Frame(body)
        bottom = ttk.Frame(body)
        body.add(top, weight=3)
        body.add(bottom, weight=2)

        columns = ("slug", "name", "reasoning", "image", "status")
        self.tree = ttk.Treeview(top, columns=columns, show="headings", height=12,
                                 selectmode="browse")
        headings = {"slug": ("模型 ID", 250), "name": ("显示名", 200),
                    "reasoning": ("推理档位", 210), "image": ("图片", 90),
                    "status": ("修改状态", 90)}
        for key in columns:
            text, width = headings[key]
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=70, anchor="w",
                             stretch=(key in ("name", "reasoning")))
        scroll = ttk.Scrollbar(top, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._show_detail())
        self.tree.bind("<Double-1>", lambda _: self.edit_selected())

        ttk.Label(bottom, text="详情（编辑副本中的完整条目）", style="Section.TLabel").pack(anchor="w")
        self.detail = tk.Text(bottom, height=10, wrap="none", state="disabled")
        self.detail.pack(fill="both", expand=True, pady=(4, 0))

        self.advanced_button = ttk.Button(self, text="高级操作  ▸", command=self.toggle_advanced)
        self.advanced_button.pack(anchor="w", pady=(4, 0))
        self.advanced = ttk.Frame(self)
        self._build_advanced(self.advanced)
        self.advanced_open = False

    def _build_advanced(self, parent):
        actions = self.advanced_actions
        sections = [
            ("目录与同步", [
                ("官方同步（预览，不写 Codex）", "sync"),
                ("同步上下文覆盖…", "context_override"),
                ("导入模型目录到编辑副本…", "import_catalog"),
                ("导出当前模型列表…", "export_catalog"),
                ("撤销写回", "undo_writeback"),
                ("撤销首次应用", "undo_first_apply"),
                ("编辑副本细节（只读）", "takeover_details"),
            ]),
            ("备份与恢复", [
                ("备份", "backup"),
                ("恢复…", "restore"),
            ]),
            ("运行时验证与应用", [
                ("设置 Codex 运行时…", "runtime_settings"),
                ("验证兼容性（运行时目录读取）", "verify"),
                ("诊断工具链", "doctor"),
                ("应用管理器目录（官方同步路径）到 Codex…", "apply_config"),
                ("撤销 apply", "undo_apply"),
            ]),
        ]
        for title, entries in sections:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=title, style="Section.TLabel", width=18).pack(side="left")
            for label, key in entries:
                callback = actions.get(key)
                if callback is None:
                    continue
                ttk.Button(row, text=label, command=callback).pack(side="left", padx=4)

    def toggle_advanced(self):
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.advanced.pack(fill="x", pady=(6, 0))
            self.advanced_button.configure(text="高级操作  ▾")
        else:
            self.advanced.pack_forget()
            self.advanced_button.configure(text="高级操作  ▸")

    # ------------------------------------------------------------- data access

    def _config(self):
        try:
            return self.config_provider()
        except Exception:  # noqa: BLE001
            return None

    def _connection_config_path(self) -> str:
        try:
            return str(self.connection_settings().get("config") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _active_path(self):
        """Resolve the catalog Codex reads, using the connection page's selection."""
        config = self._config()
        if config is None:
            return None, "配置不可用。"
        conn = self._connection_config_path()
        if conn:
            try:
                return takeover.active_catalog_path_from_config(
                    conn, (config.codex_distro or "").strip() or None), ""
            except CodexModelError as exc:
                return None, str(exc)
        try:
            return takeover.active_catalog_path(config), ""
        except CodexModelError as exc:
            return None, str(exc)

    def _read_catalog(self, path):
        if not path or not os.path.isfile(path):
            return [], {}, ""
        try:
            raw = Path(path).read_bytes()
            root = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return [], {}, f"无法读取 {path}：{exc}"
        models = root.get("models") if isinstance(root, dict) else None
        if not isinstance(models, list):
            return [], {}, f"{Path(path).name} 的 models 不是数组。"
        objects = [m for m in models if isinstance(m, dict)]
        return parse_catalog_models(raw, source="active"), {m["slug"]: m for m in objects if m.get("slug")}, ""

    def refresh(self):
        """Re-read working state + both catalogs and redraw. Writes nothing."""
        config = self._config()
        if config is None:
            self.state = None
            self.source_var.set("配置不可用：请先在“连接与兼容”页确认连接。")
            self.state_var.set("")
            self._rows = []
            self._render_rows()
            return
        self.state = takeover.working_state(config)
        active_path = self.state.active_path
        if active_path is None:
            active_path, note = self._active_path()
            if note:
                self.state.message = note
        self._active_models, self._active_raw, active_err = self._read_catalog(active_path)
        if active_path and active_err:
            self.state.message = active_err
        if self.state.working_exists:
            self._working_models, self._working_raw, working_err = self._read_catalog(self.state.working_path)
            if working_err:
                self.state.message = working_err
        else:
            self._working_models, self._working_raw = [], {}
        self._render_source()
        self._render_rows()

    def _render_source(self):
        state = self.state
        if state is None:
            return
        lines = [f"Codex 实际使用目录：{state.active_path or '未配置（model_catalog_json 未指向任何目录）'}"]
        if state.working_exists:
            lines.append(f"可编辑副本（未应用前不影响 Codex）：{state.working_path}")
        else:
            lines.append("可编辑副本：尚未创建（开始编辑时会自动创建）")
        self.source_var.set("\n".join(lines))
        bits = [state.message.rstrip("。")] if state.message else []
        if state.has_unapplied and state.diff is not None:
            bits.append(f"未应用：{state.diff.summary()}")
            if state.diff.removed:
                bits.append("写回会删除已有模型，需要单独确认")
        if state.conflict:
            bits.append(state.conflict)
        if state.working_exists and not state.writable and state.gate_reason:
            bits.append("写回校验未通过（只读）：" + state.gate_reason)
        for warning in state.structure_warnings:
            bits.append("提示：" + warning)
        pending = takeover.pending_first_apply(self._config())
        if pending:
            bits.append(
                "检测到未完成的首次应用（阶段：" + str(pending.get("phase")) + "）："
                "请用“高级操作 → 撤销首次应用”恢复；它会在确认没有外部改动后再还原文件")
        self.state_var.set("；".join(bits) if bits else "未检测到问题。")

    def _render_rows(self):
        needle = self.search_var.get().strip().lower()
        rows = []
        active = self._active_raw
        working = self._working_raw
        if self.state is not None and self.state.working_exists and working:
            for model in self._working_models:
                raw = working.get(model.slug, {})
                if model.slug not in active:
                    status = "新增"
                elif json.dumps(raw, sort_keys=True) != json.dumps(active.get(model.slug, {}), sort_keys=True):
                    status = "已修改"
                else:
                    status = "未修改"
                rows.append({"model": model, "raw": raw, "status": status})
            for slug, raw in active.items():
                if slug not in working:
                    model = next((m for m in self._active_models if m.slug == slug), None)
                    if model is not None:
                        rows.append({"model": model, "raw": raw, "status": "待删除"})
        elif self.state is not None and self.state.working_exists and not working:
            rows = [{"model": m, "raw": working.get(m.slug, {}), "status": "未修改"}
                    for m in self._working_models]
        else:
            rows = [{"model": m, "raw": active.get(m.slug, {}), "status": "未修改"}
                    for m in self._active_models]
        if needle:
            rows = [r for r in rows
                    if needle in r["model"].slug.lower() or needle in r["model"].display_name.lower()]
        rows.sort(key=lambda r: (_STATUS_ORDER.get(r["status"], 9), r["model"].slug))
        self._rows = rows
        selected = self.selected_slug()
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(rows):
            model = row["model"]
            self.tree.insert("", "end", iid=str(index), values=(
                model.slug, model.display_name, _reasoning_label(model),
                _image_label(model), row["status"]))
        if selected:
            for index, row in enumerate(rows):
                if row["model"].slug == selected:
                    self.tree.selection_set(str(index))
                    break
        self._show_detail()

    # --------------------------------------------------------------- selection

    def selected_slug(self):
        selection = self.tree.selection()
        if not selection:
            return ""
        try:
            return self._rows[int(selection[0])]["model"].slug
        except (ValueError, IndexError):
            return ""

    def _selected_row(self):
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return self._rows[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _show_detail(self):
        row = self._selected_row()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", tk.END)
        if row is None:
            self.detail.insert("1.0", "在上方列表中选择一个模型查看其完整条目。")
        else:
            self.detail.insert("1.0", json.dumps(row["raw"], ensure_ascii=False, indent=2, sort_keys=True))
        self.detail.configure(state="disabled")

    # ------------------------------------------------------------- edit copy

    def _working_path(self):
        config = self._config()
        if config is None:
            return None
        try:
            return takeover.pending_catalog_path(config)
        except CodexModelError:
            return None

    def _ensure_copy(self, action_label: str):
        """Create the editable copy on demand; return its path or None (cancelled)."""
        config = self._config()
        if config is None:
            messagebox.showerror("不可编辑", "配置不可用。")
            return None
        state = self.state or takeover.working_state(config)
        if state.working_exists:
            return state.working_path
        if state.active_exists:
            detail = (f"将从 Codex 当前使用的目录创建一份可编辑副本：\n"
                      f"{state.active_path}\n\n"
                      "之后的修改都写在这份副本里，只有点击“查看并应用修改”才会写回 Codex 目录。\n\n"
                      f"继续{action_label}？")
            if not messagebox.askyesno("创建可编辑副本", detail, parent=self.winfo_toplevel()):
                return None
        else:
            detail = ((state.message + "\n\n") if state.message else "") + (
                "将先创建一个空的可编辑副本。添加模型后点“查看并应用修改”，会先把副本发布成一个"
                "独立的生效目录文件（原文件先备份），再让 config.toml 的 model_catalog_json 引用它；"
                "编辑副本本身保持不变。\n\n继续？")
            if not messagebox.askyesno("创建空的编辑副本", detail, parent=self.winfo_toplevel()):
                return None
        try:
            state = takeover.ensure_working_copy(config, persist=self.save_config)
        except CodexModelError as exc:
            messagebox.showerror("无法创建编辑副本", str(exc))
            return None
        self.refresh()
        return state.working_path

    # ----------------------------------------------------------------- add

    def add_model(self):
        if self._ensure_copy("添加模型") is None:
            return
        pool = self._working_models or self._active_models
        if not pool:
            messagebox.showinfo(
                "目录为空",
                "编辑副本里还没有可复制的模型，接下来的添加会创建一个只包含你填写字段的新条目；"
                "之后可以用“编辑”补全能力。也可以先在“高级操作 → 官方同步”里获取模型再复制。")
        existing = {m.slug for m in self._working_models} | {m.slug for m in self._active_models}
        AddModelDialog(self.winfo_toplevel(), pool, existing,
                       on_submit=self._on_add_submit, on_preview=self._preview_draft)

    def _preview_draft(self, draft: NewModelDraft):
        body = self._draft_json(draft)
        PreviewDialog(
            self.winfo_toplevel(), f"预览新增 - {draft.normalized_slug}",
            json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True),
            note="以上是即将写入编辑副本的完整条目（复制自所选模型，包含全部未知字段）。"
                 "写入编辑副本后，仍需“查看并应用修改”才会写回 Codex 目录。")

    def _draft_json(self, draft: NewModelDraft):
        """Build the exact object the add will produce (for preview only)."""
        from ..core.custom_models import add_blank_model, add_from_template

        path = self._working_path()
        base = Path(path).read_bytes() if path and os.path.isfile(path) else b'{"models": []}'
        try:
            if draft.template_slug:
                updated = add_from_template(draft, base, catalog_data=base, existing_slugs=set())
            else:
                updated = add_blank_model(draft, base, existing_slugs=set())
            return json.loads(updated)["models"][-1]
        except Exception:  # noqa: BLE001 - preview must not raise at the user
            return {"slug": draft.normalized_slug, "display_name": draft.normalized_display_name,
                    "description": draft.normalized_description,
                    "context_window": draft.context_window}

    def _on_add_submit(self, draft: NewModelDraft):
        path = self._working_path()
        data = self.data_provider()
        if not path or data is None:
            messagebox.showerror("不可添加", "编辑副本不可用。")
            return
        try:
            data.add_model_to(draft, path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("添加失败", str(exc))
            return
        self.refresh()
        self.set_status(f"已添加 {draft.normalized_slug} 到编辑副本（尚未写回 Codex）")

    # ---------------------------------------------------------------- edit

    def edit_selected(self):
        row = self._selected_row()
        if row is None:
            messagebox.showinfo("提示", "请先在列表中选择一个模型。")
            return
        if row["status"] == "待删除":
            messagebox.showinfo(
                "该模型已标记删除",
                f"{row['model'].slug} 在编辑副本中已被删除；写回 Codex 前需要单独确认删除。\n"
                "如需恢复，请使用“放弃修改”。")
            return
        if self._ensure_copy("编辑模型") is None:
            return
        EditModelDialog(self.winfo_toplevel(), row["model"],
                        on_submit=lambda edit: self._on_edit_submit(row["model"].slug, edit),
                        on_preview=self._preview_edit)

    def _preview_edit(self, edit: ModelEdit, slug: str):
        lines = describe_edit(edit, slug)
        body = "\n".join(lines) if lines else "没有任何字段发生变化。"
        old = self._working_raw.get(slug) or self._active_raw.get(slug) or {}
        after = dict(old)
        if edit.new_slug is not None:
            after["slug"] = normalize_slug(edit.new_slug)
        if edit.display_name is not None:
            after["display_name"] = " ".join(edit.display_name.split())
        if edit.description is not None:
            after["description"] = " ".join(edit.description.split())
        if edit.context_window is not None:
            after["context_window"] = edit.context_window
        if edit.max_context_window is not None:
            after["max_context_window"] = edit.max_context_window
        if edit.reasoning is not None:
            after["default_reasoning_level"] = edit.reasoning.default_effort
        text = ("将修改的字段：\n" + body + "\n\n修改后的完整条目：\n"
                + json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True))
        PreviewDialog(self.winfo_toplevel(), f"预览修改 - {slug}", text,
                      note="未知字段保持不变。以上内容只有点击“保存”并随后“查看并应用修改”"
                           "才会写回 Codex 目录。")

    def _on_edit_submit(self, slug: str, edit: ModelEdit):
        path = self._working_path()
        data = self.data_provider()
        if not path or data is None:
            messagebox.showerror("不可编辑", "编辑副本不可用。")
            return
        try:
            data.update_model_in(edit, slug, path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("修改失败", str(exc))
            return
        self.refresh()
        renamed = edit.new_slug is not None and normalize_slug(edit.new_slug) != slug
        if renamed:
            new_slug = normalize_slug(edit.new_slug)
            self.set_status(f"已把 {slug} 重命名为 {new_slug}（编辑副本，尚未写回）")
            self._warn_bridge_scope(slug, new_slug)
        else:
            self.set_status(f"已更新 {slug}（编辑副本，尚未写回 Codex）")

    def _bridge_scope(self):
        try:
            prefs = load_connection_prefs(self.config_url)
        except Exception:  # noqa: BLE001
            return []
        raw = prefs.get("models")
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw or "").split(",")
        return [str(v).strip() for v in values if str(v).strip()]

    def _warn_bridge_scope(self, old_slug: str, new_slug: str):
        """Hint (never auto-change) when the old id is in the bridge's model scope."""
        scope = self._bridge_scope()
        if old_slug not in scope:
            return
        messagebox.showinfo(
            "旧 ID 仍在连接页的适用模型列表中",
            f"旧 ID {old_slug} 仍出现在连接页“适用模型”里：{', '.join(scope)}\n\n"
            "本工具不会自动修改桥配置。如需让桥按新 ID 处理报文，请到“连接与兼容”页把适用模型"
            "改为新 ID 并重启桥（改名写回 Codex 目录后才会真正生效）。",
            parent=self.winfo_toplevel())

    # -------------------------------------------------------------- delete

    def delete_selected(self):
        row = self._selected_row()
        if row is None:
            messagebox.showinfo("提示", "请先在列表中选择一个模型。")
            return
        if row["status"] == "待删除":
            messagebox.showinfo("已标记删除", f"{row['model'].slug} 已在编辑副本中删除。")
            return
        if self._ensure_copy("删除模型") is None:
            return
        slug = row["model"].slug
        if not messagebox.askyesno(
                "确认删除",
                f"将从编辑副本中删除以下模型：\n\n{slug}\n\n"
                "删除先只作用于编辑副本；写回 Codex 目录时会再次要求确认删除。\n"
                "继续？", parent=self.winfo_toplevel(), icon="warning"):
            return
        path = self._working_path()
        data = self.data_provider()
        if not path or data is None:
            messagebox.showerror("不可删除", "编辑副本不可用。")
            return
        try:
            data.remove_model_in(slug, path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("删除失败", str(exc))
            return
        self.refresh()
        self.set_status(f"已在编辑副本中删除 {slug}（尚未写回 Codex）")

    # ------------------------------------------------------- apply / discard

    def _first_apply(self, config):
        """Publish the edit copy as a real catalog and point config.toml at it.

        Used only when the selected config.toml has no ``model_catalog_json`` yet, so
        there is nothing to write back to. The confirmation is bound to the preview's
        state snapshot: if any of the three files changes while the dialog is open,
        apply refuses and asks for a fresh preview. The published file stays a
        separate file from the edit copy, and both files are backed up and undoable.
        """
        try:
            info = takeover.first_apply_preview(config)
        except CodexModelError as exc:
            messagebox.showerror("不可应用", str(exc))
            return
        if not info["model_count"]:
            messagebox.showinfo("没有可应用的模型", "编辑副本里还没有模型，请先添加。")
            return
        if not info["config_path"]:
            messagebox.showwarning(
                "没有可用的 config.toml",
                "请先在“连接与兼容”页选择正在使用的 Codex config.toml。")
            return
        if not info["writable"]:
            messagebox.showwarning(
                "未写入（只读）",
                (info["gate_reason"] or "写入校验未通过。")
                + "\n\n这是安全闸门（要求运行时存在，并在真实模式下要求有效的运行时目录读取证据）："
                  "不会写入 config.toml 或模型目录。可在“高级操作 → 验证兼容性”生成证据后重试。",
                parent=self.winfo_toplevel())
            self.set_status("未写入：未通过校验")
            return

        target_note = ("（将被替换，原文件会先备份）" if info["target_exists"]
                       else "（将新建）")
        config_note = "" if info["config_exists"] else "（config.toml 不存在，将新建）"
        detail = (
            "当前 config.toml 还没有引用模型目录。首次应用会做两件事：\n\n"
            f"1) 把编辑副本发布成生效目录（与编辑副本是两个文件）：\n"
            f"   {info['target_catalog']} {target_note}\n"
            f"   模型数：{info['model_count']}\n\n"
            f"2) 让 config.toml 引用它：\n"
            f"   {info['config_path']} {config_note}\n"
            f"   model_catalog_json: {info['current_value'] or '（未设置）'}"
            f"  ->  {info['proposed_value']}\n\n"
            f"编辑副本本身不会被修改，也不与生效目录共用文件：\n   {info['edit_copy']}\n\n"
            "确认时会把这三个文件的当前状态与预览时的快照核对：期间被其它程序改动就拒绝写入。\n"
            "写入前会备份原文件；config 写入失败时会自动把生效目录恢复到应用前状态。\n\n继续？")
        if not messagebox.askyesno("首次应用到 Codex（显式写入）", detail,
                                   parent=self.winfo_toplevel()):
            self.set_status("已取消首次应用")
            return
        try:
            result = takeover.apply_first_time(config, expected=info["snapshot"],
                                               persist=self.save_config)
        except ConflictError as exc:
            messagebox.showwarning(
                "未写入（需要重新预览）", str(exc), parent=self.winfo_toplevel())
            self.set_status("未写入：预览后文件发生变化")
            self.refresh()
            return
        except CodexModelError as exc:
            # The message already states exactly what was and was not restored;
            # never claim the original files are untouched here.
            messagebox.showerror("首次应用失败", str(exc), parent=self.winfo_toplevel())
            self.refresh()
            return
        self.refresh()
        self.set_status(result.message)
        body = [result.message]
        if result.catalog_backup:
            body.append("原生效目录已备份：" + result.catalog_backup)
        if result.created_catalog:
            body.append("生效目录为本次新建。")
        body.append("可用“高级操作 → 撤销首次应用”回退。")
        messagebox.showinfo("首次应用完成", "\n".join(body), parent=self.winfo_toplevel())

    def _resolve_conflict(self) -> bool:
        """Offer the two explicit recoveries for an externally changed catalog."""
        state = self.state
        if state is None or not state.conflict:
            return True
        choice = messagebox.askyesnocancel(
            "生效目录已被外部修改",
            state.conflict + "\n\n"
            "是：重新载入最新目录（放弃我的修改）\n"
            "否：保留我的修改，并把最新目录登记为新基线（写回时会覆盖最新内容）\n"
            "取消：先不处理",
            parent=self.winfo_toplevel(), icon="warning")
        if choice is None:
            return False
        config = self._config()
        try:
            if choice:
                takeover.reload_from_active(config, persist=self.save_config)
                self.set_status("已重新载入最新目录，本地修改已放弃。")
            else:
                takeover.rebind_baseline(config, persist=self.save_config)
                self.set_status("已保留本地修改，并以最新目录为新基线。")
        except CodexModelError as exc:
            messagebox.showerror("处理失败", str(exc))
            return False
        self.refresh()
        return False

    def view_and_apply(self):
        """Unified review + apply: show the diff, gate it, then write with backups."""
        config = self._config()
        if config is None:
            messagebox.showerror("不可应用", "配置不可用。")
            return
        state = takeover.working_state(config)
        self.state = state
        if not state.working_exists:
            messagebox.showinfo("没有修改", "还没有可编辑副本，也没有需要应用的修改。")
            return
        if state.conflict:
            self._resolve_conflict()
            state = takeover.working_state(config)
            self.state = state
            if state.conflict:
                return
        if not state.has_unapplied:
            messagebox.showinfo("没有修改", "编辑副本与 Codex 实际使用目录一致，没有需要写回的内容。")
            return
        if state.active_path is None:
            # The selected config.toml has no model_catalog_json yet. Publishing the
            # edit copy as a real catalog file (and pointing the config at it) is the
            # only way this can ever become live; it is a separate, gated step.
            self._first_apply(config)
            return
        if not state.writable:
            messagebox.showwarning(
                "未写入（只读）",
                (state.gate_reason or "写入校验未通过。")
                + "\n\n这是安全闸门（要求运行时存在，并在真实模式下要求有效的运行时目录读取证据）："
                  "目录不会被写入。可在“高级操作 → 验证兼容性”重新生成证据后重试。",
                parent=self.winfo_toplevel())
            self.set_status("未写入：未通过校验")
            return

        detail = takeover.render_working_state(state)
        confirm_removals = False
        allow_create = False
        if not state.active_exists:
            if not messagebox.askyesno(
                    "生效目录不存在，是否新建？",
                    detail + f"\n\n生效目录当前不存在：\n{state.active_path}\n"
                    "继续会在该路径新建模型目录文件。继续？",
                    parent=self.winfo_toplevel(), icon="warning"):
                self.set_status("已取消写回")
                return
            allow_create = True
        if state.diff is not None and state.diff.removed:
            if not messagebox.askyesno(
                    "确认删除已有模型？",
                    detail + f"\n\n注意：写回会从生效目录中删除以上 {len(state.diff.removed)} 个已有模型。"
                    "选择“否”将取消，不会写入任何内容。",
                    parent=self.winfo_toplevel(), icon="warning"):
                self.set_status("已取消写回（未删除任何模型）")
                return
            confirm_removals = True
        elif not messagebox.askyesno(
                "应用修改到 Codex（显式写入）",
                detail + "\n\n写入前会自动备份原文件，写入使用原子替换。继续？",
                parent=self.winfo_toplevel()):
            self.set_status("已取消写回")
            return

        try:
            result = takeover.apply_pending(
                config, confirm_removals=confirm_removals, allow_create=allow_create,
                persist=self.save_config)
        except ConflictError as exc:
            messagebox.showwarning("未写入（冲突）", str(exc), parent=self.winfo_toplevel())
            self.set_status("未写入：检测到冲突")
            self.refresh()
            return
        except CodexModelError as exc:
            messagebox.showerror("写回失败（已保留原文件）", str(exc), parent=self.winfo_toplevel())
            return
        self.refresh()
        self.set_status(result.message)
        body = [result.message]
        if result.applied:
            body.append("差异：" + result.diff_summary)
            if result.backup_path:
                body.append("原文件已备份：" + result.backup_path)
        messagebox.showinfo("写回完成" if result.applied else "无需写入",
                            "\n".join(body), parent=self.winfo_toplevel())

    def discard_changes(self):
        config = self._config()
        if config is None:
            messagebox.showerror("不可放弃", "配置不可用。")
            return
        state = takeover.working_state(config)
        if not state.working_exists:
            messagebox.showinfo("没有修改", "还没有可编辑副本。")
            return
        if not state.has_unapplied and not state.conflict:
            messagebox.showinfo("没有修改", "编辑副本与 Codex 实际使用目录一致，无需放弃。")
            return
        target = state.active_path or "（不存在，将重置为空目录）"
        if not messagebox.askyesno(
                "放弃修改",
                "将丢弃编辑副本中所有未应用的修改，并恢复为：\n"
                f"{target}\n\n被替换的编辑副本会先备份。继续？",
                parent=self.winfo_toplevel(), icon="warning"):
            return
        try:
            takeover.reload_from_active(config, persist=self.save_config)
        except CodexModelError as exc:
            messagebox.showerror("放弃失败", str(exc))
            return
        self.refresh()
        self.set_status("已放弃未应用的修改。")

    # ----------------------------------------------------- import / export

    def import_catalog(self):
        """Copy a catalog file into the editable copy (validated, backed up)."""
        config = self._config()
        if config is None:
            messagebox.showerror("不可导入", "配置不可用。")
            return
        path = filedialog.askopenfilename(
            title="选择要导入编辑副本的模型目录 JSON",
            filetypes=[("模型目录 JSON", "*.json"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            root = json.loads(raw.decode("utf-8"))
            models = root.get("models")
            if not isinstance(models, list):
                raise ValueError("顶层缺少 models 数组")
            from ..core.safe_preview import validate_models

            validate_models(models, require_priority=True, name="导入目录")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", f"所选文件不是有效的模型目录：{exc}")
            return
        if not messagebox.askyesno(
                "导入到编辑副本",
                f"将把 {len(models)} 个模型导入编辑副本（替换当前副本内容，旧副本会先备份）：\n{path}\n\n"
                "不会写入 Codex 实际使用的目录。继续？", parent=self.winfo_toplevel()):
            return
        try:
            state = takeover.ensure_working_copy(config, persist=self.save_config)
            working = state.working_path
            from ..core.backup import atomic_write, backup_file

            paths = config.resolved_paths()
            backup_file(working, paths.backup_directory, prefix="pending-catalog")
            atomic_write(working, raw)
            if state.active_path and os.path.isfile(state.active_path):
                takeover.rebind_baseline(config, persist=self.save_config)
            else:
                # No live catalog to bind to yet: record the imported file as the
                # editable copy so the page can add/edit before a target exists.
                takeover.record_import_without_active(
                    config, model_count=len(models), persist=self.save_config)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc))
            return
        self.refresh()
        self.set_status("已导入到编辑副本；点击“查看并应用修改”才会写回 Codex。")

    def export_catalog(self):
        """Save the current editable list (working copy, else active) to a file."""
        config = self._config()
        if config is None:
            messagebox.showerror("不可导出", "配置不可用。")
            return
        state = takeover.working_state(config)
        source = state.working_path if state.working_exists else state.active_path
        if not source or not os.path.isfile(source):
            messagebox.showwarning("无可导出内容", "当前没有可导出的模型目录。")
            return
        target = filedialog.asksaveasfilename(
            title="导出模型列表", defaultextension=".json",
            initialfile=Path(source).name, filetypes=[("JSON", "*.json")])
        if not target:
            return
        try:
            from ..core.backup import atomic_write

            atomic_write(target, Path(source).read_bytes())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc))
            return
        self.set_status(f"已导出到 {target}")
