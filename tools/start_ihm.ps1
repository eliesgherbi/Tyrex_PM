# Start the integrated Tyrex workspace on http://127.0.0.1:4173/
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
Push-Location frontend
if (-not (Test-Path "node_modules")) { npm install }
npm run build
Pop-Location
python -m tyrex_pm.research.ihm --host 127.0.0.1 --port 4173
