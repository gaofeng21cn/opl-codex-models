<p align="center">
  <img src="./Resources/AppIcon.png" alt="Codex Models 图标" width="128">
</p>

<h1 align="center">Codex Models</h1>

<p align="center">在 macOS 上查看、同步和维护 Codex 官方模型与自定义模型。</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-32CD32?style=flat-square&logo=apache&logoColor=white" alt="Apache License 2.0">
  <img src="https://img.shields.io/badge/platform-macOS%2014%2B-6C757D?style=flat-square&logo=apple&logoColor=white" alt="macOS 14 或更高版本">
</p>

## 安装

推荐使用 Homebrew：

```bash
brew tap gaofeng21cn/one-person-lab
brew install --cask opl-codex-models
```

也可以从 [Releases](https://github.com/gaofeng21cn/opl-codex-models/releases/latest) 下载 `Codex-Models.dmg`，打开后将应用拖入“应用程序”文件夹。

使用旧名称 `opl-codex-model-manager` 安装的用户，运行 `brew update` 后即可通过新名称升级，Homebrew 会自动迁移安装记录，原有模型和配置继续保留。

## 首次使用

打开应用，点击“使用推荐设置”即可。应用会自动：

- 在本机已发现的 Codex 中自动选择最新版本，包括独立安装和 ChatGPT 附带的版本
- 创建自定义模型源和合并模型目录
- 将模型目录写入 `~/.codex/config.toml`
- 安装每 24 小时执行一次、并在 Codex 运行时文件变化时触发的后台同步任务

不需要复制配置文件，也不需要手工填写本机路径。需要沿用现有目录或任务时，可在“设置”中修改所有路径后再应用。

## 可以做什么

- 查看 OpenAI 官方模型、自定义模型和当前合并结果
- 查看最后同步时间、执行结果、Codex 版本和错误日志
- 立即同步官方模型目录
- 从任一现有模型复制能力配置，新增自定义模型
- 在新增界面或模型详情中配置自定义模型的推理档位与默认档位
- 在应用内修改模型源、合并目录、Codex 运行时和每日任务
- 为任一模型选择“跟随来源”“显示”或“隐藏”，每日同步会保留本机选择
- 为官方模型单独设置当前或最大上下文，其他字段继续随官方更新
- 在“设置 → 实验功能”中开关实验性上下文管理，并查看配置状态与供应商支持条件

新增模型或修改推理配置前，应用会备份自定义模型源；模型目录和 Codex 配置也会在改写前保留备份。

## 配置推理档位

选中自定义模型，在详情中点击“编辑推理档位”，勾选供应商实际支持的档位，并从中选择默认档位，点击“保存并同步”。例如 GLM-5.3-Flash 支持 `low / high / max`，可以保留默认 `high`，按任务需要在 Codex 中切换。

新增模型时会带入模板的推理配置，也可以单独调整。官方模型的推理能力随 Codex 目录同步，详情中只读展示。这里配置的是模型选项，不会检测或改变供应商实际支持的能力；打开中的 Codex 可能需要重启才能刷新菜单。

## 模型上下文从哪里来

官方模型的上下文窗口来自 Codex 官方目录。应用优先使用本机账号在线刷新，这样刚发布、尚未进入 Codex 内置清单的模型也能及时出现；未登录或刷新失败时回退到内置清单。同步日志中的 `official_source` 会记录本次用的是 `refresh` 还是 `bundled`。

自定义模型的标准模型列表通常不提供上下文窗口，因此新增时会先复制所选模板的值，再由用户确认。图像输入能力也按模板复制，可以在保存前调整。

如需试用不同的上下文大小，选中官方模型，在详情中点击“编辑上下文覆盖”。分别勾选要修改的“当前上下文”或“最大上下文”，填写数值后“保存并同步”。例如 384K 填写 `393216`。只修改当前上下文时，最大上下文仍随官方更新；两项都勾选则分别保留本机值。

覆盖配置独立保存在本机，应用和每日任务每次都会先读取最新官方目录，再应用指定字段。模型的图像能力、推理档位和其他官方配置继续更新。取消某个勾选即可恢复该字段跟随官方；点击“全部跟随官方”后保存，即可清除该模型的上下文覆盖。详情会标明哪些上下文字段正在使用本机值。

这里调整的是 Codex 使用的上下文配置，不会增加模型或供应商实际支持的容量。

## 工作方式

```text
Codex 官方模型 ─┐
（在线刷新，    │
 失败回退内置） ├─> 合并模型目录 ─> Codex
自定义模型源 ───┘        ↑
                         └─ 每日同步与应用内立即同步
```

应用内置独立同步组件，不依赖 `jq` 或仓库中的脚本。后台任务和 GUI 使用同一套同步核心，避免出现两套合并规则。

模型详情中的“模型选择器”只覆盖可见性，不会固定或复制官方模型的上下文、图像能力或推理配置。选择“跟随来源”即可取消覆盖。更改后，已打开的 Codex 会话可能需要重新加载配置。

## 实验性上下文管理

在“设置 → 实验功能”中打开开关，会更新本机 `~/.codex/config.toml` 的 `features.context_management.experimental_mode`，并保留其他设置和注释。更改前会自动备份。

“已开启”表示配置已写入，不代表当前会话已经启用该功能。按 [Codex 官方说明](https://learn.chatgpt.com/docs/config-file/config-reference)，这项能力目前需要符合条件的 ChatGPT 登录会话；自定义供应商仍可保存实验开关，但应用会明确显示暂未获得官方支持。应用不自动切换供应商，也不修改底层 token 预算参数。

## 高级配置

应用配置保存在：

```text
~/Library/Application Support/CodexModelManager/config.json
```

普通使用不需要直接编辑这个文件。仓库中的 `Config/config.example.json` 只用于排查问题或自动化部署，其中不含个人路径、令牌或密钥。

`modelOverrides` 按官方模型标识保存字段级覆盖，目前仅支持 `context_window` 和 `max_context_window`，未填写的字段不覆盖。例如只固定当前上下文：

```json
"modelOverrides": {
  "gpt-6-astra": { "context_window": 393216 }
}
```

自定义模型仍以自定义模型源为准，不受此配置影响。暂未出现在官方目录中的模型标识会保留，待以后出现时应用；同步日志中的 `applied_model_overrides` 记录本次生效的字段和值，`inactive_model_overrides` 记录本次未应用的模型标识。无效数值或合并后当前上下文超过最大上下文时，同步会报错并保留原目录。

## 从源码构建

构建、测试、开发安装和发行流程见[开发与发行](docs/development.md)。

## Windows 版本

独立的 Python/tkinter 实现位于 [windows](./windows/README.md)，支持 Windows 原生 Codex 与 WSL 后端。它保留独立的测试和构建入口；macOS 应用继续使用本仓 Swift 实现。Windows 版目前手动同步，协议桥默认关闭，正式便携包与真实环境验收见 Windows 文档。

## 许可证

本项目采用 [Apache License 2.0](./LICENSE)。
