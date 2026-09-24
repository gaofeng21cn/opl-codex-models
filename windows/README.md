# Windows Codex 模型管理器

这是独立的 Python/tkinter 实现，源码来自 [lifelover26 的 Windows 贡献](https://github.com/gaofeng21cn/opl-codex-models/pull/4)。macOS 版本继续使用仓库根目录的 Swift 应用。

可查看和合并官方及自定义模型目录、预览并应用配置、备份和撤销修改。Windows 原生运行时与 WSL 后端分别绑定，应用目录前须通过所选运行时的兼容性探测。当前提供手动同步，没有自动更新或后台定时同步。

## 下载预览版

从 [Codex Models v0.4.2 Release](https://github.com/gaofeng21cn/opl-codex-models/releases/tag/v0.4.2) 下载 `Codex-Models-Windows-v0.4.2-preview.zip`，解压整个文件夹后运行 `CodexModelManager.exe`。这是未签名的便携预览版；首次启动可能出现 Windows 安全提示。请核对同页 `.sha256` 后再运行。此包与 macOS 版共用版本号和 Release 页面，Windows 仍采用独立实现。

官方目录会优先在隔离环境中使用所选 Codex 运行时的账号认证刷新；失败时回退内置清单，日志的 `official_source` 标记来源。在模型管理页选中模型，通过“高级操作 → 同步上下文覆盖”可分别覆盖当前与最大上下文；其余官方字段继续随目录更新。Windows 版目前需手动同步，目录应用仍需先通过兼容性验证。

## 模型管理：添加、编辑、应用

以下流程适用于本分支源码，已发布的预览包可能仍使用旧界面。

1. 在“连接与兼容”页选择实际使用的 `config.toml`，模型页沿用这一选择，默认列出该配置引用的目录。
2. 点击“添加模型”或选中模型后“编辑”。管理器自动建立独立编辑副本；复制模型保留未知字段，可修改模型 ID，并检查重复 ID。
3. 用“查看并应用修改”确认差异后写入；“放弃修改”重新载入生效目录。删除已有模型另需确认。
4. 首次尚未配置模型目录时，会预览独立生效文件及 `model_catalog_json` 引用，确认后备份并写入。编辑副本不会直接作为生效文件，未应用的编辑不影响 Codex。

首次应用绑定预览时的文件状态；外部修改会要求重新预览。分步写入失败或中断后保留恢复记录，撤销不会覆盖外部修改。高级操作保留同步、导入/导出、备份与撤销入口。

“验证兼容性”只证明所选运行时能够读取模型目录，不发起模型推理请求，也不证明中转支持该模型、图片或推理档位。重命名模型后若原 ID 在桥范围内，会提示另行调整桥范围。

## 从源码运行

在 Windows 安装 Python 3.12，然后从本目录执行：

```powershell
python -m pip install -r requirements.txt
python scripts/desktop_entry.py
```

也可使用 `Start.cmd`。首次运行只创建应用自身的 `user-data`；写入 Codex 配置需在界面预览并应用。已有目录可导入副本、检查差异后写回；未知字段保留，检测到外部修改时拒绝覆盖。

## 可选协议桥

桥默认关闭。用户明确启用后才修改选定 provider 的 `base_url`，原值和配置备份保留，可恢复直连。桥仅绑定回环地址，按所选模型转换 Responses 工具协议；不读取 keyring，客户端提供的认证仅在内存中转发。关闭 GUI 不会停止已启用的桥，须使用恢复直连操作。

若当前 Codex 与供应商直连已能稳定执行工具，可以保持桥关闭；模型管理不依赖启用桥。保留桥作为兼容选项，不代表所有新版客户端仍需要它，也不保证消除所有工具故障。

`context` 和 `both` 是实验模式，无法保证解决所有供应商或客户端的工具问题。当前 DeepSeek Flash 协议适配支持 `deepseek-flash` 和 `deepseek-v4.1-flash` 两个模型 ID；桥作用范围必须选择实际使用的准确 ID。开发捕获默认关闭，启用后可能保存模型输出。具体协议范围见 [TOOL_RECOVERY.md](TOOL_RECOVERY.md)。

## 验证和构建

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests -q -rs
python -m pip install pyinstaller
python scripts/build_windows.py
```

打包后可使用隔离的真实 Tk 检查入口（不加载个人配置、不发模型请求）：

```powershell
Start-Process -Wait -PassThru .\dist\CodexModelManager\CodexModelManager.exe -ArgumentList '--smoke-test', "$env:TEMP\model-manager-smoke.txt"
Get-Content "$env:TEMP\model-manager-smoke.txt" -Encoding UTF8
```

测试使用临时配置、模拟运行时和本机假上游。GitHub Windows CI 验证源码，预览包工作流另外构建 PyInstaller 便携包；这些验证不代表真实 WSL 或供应商场景全部通过。便携构建包含 GUI 和独立 worker，输出位于 `dist`，已有 `user-data` 会阻止原地覆盖。

第三方许可证见 [SOURCE_NOTICE.md](SOURCE_NOTICE.md)、[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) 和 [SUN_VALLEY_LICENSE.txt](SUN_VALLEY_LICENSE.txt)。
