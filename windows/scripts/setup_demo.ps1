# Idempotent demo setup: create the mock runtime wrapper + a persistent demo
# config under demo/persist so the GUI can run without any real Codex.
#
#   .\scripts\setup_demo.ps1
# Prints the demo config path.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "venv not found; run setup first" }

$codexDir = Join-Path $root "demo\codex"
New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
$codexCmd = Join-Path $codexDir "codex.cmd"
$wrapper = '@echo off' + [Environment]::NewLine + '"' + $venvPython + '" "' + (Join-Path $root "demo\mock\codex.py") + '" %*'
Set-Content -Path $codexCmd -Value $wrapper -Encoding Ascii

$persist = Join-Path $root "demo\persist"
New-Item -ItemType Directory -Force -Path $persist | Out-Null
$customSrc = Join-Path $persist "custom models.json"
if (-not (Test-Path $customSrc)) {
  Copy-Item -Path (Join-Path $root "demo\custom-models.json") -Destination $customSrc
}

$configPath = Join-Path $persist "config.json"
if (-not (Test-Path $configPath)) {
  $configJson = @{
    codexRuntimePath    = (Convert-Path $codexCmd)
    customSourcePath    = (Join-Path $persist "custom models.json")
    mergedCatalogPath   = (Join-Path $persist "merged models.json")
    syncLogPath         = (Join-Path $persist "Logs\sync.jsonl")
    errorLogPath        = (Join-Path $persist "Logs\sync.error.log")
    backupDirectoryPath = (Join-Path $persist "Backups")
    # SIMULATED apply target: never a real ~/.codex config.
    codexConfigPath     = (Join-Path $persist "target_config.toml")
    isDemo              = $true
    launchAgentLabel    = "com.onepersonlab.codex-model-manager.sync"
  } | ConvertTo-Json
  Set-Content -Path $configPath -Value $configJson -Encoding UTF8
}

# run an initial (simulated) sync so the merged catalog exists for the GUI
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
if (-not (Test-Path (Join-Path $persist "merged models.json"))) {
  & $venvPython -m codex_model_manager --config $configPath sync --append-log | Out-Null
}

Write-Output (Convert-Path $configPath)