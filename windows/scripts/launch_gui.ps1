param([switch]$Demo, [string]$Config)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { throw 'Use the portable EXE, or install requirements.txt in .venv first.' }
if ($Demo) {
    $Config = powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup_demo.ps1') | Select-Object -Last 1
    $Config = $Config.Trim()
}
if (-not $Config) { $Config = Join-Path $root 'user-data\config.json' }
$entry = Join-Path $PSScriptRoot 'desktop_entry.py'
Start-Process -FilePath $python -ArgumentList @('"' + $entry + '"', '--config', '"' + $Config + '"') -WorkingDirectory $root
