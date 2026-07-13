$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python -u .\mrmodel_monitor.py --headless
