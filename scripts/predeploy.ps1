param(
  [string]$BaseUrl = "http://127.0.0.1:5000",
  [switch]$SkipSmoke,
  [string]$LiveCheckMode = "demo"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

Write-Output "Running predeploy checks..."

# 1) Import-time production config validation (fails fast if unsafe).
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$validation = cmd /c "py -c ""import os; os.environ['FLASK_ENV']='production'; from app import app; print('prod-config-ok')""" 2>&1 | Out-String
$ErrorActionPreference = $prevErrorActionPreference
if ($LASTEXITCODE -ne 0) {
  Write-Error "Production config validation failed. Details:`n$validation"
  exit 1
}
Write-Output "PASS production config validation"

# 2) Lightweight test suite for API validation contracts.
py -m unittest tests.test_api_validation -v
if ($LASTEXITCODE -ne 0) {
  Write-Error "API validation tests failed."
  exit 1
}
Write-Output "PASS API validation tests"

py -m unittest tests.test_admin_transitions -v
if ($LASTEXITCODE -ne 0) {
  Write-Error "Admin transition tests failed."
  exit 1
}
Write-Output "PASS admin transition tests"

# 3) Runtime smoke checks against a running instance.
if (-not $SkipSmoke) {
  $env:BASE_URL = $BaseUrl
  $env:CHECK_MODE = $LiveCheckMode
  py scripts/fullstack_check.py
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Full-stack integration checks failed."
    exit 1
  }
  Write-Output "PASS full-stack integration checks"

  powershell -ExecutionPolicy Bypass -File "scripts/smoke.ps1" -BaseUrl $BaseUrl
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Smoke checks failed."
    exit 1
  }
  Write-Output "PASS smoke checks"
}

Write-Output "Predeploy checks complete."
