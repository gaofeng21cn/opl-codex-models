# Windows Codex 模型管理器

这是独立的 Python/tkinter 实现，源码来自 [lifelover26 的 Windows 贡献](https://github.com/gaofeng21cn/opl-codex-models/pull/4)。macOS 版本继续使用仓库根目录的 Swift 应用。

可查看和合并官方及自定义模型目录、预览并应用配置、备份和撤销修改。Windows 原生运行时与 WSL 后端分别绑定，应用目录前须通过所选运行时的兼容性探测。当前提供手动同步，没有自动更新或后台定时同步。

## 从源码运行

在 Windows 安装 Python 3.12，然后从本目录执行：

```powershell
python -m pip install -r requirements.txt
python scripts/desktop_entry.py
```

也可使用 `Start.cmd`。首次运行只创建应用自身的 `user-data`；写入 Codex 配置需在界面预览并应用。已有目录可导入副本、检查差异后写回；未知字段保留，检测到外部修改时拒绝覆盖。

## 可选协议桥

桥默认关闭。用户明确启用后才修改选定 provider 的 `base_url`，原值和配置备份保留，可恢复直连。桥仅绑定回环地址，按所选模型转换 Responses 工具协议；不读取 keyring，客户端提供的认证仅在内存中转发。关闭 GUI 不会停止已启用的桥，须使用恢复直连操作。

`context` 和 `both` 是实验模式，无法保证解决所有供应商或客户端的工具问题。开发捕获默认关闭，启用后可能保存模型输出。具体协议范围见 [TOOL_RECOVERY.md](TOOL_RECOVERY.md)。

## 验证和构建

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests -q -rs
python -m pip install pyinstaller
python scripts/build_windows.py
```

测试使用临时配置、模拟运行时和本机假上游。GitHub Windows CI 验证源码；源码合入不表示已发布签名安装包或完成真实 WSL/供应商验收。便携构建包含 GUI 和独立 worker，输出位于 `dist`，已有 `user-data` 会阻止原地覆盖。

第三方许可证见 [SOURCE_NOTICE.md](SOURCE_NOTICE.md)、[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) 和 [SUN_VALLEY_LICENSE.txt](SUN_VALLEY_LICENSE.txt)。
