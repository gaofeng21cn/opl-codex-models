# HANDOFF — Codex Model Manager（Windows 移植）.第一轮交付

项目：将 [gaofeng21cn/opl-codex-models](https://github.com/gaofeng21cn/opl-codex-models)
（macOS/Swift）核心功能移植到 Windows。本文件面向验收人与后续工程师。

提交者角色：执行工程师。原始参考源码已保留在 `work/opl-codex-models/`，未修改；
Windows 实现在本目录 `outputs/opl-codex-models-windows/`。许可证与改动来源声明见
`LICENSE`、`THIRD_PARTY_NOTICES.txt`、`SOURCE_NOTICE.md`。

发布说明：本文保留了多轮验收的历史记录，其中部分 `work/...` 路径指向内部联调材料。
这些材料不在公开发布目录内；可交付和可重跑内容以本目录的 `scripts/`、`tests/` 与
`integration-evidence/` 为准。

---

## 1. 实现范围与目录结构

### 本轮实现（已完成）
- **真实接口探测**：Windows 原生运行时查找 + WSL 二进制单独分类列出。
- **最小图形应用**：tkinter GUI，展示官方/自定义/合并模型、搜索、新增自定义模型、
  编辑推理档位、备份恢复、显式“应用到 Codex”（先展示路径与差异，dry-run 不写）。
- **命令行同步入口**：`probe / sync / list / add / reasoning / backup / restore / apply`。
- **核心逻辑与 GUI 分离**：`core/` 纯逻辑，`services/` 供 GUI/CLI 共享，`cli.py` 命令行。

### 暂未实现（明确留给下一轮）
- 完整自动同步（计划任务 / 常驻进程）。
- 安装包（PyInstaller / Inno Setup）。
- 真实 Windows 原生 Codex 集成（本机没有）。
- GUI 化的字段级覆盖编辑（上下文覆盖目前走 CLI/配置）。

### 目录结构
```
outputs/opl-codex-models-windows/
  src/codex_model_manager/
    core/errors.py          # CodexModelError 及子类
    core/process_runner.py  # 参数数组子进程 + 超时 + 退出码 + CREATE_NO_WINDOW
    core/runtime.py         # Windows/WSL 运行时发现 + classify(PE/ELF) + CODEX_HOME
    core/catalog_sync.py    # 合并同步（隔离 CODEX_HOME、优先级、保未知字段、覆盖、日志）
    core/overrides.py       # ModelFieldOverrides 上下文覆盖
    core/custom_models.py   # 从模板复制新增 + 校验
    core/reasoning.py       # 推理档位（KNOWN_EFFORTS、is_valid、set_enabled）
    core/config_editor.py   # tomlkit 保注释编辑 model_catalog_json
    core/backup.py          # 原子写 + 备份/恢复
    core/app_config.py      # 应用自身配置（%LOCALAPPDATA%\CodexModelManager\config.json）
    parser.py               # 模型/日志解析
    services/catalog_data_service.py  # GUI+CLI 共享（快照/新增/推理/同步）
    cli.py / __main__.py    # 命令行入口
    gui/app.py              # tkinter 界面
  tests/                    # pytest 行为测试
  demo/                     # 离线样例：mock codex、样例自定义模型、WSL probe 样本
  scripts/                  # offline_demo / setup_demo / launch_gui / gui_smoke
  README.md LICENSE THIRD_PARTY_NOTICES.txt SOURCE_NOTICE.md requirements.txt
```

## 2. 参考原项目 commit SHA

`bb588846b28beeece759adcdc917e643fdb8077c`（`git -C work\opl-codex-models rev-parse HEAD`）。
移植依据的 Swift 源码：
`Sources/CodexModelCore/CatalogSyncService.swift`、`ProcessRunner.swift`、
`ModelFieldOverrides.swift`、`AppConfiguration.swift`、`SetupService.swift`、
`Sources/CodexModelManager/Services/CustomModelEditor.swift`、`CatalogParser.swift`、
`Models/ReasoningSettings.swift`；测试参考
`Tests/CodexModelManagerTests/CatalogSyncServiceTests.swift`、`CustomModelEditorTests.swift`。

## 3. 技术选择

- **Python 3.11+**（开发/验证环境 3.12），无编译步骤，适合首轮。
- **tkinter**：Windows 内置图形界面，零第三方 GUI 依赖。
- **tomlkit**：保留注释/无关键的 TOML 编辑（语义对齐原 Swift 的行式编辑器但更安全）。
- **子进程**：`subprocess.Popen` 参数数组 + `timeout` + 退出码检查，`CREATE_NO_WINDOW`；
  全程不用 shell 字符串拼接。
- **原子写**：临时文件 + `os.replace`；备份/恢复前先保留当前文件再覆盖。

## 4. Windows / Python / Codex 版本与接口探测结论

### 运行时探测（真实执行，非假设）
- **查找方式**：先显式用户路径（`--config` 里的 `codexRuntimePath`），再 `PATH`
  查 `codex`/`codex.exe`，最后（仅 `--wsl` 时）扫 `~/.codex/bin/wsl/<sub>/codex`。
- **二进制分类**：读文件头——`MZ`→`windows-native`，`\x7fELF`→`wsl`。原生 Windows
  进程无法直接执行 ELF，故 WSL 路径统一经 `wsl.exe` 转发探测版本，永远不把 WSL
  成功当作 Windows 验证成功。
- **本机结论**：在已检查的位置（显式路径、PATH、一个 `~/.codex/bin/wsl/<sub>` 目录）
  未找到 Windows 原生 Codex；未做更广的全盘扫描。找到的唯一运行时是
  `~\Users\MECHREVO\.codex\bin\wsl\385b74eb4db8c237\codex`（ELF/WSL）。
  因此本轮**无原生 Windows 真实集成测试**，全部核心验证走 mock / 模拟。

### 接口探测（真实执行）
- `codex --version`（本机无原生，无输出）；经 WSL 探测 WSL 二进制版本：
  `codex-cli 0.155.0-alpha.9`。
- `codex debug models --bundled`：WSL 二进制返回 `{"models":[...]}`，可解析。
  **该输出是数据（bundled 目录），不是配置 schema，不构成 `model_catalog_json`
  配置读取能力的证据**；真实配置读取成功需另行验证。
- `model_catalog_json` 配置读取能力：**未验证**——`probe` 固定如实输出
  “未验证（需真实运行时成功读取 config 后另行证明；bundled 输出不是配置 schema）”，
  不会仅凭 `--version` 或 bundled 输出宣布支持。
- Windows 原生接口支持与否：**在已检查位置无原生运行时，未验证，列为未验证项**。

## 5. 测试命令与通过/失败数量

测试运行于本机 Windows + Python 3.12 的虚拟环境 `.venv`：

```powershell
cd outputs\opl-codex-models-windows
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$env:PYTHONDONTWRITEBYTECODE = "1"   # 避免在系统 Python 目录写 pycache
.\.venv\Scripts\python.exe -m pytest -q
```

**结果：37 passed，0 failed**（本次运行 `37 passed in 9.90s`）。

覆盖点（对齐原 Swift 语义）：
- `test_catalog_sync`：合并语义、重叠(overlap)拒绝、未知字段保留、字段级覆盖跟随上游
  及取消防覆盖、bundled 失败保留旧合并文件、子进程失败/超时、中文+空格路径。
- `test_custom_models`：从模板复制、空/重复/非法 slug、克隆官方模板进空自定义源
  （优先级=ceiling+index+1，不猜测）、推理编辑保留其他模型与未知元数据、非法设置拒绝、
  移除默认档位选择剩余、空选择非法。
- `test_config_editor`：TOML 顶层与 profile 表注释保留、缺键时新增、二次调用不再重复
  备份、损坏 config 不清除原文件。
- `test_backup`：备份/恢复往返（先留当前再覆盖）、失败写保留旧数据、缺失备份报错。
- `test_runtime`：PE/ELF 分类、CODEX_HOME 环境变量/默认、用户路径与原生优先。
- `test_review_fixes`（本轮新增，12 项）：apply `--diff`/`--dry-run` 严格只读（字节不变、
  无备份、无建目录）；apply 目标只取 `codexConfigPath`/`--codex-config`，绝不从环境推导；
  安全预览只展示 model_catalog_json 新旧值、不泄露配置里的 api_key 等秘密；`recommended()`
  默认落到独立应用沙箱而非真实 CODEX_HOME，且完整演示流程（init→sync→add→reasoning→
  只读 apply）不改动哨兵真实 CODEX_HOME；恢复按内容类型（`classify_backup`）校验、类型不
  匹配拒绝；probe 只报告已验证能力；无运行时/无显示下 GUI 模块可安全 import 且有离线路径。

## 6. GUI 验证方式

- **无显示环境下仅验证 import 安全与离线路径可到达**（自动）：
  `tests/test_review_fixes.py::test_gui_importable_without_display_and_survives_missing_config`：
  配置损坏/无运行时时 `resolved()` 抛 `RuntimeNotFound`，不崩溃；GUI 模块可正常 import。
- **带显示的基本操作检查（需显示桌面环境，非无头）**：
  `python scripts/gui_smoke.py --config demo\persist\config.json`
  → **PASS: basic GUI ops ok (total=3, search_hits=1)**（本机显示环境实测通过；该脚本在
  TRAE 沙箱内会被 tkinter 写系统 pycache 拦截，需在沙箱外运行）。**注意 `gui_smoke.py`
  通过 `tk.Tk()` 创建根窗口，需要显示桌面环境**——无 GUI 会话（如无桌面/CI 无 DISPLAY）
  下会失败或超时；这不是“无头”测试。
- **真实窗口人工检查**：`powershell -ExecutionPolicy Bypass -File .\scripts\launch_gui.ps1`
  打开窗口。**受限于无自动化点击真实窗口的能力，完整交互待验收人在桌面会话复核**。

## 7. 真实验证 / 模拟验证 / 未验证 —— 分别列明

### 真实（可复现的本机实际行为）
- WSL 二进制存在且可经 `wsl.exe` 探测版本：`codex-cli 0.155.0-alpha.9`（`probe --wsl` 输出）。
- WSL 二进制 `debug models --bundled` 返回 `{"models":[...]}`；公开样本存于 `demo/probe/`，仅保留目录字段，不包含真实运行时的提示文本。
- 在已检查的位置未找到 Windows 原生 Codex —— 这是真实原生集成受阻的根因（未做全盘扫描）。
- `bundled_models_wsl.json` 仅是二进制输出现象样本，**不是 `model_catalog_json` 配置
  schema 的证据**；配置读取能力保持“未验证”。

### 模拟（明确标识，绝不冒充真实）
- 全部 37 个 pytest 用例基于 mock Codex（`demo/mock/codex.py` + `codex.cmd` wrapper）。
- `scripts/offline_demo.ps1` 全流程：probe → sync → list → add → reasoning →
  二次 sync(no_change) → backup → apply --dry-run，均通过（输出标 SIMULATED）。
- GUI smoke（mock 数据，需显示环境）。
- 以上均为 **模拟验证**，不代表真实 Codex 集成通过。

### 未验证（须在下轮或具备条件后补）
- **Windows 原生 Codex 的 `--version` / `debug models --bundled`**（已检查位置无原生，未验证）。
- **原生 Windows 的 `model_catalog_json` 支持**（无运行时，未验证）。
- 真实 `~/.codex/config.toml` 的端到端“应用到 Codex”（按设计默认不触碰，且需显式
  `apply`；本轮只读 `--diff`/`--dry-run` 核对，未真写）。
- 真实凭据 / 认证流程（按要求本轮不触碰）。
- GUI 真实桌面点击（需显示环境，未自动化）。

## 8. 关键取舍与安全设计

- **默认沙箱**：`sync` 在临时隔离 `CODEX_HOME` 中读取官方目录，模型源/合并目录默认放到
  独立应用沙箱（`%LOCALAPPDATA%\CodexModelManager`）。真实写入只经显式 `apply`：目标
  config 仅取自 `codexConfigPath`/`--codex-config`（绝不从环境推导），先备份、原子写、
  保留 TOML 注释与无关配置。
- **安全预览，不在差异里回显密钥**：`--diff`/`--dry-run`/GUI 预览只展示 `model_catalog_json`
  的旧值/新值，不做整文件 unified diff，因此不会带出 api_key 等无关或机密配置；写入仍
  保留原文件所有内容。解析失败提示也不回显原始配置片段。
  （`set_model_catalog` 会读取目标 config 以保注释编辑，但这不等于“从不读取”；它只读取
  TOML 结构，从不把令牌/密钥输出到 stdout/stderr/预览。）
- **恢复按内容类型校验**：`classify_backup` 依据文件内容（而非文件名）区分
  `model_catalog_json` / `toplevel_config` / `unknown`；禁止把 config 备份恢复成模型 JSON
  或反之；未知类型拒绝。恢复前仍先备份当前目标。
- **重复模型优先级不猜测**：自定义模型 priority = 官方 `priorityCeiling` + index + 1，
  与原始逻辑一致；重叠 slug 时报错，而非自行仲裁。
- **失败/损坏保护**：解析失败、子进程失败、写失败均保留旧文件并给出明确错误。
- **路径**：支持 `CODEX_HOME`、显式用户选择，支持中文与空格（新增专项测试）。
- **GUI 可恢复/离线**：无运行时、配置损坏时不崩溃，进入离线模式并提示“设置 Codex 运行时
  …”，可手动选择运行时；不自动覆盖损坏文件。长时间子进程同步移至后台线程并防止重复提交。

## 9. 文件变更清单

- **重做/移植**（相对 `work/opl-codex-models` 为新增实现目录）：
  全部 `src/codex_model_manager/**`、`tests/**`、`demo/**`、`scripts/**`、`README.md`、
  `requirements.txt`、`LICENSE`、`THIRD_PARTY_NOTICES.txt`、`SOURCE_NOTICE.md`。
- **未改动**：`work/opl-codex-models/**`（原始参考保留）。

## 10. 已知问题

- 沙箱内创建 venv 时曾遇“restricted”报错，用 `PYTHONDONTWRITEBYTECODE=1` 规避；README
  已记录该环境变量。
- PowerShell 5.1 对“无 BOM 的 UTF-8”中文 .ps1 会按 ANSI 误读；已把含中文的脚本改为
  ASCII-only，中文文件名用码点构造（见 `offline_demo.ps1`）。
- GUI 真实桌面临时未自动化点击，有待验收人复核（`gui_smoke.py` 需显示环境，非无头）。
- 只读 `\\.vscode` 受限未影响本目录构建。
- 本轮 Windows 侧未做真实原生 Codex 集成（已检查位置无原生运行时）；未独立复跑由各
  验收方报告的 Windows 专属 mock `.cmd` 测试——本文档的 37 passed 均为本机独立复跑。

## 11. 如何启动并复现首轮验收

```powershell
cd outputs\opl-codex-models-windows
# （若尚无 .venv）新建并安装依赖：
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# A. 离线样例（模拟，无需真实 Codex）
powershell -ExecutionPolicy Bypass -File .\scripts\offline_demo.ps1

# B. GUI（mock 数据，需显示环境）
powershell -ExecutionPolicy Bypass -File .\scripts\launch_gui.ps1

# C. 测试（37 passed）
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest .\tests -q

# D. 真实运行时探测（本机仅在 WSL 下找到 Linux 二进制）
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
.\.venv\Scripts\python.exe -m codex_model_manager --config demo\persist\config.json probe --wsl
```

复现核对点：A 应输出“Demo finished (SIMULATED)”且各步退出码 0；C 应 `37 passed`（不含
GUI 显示类）；D 打印 `kind=wsl ... codex-cli 0.155.0-alpha.9`，并对 `model_catalog_json`
配置读取能力如实输出“未验证”。

---

## 12. 本轮修复与证据（验收“暂不通过”整改）

下表逐条对应首轮验收给出问题。修复文件均为
`outputs/opl-codex-models-windows` 下新增/修改，未开始安装包或自动同步，
未改动用户真实 `~/.codex` 配置。

> 测试全部运行于本机 Windows + Python 3.12 `.venv`：
> `$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m pytest tests/ -q`
> 结果：**37 passed in 9.90s**（原 25 + 本轮新增 12 全能回归）。

### P1-1 隔离演示与真实写入
- **问题**：`recommended()` 曾把模型源/合并结果指向真实 CODEX_HOME；GUI 的
  `apply_to_codex()` 会从环境/主目录推导真实 config.toml，演示数据可能被写入真实配置。
- **修复**：`core/app_config.py` 新增 `codex_config_path` 字段作为 apply 的唯一目标；
  `recommended()` 把模型源、合并目录、日志、备份全部放入独立应用沙箱
  `%LOCALAPPDATA%\CodexModelManager`，`codex_config_path` 保持 `None`（未设即无真实
  写入目标）。演示界面只允许“预览”，目标路径一律取自配置而非环境推导。
- **回归用例**：`test_recommended_uses_app_sandbox_not_real_codex_home`、
  `test_demo_flow_leaves_real_codex_home_untouched`、
  `test_apply_requires_codex_config_path_never_derives_from_env`。
- **执行结果**：通过。以临时目录模拟“真实 CODEX_HOME”写入哨兵文件；从 init→sync→add→
  reasoning→只读 apply 全流程均不改动哨兵文件。
- **剩余限制**：真实写入路径 `codexConfigPath` 未对“真实 ~/.codex”做端到端验证（无原生
  运行时，且按要求不触碰用户真实配置）。

### P1-2 仅查看差异却发生写入
- **问题**：`cmd_apply()` 在打印差异后仍继续 `set_model_catalog`，只有 `--dry-run` 才提前
  返回；审查者在临时目录复现 `apply --diff` 实际修改了 config.toml。
- **修复**：`cli.py` 的 `cmd_apply()` 把 `--diff` 与 `--dry-run` 都定义为严格只读——打印
  安全预览后即返回，不备份、不创建目录、不写字节；真实 apply 明确为“显式写入”语义。
  删除了 `init` 输出中不存在的 `sync --write` 用法。
- **回归用例**：`test_apply_diff_is_strictly_readonly`、
  `test_apply_readonly_does_not_write_even_with_merged`；显式真实写入另由
  `test_config_editor` 覆盖。
- **执行结果**：通过。只读路径验证目标 config 字节不变、备份目录文件数不变。
- **剩余限制**：无。

### P1-3 配置差异泄露无关配置
- **问题**：`cli.py:142` 与 `gui/app.py:341` 对整份 config.toml 做 `unified_diff`，会带出
  api_key 等无关/机密配置；审查者用含 `api_key="FAKE_SECRET_FOR_TEST_ONLY"` 的合成数据
  复现密钥出现在 CLI 输出。
- **修复**：新增 `core/safe_preview.py` 统一安全预览——`render_preview()` 只展示
  `model_catalog_json` 的旧值/新值，不做整文件 diff；CLI、GUI、错误提示共用该实现；
  `parse_error_note()` 解析失败时也不回显含秘密的原始片段。GUI 的 `apply_to_codex()` 与
  CLI `cmd_apply()` 均改用 `render_preview(info)`。
- **回归用例**：`test_safe_preview_hides_unrelated_config_and_api_key`、
  `test_apply_output_leaks_no_secret`（覆盖顶层与 profile[profiles.work]/provider 内合成
  密钥，stdout/stderr/GUI 预览文本均不含秘密）。
- **执行结果**：通过。`render_preview` 只含 `目标 config`、`当前 model_catalog_json`、
  `拟写入 model_catalog_json` 三行，无密钥/无关配置。
- **剩余限制**：`set_model_catalog` 写入前会读取目标 config 的 TOML 结构（保注释编辑），
  但从不把密钥输出到 stdout/stderr/预览；读取与输出的边界已用测试固定。

### P1-4 恢复备份选错文件
- **问题**：`gui/app.py:313` 以文件名含 “models” 判断恢复目标，`custom-models.*.bak`
  也含 “models” 会误覆盖合并目录。
- **修复**：新增 `core/backup.py::classify_backup(path)` 按**内容**分类（顶层含
  `models` 数组→`model_catalog_json`；可解析 TOML 的非模型目录→`toplevel_config`；
  否则→`unknown`）。`restore`（CLI + GUI）要求用户显式选择目标（merged/custom/config），
  用 `classify_backup` 校验备份类型与目标匹配，禁止 config 备份恢复成模型 JSON（或反之），
  未知类型拒绝。恢复前仍先备份当前目标。
- **回归用例**：`test_restore_rejects_wrong_backup_type_by_content`、
  `test_restore_mismatch_refused`。
- **执行结果**：通过。自定义/合并/配置备份按内容各只能恢复到正确目标；无效备份（无法
  识别）被拒绝并保留目标文件。
- **剩余限制**：无。

### P2-1 运行时探测未验证却宣布支持
- **问题**：`cmd_probe()` 曾无条件打印 model_catalog_json 支持，即使只跑过 `--version`
  或用 mock；HANDOFF 曾称“本机没有原生 Codex”；`bundled_models_wsl.json` 曾被当作
  schema 证据。
- **修复**：`cmd_probe()` 逐运行时输出 `version: 已找到/未验证`、
  `bundled 接口: 已验证（exit 0）/未验证 + detail`（新增 `runtime.py::probe_bundled`，
  对 WSL 二进制经 `wsl.exe -e bash -lc` 转发，只验证“bundled 接口”而非“配置读取”）；
  对 `model_catalog_json` 恒如实输出“未验证（bundled 输出不是配置 schema）”。
  未找到时打印“在已检查的位置未找到 Codex 运行时”并列明已检查位置。
- **回归用例**：`test_probe_reports_only_verified`。
- **执行结果**：通过。对 `version=None` 的运行时只报告“已找到/未验证”，bundled 接口
  单独标注，配置读取能力固定“未验证”。
- **剩余限制**：真实配置读取能力仍未验证（无原生运行时）。

### P2-2 新安装无运行时直接启动失败
- **问题**：GUI `__init__` 在构建界面前调用 `resolved()`，未找到原生运行时会异常退出，
  用户无法选择运行时或进入离线模式；长时子进程同步运行在 GUI 主线程。
- **修复**：`gui/app.py` 的 `__init__` 改为先加载配置（`ConfigurationNotFound` 时用
  `recommended()` 生成并保存；其他配置错误/运行时缺失时进入离线模式且**保留损坏文件**，
  不自动覆盖），再构建界面。新增“设置 Codex 运行时…”按钮可手动选择 `codex` 可执行文件；
  `_reload()` 显示离线/错误/同步中状态；`sync()` 移到后台线程（`_syncing` 防重复提交，
  用 `root.after` 回主线程更新）。
- **回归用例**：`test_gui_importable_without_display_and_survives_missing_config`。
- **执行结果**：通过。无显示环境可安全 import；无运行时时 `resolved()` 抛
  `RuntimeNotFound` 而非崩溃。
- **剩余限制**：真实窗口的“设置运行时”交互与后台同步按钮点击无法在无显示环境自动化，
  需桌面会话人工复核。

### 交付说明
- 交付目录：`outputs/opl-codex-models-windows`
- 交付文档：`outputs/opl-codex-models-windows/HANDOFF.md`
- 未改 `/work/opl-codex-models`（原始参考）。未发布、未安装后台任务、未改真实 Codex 配置。

---

## 13. 第三轮验收修复与证据（第二轮"部分通过，两个阻塞项"）

本项目同一目录继续修改，未扩展安装包或后台任务，仍未改动用户真实 `~/.codex` 配置。
审查者已读实际代码、独立复跑 18 项平台无关测试（18 passed，7 deselected）；本机再复跑
Windows 全套并增补针对两个阻塞项实际缺陷的回归。

> 回归命令（全部在本机 Windows + Python 3.12 `.venv`）：
> ```powershell
> cd outputs\opl-codex-models-windows
> $env:PYTHONPATH = (Join-Path (Get-Location) "src")
> $env:PYTHONDONTWRITEBYTECODE = "1"
> .\.venv\Scripts\python.exe -m pytest tests/ -q
> ```
> **结果：44 passed in 12.44s**（原 37 + 本轮新增/改写 7 项全能回归，`test_review_fixes.py` 内
> 由“仅导入/命名强于覆盖”改成的实际入口用例已在其中）。另复跑 `scripts/offline_demo.ps1`
> （SIMULATED 全流程 PASS）与 `scripts/gui_smoke.py --config demo\persist\config.json`
> （真实显示环境 **PASS: basic GUI ops ok (total=3, search_hits=1)**）。

### 阻塞项 1（P1）：GUI 同步失败后无法恢复
- **问题**：`gui/app.py` 的 `sync()` 在 `except Exception as exc:` 内写
  `self.root.after(0, lambda: self._on_sync_error(exc))`。Python 离开 except 块后 `exc`
  会被清除，延迟回调真正执行时抛出 `NameError: cannot access free variable 'exc'`，
  `_on_sync_error` 未执行，`_syncing` 持续为 `True`，之后永远无法再次同步。审查者用 AST
  提取实际方法、排队 `root.after` + 失败服务（不依赖图形显示）复现了该 NameError 且
  `_syncing` 卡死。
- **修复**：`gui/app.py` 提取模块级 `run_sync_in_background(service, schedule, on_done, on_error)`——
  在后台线程 `work()` 内用 `try/except` 立即 `sys.exc_info()[1]` 取出异常文本，通过
  `lambda` **默认参数**（`lambda text=msg: on_error(text)`）把文本绑定进回调，杜绝闭包读取
  已清除的 except 变量；`service` 在同步开始时捕获（捕获到局部变量），避免用户中途更换运行时
  导致任务使用不同配置；`_on_sync_done` / `_on_sync_error` 都置 `_syncing=False`；`schedule`
  抛异常（窗口关闭等）时吞掉不中断。有任务运行（`_syncing` 为真）时 UI 不允许再触发同步，
  同时提供单独的运行时更换受限逻辑（任务运行中禁用更换）。
- **回归用例**：`tests/test_review_fixes.py::test_sync_background_error_resets_and_recovers`。
  用受控 `Surrogate` 界面对替身驱动 `run_sync_in_background`，先**等 worker 线程排好回调
  （即已离开 except 块）**再 `flush()` 执行回调——正是旧闭包崩溃发生的时机——断言：错误文本
  `"BOOM-sync-failed"` 正确到达、`syncing is False`、随后第二次同步成功（`status == no_change`）。
 该用例不依赖图形显示，也**不是**仅导入模块或仅测“同步成功”，直接覆盖本缺陷。
- **执行结果**：通过。

### 阻塞项 2（P1）：真实 apply 未验证运行时与兼容性
- **问题**：`core/app_config.py::resolved()` 对显式 `codexRuntimePath` 只验证绝对路径，不
  验证文件存在/可执行；GUI `self.data` 非空只代表建了 Python 服务对象；CLI/GUI apply 均未读取
  兼容性检查结果。审查者在临时目录设 `codexRuntimePath=绝对路径/DOES_NOT_EXIST.exe`、
  `{"models":[]}`，`apply` 返回 0 且目标 config.toml 被写入。绝不能把 mock 的 `--version`
  或 bundled 输出冒充“真实能力”。
- **修复**：
  - `core/app_config.py::resolved()`：显式 `codexRuntimePath` 现在先 `_resolve_path` 再
    `os.path.exists`，不存在即抛 `InvalidConfiguration("codexRuntimePath 指向的文件不存在")`。
  - 新增共享写门 `core/safe_preview.py::apply_gate(config, merged_catalog, requested_target,
    readonly)`，CLI `cmd_apply` 与 GUI `apply_to_codex` **共用同一检查逻辑**。规则：
    readonly（`--diff`/`--dry-run`/GUI 预览）恒可预览但永不写；
    真实写需依次通过：运行时存在且可执行（`_resolve_runtime`）→ 合并目录结构有效
    （`_catalog_models`：存在且 `models` 为非空列表）→ 目标在允许范围。演示模式（新增
    配置字段 `is_demo`，见 `setup_demo.ps1`/`offline_demo.ps1`）仅允许写应用沙箱内
    （merged_catalog 所在目录），目标越界即拒绝——**禁止用 CLI `--codex-config` 覆盖成沙箱外
    目标**；真实模式**无真实运行时成功读取 config 的 `model_catalog_json` 兼容性证据就保持
    只读并明确“未验证”**（不拿 mock `--version`/bundled 冒充）。gate 被拒时仅打印安全预览
    +“（只读）未写入”与原因并返回退出码 1，目标字节不变。
  - `sync()` 输出一旦被真实 Codex 引用即非“预览”：我们已经做了明确的**演示/真实分离**——
    写入只发生在演示沙箱内或经证据证明兼容的路径，否则一律只读，语义不再把实际写入称作只读。
- **回归用例**（`tests/test_review_fixes.py`，均走真实 `apply` 入口、仅用 mock 数据构造）：
  `test_apply_nonexistent_runtime_rejected_bytes_unchanged`（不存在运行时拒绝且字节不变）、
  `test_apply_real_mode_unverified_refused`（真实模式、无兼容证据→拒绝、“未验证”、字节未变）、
  `test_apply_invalid_catalog_refused`（无效合并目录拒绝、字节未变）、
  `test_apply_demo_out_of_sandbox_refused`（演示越界拒绝、字节未变）、
  `test_apply_demo_insandbox_allowed`（允许的沙箱写入单独通过）。预览仍可离线工作
  （`test_apply_diff_is_strictly_readonly`、`test_apply_target_uses_config_path` 保持通过）。
- **执行结果**：通过。

### 补齐实际行为测试（第 3 点）
原 `test_review_fixes.py` 里三个“命名强于覆盖”的用例已改造成真正走到入口/共享服务的行为测试，
环境变量用 monkeypatch 隔离（`test_review_fixes.py` 各 `_run_cli` 用例在子进程里设
`MOCK_BUNDLED_JSON`/`CODEX_HOME`，`_apply_cfg` 走 `monkeypatch.setenv`），并补出成功 + 拒绝
两条支路：
- 恢复用例改为**真正调用 CLI `restore`**：`test_restore_config_backup_rejected_for_model_target`
  （config 备份恢复到 merged 目标→拒绝“数据类型不匹配”、目标字节不变、不新增备份）、
  `test_restore_mismatch_refused_unknown_backup`（未知备份→“无法识别”、目标不变）、
  `test_restore_custom_backup_to_custom_target_succeeds`（模型备份成功恢复到 custom 目标、
  内容替换、先备份当前文件——备份名按 `Path(target).stem` 前缀，故用 `custom*` 匹配含空格
  的 `custom models`）。
- `test_demo_flow_leaves_real_codex_home_untouched` 改为尾部**真正调用 `cli_main([...,"apply"])`**
  走真实模式入口，断言退出码非 0 且哨兵真实 config 字节不变（不再只调 `preview`）。
- 报告口径（下方逐项）严格区分“Windows 真跑 / 替身 / 真实 GUI 未验证”，避免只增数量或刷 PASS。

### 本轮测试报告口径（哪些真跑 / 替身 / 未验证）
- **Windows 真跑（本机 `.venv`，无外部 Codex）**：`pytest tests/ -q` 44 passed；所有 CLI 用例
  均为真实 `cli_main`/子进程入口 + mock Codex（`demo/mock/codex.py` + `codex.cmd` wrapper）；
  `test_sync_background_error_resets_and_recovers` 用受控 `Surrogate` 界面对替身验证回调
  （等价 GUI 主循环的队列时机），非图形显示也可运行。
- **显示环境真跑**：`scripts/gui_smoke.py` 在真实 Windows 桌面会话 PASS（创建真实 `tk.Tk()`
  → 列表渲染 → 搜索过滤 → 选中 → 详情），`scripts/offline_demo.ps1` 全流程 PASS（输出 SIMULATED）。
- **替身**：无显示环境下 GUI 仅验证 import 安全 + 离线/`resolved()` 抛错路径
  （`test_gui_importable_without_display_and_survives_missing_config`）。后台同步回调用
  `Surrogate` 替身驱动（无需桌面）。
- **真实 GUI 操作 / 真实原生集成 未验证（如实保留）**：真实窗口中手动“设置运行时”、同步失败
  弹窗、`apply` 确认按钮的真实点击未自动化（无自动化点击能力）；本机已检查位置无 Windows 原生
  Codex，真实 `model_catalog_json` 配置读取兼容性证据仍**未验证**，故真实模式 apply 按修复后
  保持在只读“未验证”分支。

### 第 5 / 12 节数字更新
- 第 5 节为**第一轮**结果（37 passed）；第 12 节为第二轮整改结果（37 passed）。
- 本节（第三轮）在全套上为 **44 passed in 12.44s**。三处测试计数不同是因为每轮都**增补**了回归，
  属预期；README 已把“验证情况”更新为最新 44 passed。

### 交付说明（第三轮）
- 修复文件：`src/codex_model_manager/core/app_config.py`（`resolved()` 存在性检查、`is_demo`）、
  `src/codex_model_manager/core/safe_preview.py`（新增 `apply_gate`）、`src/codex_model_manager/cli.py`
  （`cmd_apply` 用共享门）、`src/codex_model_manager/gui/app.py`（`run_sync_in_background`、
  `sync()`、`apply_to_codex()`）、`scripts/setup_demo.ps1`、`scripts/offline_demo.ps1`（写 `isDemo`）、
  `tests/test_review_fixes.py`（实际行为回归）。
- 未改 `/work/opl-codex-models`（原始参考）。未发布、未安装后台任务、未改真实 Codex 配置。

---

## 14. 第四轮修复与证据（第三轮验收：写入限制三项整改）

第三轮验收结论：异步回调（`run_sync_in_background`）已通过；写入限制未通过。本节针对
三项写入限制缺陷修复，**不扩展功能**：真实模式 apply 仍然**始终拒绝**（未实现"可验证后
放行"的真实集成），如实称为"离线原型 / 真实写入未启用"，不称 Windows 移植已完整完成。

> 回归命令（本机 Windows + Python 3.12 `.venv`）：
> ```powershell
> cd outputs\opl-codex-models-windows
> $env:PYTHONPATH = (Join-Path (Get-Location) "src")
> $env:PYTHONDONTWRITEBYTECODE = "1"
> .\.venv\Scripts\python.exe -m pytest tests/ -q -rs
> ```
> **结果：59 passed, 1 skipped in 15.25s**。跳过项：`test_apply_demo_symlink_escape_refused`
> （本机 Windows 无开发者模式/管理员权限，`os.symlink` 抛 `OSError`，测试按约定 `pytest.skip`
> 并明确原因 `platform does not support symlinks`）。另有
> `scripts/offline_demo.ps1`（SIMULATED 全流程 **PASS**）与
> `scripts/gui_smoke.py --config demo\demo_home\config.json`（真实显示环境
> **PASS: basic GUI ops ok (total=4, search_hits=1)**）。

### 缺陷 1（P1）：演示目标可用 `..`/符号链接越过沙箱并实际写入
- **问题**：原 `apply_gate` 用 `Path.absolute() + str.startswith` 字符串前缀判断沙箱成员，
  `absolute()` 不消 `..`、`startswith` 把相似前缀目录（`sandbox_evil`）误判为在内，且不解析
  Windows junction/符号链接。审查者用 `--codex-config <沙箱>/../outside.toml` 复现沙箱外文件被改写。
- **修复**：`core/safe_preview.py` 新增 `_canonical(path)=os.path.realpath(os.path.abspath(path))`
  （消 `..`、解析符号链接/junction）、`_is_within(sandbox, target)=commonpath(normcase(...))`
  （成员关系比较，Windows 大小写不敏感，跨盘 `ValueError` 视为越界）。`apply_gate` 对沙箱根
  （演示模式下取 `merged_catalog` 所在目录的 canonical 形）与候选目标都做 canonical 化后比较；
  `gate.target` 即该 canonical 目标，检查和写入共用同一目标——**禁止用单次 `--codex-config`
  改变沙箱根**；真实写路径在进入沙箱判断前先 `os.path.isdir` **拒绝目录目标**。预览对目录不
  再尝试读取：`_read_text` 的 `exists()` 改为 `is_file()`（否则目录目标会在打印预告时抛
  `Permission denied`）。
- **回归用例**（均走真实 CLI `apply` 子进程、核对沙箱外哨兵字节，非仅断言 gate 布尔值）：
  `test_apply_demo_dotdot_escape_refused`（`sandbox/../outside.toml`，rc≠0、outside 字节不变）、
  `test_apply_demo_directory_target_refused`（目录目标 rc≠0、含"目录"）、
  `test_apply_demo_sibling_prefix_outside_refused`（相似前缀 `sandbox_evil` 拒绝、字节不变）、
  `test_apply_demo_symlink_escape_refused`（沙箱内链接越界拒绝、字节不变；平台不支持创建链接则
  skip）、合法沙箱写入仍由既有 `test_apply_demo_insandbox_allowed` 覆盖；绝对越界由既有
  `test_apply_demo_out_of_sandbox_refused` 覆盖。
- **执行结果**：3 passed + 1 skipped（symlink，平台限制），越界哨兵字节均不变。

### 缺陷 2（P2）：模型校验只看非空数组，`{"models":[null]}` 仍被接受
- **问题**：原 `_catalog_models` 只判 `models` 为非空 list，`{"models":[null]}` 被判
  `writable=True`（演示模式）。
- **修复**：`core/safe_preview.py` 新增共享验证器 `validate_models(models, *, require_priority,
  name)` + `validate_catalog_file(path, *, require_priority, name)`：元素必须为对象、slug 非空
  字符串且唯一、priority/context_window/max_context_window 为数字（merged 必须含整数
  priority，custom 不要求）、context/max 为正且 max≥context、input_modalities 为字符串数组、
  display_name/description/visibility 为字符串、`supported_reasoning_levels` 为含非空 effort 的
  对象数组且 effort 唯一、`default_reasoning_level` 须在其中；**未知字段保留不拒绝**。
  `apply_gate` 用 `_catalog_valid` 调用（require_priority=True）；`restore`（CLI + GUI）对
  `model_catalog_json` 备份用 `validate_catalog_file`（`--target merged` 时 require_priority=True、
  `custom` 时 False），`InvalidCatalog` 包装为 `CodexModelError` 拒绝恢复——**不让损坏备份覆盖健康
  模型源**。
- **回归用例**：`test_apply_invalid_merged_catalog_refused`（参数化 10 例：null/字符串元素、缺失/
  重复 slug、priority 非数字、字段类型错误、上下文字段错误/非正、max<context、display_name 错误、
  默认推理档位不在支持档位内——均 rc≠0 且目标字节不变）；`test_restore_invalid_model_backup_rejected`
  （`{"models":[null]}` 备份恢复 merged 拒绝、健康目标字节不变）。
- **执行结果**：10 + 1 均通过。

### 缺陷 3（P2）：离线预览仍被运行时检查挡住
- **问题**：`cmd_apply` 在 `apply_gate(readonly=True)` 之前调 `config.resolved()`，无运行时即
  抛 `RuntimeNotFound`/`InvalidConfiguration` 退出，`apply --diff` 无法离线预览。
- **修复**：`core/app_config.py` 拆分 `resolved_paths()`（运行时无关：验证 custom/merged 非空、
  launchAgentLabel 非空、`codex_runtime` 仅按显式路径填充且不查存在性）与 `resolved()`（在前者
  基础上再做运行时存在性/发现校验）。CLI `cmd_apply`/`cmd_backup`/`cmd_restore` 与 GUI
  `backup`/`restore`/`apply_to_codex` 均改用 `resolved_paths()`；真实写门的运行时校验仍由
  `apply_gate`（真实写路径 `_resolve_runtime`）完成，只读/预览/备份恢复不要求能发现/启动 Codex。
- **回归用例**：`test_apply_diff_offline_without_runtime`（`codexRuntimePath` 指向不存在文件、
  无任何可发现运行时，`apply --diff` rc==0、输出含 `model_catalog_json` 单字段变化、目标文件
  未写入/未变化）。
- **执行结果**：通过。

### 本轮报告口径（真跑 / 替身 / 未验证）
- **Windows 真跑（本机 `.venv`，mock Codex）**：`pytest tests/ -q` 59 passed、1 skipped；所有
  apply/restore 回归均为真实 CLI 子进程入口 + mock（`demo/mock/codex.py` + `codex.cmd`）。
- **显示环境真跑**：`gui_smoke.py`（真实 `tk.Tk()`）PASS、`offline_demo.ps1`（SIMULATED）PASS。
- **未验证（如实保留）**：真实 Windows GUI 的窗口交互（手动"设置运行时"、同步失败弹窗、apply
  确认按钮点击）仍未自动化；本机已检查位置无 Windows 原生 Codex，`model_catalog_json` 真实配置
  读取兼容性**未验证**，故真实模式 apply 仍停留在只读"未验证"分支（始终拒绝真实写入）。

### 交付说明（第四轮）
- 修复文件：`src/codex_model_manager/core/safe_preview.py`（canonical 沙箱成员判断、共享模型
  验证器、`_read_text` 目录保护）、`src/codex_model_manager/core/app_config.py`（`resolved_paths`
  拆分）、`src/codex_model_manager/cli.py`（`cmd_backup/restore/apply` 用 `resolved_paths`、restore
  模型结构校验）、`src/codex_model_manager/gui/app.py`（同上）、`tests/test_review_fixes.py`（16 项
  回归）。
- 未改 `/work/opl-codex-models`（原始参考）。未发布、未安装后台任务、未改真实 Codex 配置。
- **口径**：当前真实模式 apply 为"离线原型 / 真实写入未启用"（始终拒绝），**不称** Windows 移植
  已完整完成。真实 Windows junction 越界与真实 GUI 点击仍未实测（见第 14 节"未验证"）。

---

## 15. 第五轮修复与证据（第四轮验收：合法空自定义模型源无法恢复）

第四轮验收结论：上一轮三项缺陷通过独立定向复核；唯一剩余问题是"合法空自定义模型源无法恢复"。
本节仅修此一项，**不扩展真实集成、安装包或后台任务**；真实模式 apply 仍为"离线原型 / 真实写入
未启用"（始终拒绝）。

### 问题
`validate_models` 一刀切把 `models=[]` 判为 `InvalidCatalog("models 必须是非空数组")`。但应用初始化
自定义源本就合法地创建空数组（`{"schema": "...","models":[]}`）；用户新增第一个模型**之前**产生的
备份是空自定义源，应允许恢复到"无自定义模型"状态。审查者用实际 CLI `restore --target custom`
恢复 `{"schema":"codex_model_manager_custom_models.v1","models":[]}` 返回 1。

### 修复
- `core/safe_preview.py::validate_models` / `validate_catalog_file` 新增**显式**参数
  `allow_empty: bool = False`：空数组仅在 `allow_empty=True` 时被接受；默认（merged/apply）仍要求
  非空。`require_priority` 与 `allow_empty` 作为两个**独立、显式**的目录类型开关，不再靠
  `require_priority` 偶然决定空/非空语义。
- CLI `cmd_restore` 与 GUI `restore` 共用同一规则：`require_priority=(target=="merged")`、
  `allow_empty=(target=="custom")`。custom 恢复允许空源（并保持原有"恢复前先备份 + 原子写"），
  merged 恢复与 apply 依旧 `allow_empty=False` 拒绝空数组。null/非对象元素/缺失重复 slug/错误字段
  类型/上下文与推理约束等既有校验全部保留，未知字段（如顶层 `schema`）仍不拒绝。

### 回归用例（`tests/test_review_fixes.py`，均走真实 CLI restore/apply 子进程）
- `test_restore_empty_custom_backup_succeeds`：`{"schema":..., "models":[]}` 恢复到 custom 目标
  rc==0、目标变为空数组、恢复前内容已备份（`*.before-restore*.bak` 存在——原子替换先留旧）。
- `test_restore_empty_merged_backup_refused`：同一空数组恢复到 merged 目标 rc≠0、健康目标字节不变。
- `test_apply_invalid_merged_catalog_refused` 参数化新增 `[]`（空数组）一例：apply 对空 merged 拒绝
  且目标字节不变（与既有 null/字符串元素/缺失重复 slug/字段类型等 10 例共用断言）。

### 回归命令与结果
```powershell
cd outputs\opl-codex-models-windows
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest tests/ -q -rs
```
**结果：62 passed, 1 skipped in 15.78s**（跳过项仍为 `test_apply_demo_symlink_escape_refused`
——Windows 无符号链接权限，`platform does not support symlinks`）。相较上一轮 59 passed 增加 3 例
（空 custom 恢复成功 + 空 merged 恢复拒绝 + 空 merged apply 拒绝）。

### 报告口径（如实保留）
- **离线原型 / 真实写入未启用**：真实模式 apply 仍**始终拒绝**，未实现"可验证后放行"的真实集成。
- **未验证（继续列出）**：真实 Windows GUI 窗口交互（手动设置运行时、同步失败弹窗、apply 确认点击）
  未自动化；本机已检查位置无 Windows 原生 Codex，`model_catalog_json` 真实配置读取兼容性未验证；
  Linux/WSL 符号链接越界已由验收方独立验证，但 **Windows junction 越界仍未经实测**。

### 交付说明（第五轮）
- 修复文件：`src/codex_model_manager/core/safe_preview.py`（`validate_models`/`validate_catalog_file`
  增加 `allow_empty`）、`src/codex_model_manager/cli.py`（`cmd_restore` 传 `allow_empty`）、
  `src/codex_model_manager/gui/app.py`（`restore` 传 `allow_empty`）、
  `tests/test_review_fixes.py`（3 项回归）。
- 未改 `/work/opl-codex-models`（原始参考）。未发布、未安装后台任务、未改真实 Codex 配置。

---

## 16. 真实 WSL 接入（第六轮：从离线原型到真实 WSL 后端可用）

本轮将"离线原型 / 真实写入未启用"推进到"真实 WSL 后端可用、用户可显式启用"。核心变化：
用**行为证明 + 证据门控**替换之前的恒拒绝真实模式 apply。不再只改文案或取消拒绝条件——
真实写入现在**有条件放行**：兼容性证据有效时允许 apply，证据过期/不匹配/缺失时仍拒绝。

### 16.1 实测链路

```
Windows Python 3.12
  → wsl.exe --distribution Ubuntu --exec env CODEX_HOME=<linux> <codex>
    → codex --version / debug models --bundled / debug models
```

全部经 `core/wsl_adapter.py::RuntimeTarget` 适配器，参数数组传递，不经 `bash -lc` 拼接。
路径转换用发行版自己的 `wslpath -a -u`，回退到 `/mnt/<drive>` 映射。

### 16.2 运行时身份

| 属性 | 值 |
|---|---|
| 发行版 | `Ubuntu` |
| 运行时路径 | `/mnt/c/Users/MECHREVO/.codex/bin/wsl/385b74eb4db8c237/codex` |
| 版本 | `codex-cli 0.155.0-alpha.9` |
| SHA256 | `b544b069ae61d5e68e1d66cd5dbecbe8f975de5d57235150ceeb2cb2da3cd82e` |
| 探测方案 | `wsl-adapter-v1` |

### 16.3 兼容性探测（行为证明，替换恒拒绝）

`core/compat_probe.py::probe_compatibility(target)` 执行以下步骤，全部在隔离 temp CODEX_HOME 中：

1. **`--version`**：真实执行，解析版本字符串。
2. **`debug models --bundled`**：真实执行，解析 JSON，取首个模型为模板。
3. **正对照**：生成唯一随机 slug `compat-probe-<uuid12>`，写独立 catalog JSON + 临时
   `config.toml` 的 `model_catalog_json`，执行 `debug models`（非 bundled），断言返回该 slug。
4. **负对照 1**：bundled 目录不含该随机 slug（碰撞检测）。
5. **负对照 2**：`model_catalog_json` 指向不存在文件时 `debug models` 退出非零。
6. 清理 temp 目录，返回脱敏 `CompatEvidence`（frozen dataclass）。

证据绑定：`runtime_path` / `runtime_sha256` / `runtime_version` / `runtime_kind` / `distro` /
`probe_scheme`。运行时更换、更新、发行版改变或探测方案升级后 `evidence_valid()` 返回 False，
apply 门拒绝并要求重新验证。证据中**不含用户配置内容**，只有指纹和布尔/计数结果。

### 16.4 证据门控 apply

`core/safe_preview.py::apply_gate` 真实模式分支不再恒拒绝，改为检查
`evidence_valid(evidence_from_dict(config.compat_evidence), config.runtime_target())`：
- 证据有效 → 通过 apply 门，执行 `set_model_catalog`（先备份、原子写、保注释/无关配置）；
- 证据缺失/过期/不匹配 → 拒绝，打印安全预览 + "（只读）未写入" + 原因，退出码 1，目标字节不变。

`model_catalog_json` 写入的是**运行时能读到的路径**：WSL 后端写 Linux 路径
（`target.to_runtime_path(merged_catalog)` 经 `wslpath` 转换），不是 `C:\` 路径。

### 16.5 撤销支持

`core/config_editor.py::set_model_catalog` 现返回旧值；`undo_model_catalog` 恢复旧值
（原本不存在则删除字段），保留其他无关配置和注释。冲突检测：若当前值与 apply 时记录的
`appliedValue` 不同，说明被外部修改，拒绝撤销。`AppConfiguration.last_apply` 记录
`targetConfig` / `previousValue` / `appliedValue`，撤销成功后清除。

### 16.6 CLI 新增

- `probe --verify`：执行完整兼容性探测，存储证据到应用配置，打印结果。
- `undo`：读取 `last_apply` 记录，恢复上次 apply 前的 `model_catalog_json` 值。
- `apply`：真实模式现在有条件放行（证据有效时），不再恒拒绝。

### 16.7 GUI 新增

- **SettingsDialog**：后端（auto/native/wsl）、发行版（自动发现）、运行时路径、CODEX_HOME、
  配置路径，全部可浏览选择。
- "验证兼容性"按钮：执行 `probe_compatibility`，存储证据，显示结果对话框。
- "撤销 apply"按钮：执行 `undo_model_catalog`，恢复上次 apply。
- 状态栏：显示证据状态——"兼容性已验证" / "证据过期/不匹配，需重新验证" / "未验证兼容性"。

### 16.8 验证命令与结果

```powershell
cd outputs\opl-codex-models-windows
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest tests\ -q -rs
```
**结果：76 passed, 1 skipped in 27.75s**（原 62 + 本轮新增 14 项 `test_compat_probe.py`）。
跳过项仍为 `test_apply_demo_symlink_escape_refused`（Windows 无符号链接权限）。

### 16.9 integration-evidence/

| 文件 | 说明 |
|---|---|
| `wsl-direct-evidence.json` | 审查者直接在 WSL 内执行的脱敏证据（正反对照通过）。 |
| `probe_wsl_compat.py` | 可重跑：Windows→wsl.exe→Ubuntu→codex 全链路兼容性探测。 |
| `integration_flow.py` | 可重跑：probe→apply→read→undo 完整流程（隔离 temp CODEX_HOME）。 |
| `README.md` | 证据字段说明、重跑方法、安全约束。 |

`probe_wsl_compat.py` / `integration_flow.py` 使用与 CLI/GUI 相同的适配器和探测函数，
交付为可重跑脚本供验收人在具备 wsl.exe 的环境执行。

### 16.10 配置和路径空间解释

- **draft（草稿）**：编辑/同步用的工作目录（应用沙箱内）。
- **applied**：显式应用生成的版本化快照；`model_catalog_json` 指向运行时能读到的路径。
- **target config**：明确选择的 Codex 配置（`codexConfigPath` / `--codex-config`）。
- WSL 后端：`model_catalog_json` 写 Linux 路径（如 `/mnt/c/.../models.json`），
  Windows GUI 通过对应 Windows 路径管理相同文件，不混用路径空间。

### 16.11 分别列出：mock 单测 / 真实 WSL / Windows 原生 / GUI

- **mock 单测（Windows 真跑，76 passed + 1 skipped）**：全部基于 mock Codex
  （`demo/mock/codex.py` + `codex.cmd`），包括新增 14 项 `test_compat_probe.py`
  覆盖 probe/evidence/CLI verify/apply/undo。
- **真实 WSL 集成**：审查者直接在 WSL 内执行的正反对照通过
  （`wsl-direct-evidence.json`）；Windows→wsl.exe 全链路脚本交付为可重跑
  （`probe_wsl_compat.py` / `integration_flow.py`），**未在本会话中运行**（需 wsl.exe +
  真实 Codex 运行时），不称已通过。
- **Windows 原生集成**：本机已检查位置无原生 Codex，**未验证**。
- **GUI 自动/人工检查**：`gui_smoke.py` 需显示环境（非无头）；无显示下仅验证 import 安全
  + 离线路径。SettingsDialog / 验证兼容性 / 撤销按钮的真实点击**未自动化**，需桌面会话人工复核。

### 16.12 限制（如实保留）

- Windows→wsl.exe 全链路探测脚本未在本会话运行（需 wsl.exe + 真实运行时），交付为可重跑脚本。
- 真实 Windows GUI 窗口交互（SettingsDialog、验证兼容性、撤销按钮点击）未自动化。
- 本机已检查位置无 Windows 原生 Codex，原生集成未验证。
- Windows junction 越界仍未经实测（Linux/WSL 符号链接越界已由验收方独立验证）。
- 不安装后台任务或打包安装器；不修改用户真实 `~/.codex/config.toml` / `auth.json`。

---

## 17. 第七轮：本地桥（默认关闭的开关）+ 跨轮次工具语义 —— 与两处新发现的修正

### 17.1 本轮目标

1. 把本地桥做成**默认关闭、可显式启用/关闭的选项**；
2. 在中转尚未兼容 `type=custom` 时，**尝试支持 DeepSeek 的工具调用**；
3. 观察代理（`work/observe-proxy`）只负责取证，**不代表协议桥已完成**；
4. 不修改真实 Codex 配置或认证；不把「模型目录可读取」等同于「工具调用已兼容」。

### 17.2 核对到的真实状态（不依据交付总结）

上一轮已存在但**文档零记录**的部分：`core/bridge.py`、`cli.py bridge enable/disable/start/status`、
`config_editor.set_provider_base_url/undo_provider_base_url`、`tests/test_bridge.py`（5 项）。
`grep "本地桥|bridge|Bridge"` 在 README/HANDOFF 中 **0 命中** —— 文档缺口属实，本轮补齐。

**读代码发现并修复的功能缺口（不是文档问题）：**

| # | 缺口 | 后果 | 修复 |
|---|---|---|---|
| 1 | 只处理 `input[].additional_tools`，**顶层 `tools[]` 未转换** | `use_responses_lite=false` 时 `exec` 仍是 `type=custom`，中转照旧报 `Unsupported custom tool: 'exec'` | 新增 `convert_top_level_tools()`，与 `additional_tools` 合并去重 |
| 2 | `custom_names` 只在**单次请求**内解析 | 第二轮请求不带 `additional_tools` 时，`function_call(name=exec)` **不会**被转回 `custom_tool_call`，Codex 拿到陌生调用 | `translate_request(payload, known_names)` + `BridgeServer` 跨轮次记忆；历史 `custom_tool_call` 无条件降级 |
| 3 | loopback 校验只在 `cmd_bridge_enable` 里，`run_bridge` 自身不校验 | 绕过 CLI 直接调用即可对外监听 | 校验移入 `ensure_loopback()`，在 `BridgeServer.__post_init__` 与 `run_bridge` 双重强制 |
| 4 | `functions__exec` 无显式拒绝，只是顺其自然漏过 | 命名空间名可能被当成可执行调用交给 Codex | `_transform_response` 识别 `__` 前缀 → **不转换**，收集到 `warnings` 并在 stderr 报告 |

**另外自查发现并修掉的两处：**

| # | 问题 | 说明 |
|---|---|---|
| 5 | `BridgeServer.stop()` 是死代码 | `self._server` 从未赋值，`stop()` 永远空转；补上赋值 + `_ready` 事件 + `bound_port`，E2E 才有可靠启停 |
| 6 | 空 `tools: []` 被原样透传 | 严格中转会对空数组报错；改为无工具时**删除该键** |
| 7 | CLI 的 loopback 校验是第二份硬编码副本 | 改为复用 `ensure_loopback()`，避免两处漂移 |
| 8 | `bridge start --host 0.0.0.0` 先打印「运行中」再报错 | 校验提到打印之前（实测确认过这个误导顺序） |

### 17.3 安全约束（均由测试锁定）

- **loopback 强制**在数据模型层（`BridgeServer.__post_init__`），CLI/GUI/脚本/直接构造对象都绕不过。
- **不保存凭据**：只透传 Codex 已有的 `Authorization` 头。
- **demo 沙箱一致性**：新增 `safe_preview.demo_write_block_reason()`，让桥开关与 `apply` 共用同一条
  demo 越界规则（此前桥开关**绕过**了沙箱检查，是本轮自查发现的一致性缺口）。
- 上游错误**原样透传**，不伪造成功；命名空间名**不转换、不执行**。

### 17.4 最小端到端验证方案与实际结果

**L1（本轮已跑，可重跑，零外部依赖、不碰真实配置）**

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
.\.venv\Scripts\python.exe scripts\bridge_l1_e2e.py
```

内置假中转，断言 17 项，**全部 PASS**：`additional_tools` 被剥离、`exec` 降为 `function` 且名字不变、
上游 `stream=false`、SSE 事件序列（`created` → `output_item.added`×N → `completed`）、
返回 `custom_tool_call` 且 `call_id` 保留、**第二轮只带历史**时降级为
`function_call`/`function_call_output` 且 `call_id` 字节级配对、不凭空补 `tools[]`。

**L2（未执行，需人工，必须可回滚）**：`Codex → observe-proxy(8787) → bridge → 中转`，
让观察代理同时拍到「Codex 原样发出的工具」与「桥转换后的响应」。
本轮**没有**打真实中转，因此**不能声称 DeepSeek 侧工具调用已兼容**。

### 17.5 CLI / GUI 开关实测

CLI 沙箱冒烟（临时 config.toml + 临时 app 配置，**未触碰真实配置**）实测走通：

```
status → 关闭（默认）
enable → 已启用，model_providers.DeepSeek.base_url: https://relay.example/v1 -> http://127.0.0.1:8787
         （注释保留、生成 .bak 备份）
status → 已启用
disable → 已恢复，config.toml 逐字节还原为原文
enable --host 0.0.0.0 → 拒绝（EXIT=1，错误信息在「运行中」之前）
```

GUI 新增「**启用本地桥…**」/「**关闭本地桥**」按钮，均先弹出 `旧地址 -> 新地址` 差异再写，取消则不写任何内容。
`scripts/gui_smoke.py --bridge` 在真实 tkinter 窗口下驱动这两个方法完成往返，**PASS**。

### 17.6 两处新发现的修正（既存问题，被 skip 掩盖）

1. **`test_apply_demo_symlink_escape_refused` 一直是被 skip 的，不是通过的。**
   全面执行时本机 `os.symlink()` **返回成功但什么都没创建**（沙箱静默拦截重解析点），
   于是守卫（只捕获异常）没触发，测试断言了一个**根本不存在的逃逸**而失败。
   → 守卫改为校验 `os.path.islink(link)`，未真正创建则**如实 skip**，
   既不削弱断言，也不谎称已拒绝逃逸。

2. **Windows junction 越界：从未实测 → 本轮实测通过。**
   junction 不需要特权，可真实创建。实测 `os.path.realpath` **能正确解析 junction**
   （`_canonical(junc)` → 沙箱外真实目录），`_is_within` 判定为越界，`apply` 被拒绝且目标文件未变。
   → 新增 `test_apply_demo_junction_escape_refused` 固化为**可运行的**回归用例，
   关闭第 16.12 节的遗留项。

> 注意口径：这**不**等于「Windows 符号链接越界已验证」。本机沙箱无法创建符号链接，
> 该用例仍为 skip，如实保留。

### 17.7 验证命令与结果（本机真跑）

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m pytest tests -q -rs
```

**结果：106 passed, 1 skipped in 66.99s**（收集 107 项）。
跳过项：`test_apply_demo_symlink_escape_refused` —— *os.symlink() did not actually create a link in this environment*。

按文件实测收集数（本轮结束时）：

| 文件 | 用例数 |
|---|---|
| `test_bridge.py` | **29**（本轮开始时为 **5**，+24） |
| `test_review_fixes.py` | 39（本轮新增 1 项 junction 用例） |
| `test_compat_probe.py` | 14 |
| `test_catalog_sync.py` | 7 |
| `test_custom_models.py` | 6 |
| `test_runtime.py` | 5 |
| `test_backup.py` | 4 |
| `test_config_editor.py` | 3 |
| **合计** | **107**（106 passed + 1 skipped） |

直接可核对的变化：本轮开始时 `pytest tests --ignore=tests/test_review_fixes.py` 为 **44 passed**，
结束时为 **68 passed**，差值 **+24**，与 `test_bridge.py` 的 5 → 29 完全一致。

> 口径说明：第 16.8 节记录的上一轮总数是 **76 passed, 1 skipped**，与本轮 107 相差 31；
> 但逐文件核对后无法把这 31 项全部归因于本轮（本轮可确认贡献为 +24 桥用例、+1 junction 用例）。
> 第 16.8 节那个数字与当前逐文件汇总对不齐，**以本节实测为准**，不把差额算作本轮的增量。

### 17.8 报告口径：真跑 / 替身 / 未验证

- **真跑**：`test_bridge.py`（29）、全量 106 passed；L1 假中转端到端；CLI 开关沙箱冒烟；
  CLI/GUI 两侧的 loopback 拒绝与 demo 越界拒绝；GUI 窗口下开关往返（`gui_smoke --bridge`）；
  Windows junction 越界拒绝。
- **替身**：L1 的「上游」是内置假中转（明确标注），用来固定 **wire 形状**，不代表真实中转行为。
- **未验证**：
  - **真实 DeepSeek 中转的工具调用兼容性** —— 需 L2 取证；
  - Windows 符号链接越界（沙箱无法创建 symlink）；
  - 真实 Codex 客户端端到端（未运行真实 Codex，未改真实 `config.toml`/`auth.json`）。

### 17.9 本轮文件变更清单

| 文件 | 变更 |
|---|---|
| `src/codex_model_manager/core/bridge.py` | 顶层 `tools[]` 转换、跨轮次记忆、loopback 强制、命名空间名拒绝、空 `tools[]` 删除、`stop()/_ready/bound_port` 修复 |
| `src/codex_model_manager/core/config_editor.py` | 新增共享 `read_provider_base_url()`（CLI/GUI 不再各写一份） |
| `src/codex_model_manager/core/safe_preview.py` | 新增 `demo_write_block_reason()`，桥开关与 apply 共用 demo 沙箱规则 |
| `src/codex_model_manager/cli.py` | `bridge start` 校验先于宣告；复用 `ensure_loopback`/`read_provider_base_url`；`bridge enable` 加 demo 沙箱闸门；移除已无用的 `tomllib` 导入 |
| `src/codex_model_manager/gui/app.py` | 新增「启用本地桥…」/「关闭本地桥」按钮与实现（差异确认 + demo 沙箱闸门） |
| `tests/test_bridge.py` | 5 → **29** 项：wire 形状、跨轮次、loopback、命名空间、空 tools、L1 E2E ×3、CLI 开关、GUI 开关、共享解析、demo 越界 |
| `tests/test_review_fixes.py` | symlink 用例守卫改为校验链接真实创建；新增 junction 越界用例 |
| `scripts/bridge_l1_e2e.py` | **新增**：L1 端到端脚本（假中转默认；`--upstream/--api-key` 走 L2） |
| `scripts/gui_smoke.py` | 新增 `--bridge`：真实 tkinter 下驱动桥开关往返 |
| `README.md` | 新增「本地桥（可选，默认关闭）」章节；目录结构、验证情况、下一轮同步更新 |
| `HANDOFF.md` | 本第 17 节 |

### 17.10 交付说明（第七轮）

本地桥现在是**默认关闭、可启停的开关**，CLI 与 GUI 都可操作，且与 `apply` 共用同一套 demo 沙箱
与「先看差异再写、写前备份、可逐字节还原」的约束。桥自身对 `custom`↔`function` 的转换已由
L1 端到端和 29 项单测取证，**包括此前完全没覆盖的顶层 `tools[]` 路径与第二轮历史降级**。

需要明确的是：**L1 全绿只说明桥的转换正确，不等于 DeepSeek 中转已兼容工具调用。**
真实兼容性仍需 L2 对着真实中转取证——这一步本轮未做，也未改动任何真实 Codex 配置或认证。

---

## 18. 第八轮：L2 真实中转联调 + 真实 Codex 隔离接入

本轮把第 17 节留下的最后两项「未验证」补上：**真实 DeepSeek 中转的工具调用兼容性**，
以及**真实 Codex 客户端的端到端接入**。两项均为真跑，证据见 `integration-evidence/`。

### 18.1 两项前置核对（按本轮要求）

**（一）工具名称转换必须基于明确的注册映射。** 桥不再持有任何「全局名称集合」——
一个可变的 `custom_names` 集合在语义上无法区分「本请求声明的」与「别处见过的」，
正是本轮要求排除的做法。改为：

- 每次请求先产出显式注册表 `ToolDeclarations.lookup`：把「上游可能返回的拼写」映射到一条
  `ToolRegistration`（记录其命名空间路径、发往上游的叶名、Codex 声明的原名、是 `custom`
  还是普通 `function`）；
- `functions__exec` 是**命名空间拼名的合法产物**：`functions.exec` 是 Codex 的内部记法，
  `exec` 才是可调用名。桥把 `custom` 工具扁平化送出后，模型按它看到的命名空间限定名回报，
  所以这条拼写**应当被准确还原**为 `custom_tool_call(name="exec")`，而不是一律拒绝；
- **无映射**（`undeclared_in_this_conversation`）与**歧义**（同一拼写被两条不同注册认领，
  `ambiguous_registration`）一律**拒绝转换并记账**，绝不做字符串猜测或静默替换。

**（二）多轮映射隔离到请求/会话。** `ToolRegistry` 的键来自 `conversation_key()`：
`previous_response_id` 优先，其次客户端会话字段（`conversation_id`/`session_id`/`prompt_cache_key`
及 `client_metadata` 内同名项），都没有则**只用本请求自身的声明**——不存在跨会话兜底。
条目有 LRU 上限（512）与空闲 TTL（3600s）。历史项（`custom_tool_call`）会被降级为
`function_call`，但**不写入注册表**：历史是「发生过什么」的记录，不是当前轮次的工具授权。

关于 `previous_response_id`：抓取实证显示 **该 Codex 全程不发这个字段**（见 18.2），
因此续接路径只是有界的兜底，映射的权威来源始终是本轮声明。失效方式是显式的：
过期即删除，命中不了就退化为「无映射 → 拒绝并记账」。

隔离性有专门测试：**两个并发会话使用同名但不同类型的工具**（会话 A 把 `exec` 声明为
`custom`，会话 B 声明为普通 `function`），断言 A 的调用被还原为 `custom_tool_call`
而 B 的保持 `function_call`，互不污染。

### 18.2 真实请求/响应基线（抓取，非推测）

用真实 Codex 0.155.0-alpha.9.2（WSL）对着观察代理抓了两组基线（脚本：
`work/l2/capture_real_request.sh`、`work/l2/capture_tools_shape.sh`）：

| 观测项 | 实测结果 |
|---|---|
| 工具声明位置 | **每一轮**都在 `input[]` 的 `additional_tools` 项里；顶层 `tools` 恒为 **absent** |
| `previous_response_id` | **全程为 false** —— 该 Codex 不做响应链式关联 |
| 命名空间 | `functions` / `clock` / `collaboration` 三个 |
| 上游回报的调用名 | `function_call(name="functions__exec")` |
| Codex 当时的反应 | `unsupported call: functions__exec` |

最后一行正是第 17 节遗留的真实故障：模型按命名空间限定名回报，而 Codex 只认 `exec`。

### 18.3 认证通道

沿用既有已授权机制：WSL Secret Service 里 Codex 自己写的那条条目
（`service="Codex Auth"`，`username="cli|"+sha256(CODEX_HOME)[:16]`，内容为
`{"auth_mode":"apikey","OPENAI_API_KEY":...}`）。取值只在内存中，绝不落到
argv、提示词或日志：

- 新增 `scripts/l2_credentials.py`，只提供 `env` / `secret-tool` / `stdin` 三个来源，
  **刻意没有**「把 Key 当参数传」的选项，另提供 `redact()`/`describe()`（只报长度类别）；
- 第 17 节 `bridge_l1_e2e.py` 里原有的 `--api-key` 明文入口**已移除**，改用 `--credential`；
- Codex 侧用进程级配置覆盖：隔离 `CODEX_HOME` 的 provider 表写 `env_key = "L2_CODEX_KEY"`，
  值由 `secret-tool` 经管道直接注入子进程环境，不经 argv、不落盘；
- **未关闭 TLS 校验**，全程 `urllib`/`ssl` 默认。

过程中出现一次环境障碍并已解决：Secret Service 集合一度处于锁定态（`secret-tool` 返回
rc=1、0 字节），重试后即恢复；`l2_credentials.py` 对该情形有明确报错文案。

**安全事件（如实记录）**：诊断期间我执行了 `secret-tool search --all service "Codex Auth"`，
该命令会把条目内容**明文打印**，导致这把 `OPENAI_API_KEY` 出现在本次会话记录中。
这是我的操作失误——诊断应只用 `lookup` 并把结果留在内存。仓库内已扫描确认无任何
`sk-` 长串残留（`grep -rIl 'sk-[A-Za-z0-9]{20,}'` 为空），但**建议轮换这把 Key**。

### 18.4 L2 真实中转协议闭环（真跑，通过）

`scripts/bridge_l2_probe.py` 经本地桥对真实中转 `gflabtoken.cn` 发请求，
模型 `deepseek-v4.1-flash`，主档位 `low`，`high` 另测；白名单工具只有一个
无副作用的 `probe_echo`（返回固定字符串），**测试程序只执行这个白名单工具，
不执行模型生成的任意命令**。

三条场景全部 `closed_loop = true`（证据：`integration-evidence/l2-protocol-loop.json`）：

| 场景 | 声明路径 | 档位 | 结果 |
|---|---|---|---|
| 1 | `additional_tools` | low | 闭环 |
| 2 | `top_level_tools` | low | 闭环 |
| 3 | `additional_tools` | high | 闭环 |

每条的完整链路：模型返回结构化工具调用 → 桥还原类型/名称/参数/`call_id`
→ 测试程序返回工具结果 → 模型下一轮据结果作答（原文含 `PROBE_ECHO_OK:PING-L2`）。
桥的脱敏记录显示 **`call_ref` 在上游与转换后完全一致**，即 `call_id` 字节级保留。

### 18.5 真实 Codex 隔离接入验收（真跑，四轮全过）

`work/l2/codex_isolated_acceptance.sh`：隔离 `CODEX_HOME=/tmp/l2codex/home`
（每次重建），真实全局 `~/.codex/config.toml` 既不读入也不写出，脚本首尾各做一次 sha256 对照。
专用测试目录 `/tmp/l2codex/work`，只操作它。

| 轮次 | 动作 | Codex 退出码 | 桥看到的工具 | 结果 |
|---|---|---|---|---|
| 1 | 列出专用测试目录 | 0 | `exec` | 真实执行 `ls -la`，exit_code 0 |
| 2 | 写入 `hello` | 0 | `exec` | 落盘 `probe_hello.txt`，5 字节，`od -c` 确认无尾随换行 |
| 3 | 下一轮读取 | 0 | `exec` | 读回 **`hello`** |
| 4 | 改为 `world` 并读取 | 0 | `exec` | 读回 **`world`**，最终文件 `world` / 5 字节 |

桥共处理 8 个请求（4 轮 × 2：工具调用 + 结果回传），**全部 `diagnostics=[]`**，
全部 `declared_sources=['additional_tools']`。
真实 `config.toml` sha256 前后一致（`1a9e46e6…`），`auth.json` 本机不存在。
临时桥进程已停止、端口已释放。

**路径覆盖说明（按要求明确）**：真实 Codex 的这 4 轮**全部走 `additional_tools` 路径**，
顶层 `tools[]` 路径**未被真实 Codex 客户端触发**——实测该 Codex 在所有轮次都不发顶层 `tools`。
顶层 `tools[]` 的覆盖来自 18.4 的场景 2（真实中转）与 L1 端到端（假中转），
不能把 18.5 的成功当作顶层路径已被真实客户端验证。

### 18.6 本轮发现并修掉的真实缺陷：SSE 帧格式

第一次接入时出现一个**只会在真实客户端暴露**的故障：桥把流式请求上游的非流式响应重新
包装成 SSE 后，Codex 的回合以 `turn.completed` 结束却**一个 output item 都没有**——
既没有工具调用项，也没有助手消息；模型明明返回了 `function_call(exec)`，桥也确实转换成了
`custom_tool_call`，但 Codex 全部丢弃，因此既不执行工具也不发第二轮。

根因在客户端的事件表里可以直接证实：

```
$ grep -a -o "response.output_item.added" codex   → 0     # 这个字面量根本不存在
$ grep -a -o "response.output_item.done"  codex   → 1
$ grep -a -o "response.custom_tool_call_input.delta" codex → 1
```

**这个 Codex 客户端根本不处理 `response.output_item.added`**，它从
`response.output_item.done` 组装 output item。而桥此前只发 `added`，于是整条流被消费后
静默丢弃。修法（`_sse_response`）：

```
response.created      (response 为 in_progress、output 为空)
response.in_progress
response.output_item.added  ┐ 每个 item 都
response.output_item.done   ┘ 先宣告后完成
response.completed    (response 携带完整 output)
```

每个事件都带单调递增的 `sequence_number`。修完后 18.5 的四轮全部通过。

这条缺陷此前无法被任何离线测试发现：L1 的桩客户端是宽松的（只断言最后一个事件），
所以它对「客户端严格解析帧」这件事**零覆盖**。已加回归断言锁住
`response.output_item.done` 存在且 `sequence_number` 连续。

### 18.7 本轮新增的取证工具（都可重跑）

| 工具 | 作用 |
|---|---|
| `scripts/bridge_l2_probe.py` | L2 协议闭环探针；`--fake` 为离线自检，白名单单工具 |
| `scripts/bridge_l2_serve.py` | 前台常驻桥，供真实 Codex 指向；支持 `--record`/`--capture` |
| `scripts/bridge_replay.py` | **离线回放**：把捕获的上游响应体经桥自身的 `_sse_response` 重放，让真实客户端在**零模型调用**下验证帧格式 |
| `bridge.py --capture` | 保存每个请求的原始上游响应体与**实际发给客户端的 SSE 字节** |
| 证据里的 `declared_sources` | 记录该轮走的是 `additional_tools` 还是顶层 `tools[]` |

`bridge_replay.py` 是这轮最省成本的一步：帧格式迭代不再需要真实模型调用。

### 18.8 报告（按本轮要求的五项）

| 项 | 结果 |
|---|---|
| 请求中声明了哪些工具 | 真实 Codex：`exec`（`custom`，命名空间 `functions`），声明位置 `additional_tools`；探针：`probe_echo`（合成，无副作用） |
| 上游返回的调用类型/名称 | `function_call` / `exec`（探针白名单场景）；抓取基线中另见 `functions__exec` |
| 桥转换后的类型/名称 | `custom_tool_call` / `exec`，`call_ref` 与上游一致，`diagnostics=[]` |
| 客户端是否执行成功 | 是。Codex 真实执行 `/bin/bash -lc 'ls -la'` 等，exit_code 0 |
| 工具结果能否完成下一轮 | 能。工具结果回传后模型据结果作答；四轮中 `hello → 读回 hello → 改为 world → 读回 world` 全部成立 |

### 18.9 未验证 / 边界（不含糊）

- **顶层 `tools[]` 未经真实 Codex 客户端验证**（该 Codex 恒用 `additional_tools`）；
- **`high` 档位**只在协议探针里测过，未跑 Codex 验收；
- 真实集成走 **WSL** 后端；本机仍无 Windows 原生 Codex（唯一二进制是 ELF）；
- L2 只用了**一个**合成白名单工具，未验证 Codex 真实工具集（`clock`/`collaboration`）的转换；
- `agent-mail` 等无关连接器未参与。

### 18.10 本轮文件变更清单

| 文件 | 变更 |
|---|---|
| `src/codex_model_manager/core/bridge.py` | 注册表/会话隔离重构（`ToolRegistration`/`ToolDeclarations`/`ToolRegistry`）；`_sse_response` 帧格式修复；`_sse_event_names`；`capture_dir` 与 `capture()`；证据新增 `declared_sources`/`client_events` |
| `tests/test_bridge.py` | 29 → **41** 项：SSE `output_item.done` 与 `sequence_number` 回归、注册映射/歧义拒绝、两会话同名不同类型隔离 |
| `scripts/l2_credentials.py` | **新增**：内存凭据通道（`env`/`secret-tool`/`stdin`，无参数传入） |
| `scripts/bridge_l2_probe.py` | **新增**：L2 协议闭环探针 |
| `scripts/bridge_l2_serve.py` | **新增**：常驻桥（供真实 Codex 指向，支持 `--capture`） |
| `scripts/bridge_replay.py` | **新增**：离线回放帧格式，零模型调用 |
| `scripts/bridge_l1_e2e.py` | 移除 `--api-key` 明文入口，改用 `--credential` |
| `integration-evidence/l2-protocol-loop.json` | **新增**：协议闭环证据 |
| `integration-evidence/l2-codex-acceptance.json` | **新增**：Codex 隔离接入证据（脱敏） |
| `work/l2/*.sh`、`work/l2/build_evidence.py` | **新增**：抓取基线与验收脚本、证据抽取脚本 |
| `README.md` / `HANDOFF.md` | 本节及验证情况同步 |

**收尾**：临时桥与回放服务均已停止，端口释放；代码与脱敏证据保留。

---

## 19. 第九轮：桌面试用准备 + 模型范围化 + 凭据口径修正

### 19.1 本轮任务与边界

用户给出五条：修正凭据文档口径、核对 `--capture`、准备桌面启用方案（先核实 GPT 与 DeepSeek
是否共用 provider）、用**已轮换的凭据**做桌面试用、以及"客户端自身执行失败时不得掩盖"。

**本轮硬边界（用户明示）**：掉工具的客户端由另一个会话处理，**本轮不修改其运行时或启动环境**。
据此，本轮**没有**改真实 `~/.codex/config.toml`、没有改桌面客户端的启动参数、**也没有驱动 GUI**；
桌面端的启用方案以操作手册形式交付（`desktop-enable.md`），试用走同一 Codex 运行时的隔离接入。

### 19.2 凭据口径修正：`secret-tool lookup` 会把它写到 stdout

原文档只说"只把结果读回内存"，没有点明**它写的是 stdout**。这是本轮要修的核心事实：

- `secret-tool lookup` 不做专用句柄，行为等同于 `cat`：秘密**打印到自己的 stdout**；
- 因此那条管道必须**由程序捕获**（`subprocess.run(..., capture_output=True)`），
  **不得**交给终端、日志、取证文件，**尤其不得进异常正文**——"把子进程输出贴出来看看"
  正是上一轮密钥泄漏进会话记录的方式；
- 错误分支现在明确不引用 `result.stdout` / `result.stderr`（代码注释里写明了这一条，
  防止后来者"顺手加上"）；
- 新增可 grep 的标记 `SECRET_TOOL_STDOUT_CARRIES_SECRET = True` 与 `describe()`／`redact()` 的用法约束；
- **已暴露的旧 Key 不再读取、不再打印、不再比对**：模块**刻意没有**"跟旧值比一比"的路径。

`scripts/l2_credentials.py` 的模块 docstring 增加了一节 "SECURITY — `secret-tool lookup` writes
the secret to its own stdout"，把上述规则写成可执行口径，并由新测试锁定：

| 用例 | 锁什么 |
|---|---|
| `test_secret_is_captured_in_memory_and_never_printed` | 秘密进内存，**不出现在进程 stdout/stderr** |
| `test_failure_path_reports_exit_code_only` | 子进程失败**且**写了秘密时，异常正文只报退出码，不引用子进程输出 |
| `test_missing_field_names_the_field_not_the_value` | 只报字段名 |
| `test_diagnostic_helpers_never_reveal_the_value` | `describe()` 只回长度类别；`redact()` 能拆掉嵌在大段文本里的值 |
| `test_stdout_capture_rule_is_documented_and_flagged` | 上面这条规则**写在模块里**，不是提交信息里的口口相传 |
| `test_no_script_offers_a_command_line_key_channel` | AST 扫描 `scripts/*.py`，**任何**含 "key" 的命令行参数都算违规 |
| `test_repository_holds_no_secret_shaped_literal` | 全仓库（含测试与证据）扫描 `sk-` 形状长串，必须为空 |

另新增只看不放的探针 `work/l2/credential_probe.py`：在进程内取凭据，只打印
`describe()` 的结果。本轮实测输出 `已载入内存（长度 67，类别 long，未落盘）`——
**只报长度类别，没有读取旧值、没有比对、没有打印**。

### 19.3 `--capture` 核对：默认关闭，内容可能敏感，位置已列明

| 核对项 | 结论 |
|---|---|
| 默认是否关闭 | **是**。`--capture` 是可选参数，缺省 `None`；`BridgeServer.capture_dir` 缺省 `None` 时 `capture()` 直接返回 |
| 常规启动命令是否带它 | **不带**。`bridge start` / `bridge_l2_serve.py --upstream …` 都不含；只有上一轮 L2 验收脚本显式开了它 |
| 是否标注敏感 | 帮助文本、`capture_dir` 字段注释、README、`desktop-enable.md` 四处都已标注"模型输出、可能敏感、只写不打印、不自动删除" |
| 是否会被打印到会话 | **不会**。`capture()` 只写盘；本程序没有任何把捕获内容回显的路径 |
| 是否会自动删除 | **不会**（明确保证） |
| 范围外模型是否被捕获 | **不会**——`--only-model` 之外的请求走直通分支，不调用 `capture()` |

现有取证文件（WSL 路径，只读清点，**未打印内容**）：

| 位置 | 内容 | 体量 |
|---|---|---|
| `/tmp/l2codex/capture/upstream-{1..8}.json` | 上游原始响应体（模型输出） | 552–876 B/份 |
| `/tmp/l2codex/capture/client-{1..8}.sse` | 实际发给客户端的 SSE 字节 | 2.2–3.7 KB/份 |
| `/tmp/l2codex/capture/index.jsonl` | 8 行索引 | 3.6 KB |
| `/tmp/l2codex/bridge.jsonl` | 8 行 `--record` 脱敏元数据 | 24 KB |

只读核对结果：`sk-` 形状长串 **0** 处、`Authorization`/`Bearer` 字面量 **0** 处；
索引仅含模型名、声明来源、调用名/类型、事件名、诊断。

### 19.4 核实：GPT 与 DeepSeek **共用同一个 provider**

| 证据 | 内容 |
|---|---|
| `~/.codex/config.toml` | 仅 `model_providers.OpenAI` → `https://gflabtoken.cn/`；`model_provider = "OpenAI"` 是全局单选 |
| `models-with-deepseek.json` | 10 个模型、每条 **42** 个字段，**无 provider 字段**；`deepseek-v4.1-flash` 与 `gpt-6-astra` 字段集合逐项相同 |
| `codex app-server` v2 schema | 目录 `Model` 类型 19 个字段，无 provider；provider 只出现在线程级 `ThreadStartParams.modelProvider` / `Thread.*` 与托管策略 `ConfigRequirements.modelProvider` |
| `enable-deepseek-menu.py` | 只替换 `model_catalog_json`，provider 一个字节不动 |
| `.codex-global-state.json` | 桌面模型选择器存 `{model, reasoningEffort, serviceTier}`——**没有 provider 维度** |
| 客户端二进制 | `profiles` 已是 legacy：`use --profile <name> with <name>.config.toml instead`；且 `--profile` **只对 CLI 运行时命令生效**（`codex`/`exec`/`review`/`resume`/…），app-server 不在其中 |

**结论：这台客户端不能按模型选择 provider。** 因此**不能**声称"开关只影响 DeepSeek"。
正确的说法是：**只有 DeepSeek 的报文会被改写**。

### 19.5 模型范围化（`--only-model`）：把影响面收窄到"报文"

`BridgeServer` 新增 `scoped_models`（`frozenset`）：

- `None`（默认）＝原行为，翻译一切到达的请求；
- 非空集合＝只翻译列出的模型；**其余一律字节级直通**：客户端原始字节原样上行
  （含它自己的 `stream` 标志）、上游响应字节原样回程（流式分块转发、非流式整段转发），
  **不重排 JSON、不剥 `additional_tools`、不写注册表、不写取证**；
- **空集合被拒绝**（`ValueError`）：那只会得到一个"多一跳却不做事的桥"；
- 不认得的模型（字段缺失/拼写不同/未来新增）一律直通——桥不认识的请求绝不该被改写。

本地锁定（`tests/test_bridge.py` 新增 5 项）：上游收到的字节与客户端发出的**逐字节相同**、
回程同样逐字节相同（含 `"stream": true` 与 `additional_tools` 原样）、范围外请求
`mode=passthrough` 且捕获目录为空、范围内请求仍翻译、默认值仍是"翻译一切"。

**两条必须讲清楚的限制**：桥仍然挡在所有模型前面（桥停则范围外模型同样失败）；
范围外模型多一跳环回。这两条写进了帮助文本、README 与 `desktop-enable.md`。

### 19.6 桌面试用（真跑）：隔离接入 + 三个维度分开报

脚本 `work/l2/desktop_trial.sh`。隔离与安全：`CODEX_HOME=/tmp/desktop-trial/home`、
专用目录 `/tmp/desktop-trial/dedicated`；凭据经 `secret-tool` 在内存取得后**只注入子进程
环境**（不进 argv、不落盘、不打印）；真实全局 `config.toml` sha256 前后一致
（`1a9e46e6dd22e80a204976b8755d6384da7f6f145ed1f6e0988b3ccb10468245`，`UNCHANGED_OK`）。

> **这个 sha256 结论只覆盖本轮运行窗口（15:09→15:11）。** 收尾复核时（15:20 前后）
> 该文件已变为 `b2a4573d…`——**不是本轮改的**：并行处理"客户端掉工具"的另一个会话在
> 15:07 与 15:14 各留了一份 `config.toml.bak-observe-20260919-*` 备份，是它在切换/恢复
> 观察代理。已只读核对：当前文件与 15:14 备份的**键集合完全相同**，且
> `model_provider="OpenAI"`、`base_url="https://gflabtoken.cn/"`、`model="gpt-6-astra"`
> 均未改变（仅行数 286 vs 283，属重新序列化的格式差异）。两件事互不覆盖，但**引用时
> 必须带窗口**。

**（一）协议调用**：桥共处理 12 个请求——11 个翻译（全 `status=200`、`diagnostics=[]`）
＋ 1 个直通（`gpt-6-astra`，`mode=passthrough`、`error=None`）。

- 声明路径：**恒为 `additional_tools`**（与上一轮一致，顶层 `tools[]` 仍未由真实客户端触发）；
- 上游返回类型/名称 `function_call`/`exec` → 转换后 `custom_tool_call`/`exec`（`call_id` 保留）；
- SSE 事件序列：`response.created` → `response.in_progress` → 每 item 一对
  `output_item.added`+`output_item.done` → `response.completed`。

**（二）实际执行**：

| 轮 | 档位 | codex exit | 客户端执行的命令数 | 命令退出码 |
|---|---|---|---|---|
| 1 | low | 0 | 1 | 0 |
| 2 | low | 0 | 2 | 0 / 0 |
| 3 | low | 0 | 1 | 0 |
| 4 | low | 0 | 2 | 0 / 0 |
| 5 | **high**（独立会话） | 0 | 2 | 0 / 0 |

五轮全部 `exit 0`，所有命令 `status=completed`、`exit_code=0`。

**（三）后续回合**：第 1–4 轮**同一会话**（`thread_id` 哈希同为 `524276920e85`），
第 5 轮独立会话（`5624a3f716ae`）。落盘内容逐轮推进并逐项吻合：轮 1 目录为空 →
轮 2 写 `hello`（5 字节、无尾换行）→ 轮 3 读回 `hello`（命令输出确实含该值）→
轮 4 改为 `world` 并读回 `world`。即上一轮的工具结果构成了下一轮的输入。

**（四）非目标模型直通对照**（`work/l2/passthrough_probe.py`，`gpt-6-astra`）：
直接打中转 HTTP 200 / `pong`，经桥 HTTP 200 / `pong`，桥记录 `mode=passthrough`，
判定 `passthrough_ok`。字节级不变属本地性质，由捕获式中转用例锁定（见 19.5）。

证据：`integration-evidence/desktop-trial.json`（脱敏；`sk-` 形状长串 0 处、`Bearer` 0 处）。

### 19.7 方案 B 的零模型调用验证：`--profile` 确实给出独立 provider 层

桌面端不能按模型选 provider，但 CLI 可以。该结论用死端探针验证（`work/l2/profile_probe.py`）：
两份配置各指向一个只记录不服务的监听器，跑 `codex exec --profile probe`——

- 请求落在 **profile 层**的监听器（`model: deepseek-v4.1-flash`，path `/responses`）；
- **基础层监听器一次都没有被拨号** → 判定 `profile_layer_wins`。

所以"给 DeepSeek 一份独立配置层、全局 provider 不动"是**已验证**的真隔离路径，
用于 CLI/TUI；桌面端用不上。认证方面**只验证了 `env_key` 这条路径**；
`requires_openai_auth = true` 在 profile 层是否生效**没有验证**（已在手册中标注）。

### 19.8 失败与边界（如实记录）

- **本轮没有出现"客户端自身没有执行入口/创建进程失败"的情况**，五轮全部正常。
  按第 5 条约定，若出现则应保留原始错误并交给正在处理桌面环境的会话，
  **不通过改桥、关闭沙箱或重新安装来掩盖**——本轮不需要用到这条。
- **GUI 桌面端本身没有被驱动**：它由另一个会话处理，本轮不得改其运行时/启动环境，
  且 GUI 没有可从外部安全驱动的入口。本轮跑的是**与桌面端同一个 Codex 运行时**
  与同一请求路径。手册中的桌面点击需用户自行完成。这一点不含糊。
- 顶层 `tools[]` 路径**仍未**由真实客户端触发（该客户端恒用 `additional_tools`）。
- `--only-model` 只验证了"DeepSeek 翻译 + GPT 直通可用"；**没有**验证"开关打开后 GPT 的
  可用性不受桥进程影响"——按设计该命题**不成立**，已写进手册。
- 证据抽取过程中的两处口径错误已修：轮次文件快照一度取到最终态（应按运行日志逐轮取值）、
  `od -c` 的输出被"子串必须连续"的判据误判为不含期望值（应按去空白后的紧凑串比对）。
  修正后逐项吻合。
- 临时服务已停：桥随脚本 `trap` 退出，8787/8798/8799/8801 均未监听。

### 19.9 本轮文件变更清单

| 文件 | 变更 |
|---|---|
| `src/codex_model_manager/core/bridge.py` | 新增 `scoped_models` + `translates()`（空集合拒绝）；`_relay_verbatim()` 直通分支（字节级、不捕获）；`capture_dir` 注释标注敏感与不自动删除；模块 docstring 增 "Model scope" 一节；`run_bridge` 透传范围 |
| `src/codex_model_manager/cli.py` | `bridge start --only-model`（可重复）；启动时打印翻译范围与两条限制 |
| `scripts/bridge_l2_serve.py` | 新增 `--only-model`；`--capture` 帮助文本标注"可能敏感、只写不打印、不自动删除"；启动横幅打印范围/捕获路径 |
| `scripts/l2_credentials.py` | docstring 增 "SECURITY — stdout" 一节；新增 `SECRET_TOOL_STDOUT_CARRIES_SECRET`；错误分支注释明确不引用子进程输出 |
| `tests/test_bridge.py` | 41 → **46**：模型范围化 5 项（逐字节直通、不被捕获、范围内仍翻译、默认不变、空集合拒绝） |
| `tests/test_credentials.py` | **新增** 11 项：凭据通道安全口径 + 命令行传 Key 入口扫描 + 全仓库秘密形状扫描 |
| `desktop-enable.md` | **新增**：桌面启用方案（provider 共用证据、三方案、配置差异、启动/关闭/恢复、验证清单、现有取证文件、实测结果） |
| `work/l2/desktop_trial.sh` | **新增**：桌面试用（范围化桥 + 四轮同会话 + 独立 high + 直通对照） |
| `work/l2/credential_probe.py` | **新增**：只看长度类别的凭据探针 |
| `work/l2/passthrough_probe.py` | **新增**：非目标模型直通对照（含直接/经桥 A-B） |
| `work/l2/profile_probe.py` | **新增**：`--profile` 独立 provider 层的零调用验证 |
| `work/l2/build_desktop_evidence.py` | **新增**：三轮度脱敏证据抽取 |
| `integration-evidence/desktop-trial.json` | **新增**：桌面试用脱敏证据 |
| `README.md` / `HANDOFF.md` | 凭据口径、`--capture`/`--record` 说明、模型范围与"开关只影响 DeepSeek"的更正、本节 |

### 19.10 收尾

测试：`pytest tests -q -rs` → **134 passed, 1 skipped**（跳过项仍是本机沙箱创建不出符号链接）。
`bridge_l1_e2e.py` 全部通过；`bridge_l2_probe.py --fake --path both` 主测试全部闭环。
真实全局 `config.toml` sha256 未被改动；仓库全域无 `sk-` 形状残留；临时桥与探针进程均已停止，
端口释放；代码与脱敏证据保留。

**给用户的处置建议（唯一一条）**：上一轮误把真实 Key 打印进会话记录，本轮已按"不再读取、
不再打印、不再比对"的口径加固。**若尚未轮换，请轮换后重启桌面客户端**；本仓库内没有任何
该 Key 的落盘残留，本轮也未读取它。


## 第 20 节：桥实现复核与 DeepSeek 档位补齐（2026-09-19）

本轮复核了 WorkBuddy 交付的本地 Responses 桥，并补齐了几项会影响持续使用的边界：

- namespace 下的普通 `function_call` 也还原为客户端声明的叶名称（例如 `functions__wait` → `wait`），保留普通 function 类型；
- 同一会话重新声明工具时，以当前请求声明为准，不把上一轮的旧注册混入当前映射；
- `bridge enable` 重复执行变为幂等操作，保留最初的上游地址，避免第二次启用破坏 `disable` 的回退记录；
- 范围外模型的非流式响应保持长度分隔，流式响应增量转发；上游 URL 禁止凭据、查询串和片段，继续只监听 loopback；
- 新增 `scripts/update_reasoning_levels.py`，对显式指定的模型目录只修改 `deepseek-v4.1-flash` 的推理字段，先备份、再原子替换。

DeepSeek V4.1 Flash 的目录档位现在为 `low`、`high`、`max`，默认 `high`。官方 DeepSeek 文档列出了这三个 `reasoning_effort` 值；目录中保留的是菜单声明，实际中转是否按请求应用档位仍需以请求/服务端结果验证。

验证：桥、档位更新与只读诊断测试在 WSL Python 环境通过 `48 passed, 5 deselected`（5 个 GUI 测试因该环境没有 tkinter 而未运行）；Windows 专用完整套件仍需在项目的 Windows 虚拟环境中运行。桌面 DeepSeek 四轮真实试用此前已完成，当前桥默认关闭，桌面启用仍需在同一 WSL 网络侧启动桥。

### 20.1 GPT 工具链只读诊断入口

本轮新增 `core/doctor.py`、CLI `doctor` 子命令和 GUI「诊断工具链」按钮。诊断分开报告：

- app/config.toml 是否可读取，以及脱敏后的 provider/base_url（不读取凭据）；
- bridge 是否关闭、loopback 端口是否可达；
- 显式运行时文件及同目录 `codex-code-mode-host` 文件是否存在（不启动 `--version`）；
- 当前系统 `/proc` 能否观察到 `codex` 与 `code-mode-host` 进程；
- `tool_registry=unobservable`：管理器不能从桌面日志推断当前回合的 `tools`/
  `additional_tools`，必须用新 GPT 回合的真实命令调用确认。
- `doctor --observe-log <obs.jsonl>`：读取观察代理已经脱敏的 JSONL，只汇总成对的
  request/response 工具数量、模型和调用类型。若报告 `request_missing_tools`，可把问题
  锁到“客户端发出请求前没有工具声明”；它仍不区分 app-server、code-mode-host 或桌面壳的
  具体内部触发点。

命令示例：

```powershell
& $py -m codex_model_manager --config <app-config.json> doctor `
  --codex-config <显式 config.toml> --json
```

该入口是只读证据收集，不会自动重启、切换 provider、清理运行时或读取 API Key。观察代理
仍默认关闭；启用和恢复步骤见 `work/observe-proxy/README.md`。
`host_not_observed` 只表示本次进程视图没有看到宿主，不能单独证明 GPT 掉工具的根因。

## 第 21 节：发布前收口（2026-09-19）

- Windows 目标环境使用项目 `.venv` 完整执行 `pytest tests -q -rs`：**143 passed**。
- 测试夹具的 Windows 清理竞态已修复：关闭监听 socket 后，后台 `accept()` 收到
  `WSAENOTSOCK`（WinError 10038）现在按正常 teardown 退出，不再产生未处理线程警告。
  这项改动只影响测试夹具，不改变桥或产品运行路径。
- WSL 环境不能替代 Windows 验收：没有 `tkinter`、Windows `cmd/mklink` 和可执行 `.cmd`
  mock，因此在 WSL 直接运行全套会产生平台预期失败；发布门槛以 Windows `.venv` 结果为准。
- 发布内容增加 `.gitignore`，排除 `.venv`、Python 缓存、pytest 缓存、日志、备份和本地桥捕获。
- `demo/probe/bundled_models_wsl.json` 已替换为只含目录字段的脱敏样例；真实运行时输出中的
  内部提示文本不再随项目发布。
- `work/` 下的桌面联调和取证脚本属于内部工作材料，未复制到发布目录；公开文档已改为引用
  `scripts/` 和 `integration-evidence/` 中实际交付的脚本与脱敏结果。
- 仍需发布者在目标 Git 仓库中完成一次 Windows 全套测试复跑，并检查待提交文件列表；桥默认关闭，
  不会因为安装或导入本项目而修改 Codex provider 配置。


## Windows 图形连接管理（2026-09-20）

新增 bridge_panel、BridgeClient 与 managed_bridge；支持 Windows/WSL 后台服务、原脚本事务接管、模式切换、两模型作用范围、进程身份校验、配置冲突检测及恢复。凭据由 Codex 注入，不读取 keyring。

184 passed, 1 skipped。gui_connection_smoke.py 在真实 tkinter 窗口驱动按钮，临时配置全流程通过。便携 EXE native worker 与打包 WSL worker 只读查询通过。当前真实桥仍只针对 GPT；用户退出 Codex 后选“两者”并重启桥启用 DeepSeek。

入口 scripts/desktop_entry.py；构建 scripts/build_windows.py；源码双击 Start.cmd；说明 QUICKSTART.md。Computer Use 节点仍有 sandboxCwd URI 初始化错误；本轮以应用自身 Tk 按钮测试和截图验收，未宣称 Computer Use 成功。未推送或发布新版本。

## 第 22 节：接管功能收口与委派层退役（2026-09-20）

- 修复 `takeover-apply --active A` 预览 A 却写配置引用 B 的目标错位；导入、预览和实际写回现在绑定同一个目标，换目标必须重新导入。
- `edit-model --catalog` 只允许配置中明确登记的 `takeoverCatalogPath`，演示配置的待应用目录继续受沙箱约束，不能借该参数写任意 JSON。
- 旧版应用配置缺少 `takeoverCatalogPath` 时，在内存中自动派生为管理器合并目录旁的 `pending-models.json`；便携配置也使用自身 `user-data` 目录，不因打开界面改写旧配置。
- 写回活跃目录前先持久化完整撤销记录；若应用配置保存失败，活跃目录字节保持不变。备份、原子替换、外部修改冲突与删除确认语义保留。
- Windows TOML 回归使用 POSIX 形式路径，避免反斜杠被 TOML 当作非法转义。
- 与外部 `dsh` 直接委派重复的 `deepseek-delegation` MCP 已从发布源码、测试和文档移除；全局 MCP 注册通过 Codex CLI 正常删除。退役源码归档在项目外层 `work/archive/deepseek-mcp-retired-20260920.tar.gz`，不进入发布包。
- Windows `.venv` 定向测试：`tests/test_takeover.py` **32 passed**。移除 MCP 前全套为 **236 passed, 1 skipped**；发布范围移除其 6 项测试后，最终门槛为 **230 passed, 1 skipped**（唯一跳过仍为本机无符号链接权限）。
