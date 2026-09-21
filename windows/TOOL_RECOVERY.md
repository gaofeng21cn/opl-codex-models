# GPT 工具恢复：两个独立的实验补丁

这些选项用于尝试恢复工具使用，不能承诺永久修复，也不根据一次成功宣称根因。
桥默认不启用；原有 DeepSeek 使用方式保持兼容。启用补丁须明确指定目标模型。

| 模式 | 协议兼容 A | 上下文纠偏 B |
| --- | --- | --- |
| off | 关闭 | 关闭 |
| protocol | 开启 | 关闭 |
| context | 关闭 | 开启 |
| both | 开启 | 开启 |

A 使用已有 custom → function → custom 转换，包括历史调用、返回结果、命名映射
与 call_id 配对。该路径会向上游请求非流式结果，再转换成客户端 SSE，并在等待时
发送心跳。因此它是一个协议兼容方案；若有效，不能仅据此确定是哪个字段导致问题。

**压缩请求独立透传**：`input` 最后一项为结构化 `compaction_trigger` 时，以及
`/responses/compact`、`/v1/responses/compact` 路由，始终绕过 A/B。保留请求原始字节、
`stream`、原生工具/历史、上游响应字节及 HTTP 错误状态，不重建 SSE、不自动重试。
正常回合携带的旧 `compaction`/`context_compaction` 及用户文本提及“压缩”不会触发旁路。
压缩结束后的普通工具请求继续按当前模式转换。这同样适用于 DeepSeek 桥。
`/healthz` 返回 `compaction_passthrough=true` 才说明运行中的进程已加载这项修复；
仅修改代码不会更新已经运行的 Python 进程。

B 只修改本次请求的副本：移除精确匹配的、仅包含旧“没有工具”诊断的 assistant
消息，并在最新用户消息前加入一条依据当前 exec 声明的说明。
只处理当前请求确实声明且无歧义的 exec；不从历史调用恢复工具权限。
不删除用户要求、工具调用、工具结果、reasoning 或混有工作成果的回复。
它不会改动保存的原任务历史；未匹配的历史原样保留，也不自动压缩整个对话。
存在 previous_response_id 或服务器 conversation 时跳过 B，并在元数据说明原因。

## 启动选项

在现有桥启用/恢复流程下运行。以下命令只启动进程，不修改 Codex 配置；
需要使用既有 bridge enable/disable 或有备份与冲突保护的桌面试验入口。

```bash
# A：只做协议兼容
python scripts/bridge_l2_serve.py --upstream https://YOUR-RELAY \
  --only-model gpt-6-astra --protocol function --record /tmp/tool-trial.jsonl

# B：保留原工具协议，只做上下文纠偏
python scripts/bridge_l2_serve.py --upstream https://YOUR-RELAY \
  --only-model gpt-6-astra --protocol native --context-recovery --record /tmp/tool-trial.jsonl

# AB：同时启用
python scripts/bridge_l2_serve.py --upstream https://YOUR-RELAY \
  --only-model gpt-6-astra --protocol function --context-recovery --record /tmp/tool-trial.jsonl
```

`python -m codex_model_manager --config APP_CONFIG bridge start` 也支持上述三个选项，
以及 `--experiment-mode-file PATH`。模式文件只能是 `{"mode":"off"}`（或其他三种
模式），每个请求开始时读取一次。使用临时文件 + 原子替换修改它；等当前回合完成
再切换。模式文件无效会拒绝请求，不会悄悄切成另一种补丁。补丁按确切模型名生效。

mode=off 对请求及响应原样转发，但仍经过同一个本地服务。
模型范围外原样转发；因为 base_url 属于 provider，停桥前必须先恢复直连。

## 怎么比较结果

建议先测试 0 → A → 0 → B → 0 → AB，每组至少三轮真实的只读命令/文件读取，
使用相同模型和推理档位，记录实际工具调用及复发。一次成功不是持续稳定的证据。

- A 多次有效、B 无效：优先考虑协议兼容路径；还不能定位中转内的具体缺陷。
- B 多次有效、A 无效：支持上下文影响，仍需区分移除旧回复与新增提示各自的作用。
- 只有 AB 有效：可能是组合效果，不强行二选一。
- 都无效：停止叠加更多补丁，保留失败请求元数据。

同一任务连续试验会积累新的调用历史，存在顺序影响；切回 off 不会删除这些新回合。
需要更严格的归因时，应从同一历史快照建立隔离副本，固定其它条件后比较。
此阶段是可用性试验，不能把恢复/复发直接等同于根因已经被证明。

## 记录与验收

不传 --capture。--record 仅记录模式、工具名称/类型/命名空间、工具 schema 哈希、
调用关联哈希、参数长度、B 处理的消息数量、HTTP 状态等，不保存凭据或请求/响应正文。
回复有调用不等于客户端执行成功，需对齐客户端实际执行结果。
不会因模型说“没有工具”而自动重试，更不会自动重放可能产生副作用的命令。

2026-09-20 验证：Windows 完整测试 173 passed、1 skipped（符号链接权限）；
四种模式各在真实 Codex 运行时连续两轮执行 pwd 并回传，8/8 通过。
该运行时验收使用本地合成上游、假凭据及隔离 CODEX_HOME/SQLite，未调用真实模型。
临时配置的服务启动、四种模式热切换、逐字节恢复及停止自有进程也已通过。
旧对话实测：0 模式前两轮实际调用成功、第三轮未调用；A 连续三轮调用成功。
这是短期恢复证据，未证明永久稳定，也未定位上游具体字段的处理错误。

修复前发现 A 下压缩报 502 或缺少 response.completed，切 0 后完成压缩。
旧元数据未记录请求是否为压缩，不能把所有 502 都归为同一原因。
修复后用真实桌面配套 WSL 内核和隔离合成上游验证了：A 手动压缩、AB 手动压缩、
A 达到 token 阈值后的自动压缩；三者均完成“执行 pwd → 压缩事件落盘 → 再执行 pwd”。
合成上游只用来验证协议和客户端行为，不证明真实中转的内部实现。
新增记录 `request_kind`、`bypass_reason`；不记录压缩正文或加密内容。
修复后的真实中转压缩及长期复发仍待桌面复测。

协议依据：[OpenAI custom tools 文档](https://developers.openai.com/api/docs/guides/function-calling#custom-tools)。
