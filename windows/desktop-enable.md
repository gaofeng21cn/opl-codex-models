# 桌面客户端启用本地桥（面向 DeepSeek 的工具调用兼容）

本文件回答一件事：**在这台机器上，怎么让桌面 Codex 用 DeepSeek 时走本地桥，同时
尽量不牵连 GPT**。先说清楚能做到什么、做不到什么，再给可执行的配置差异与回退步骤。

> 本文只描述操作与限制。所有"实测"结论都标注了档位与来源；"未验证"的地方明确写未验证。

---

## 0. 结论先行：这台客户端**不能按模型选择 provider**

四条证据，都是本机实测/实读，不是推测：

| 证据 | 内容 |
|---|---|
| `~/.codex/config.toml` | 只有一个 provider：`model_providers.OpenAI` → `https://gflabtoken.cn/`；`model_provider = "OpenAI"` 是一个**全局单选值** |
| 模型目录 `models-with-deepseek.json` | 10 个模型、每条 42 个字段，**没有任何 provider 字段**；`deepseek-v4.1-flash` 与 `gpt-6-astra` 的字段集合逐项相同（DeepSeek 只是通过 `model_catalog_json` 挂进同一个 provider 的模型菜单） |
| app-server 协议 schema | 目录里的 `Model` 类型 19 个字段，同样没有 provider；provider 只出现在**线程级** `ThreadStartParams.modelProvider` 与托管策略 `ConfigRequirements.modelProvider` |
| 桌面端持久化状态 | 模型选择器存的是 `{model, reasoningEffort, serviceTier}`（`.codex-global-state.json` → `composer-recent-model-configurations-v1`），**没有 provider 维度** |

另外，`[profiles.*]` 在这版客户端里已是 legacy，客户端自己的原话是：

> `profiles` contains legacy config profile tables and can no longer be written;
> use `--profile <name>` with `<name>.config.toml` instead

而 `--profile` 只对 CLI 运行时命令生效（`codex`、`codex exec`、`codex review`、`codex resume` …），
**app-server / 桌面端不在其中**。

**所以：把 provider 的 `base_url` 指向桥，就等于把所有模型都送上桥。**
不能说"这个开关只影响 DeepSeek"——详见下面方案 A 的两条限制。

---

## 1. 三种隔离方案，按隔离强度排序

### 方案 A：范围化桥（桌面端**唯一**可用）

```powershell
bridge enable  --provider OpenAI --upstream https://gflabtoken.cn --port 8787
bridge start   --only-model deepseek-v4.1-flash
```

- 范围内的模型照旧做协议转换（`custom` ↔ `function`、SSE 帧、注册映射、会话隔离都不变）；
- 范围外的模型**请求与响应字节不变**地直通：不剥离 `additional_tools`、不改 `stream`、
  不重新序列化 JSON、不写 `--capture`、不写注册表。它们看到的就是直连中转时会看到的东西；
- 确定性做法是**不认得的模型一律直通**（模型字段缺失、拼写不同、未来新增的 GPT 型号都算范围外），
  因为"桥不认识的请求"绝不该被改写。

**两条必须讲清楚的限制：**

1. **桥仍然挡在所有模型前面。** 桥进程没起来时，范围外的模型（GPT）**一样会失败**。
   收窄的是"报文的改写面"，不是"开关的影响面"。
2. **多一跳环回。** 范围外模型的流量多经一次本地转发；流式为分块转发、非流式为整段转发，
   不改变内容但改变路径。

### 方案 B：CLI 专用配置文件（**真隔离，已离线验证；但只对 CLI 生效**）

全局 provider 一个字节都不动，给 DeepSeek 单独一份配置层：在同一个 `CODEX_HOME` 下建
`<name>.config.toml`，用 `--profile <name>` 选它。

**已验证**（内部零模型调用探针，脱敏结果保存在 `integration-evidence/`）：两份配置各指向一个只记录不服务的
死端监听器，然后跑 `codex exec --profile probe`——

- 请求落在 **profile 层的监听器**上（`model: deepseek-v4.1-flash`，path `/responses`）；
- **基础层监听器一次都没有被拨号**；
- 所以 `--profile` 给出的是一层**独立的 provider 配置**，基础 provider 不受影响。

