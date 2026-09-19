# Launch the tkinter GUI against the persistent mock demo environment.
# No real Codex required; everything is simulated.
#
#   .\scripts\launch_gui.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$config = powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\setup_demo.ps1") | Select-Object -Last 1
$config = $config.Trim()
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
Write-Host "Launching GUI with demo config: $config"
& $venvPython -m codex_model_manager.gui.app --config $config