# Codex Models Repository Guide

This repository owns the native macOS SwiftUI app (`CodexModelManager.app`) and
companion CLI (`CodexModelSync`), plus the Windows Python/tkinter port.

## Runtime Contract

- Preserve first-class in-app automatic updating on macOS as a non-negotiable
  product contract: macOS ships dual-track updates (in-app Sparkle 2 updates
  from Ed25519-signed `appcast.xml` at `releases/latest/download/appcast.xml`
  plus downstream Homebrew Cask distribution with `auto_updates true`).
- Refactoring or distribution-simplification tasks must never remove Sparkle 2
  in-app updates or replace native update controls with CLI upgrade hints.
- Use the shared OPL macOS Sparkle release workflow
  (`gaofeng21cn/one-person-lab/.github/workflows/reusable-macos-sparkle-release.yml`)
  for Developer ID signing, app/DMG notarization, signed `appcast.xml`
  generation, and artifact verification.
- Keep `CFBundleVersion` strictly monotonic (`major*10000 + minor*100 + patch`).

## GitHub Text Policy

- Write new GitHub-visible text in English (commit messages, PRs, issues,
  comments, and release notes). Product names, code identifiers, paths, and
  commands are excepted.