```
# ~/.codex/deepseek-bridge.config.toml（示例）
model = "deepseek-v4.1-flash"
model_provider = "DeepSeekBridge"
model_reasoning_effort = "low"
model_catalog_json = "……/models-with-deepseek.json"

[model_providers.DeepSeekBridge]
name = "DeepSeekBridge"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
env_key = "DEEPSEEK_RELAY_KEY"        # ← 本探针验证的就是 env_key 这条路径
```

- **限制**：桌面端没有 `--profile` 入口，**桌面会话用不上**。`--profile` 只对 CLI 运行时
  命令生效（`codex`/`exec`/`review`/`resume`/…）。
- 认证：探针验证的是 `env_key`（把环境变量名写在配置里，值由进程环境提供）。
  `requires_openai_auth = true`（复用客户端已有的 keyring 条目）在 profile 层是否同样生效
  **没有验证过**，用之前请自行确认。

### 方案 C：按线程选 provider（需要客户端侧改动，本轮不做）

协议本身支持 `ThreadStartParams.modelProvider`／`Thread.resume|fork` 的同名字段，
托管策略也有 `ConfigRequirements.modelProvider`。缺的是桌面端的入口：模型菜单不传它。
这一条属于客户端侧改动，**不在本轮范围内**，也没有被验证。

---

## 2. 方案 A 的实际配置差异

**只改一个键**：`model_providers.<provider>.base_url`。改动前后：

```diff
 model = "gpt-6-astra"
 model_provider = "OpenAI"
 ...
 [model_providers.OpenAI]
 name = "OpenAI"
-base_url = "https://gflabtoken.cn/"
+base_url = "http://127.0.0.1:8787"
 wire_api = "responses"
 requires_openai_auth = true
```

工具链保证（不是约定，是代码行为）：

- **只动这一个键**，`tomlkit` 编辑、注释保留；
- 写前自动备份到 `%LOCALAPPDATA%\CodexModelManager\Backups`；
- 记录 `previousUrl` / `appliedUrl`，`bridge disable` 据此**逐字节还原**；
- 还原前检测冲突：如果当前值与 `appliedUrl` 不一致（有人手动改过），**拒绝动手并报错**；
- `codexConfigPath` 只由 `--codex-config` 或应用配置显式给出，**绝不从环境变量推导**。

---

## 3. 启动命令（完整序列）

> **WSL 后端注意**：本机桌面 Codex 的 `app-server` 在 WSL2 NAT 网络内运行。
> 因此桥进程也必须在同一 WSL 发行版内监听 `127.0.0.1`；Windows 侧监听的
> loopback 地址不会被 WSL 后端当作同一个地址。管理器命令可在 Windows 环境
> 完成配置写入，但 `bridge start` 请在 Ubuntu/WSL 终端运行。若端口已被
> Windows 系统服务占用，改用空闲端口，并让 `bridge enable --port` 与启动参数一致。

```powershell
# 0) 应用配置尚未创建时先建一份；--codex-config 必须显式给出真实 config.toml
$py  = ".\.venv\Scripts\python.exe"
$cfg = "$env:LOCALAPPDATA\CodexModelManager\config.json"
$real = "$env:USERPROFILE\.codex\config.toml"

# 1) 确认开关默认是关闭的
& $py -m codex_model_manager --config $cfg bridge status

# 2) 只改 base_url 一项（写前自动备份，保留注释）
& $py -m codex_model_manager --config $cfg bridge enable `
      --codex-config $real --provider OpenAI `
      --upstream https://gflabtoken.cn --host 127.0.0.1 --port 8787

```

在 Codex 所在的 WSL 发行版终端运行（Ctrl+C 停止）：

```bash
# 先 cd 到仓库的 windows 目录。
PROJECT_DIR="$PWD"
CONFIG_PATH="/mnt/c/Users/<你的 Windows 用户名>/AppData/Local/CodexModelManager/config.json"
PYTHONPATH="$PROJECT_DIR/src" \
python3 -m codex_model_manager --config "$CONFIG_PATH" \
  bridge start --host 127.0.0.1 --port 8787 --only-model deepseek-v4.1-flash
```

回到桌面客户端，新开一个会话，模型菜单里选 DeepSeek V4.1 Flash。

