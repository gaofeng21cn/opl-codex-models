# Windows port of gaofeng21cn/opl-codex-models

Copyright (c) 2026 the port authors.
Derived from **gaofeng21cn/opl-codex-models** (original commit `bb588846b28beeece759adcdc917e643fdb8077c`),
Copyright (c) 2025 the original authors, licensed under the **Apache License 2.0**.

The original project (macOS/Swift) and this Windows port are both released under Apache-2.0.
See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) (TOMLKit dependency from the original).

## 改动来源 / Change provenance

This project is a **Windows reimplementation** of the original's core functionality.
The merge rules, model-override semantics, custom-model editor, backup-under-write
behaviour and sync-log format are direct ports of the Swift sources:

- `Sources/CodexModelCore/CatalogSyncService.swift`
- `Sources/CodexModelCore/ModelFieldOverrides.swift`
- `Sources/CodexModelCore/AppConfiguration.swift`
- `Sources/CodexModelCore/SetupService.swift` (CodexConfigurationEditor)
- `Sources/CodexModelCore/ProcessRunner.swift`
- `Sources/CodexModelManager/Services/CustomModelEditor.swift`
- `Sources/CodexModelManager/Models/ReasoningSettings.swift`
- `Sources/CodexModelManager/Services/CatalogParser.swift`

Changed for Windows / Python:
- Runtime discovery via PATH + user path + CODEX_HOME, distinguishing native PE binaries from WSL ELF binaries.
- Config/backup dirs under `%LOCALAPPDATA%\CodexModelManager` and Codex dir under `$CODEX_HOME` / `~/.codex`.
- GUI in tkinter (was SwiftUI); CLI entry (`python -m codex_model_manager`) replaces the `CodexModelSync` executable.
- TOML editing via **tomlkit** (comment-preserving) instead of a line-based editor.
- Subprocess launch via `subprocess.Popen` argument arrays (no shell), with timeout + exit-code checks.
- TODO-mirrored fixtures/tests under `tests/`; the mock Codex under `demo/mock/` is a **simulation**, never a real integration.

Imported from lifelover26/opl-codex-models, commit `8586e49c75970a2cb1f00be65cc23afc20e4105f`.
The maintained macOS source remains in the parent repository. Windows source is isolated under `windows/`; historical private environment handoff and integration receipts are not distributed.
