# 开发与发行

本文面向维护者，负责源码验证和交付流程；用户安装与使用见 [README](../README.md)。
`Package.swift` 持有平台与构建合同，`script/` 持有构建和发行实现。

## 本地验证

需要 macOS 14、Xcode 命令行工具和 Swift 5.10 或更高版本：

```bash
swift test
```

需要安装并启动开发版本时执行：

```bash
./script/build_and_run.sh --verify
```

该命令停止已有开发进程，将临时签名构建写入
`~/Applications/CodexModelManager.app`，启动应用，并确认进程和内置
`CodexModelSync` helper 存在。它不证明每日同步或用户配置已生效。

GUI 和同步 helper 复用 `Sources/CodexModelCore`。排查模型合并与设置问题时，
从共享核心及 `Tests/CodexModelManagerTests` 查起，避免在 GUI 重建另一套规则。

## 发行

`script/release.sh X.Y.Z` 从源码构建 universal App，验证 Developer ID、Team ID
和 hardened runtime，分别公证并装订 App 与 DMG，再生成 SHA-256 文件。
签名与公证凭据通过环境提供，不写入文档、日志或仓库。

产物位于 `dist/`。这个脚本生成并验证产物，不创建 GitHub Release；发布状态以
实际 owner release 和下载字节回读为准。Homebrew Tap 只从该 release 投影 Cask。

## 文档维护

README 只解释当前用户流程，本页只解释维护交付。行为变化时更新对应段落，
命令、默认配置和平台条件对照源码验证；不追加旧版本迁移段落或历史验收清单。
过时说明保留在 Git 历史，新增文档须有独立读者任务并由现有入口链接。