桥自己**不持有凭据**：它只透传客户端已经带来的 `Authorization` 头。所以第 3 步不需要任何
`--api-key`（全项目也刻意没有这个入口）。

---

## 4. 关闭与恢复

```powershell
# a) 在桥的终端按 Ctrl+C（或直接结束该进程）
# b) 恢复 provider 地址（检测冲突；被外部改过就不动手）
& $py -m codex_model_manager --config $cfg bridge disable

# c) 复核：应当回到 https://gflabtoken.cn/
& $py -m codex_model_manager --config $cfg bridge status
```

若 `bridge disable` 报"冲突"，说明当前 base_url 与当初写入的不一致，**它不会覆盖你的改动**——
先手工确认现场，再决定是手工改回还是重新走一遍 `enable`。

---

## 5. 上机前的验证清单

```powershell
# 桥自身：内置假中转，零外部依赖
& $py .\scripts\bridge_l1_e2e.py

# 协议闭环：合成白名单工具 probe_echo（--fake 为离线自检）
& $py .\scripts\bridge_l2_probe.py --fake --path both

# SSE 帧格式：用真实客户端 + 零模型调用回放
& $py .\scripts\bridge_replay.py --capture <capture_dir> --port 8798
```

换中转或换 Codex 版本后**应重跑**：尤其 `bridge_replay.py`，它能零成本验证帧格式是否仍被客户端接受。

---

## 6. 现有取证文件（内容可能敏感）

`--record` 与 `--capture` **默认都不启用**，常规启动命令（`bridge start`、
`bridge_l2_serve.py --upstream …`）都不带它们。上一轮 L2 联调确实开过 `--capture`，
所以本机现在有这些文件：

| 位置 | 内容 | 体量 |
|---|---|---|
| `/tmp/l2codex/capture/upstream-*.json` | 8 份上游**原始响应体**（模型输出） | 各 552–876 B |
| `/tmp/l2codex/capture/client-*.sse` | 8 份**实际发给客户端的 SSE 字节** | 各 2.2–3.7 KB |
| `/tmp/l2codex/capture/index.jsonl` | 8 行索引（模型名、声明来源、调用名/类型、诊断） | 3.6 KB |
| `/tmp/l2codex/bridge.jsonl` | 8 行 `--record` 脱敏元数据 | 24 KB |

（WSL 路径。捕获目录合计 72 KB、17 个文件。）

已做过的只读核对（**不打印内容**）：

- `sk-` 形状长串：`upstream-*.json` 0 处、`client-*.sse` 0 处；
- `Authorization` / `Bearer` 字面量：0 处；
- 索引里没有提示词、没有工具参数。

本程序**不自动删除**这些文件。它们是模型输出、可能敏感，请按需自行清理；
清理前若还想留着做帧格式回放，`bridge_replay.py --capture <该目录>` 是唯一消费者。

---

## 7. 本轮桌面试用的实测结果

**先划清一件事**：GUI 桌面客户端本身**没有被驱动**。它由另一个会话在处理掉工具的问题，
本轮不允许改动它的运行时与启动环境，而 GUI 也没有可从外部安全驱动的入口。所以下面跑的是
**与桌面端同一个 Codex 运行时**（`codex 0.155.0-alpha.9.2`，WSL）、用**隔离 `CODEX_HOME`**
走同一条请求路径；桌面端的实际点击由用户按第 3–4 节自行完成。这一点不含糊。

隔离与安全：`CODEX_HOME=/tmp/desktop-trial/home`、专用目录
`/tmp/desktop-trial/dedicated`；真实全局 `config.toml` 的 sha256 **在本轮运行窗口
（15:09→15:11）内前后一致**（`1a9e46e6…8245`，`UNCHANGED_OK`）。收尾复核时它已变为
`b2a4573d…`——**不是本轮改的**：并行处理"客户端掉工具"的另一个会话在 15:07/15:14 留了
`config.toml.bak-observe-20260919-*` 备份，是它在切换/恢复观察代理。只读核对：
当前文件与 15:14 备份**键集合相同**，`model_provider="OpenAI"`、
`base_url="https://gflabtoken.cn/"`、`model="gpt-6-astra"` 均未变（仅重新序列化的格式差异）。

