# 发布检查清单

在 Windows PowerShell 中从本目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
& ".\.venv\Scripts\python.exe" -m pytest ".\tests" -q -rs
powershell -ExecutionPolicy Bypass -File .\scripts\offline_demo.ps1
& ".\.venv\Scripts\python.exe" .\scripts\bridge_l1_e2e.py
& ".\.venv\Scripts\python.exe" .\scripts\gui_takeover_smoke.py
```

验收标准：Windows 测试全部通过；受限机器无法创建符号链接时，相关用例允许按平台跳过；
离线 demo 输出 `SIMULATED`；桥 L1 与接管 GUI 冒烟输出 `PASS`。

发布前检查：

- 不提交 `.venv/`、`__pycache__/`、`.pytest_cache/`、日志、备份或 bridge capture；根目录
  `.gitignore` 已覆盖这些路径。
- 不提交 `auth.json`、API Key、Authorization 头或真实请求正文。桥默认关闭，也不会自动改 Codex
  配置。
- `demo/probe/bundled_models_wsl.json` 是脱敏结构样例，不是用户运行时导出物。
- 真实 DeepSeek 中转和桌面 GUI 验收证据属于当前环境记录；换机器、运行时或中转后应重新验证。
- 发布版不包含已退役的 `deepseek-delegation` MCP；DeepSeek 任务委派由独立的 `dsh` 集成负责。

当前对外版本与 macOS 共用 `v0.4.2`，Windows 资产标注为未签名预览版。正式发布以同一 Release 页的 Windows ZIP 与校验和可下载为准；Windows 工作流只构建与验收，不会阻断 macOS 主发布。
