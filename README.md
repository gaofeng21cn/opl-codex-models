<p align="center">
  <img src="./Resources/AppIcon.png" alt="Codex 模型管理器图标" width="128">
</p>

<h1 align="center">Codex 模型管理器</h1>

<p align="center">在 macOS 上查看、同步和维护 Codex 官方模型与自定义模型。</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-32CD32?style=flat-square&logo=apache&logoColor=white" alt="Apache License 2.0">
  <img src="https://img.shields.io/badge/platform-macOS%2014%2B-6C757D?style=flat-square&logo=apple&logoColor=white" alt="macOS 14 或更高版本">
</p>

## 安装

推荐使用 Homebrew：

```bash
brew tap gaofeng21cn/one-person-lab
brew install --cask opl-codex-model-manager
```

也可以从 [Releases](https://github.com/gaofeng21cn/opl-codex-model-manager/releases/latest) 下载 DMG，打开后将应用拖入“应用程序”文件夹。

## 首次使用

打开应用，点击“使用推荐设置”即可。应用会自动：

- 优先查找 ChatGPT 附带的 Codex，其次检查 Homebrew 或 `/usr/local/bin` 中的 Codex
- 创建自定义模型源和合并模型目录
- 将模型目录写入 `~/.codex/config.toml`
- 安装每 24 小时执行一次、并在 Codex 运行时文件变化时触发的后台同步任务

不需要复制配置文件，也不需要手工填写本机路径。需要沿用现有目录或任务时，可在“设置”中修改所有路径后再应用。

## 可以做什么

- 查看 OpenAI 官方模型、自定义模型和当前合并结果
- 查看最后同步时间、执行结果、Codex 版本和错误日志
- 立即同步官方模型目录
- 从任一现有模型复制能力配置，新增自定义模型
- 在应用内修改模型源、合并目录、Codex 运行时和每日任务

新增模型前，应用会备份自定义模型源；模型目录和 Codex 配置也会在改写前保留备份。

## 模型上下文从哪里来

官方模型的上下文窗口来自当前 Codex 内置目录。自定义模型的标准模型列表通常不提供上下文窗口，因此新增时会先复制所选模板的值，再由用户确认。图像输入能力也按模板复制，可以在保存前调整。

## 工作方式

```text
Codex 内置模型 ─┐
                ├─> 合并模型目录 ─> Codex
自定义模型源 ───┘        ↑
                         └─ 每日同步与应用内立即同步
```

应用内置独立同步组件，不依赖 `jq` 或仓库中的脚本。后台任务和 GUI 使用同一套同步核心，避免出现两套合并规则。

## 高级配置

应用配置保存在：

```text
~/Library/Application Support/CodexModelManager/config.json
```

普通使用不需要直接编辑这个文件。仓库中的 `Config/config.example.json` 只用于排查问题或自动化部署，其中不含个人路径、令牌或密钥。

## 从源码构建

构建、测试、开发安装和发行流程见[开发与发行](docs/development.md)。

## 许可证

本项目采用 [Apache License 2.0](./LICENSE)。