凭据经 `secret-tool` 在内存取得后只注入子进程环境，**未打印、未落盘、未与旧值比对**。

### 7.1 协议调用（桥看到的）

桥以 `--only-model deepseek-v4.1-flash` 启动，共处理 **12** 个请求：

| 类别 | 数量 | 结果 |
|---|---|---|
| 翻译（DeepSeek） | 11 | 全部 `status=200`，`diagnostics=[]`（无一次无法还原的调用） |
| 直通（gpt-6-astra） | 1 | `mode=passthrough`，`error=None` |

- 声明路径：**恒为 `additional_tools`**（与上一轮一致，顶层 `tools[]` 仍未由真实客户端触发）；
- 上游返回类型/名称：`function_call` / `exec`；转换后：`custom_tool_call` / `exec`（`call_id` 保留）；
- 发给客户端的 SSE 事件序列：`response.created` → `response.in_progress` →
  `output_item.added` + `output_item.done`（每个 item 一对）→ `response.completed`。

### 7.2 实际执行（客户端真的跑起来了吗）

| 轮 | 档位 | exit | 客户端执行的命令 | 命令退出码 |
|---|---|---|---|---|
| 1 | low | 0 | 1 条（`ls -la`） | 0 |
| 2 | low | 0 | 2 条（写文件 + 校验） | 0 / 0 |
| 3 | low | 0 | 1 条（`od -c probe_hello.txt`） | 0 |
| 4 | low | 0 | 2 条（改写 + 读回） | 0 / 0 |
| 5 | **high**（独立会话） | 0 | 2 条（`ls -la` + `cat`） | 0 / 0 |

**五轮全部 `exit 0`，全部命令 `status=completed`、`exit_code=0`**——工具不只是被协议
还原出来，而是真的被执行了。

### 7.3 后续回合（工具结果有没有被下一轮用上）

- 第 1–4 轮**同一个会话**（`thread_id` 哈希全为 `524276920e85`），第 5 轮是**独立会话**
  （`5624a3f716ae`）——即 high 是单独的测试，不是接在低档位对话后面；
- 落盘内容逐轮推进并与期望逐项吻合：轮 1 目录为空 → 轮 2 写 `hello`（5 字节、无尾换行）
  → 轮 3 读回 `hello`（命令输出里确实含该值）→ 轮 4 改为 `world` 并读回 `world`；
- 也就是说**上一轮的工具结果构成了下一轮的输入**：第 3 轮的读命令输出里含第 2 轮写入的值，
  第 4 轮读到的是自己刚写的值。

### 7.4 非目标模型直通对照（GPT 走桥还能不能用）

内部 A/B 探针用 `gpt-6-astra` 发同一份请求两次（脱敏结果见 `integration-evidence/desktop-trial.json`）：

| 路径 | 状态 | 文本 |
|---|---|---|
| 直接打中转（对照） | HTTP 200 | `pong` |
| 经范围化桥 | HTTP 200 | `pong` |
| 桥的归因记录 | `mode=passthrough` | — |

结论 `passthrough_ok`：**范围外模型经桥仍然可用，且桥确认自己没有碰它**。
字节级不变是本地性质，由 `tests/test_bridge.py` 的捕获式中转用例锁定
（上游收到的字节与客户端发出的逐字节相同，回程同样逐字节相同）。

### 7.5 失败与边界（如实记录）

- **本轮没有出现"客户端自身不执行/创建进程失败"的情况**，五轮全部正常。若出现，
  按第 5 条约定处理：保留原始错误、交给正在处理桌面环境的会话，**不通过改桥、
  关沙箱或重装来掩盖**。
- 顶层 `tools[]` 路径**仍未由真实客户端触发**（该客户端恒用 `additional_tools`）。
- 桌面端本体的点击、以及"开关打开后 GPT **可用性**也受桥进程影响"这一点，属于方案 A 的
  既有性质，未在 GUI 内实测。
- 临时服务已停：桥随脚本退出（`trap` 清理），相关端口（8787/8798/8799/8801）均未监听。

证据：`integration-evidence/desktop-trial.json`（脱敏；0 处 `sk-` 形状长串、0 处 `Bearer`）。
