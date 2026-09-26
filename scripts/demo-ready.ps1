param(
  [string]$BaseUrl = "http://127.0.0.1:5000"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Output "Checking demo readiness at $BaseUrl ..."

# Ensure the app is reachable before running checks.
try {
  $resp = Invoke-WebRequest -Uri "$BaseUrl/api/health" -UseBasicParsing
  if ([int]$resp.StatusCode -ne 200) {
    throw "Health endpoint returned status $($resp.StatusCode)"
  }
} catch {
  Write-Error "Server is not reachable at $BaseUrl. Start the backend first (py app.py)."
  exit 1
}

powershell -ExecutionPolicy Bypass -File "scripts/predeploy.ps1" -BaseUrl $BaseUrl -LiveCheckMode demo
if ($LASTEXITCODE -ne 0) {
  Write-Error "Predeploy checks failed."
  exit 1
}

Write-Output ""
Write-Output "DEMO READY"
Write-Output "- Backend checks passed"
Write-Output "- API validation tests passed"
Write-Output "- Admin transition tests passed"
Write-Output "- Full-stack integration checks passed"
Write-Output "- Smoke checks passed"
