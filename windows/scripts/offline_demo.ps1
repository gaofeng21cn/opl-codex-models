# Offline demo: run the whole pipeline against the MOCK Codex (no real Codex,
# no writes to the real ~/.codex). Every result here is a *simulated* run.
#
# Usage:
#   .\scripts\offline_demo.ps1
#   or from PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\offline_demo.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "venv not found; run setup first" }

$mockPy   = Join-Path $root "demo\mock\codex.py"
$codexDir = Join-Path $root "demo\codex"
New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
$codexCmd = Join-Path $codexDir "codex.cmd"
$wrapper  = '@echo off' + [Environment]::NewLine + '"' + $venvPython + '" "' + $mockPy + '" %*'
Set-Content -Path $codexCmd -Value $wrapper -Encoding Ascii

$sandbox  = Join-Path $root "demo\demo_home"
if (Test-Path $sandbox) { Remove-Item -Path $sandbox -Recurse -Force }
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

# Build a real Chinese filename from code points so this .ps1 stays ASCII-only
# (PowerShell 5.1 mis-reads a UTF-8-no-BOM Chinese literal as ANSI). The path we
# hand to the app is still a genuine Unicode path the pipeline must support.
$customSrc  = Join-Path $sandbox ("custom " + [string][char]0x6A21 + [string][char]0x578B + ".json")
Copy-Item -Path (Join-Path $root "demo\custom-models.json") -Destination $customSrc
$merged = Join-Path $sandbox "merged models.json"
$configPath = Join-Path $sandbox "config.json"

$configJson = @{
  codexRuntimePath      = (Convert-Path $codexCmd)
  customSourcePath      = (Convert-Path $customSrc)
  mergedCatalogPath     = $merged
  syncLogPath           = (Join-Path $sandbox "Logs\sync.jsonl")
  errorLogPath          = (Join-Path $sandbox "Logs\sync.error.log")
  backupDirectoryPath   = (Join-Path $sandbox "Backups")
  # SIMULATED apply target: never a real ~/.codex config.
  codexConfigPath       = (Join-Path $sandbox "target_config.toml")
  isDemo                = $true
  launchAgentLabel      = "com.onepersonlab.codex-model-manager.sync"
} | ConvertTo-Json
Set-Content -Path $configPath -Value $configJson -Encoding UTF8

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$global:LASTEXITCODE = 0

Write-Host "== probe (simulated; mock runtime) =="
& $venvPython -m codex_model_manager --config $configPath probe

Write-Host ""
Write-Host "== sync (simulated; isolated, does NOT touch real .codex) =="
& $venvPython -m codex_model_manager --config $configPath sync --append-log
if ($LASTEXITCODE -ne 0) { throw "sync failed" }

Write-Host ""
Write-Host "== list =="
& $venvPython -m codex_model_manager --config $configPath list

Write-Host ""
Write-Host "== add custom model from template =="
& $venvPython -m codex_model_manager --config $configPath add "deepseek-v3x" --name "DeepSeek V3X" --description "sample custom model" --template "gpt-6-astra" --context-window 131072
if ($LASTEXITCODE -ne 0) { throw "add failed" }

Write-Host ""
Write-Host "== reasoning =="
& $venvPython -m codex_model_manager --config $configPath reasoning "deepseek-v3x" --efforts low,high,max --default high
if ($LASTEXITCODE -ne 0) { throw "reasoning failed" }

Write-Host ""
Write-Host "== re-sync (first updated, second no_change) =="
& $venvPython -m codex_model_manager --config $configPath sync --append-log
& $venvPython -m codex_model_manager --config $configPath sync --append-log

Write-Host ""
Write-Host "== backup + apply --dry-run (no real write) =="
& $venvPython -m codex_model_manager --config $configPath backup
& $venvPython -m codex_model_manager --config $configPath apply --diff --dry-run --codex-config (Join-Path $sandbox "target_config.toml")

Write-Host ""
Write-Host "Demo finished (SIMULATED). Merged catalog: $merged"