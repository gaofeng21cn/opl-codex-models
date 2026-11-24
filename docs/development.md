# 开发与发行

本文面向维护者，负责源码验证和交付流程；用户安装与使用见 [README](../README.md)。
版本唯一真相由 [`product-version.json`](../product-version.json) 持有，`Package.swift`
持有依赖与目标，[`script/`](../script) 持有本地构建与打包脚本，`.github/workflows/`
持有 CI 与发行流水线。

## 本地验证与开发运行

需要 macOS 14+、Xcode 命令行工具与 Swift 5.10+：

```bash
swift test
./script/verify_sparkle_contract.sh
```

`Package.swift` 通过 SwiftPM 解析 TOMLKit 与 Sparkle 2 依赖；`verify_sparkle_contract.sh`
验证 Sparkle feed URL（`releases/latest/download/appcast.xml`）、`SPUStandardUpdaterController`、
`SUPublicEDKey` 与 `@executable_path/../Frameworks` rpath 契约。GUI 与同步 helper
共用 `Sources/CodexModelCore`；排查模型合并或同步问题优先从共享核心及
`Tests/CodexModelManagerTests` 定位。

本地安装并启动开发版：

```bash
./script/build_and_run.sh --verify
```

该命令停止旧开发进程，生成临时签名包到 `~/Applications/CodexModelManager-Dev.app`，
并验证主进程与内置 `CodexModelSync` helper 运行。Windows 端测试与构筑入口位于
`windows/`，由 `.github/workflows/windows.yml` 与 `windows-preview.yml` 维护。

## 发行流程

- **macOS 自动发行**（`.github/workflows/release.yml`）：推送与 `product-version.json`
  匹配的 `v<version>` Annotated Tag 后，先核对祖先关系与现有 Release，再调用
  `one-person-lab/.github/workflows/reusable-macos-sparkle-release.yml@main` 执行
  通用二进制构筑（`--universal`）、Developer ID 签名、App/DMG 公证装订、`appcast.xml`
  Ed25519 签名与公开发布回读校验；`release.yml` 仅发布精确的 `Codex-Models.dmg`、
  `.sha256` 与 `appcast.xml`。
- **本地离线打包**（调试用）：`SIGNING_IDENTITY=... TEAM_ID=... APPLE_NOTARY_PROFILE=... ./script/release.sh X.Y.Z`
  构筑并公证通用 DMG 到 `dist/`。
- **Homebrew 分发**：`homebrew-one-person-lab` 的 `Casks/opl-codex-models.rb` 开启
  `auto_updates true` 并与 GitHub Release + Sparkle 2 自动更新协同工作。应用包内部
  bundle ID（`com.onepersonlab.codex-model-manager`）、helper 名称与配置目录保持稳定。
