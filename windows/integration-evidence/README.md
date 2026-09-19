# integration-evidence — 真实 WSL Codex 兼容性证据

本目录保存脱敏的结构化兼容性证据和可重跑脚本。**不保存任何凭据或整个用户配置。**
随机临时路径在证据中标注为当次路径，重跑后会变化。

## 文件

| 文件 | 说明 |
|---|---|
| `wsl-direct-evidence.json` | 审查者**直接在 WSL 内**执行隔离探针获得的证据。这是真实 WSL 行为，但**不是** Windows→wsl.exe 调用链的结果；内部探针脚本不属于公开发布目录。 |
| `probe_wsl_compat.py` | 可重跑脚本：通过本项目的 `RuntimeTarget` 适配器测试**完整 Windows→wsl.exe→Ubuntu→codex** 调用链的兼容性。使用与 CLI `probe --verify` / GUI "验证兼容性" 相同的 `probe_compatibility()`。 |
| `integration_flow.py` | 可重跑脚本：完整集成流程 probe→apply→read→undo，全部在隔离 temp CODEX_HOME 中执行。 |
| `wsl-chain-evidence.json` | `probe_wsl_compat.py` 运行成功后生成；当前可能不存在（需在具备 wsl.exe 的 Windows 环境运行脚本后产生）。 |

## 两种证据的区别（重要）

1. **直接 WSL 证据**（`wsl-direct-evidence.json`）：审查者在 WSL shell 内直接运行隔离探针，
   子进程是 Linux 原生进程，不经 wsl.exe 转发。内部探针脚本不随本项目发布。
   证明 Codex 运行时本身支持 `model_catalog_json` 配置读取。

2. **Windows 链路证据**（`wsl-chain-evidence.json`，待生成）：在 Windows Python 中运行
   `probe_wsl_compat.py`，子进程经 `wsl.exe --distribution Ubuntu --exec env CODEX_HOME=... codex`
   调用。证明本项目的适配器正确连接 Windows→WSL→Codex。

两者都需要：前者证明运行时能力，后者证明适配器链路。`probe_wsl_compat.py` 尚未在本会话
中运行（需 wsl.exe + 真实 Codex 运行时），交付为可重跑脚本供验收人执行。

## 如何重跑

```powershell
cd windows
$env:PYTHONPATH = (Join-Path (Get-Location) "src")

# A. 仅探测兼容性（生成 wsl-chain-evidence.json）
.\.venv\Scripts\python.exe .\integration-evidence\probe_wsl_compat.py

# B. 完整流程：探测 → apply → 读取验证 → 撤销
.\.venv\Scripts\python.exe .\integration-evidence\integration_flow.py

# 自定义运行时/发行版
.\.venv\Scripts\python.exe .\integration-evidence\probe_wsl_compat.py `
    --runtime C:\path\to\codex --distro Ubuntu
```

退出码 0 = 通过；非零 = 某步失败（见 stderr）。所有写入在临时 CODEX_HOME 中，
不触碰用户真实 `~/.codex/config.toml` 或 `auth.json`。

## 证据字段说明（probe_wsl_compat.py 输出）

| 字段 | 含义 |
|---|---|
| `ok` | 全部行为证明步骤通过 |
| `runtimePath` / `runtimeSha256` / `runtimeVersion` | 运行时路径/文件指纹/版本（证据绑定） |
| `runtimeKind` / `distro` | 后端类型/发行版（证据绑定） |
| `probeScheme` | 探测方案版本（`wsl-adapter-v1`），方案变更后旧证据失效 |
| `probeSlug` | 当次随机测试 slug（`compat-probe-<uuid12>`），不泄露用户数据 |
| `bundledModelCount` | bundled 目录模型数 |
| `markerLoaded` | 正对照：非 bundled 读取返回了测试 slug |
| `markerAbsentFromBundled` | 负对照：测试 slug 不在 bundled 中 |
| `missingCatalogExitCode` | 负对照：指向不存在文件时退出码（应为非零） |
| `probedAt` | 探测时间（UTC ISO 8601） |

## 安全约束

- 不复制或输出 `auth.json` 或任何认证文件。
- 探测使用隔离 HOME/CODEX_HOME/cwd，过滤认证环境变量，不启动会话生成，不调用模型推理接口。
- 证据中不含用户配置内容，只有运行时身份指纹和行为证明的布尔/计数结果。
