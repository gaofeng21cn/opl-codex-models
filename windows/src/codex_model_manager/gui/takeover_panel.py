"""Takeover panel: the directory Codex reads vs the manager's pending copy.

Self-contained so the model page stays readable. It owns the two path labels, the
import / preview / write-back / undo buttons and the state summary; every decision
lives in codex_model_manager.core.takeover, so the GUI and the CLI cannot drift
apart on what counts as writable, conflicting or destructive.
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..core import takeover
from ..core.errors import CodexModelError, ConflictError
from ..services.catalog_data_service import CatalogSnapshot


class TakeoverPanel(ttk.Frame):
    def __init__(self, master, config_getter, config_url, on_changed=None,
                 set_status=None, snapshot_provider=None):
        super().__init__(master)
        self.config_getter = config_getter
        self.config_url = config_url
        self.on_changed = on_changed or (lambda select_pending=False: None)
        self.set_status = set_status or (lambda text: None)
        self.snapshot_provider = snapshot_provider
        self.state = None
        self.paths_var = tk.StringVar(value='生效配置引用目录：（未确定）')
        self.state_var = tk.StringVar(value='接管尚未开始：请先“从现有目录导入副本”。')
        self._build()

    # ---- widgets ----

    def _build(self):
        ttk.Label(self, text='接管现有目录（Codex 读取的目录 <-> 管理器待应用目录）',
                  style='Section.TLabel').pack(anchor='w')
        ttk.Label(self, textvariable=self.paths_var, wraplength=950, justify='left',
                  style='Muted.TLabel').pack(anchor='w', pady=(4, 2))
        ttk.Label(self, textvariable=self.state_var, wraplength=950, justify='left',
                  style='Muted.TLabel').pack(anchor='w', pady=(0, 6))
        row = ttk.Frame(self)
        row.pack(fill='x')
        pad = {'padx': 6, 'pady': 2}
        ttk.Button(row, text='从现有目录导入副本…', command=self.import_copy).pack(side='left', **pad)
        ttk.Button(row, text='差异预览（只读）', command=self.preview).pack(side='left', **pad)
        ttk.Button(row, text='写回生效目录…', command=self.apply_writeback).pack(side='left', **pad)
        ttk.Button(row, text='撤销写回', command=self.undo).pack(side='left', **pad)
        ttk.Button(row, text='刷新接管状态', command=self.refresh_state).pack(side='left', **pad)

    # ---- state ----

    def paths(self):
        """(active, pending) catalog paths, each None when it cannot be resolved."""
        config = self.config_getter()
        if config is None:
            return None, None
        try:
            active = takeover.active_catalog_path(config)
        except CodexModelError:
            active = None
        try:
            pending = takeover.pending_catalog_path(config)
        except CodexModelError:
            pending = None
        return active, pending

    def _active_or_empty(self):
        try:
            return takeover.active_catalog_path(self.config_getter()) or ''
        except CodexModelError:
            return ''

    def refresh_state(self):
        """Re-read paths, gate, conflict and diff. Writes nothing."""
        config = self.config_getter()
        self.state = None
        if config is None:
            self.paths_var.set('配置不可用：接管需要有效的应用配置。')
            self.state_var.set('')
            return None
        record = config.takeover_import or {}
        active = record.get('activePath') or self._active_or_empty() or '未确定（导入时选择）'
        pending = record.get('pendingPath') or config.takeover_catalog_path or '未配置'
        lines = ['生效配置引用目录（Codex 实际读取）: ' + str(active),
                 '待应用目录（管理器编辑，尚未生效）: ' + str(pending)]
        if record.get('importedAt'):
            lines.append('导入时间: ' + str(record['importedAt']))
        self.paths_var.set('\n'.join(lines))
        try:
            self.state = takeover.inspect(config)
        except CodexModelError as exc:
            self.state_var.set('接管状态不可用：' + str(exc))
            return None
        state = self.state
        bits = []
        if state.diff is not None and state.pending_exists:
            bits.append('差异：' + state.diff.summary()
                        + ('（写回需显式确认删除）' if state.diff.removed else ''))
        if not state.pending_exists:
            bits.append('尚未导入副本')
        if state.pending_exists and not state.pending_ok:
            bits.append('待应用目录结构无效：' + state.pending_error)
        if not state.writable:
            bits.append('写入校验未通过（只读）：' + state.gate_reason)
        if state.conflict:
            bits.append(state.conflict)
        for warning in state.structure_warnings:
            bits.append('提示：' + warning)
        self.state_var.set('；'.join(bits) or '接管就绪。')
        return state

    def snapshot(self):
        """The pending catalog as a snapshot, or None when there is none yet."""
        config = self.config_getter()
        if config is None:
            return None
        try:
            pending = takeover.pending_catalog_path(config)
        except CodexModelError:
            return None
        if not pending or not os.path.isfile(pending):
            return None
        if self.snapshot_provider is not None:
            return self.snapshot_provider(pending)
        from ..parser import parse_catalog_models

        data = Path(pending).read_bytes()
        return CatalogSnapshot(
            models=parse_catalog_models(data, source='pending'), records=[],
            sync_log_path='')

    # ---- operations ----

    def import_copy(self):
        """Copy the live catalog into the manager's pending file (nothing else changes)."""
        config = self.config_getter()
        if config is None:
            messagebox.showerror('不可导入', '配置不可用。', parent=self)
            return
        initial = self._active_or_empty()
        path = filedialog.askopenfilename(
            title='选择 Codex 当前引用的模型目录（config.toml 的 model_catalog_json）',
            initialdir=os.path.dirname(initial) if initial else None,
            initialfile=os.path.basename(initial) if initial else None,
            filetypes=[('模型目录 JSON', '*.json'), ('全部文件', '*.*')])
        create_empty = False
        if not path:
            if not messagebox.askyesno(
                    '未选择文件',
                    '没有选择生效目录。是否改为创建一个空白的待应用目录（从零开始接管）？',
                    parent=self):
                self.set_status('已取消导入')
                return
            create_empty = True
            path = initial

        # Overwriting a pending copy that holds unapplied edits is never silent:
        # the import backs the old copy up, but the user still gets to say no.
        try:
            existing = takeover.pending_catalog_path(config)
        except CodexModelError:
            existing = ''
        if existing and os.path.isfile(existing):
            current = takeover.inspect(config)
            if current.diff is not None and current.diff.has_changes:
                if not messagebox.askyesno(
                        '待应用目录有未写回的修改',
                        '待应用目录与生效目录仍有差异（尚未写回）。\n'
                        '继续导入会用它覆盖当前待应用副本（旧副本会先备份）。\n\n继续？',
                        parent=self):
                    self.set_status('已取消导入（保留现有待应用副本）')
                    return
        try:
            record = takeover.import_active(config, path, create_empty=create_empty)
            config.save(self.config_url)
        except CodexModelError as exc:
            messagebox.showerror('导入失败', str(exc), parent=self)
            return
        self.refresh_state()
        self.on_changed(True)
        self.set_status('已导入副本：' + str(record['modelCount']) + ' 个模型（尚未影响 Codex 读取的目录）')
        warnings = record.get('structureWarnings') or []
        body = [
            '生效配置引用目录：',
            str(record['activePath']),
            '',
            '待应用目录（管理器编辑，尚未生效）：',
            str(record['pendingPath']),
            '',
            '模型数：' + str(record['modelCount']),
        ]
        body += [''] + ['提示：' + w for w in warnings]
        body += ['', '下一步：在待应用目录视图中编辑，再点“写回生效目录…”。']
        messagebox.showinfo('已导入待应用副本', '\n'.join(body), parent=self)

    def preview(self):
        """Read-only: show both directories and the difference between them."""
        config = self.config_getter()
        if config is None:
            messagebox.showerror('不可预览', '配置不可用。', parent=self)
            return
        state = self.refresh_state()
        if state is None:
            messagebox.showwarning('不可预览', self.state_var.get(), parent=self)
            return
        messagebox.showinfo(
            '接管差异预览（只读）',
            takeover.render_state(state) + '\n\n只读预览：未写入、未备份、未创建任何文件。',
            parent=self)
        self.set_status('接管差异：' + (state.diff.summary() if state.diff else '未知'))

    def apply_writeback(self):
        """Write the pending catalog back over the active one: shown, confirmed, gated."""
        config = self.config_getter()
        if config is None:
            messagebox.showerror('不可写回', '配置不可用。', parent=self)
            return
        state = self.refresh_state()
        if state is None:
            messagebox.showerror('不可写回', self.state_var.get(), parent=self)
            return
        if not state.pending_exists:
            messagebox.showwarning('尚未导入', '还没有待应用目录：请先“从现有目录导入副本”。',
                                   parent=self)
            return
        if state.conflict:
            messagebox.showerror('接管冲突', state.conflict, parent=self)
            return
        if not state.pending_ok:
            messagebox.showerror('待应用目录无效', state.pending_error, parent=self)
            return
        if not state.writable:
            messagebox.showwarning('未写入（只读）', state.gate_reason, parent=self)
            self.set_status('未写入：未通过校验')
            return
        if state.diff is not None and not state.diff.has_changes:
            messagebox.showinfo('无需写入', '待应用目录与生效目录内容一致，没有需要写回的变化。',
                                parent=self)
            self.set_status('待应用目录与生效目录一致，未写入。')
            return

        detail = takeover.render_state(state)
        confirm_removals = False
        allow_create = False
        if not state.active_exists:
            # The live catalog does not exist yet (taken over from empty): creating it
            # is a first write, so it is named and confirmed separately.
            if not messagebox.askyesno(
                    '生效目录不存在，是否新建？',
                    detail + '\n\n生效配置引用目录当前不存在。继续会在该路径新建模型目录文件'
                    '（先备份策略不适用，因为没有原文件）。继续？', parent=self, icon='warning'):
                self.set_status('已取消写回')
                return
            allow_create = True
        if state.diff is not None and state.diff.removed:
            text = (detail + '\n\n注意：写回会从生效目录中删除以上 '
                    + str(len(state.diff.removed)) + ' 个已有模型。'
                    + '\n\n确认要连同这些删除一起写回吗？选择“否”将取消，不会写入任何内容。')
            if not messagebox.askyesno('确认删除已有模型？', text, parent=self, icon='warning'):
                self.set_status('已取消写回（未删除任何模型）')
                return
            confirm_removals = True
        elif not messagebox.askyesno(
                '写回生效目录（显式写入）',
                detail + '\n\n写入前会自动备份原文件，写入使用原子替换。继续？', parent=self):
            self.set_status('已取消写回')
            return

        try:
            result = takeover.apply_pending(
                config, confirm_removals=confirm_removals, allow_create=allow_create,
                persist=lambda: config.save(self.config_url))
        except ConflictError as exc:
            messagebox.showwarning('未写入（冲突）', str(exc), parent=self)
            self.set_status('未写入：检测到冲突')
            return
        except CodexModelError as exc:
            messagebox.showerror('写回失败（已保留原文件）', str(exc), parent=self)
            return
        self.refresh_state()
        self.on_changed(False)
        self.set_status(result.message)
        body = [result.message]
        if result.applied:
            body.append('差异：' + result.diff_summary)
            if result.backup_path:
                body.append('原文件已备份：' + result.backup_path)
            body += ['', '可用“撤销写回”恢复。']
        messagebox.showinfo('写回完成' if result.applied else '无需写入',
                            '\n'.join(body), parent=self)

    def undo(self):
        """Undo the last write-back (refused when the file moved under us)."""
        config = self.config_getter()
        if config is None:
            messagebox.showerror('不可撤销', '配置不可用。', parent=self)
            return
        record = config.last_takeover
        if not record:
            messagebox.showwarning('无可撤销', '没有已记录的写回操作可撤销。', parent=self)
            return
        target = record.get('backupPath') or '（删除本次新建的文件）'
        if not messagebox.askyesno(
                '确认撤销写回',
                '将恢复：\n' + str(record.get('activePath')) + '\n\n'
                '写回差异：' + str(record.get('diffSummary') or '（未记录）') + '\n'
                '恢复为应用前内容：' + str(target) + '\n\n'
                '仅当生效目录仍等于上次写回值时才恢复，否则报告冲突、不改动。继续？',
                parent=self):
            return
        try:
            message = takeover.undo_pending(config)
            config.save(self.config_url)
        except ConflictError as exc:
            messagebox.showwarning('未自动撤销', str(exc), parent=self)
            self.set_status('未自动撤销：检测到冲突')
            return
        except CodexModelError as exc:
            messagebox.showerror('撤销失败', str(exc), parent=self)
            return
        self.refresh_state()
        self.on_changed(False)
        self.set_status('已撤销写回')
        messagebox.showinfo('撤销完成', message, parent=self)
