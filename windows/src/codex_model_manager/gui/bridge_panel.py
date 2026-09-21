"""Connection home page. All subprocess work stays off tkinter's UI thread."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ..core.managed_bridge import BridgeActionError, atomic
from ..services.bridge_client import BridgeClient

MODE_LABELS = {'protocol': 'A · 协议兼容（建议先用）', 'off': '0 · 原样通过',
               'context': 'B · 上下文纠偏（实验）', 'both': 'AB · 两项同时启用（实验）'}


class BridgePanel(ttk.Frame):
    def __init__(self, master, config_url, config_getter, client=None):
        super().__init__(master, padding=22)
        self.client = client or BridgeClient(); self.config_getter = config_getter
        self.prefs_path = Path(config_url).with_name('connection.json')
        self.events = queue.Queue(); self.busy = False; self.snapshot = None; self.bound = False
        self._poll_id = None; self._queue_id = None; self.closed = False
        self.target = tk.StringVar(value=str(Path.home()/'.codex/config.toml'))
        self.backend = tk.StringVar(value='auto'); self.distro = tk.StringVar(value='Ubuntu')
        self.port = tk.StringVar(value='18787'); self.models = tk.StringVar(value='gpt-6-astra')
        self.mode = tk.StringVar(value=MODE_LABELS['protocol'])
        self.title = tk.StringVar(value='先检查当前连接')
        self.subtitle = tk.StringVar(value='首次使用：确认下面的 Codex 配置，点击“刷新状态”。')
        self.endpoint = tk.StringVar(value='尚未检测'); self.protection = tk.StringVar(value='尚未检测')
        self.note = tk.StringVar(value='关闭这个窗口不会停止后台桥；停止请使用“恢复直连并停止”。')
        self._build(); self._load_prefs(); self._pump()
        self.bind('<Destroy>', self._destroyed, add='+')

    def _build(self):
        self.columnconfigure(0, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 22))
        ttk.Label(header, text='连接与兼容', style='Title.TLabel').pack(side='left')
        self.refresh_button = ttk.Button(header, text='刷新状态', command=self.refresh)
        self.refresh_button.pack(side='right')

        status = ttk.Frame(self)
        status.grid(row=1, column=0, sticky='ew')
        ttk.Label(status, textvariable=self.title, style='Heading.TLabel').pack(anchor='w')
        ttk.Label(status, textvariable=self.subtitle, style='Muted.TLabel', wraplength=920).pack(anchor='w', pady=(8, 0))
        ttk.Label(status, textvariable=self.endpoint, style='Muted.TLabel', wraplength=920).pack(anchor='w', pady=(6, 0))
        ttk.Label(status, textvariable=self.protection, style='Muted.TLabel').pack(anchor='w', pady=(6, 0))
        ttk.Separator(self).grid(row=2, column=0, sticky='ew', pady=22)

        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, sticky='ew')
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text='兼容模式', style='Section.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 32))
        self.mode_box = ttk.Combobox(controls, textvariable=self.mode, values=list(MODE_LABELS.values()), state='readonly', width=34)
        self.mode_box.grid(row=0, column=1, sticky='ew')
        self.mode_button = ttk.Button(controls, text='应用模式', command=self.change_mode)
        self.mode_button.grid(row=0, column=2, padx=(12, 0))
        ttk.Label(controls, text='推荐先用 A。压缩请求始终原样通过。', style='Muted.TLabel').grid(row=1, column=1, columnspan=2, sticky='w', pady=(8, 22))
        ttk.Label(controls, text='适用模型', style='Section.TLabel').grid(row=2, column=0, sticky='w')
        presets = ttk.Frame(controls)
        presets.grid(row=2, column=1, columnspan=2, sticky='w')
        for label, value in [('GPT', 'gpt-6-astra'), ('DeepSeek', 'deepseek-v4.1-flash'), ('两者', 'gpt-6-astra, deepseek-v4.1-flash')]:
            ttk.Radiobutton(presets, text=label, variable=self.models, value=value).pack(side='left', padx=(0, 28))
        ttk.Label(controls, text='其余模型不改写报文，但仍通过本地桥连接。', style='Muted.TLabel').grid(row=3, column=1, columnspan=2, sticky='w', pady=(8, 0))

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky='ew', pady=(26, 10))
        self.start_button = ttk.Button(actions, text='启动并启用桥', style='Accent.TButton', command=self.enable)
        self.start_button.pack(side='left', padx=(0, 12))
        self.restart_button = ttk.Button(actions, text='重启桥 / 加载更新', command=lambda: self.enable(restart=True))
        self.restart_button.pack(side='left', padx=(0, 12))
        self.restore_button = ttk.Button(actions, text='恢复直连并停止', command=self.restore)
        self.restore_button.pack(side='left')
        ttk.Label(self, textvariable=self.note, wraplength=930, style='Muted.TLabel').grid(row=5, column=0, sticky='w', pady=(0, 20))
        ttk.Separator(self).grid(row=6, column=0, sticky='ew')

        self.details = ttk.Frame(self)
        self.details.columnconfigure(1, weight=1)
        self.details_open = False
        self.details_button = ttk.Button(self, text='连接设置与诊断  ▸', command=self.toggle_details)
        self.details_button.grid(row=7, column=0, sticky='w', pady=(14, 0))
        advanced = self.details
        ttk.Label(advanced, text='配置文件').grid(row=0, column=0, sticky='w', padx=(0, 20))
        ttk.Entry(advanced, textvariable=self.target).grid(row=0, column=1, columnspan=3, sticky='ew')
        ttk.Button(advanced, text='浏览…', command=self.browse).grid(row=0, column=4, padx=(10, 0))
        ttk.Label(advanced, text='运行位置').grid(row=1, column=0, sticky='w', pady=10)
        ttk.Combobox(advanced, textvariable=self.backend, values=['auto', 'wsl', 'native'], state='readonly', width=10).grid(row=1, column=1, sticky='w')
        ttk.Label(advanced, text='WSL 发行版').grid(row=1, column=2, padx=12)
        ttk.Entry(advanced, textvariable=self.distro, width=16).grid(row=1, column=3, sticky='ew')
        ttk.Label(advanced, text='端口').grid(row=2, column=0, sticky='w')
        ttk.Entry(advanced, textvariable=self.port, width=10).grid(row=2, column=1, sticky='w')
        ttk.Label(advanced, text='模型名').grid(row=3, column=0, sticky='w', pady=10)
        ttk.Entry(advanced, textvariable=self.models).grid(row=3, column=1, columnspan=4, sticky='ew')
        ttk.Button(advanced, text='导出诊断', command=self.export).grid(row=4, column=0, sticky='w', pady=(0, 8))
        self.activity = tk.Text(advanced, height=3, relief='flat', borderwidth=0, wrap='word', state='disabled', background='#fafafa', foreground='#666666', font=('Microsoft YaHei UI', 10))
        self.activity.grid(row=5, column=0, columnspan=5, sticky='ew')

    def toggle_details(self):
        self.details_open = not self.details_open
        if self.details_open:
            self.details.grid(row=8, column=0, sticky='ew', pady=(14, 0))
            self.details_button.configure(text='连接设置与诊断  ▾')
            self.winfo_toplevel().geometry('1060x960')
        else:
            self.details.grid_remove()
            self.details_button.configure(text='连接设置与诊断  ▸')
            self.winfo_toplevel().geometry('1060x640')

    def bind_configuration(self):
        if self.bound: return
        cfg = self.config_getter()
        if cfg and cfg.codex_config_path: self.target.set(cfg.codex_config_path)
        if cfg and cfg.is_demo:
            self.target.set(cfg.codex_config_path or str(Path(cfg.merged_catalog_path).parent/'codex/config.toml'))
            self.backend.set('native')
        self.bound = True
        self.after(200, self.refresh)
        self._poll_id = self.after(12000, self._poll)

    def _load_prefs(self):
        try: data = json.loads(self.prefs_path.read_text())
        except (OSError, ValueError): return
        for key, var in [('config', self.target), ('backend', self.backend), ('distro', self.distro), ('port', self.port), ('models', self.models)]:
            if key in data: var.set(str(data[key]))

    def settings(self):
        try: port = int(self.port.get())
        except ValueError: raise BridgeActionError('请输入有效端口号。') from None
        data = {'config': self.target.get().strip(), 'backend': self.backend.get(), 'distro': self.distro.get().strip(),
                'port': port, 'models': [s.strip() for s in self.models.get().split(',') if s.strip()],
                'mode': next(k for k, v in MODE_LABELS.items() if v == self.mode.get())}
        cfg = self.config_getter()
        if cfg and cfg.is_demo: data['allowed_root'] = str(Path(cfg.merged_catalog_path).parent)
        return data

    def browse(self):
        path = filedialog.askopenfilename(title='选择实际使用的 Codex config.toml', filetypes=[('TOML', '*.toml')])
        if path: self.target.set(path); self.refresh()

    def _submit(self, action, settings, callback=None):
        if self.busy: return
        self.busy = True; self.note.set('正在检查后台状态…' if action == 'status' else '正在处理，请稍候…')
        for b in self._buttons(): b.configure(state='disabled')
        def work():
            try: self.events.put((True, self.client.call(action, settings), callback, settings, action))
            except Exception as exc:
                message = str(exc) if isinstance(exc, BridgeActionError) else '操作未完成，请刷新状态。'
                self.events.put((False, message, callback, settings, action))
        threading.Thread(target=work, daemon=True, name='bridge-gui-worker').start()

    def _buttons(self):
        return [self.start_button, self.restart_button, self.restore_button, self.refresh_button, self.mode_button]

    def _pump(self):
        if self.closed: return
        try:
            ok, result, callback, settings, action = self.events.get_nowait()
        except queue.Empty: pass
        else:
            self.busy = False
            for b in self._buttons(): b.configure(state='normal')
            if ok:
                self.render(result, sync_controls=action != 'status')
                if action != 'status':
                    saved = {k: settings[k] for k in ('config', 'backend', 'distro', 'port')}
                    saved['models'] = ', '.join(settings['models'])
                    try: atomic(self.prefs_path, json.dumps(saved, ensure_ascii=False, indent=2).encode())
                    except OSError: self.note.set('操作已完成，但窗口偏好保存失败。')
                if callback: callback(result)
            else:
                self.note.set(result)
                if action == 'status': self.title.set('连接尚未确认'); self.subtitle.set(result)
                else: messagebox.showerror('操作未完成', result, parent=self)
        self._queue_id = self.after(100, self._pump)

    def refresh(self):
        if self.closed or self.busy: return
        try: self._submit('status', self.settings())
        except BridgeActionError as exc: self.note.set(str(exc))

    def _poll(self):
        if self.closed: return
        if not self.busy and self.snapshot:
            self.refresh()
        self._poll_id = self.after(12000, self._poll)

    def render(self, data, sync_controls=False):
        first = self.snapshot is None; self.snapshot = data
        configured, running = data['configured'], data['running']
        self.title.set('桥已连接 · 可切换兼容模式' if configured and running else
                       '桥已离线 · 请启动或修复' if configured else '当前为直连' if not running else '直连配置 · 桥仍在后台')
        current_mode = MODE_LABELS.get(data.get('mode'), '未知')
        self.subtitle.set(f'当前模式：{current_mode}。切换前请先等当前回合结束。' if configured and running else
                          '退出 Codex 后点击“启动并启用桥”，完成后重新打开 Codex。')
        self.endpoint.set(f"{data.get('backend', '')}  |  当前地址：{data['current_url']}")
        self.protection.set(('压缩原样透传：已启用' if data['compaction_passthrough'] else '压缩保护：服务未运行或需要加载更新') +
                            f"  |  进行中请求：{data['active_requests']}")
        if first and data.get('models'): self.models.set(', '.join(data['models']))
        if first: self.port.set(str(data['port']))
        if (first or sync_controls) and data.get('mode') in MODE_LABELS: self.mode.set(MODE_LABELS[data['mode']])
        self.note.set('关闭这个窗口不会停止后台桥。恢复直连或更改适用模型前，请先退出 Codex。')
        lines = []
        for h in data.get('history', [])[-4:]:
            kind = '压缩透传' if h.get('request_kind') == 'compaction' else '协议转换' if h.get('mode') == 'translate' else '原样通过'
            lines.append(f"{h.get('ts') or ''}   {kind}   HTTP {h.get('upstream_status') or '未完成'}")
        self.activity.configure(state='normal'); self.activity.delete('1.0', 'end')
        self.activity.insert('1.0', '\n'.join(lines) or '尚无请求记录。诊断只保存状态元数据，不读取 Key，不保存对话正文。')
        self.activity.configure(state='disabled')

    def enable(self, restart=False):
        if self.busy: return
        try: settings = self.settings()
        except BridgeActionError as exc: self.note.set(str(exc)); return
        def confirmed(data):
            settings['expected_sha256'] = data['config_sha256']; settings['restart'] = restart
            text = (f"配置：{settings['config']}\n当前地址：{data['current_url']}\n"
                    f"本地端口：{settings['port']}\n适用模型：{', '.join(settings['models'])}\n\n"
                    '先启动并检查后台桥，再切换地址；自动备份。其他模型原样通过，但也依赖桥运行。\n'
                    '首次启用或重启桥前请退出 Codex。继续？')
            if messagebox.askyesno('重启后台桥' if restart else '启用兼容连接', text, parent=self):
                self._submit('enable', settings, lambda _: self.note.set('操作完成。首次启用或重启后，请重新打开 Codex。'))
        self._submit('status', settings, confirmed)

    def change_mode(self):
        try: self._submit('mode', self.settings())
        except BridgeActionError as exc: self.note.set(str(exc))

    def restore(self):
        if self.busy: return
        try: settings = self.settings()
        except BridgeActionError as exc: self.note.set(str(exc)); return
        if messagebox.askyesno('恢复直连并停止', '请先退出 Codex。\n将恢复本次桥记录的原地址，保留其他设置，并停止本工具的后台桥。继续？', parent=self):
            self._submit('restore', settings, lambda _: self.note.set('已恢复直连。现在可以重新打开 Codex。'))

    def export(self):
        if not self.snapshot: self.note.set('请先刷新状态，再导出诊断。'); return
        target = filedialog.asksaveasfilename(defaultextension='.json', initialfile='codex-bridge-diagnostic.json', filetypes=[('JSON', '*.json')])
        if target:
            data = {k: v for k, v in self.snapshot.items() if k not in ('config', 'config_sha256', 'upstream', 'current_url')}
            atomic(target, json.dumps(data, ensure_ascii=False, indent=2).encode())
            self.note.set('已导出状态和请求结果；不含 Key、配置正文或对话内容。')

    def _destroyed(self, event):
        if event.widget is self:
            self.closed = True
            if self._queue_id: self.after_cancel(self._queue_id)
            if self._poll_id: self.after_cancel(self._poll_id)
