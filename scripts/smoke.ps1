param(
  [string]$BaseUrl = "http://127.0.0.1:5000"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Check {
  param(
    [string]$Name,
    [string]$Method,
    [string]$Url,
    [int]$ExpectedStatus = 200,
    [string]$Body = ""
  )

  $args = @{
    Uri         = $Url
    Method      = $Method
    ContentType = "application/json"
    UseBasicParsing = $true
  }
  if ($Body -ne "") {
    $args["Body"] = $Body
  }

  try {
    $resp = Invoke-WebRequest @args
    $status = [int]$resp.StatusCode
  } catch {
    $status = [int]$_.Exception.Response.StatusCode.value__
  }

  if ($status -ne $ExpectedStatus) {
    throw "FAIL $Name expected=$ExpectedStatus actual=$status"
  }

  Write-Output "PASS $Name status=$status"
}

Write-Output "Running backend smoke checks against $BaseUrl"

# Core readiness
Invoke-Check -Name "health" -Method "GET" -Url "$BaseUrl/api/health" -ExpectedStatus 200
Invoke-Check -Name "ready" -Method "GET" -Url "$BaseUrl/api/ready" -ExpectedStatus 200
Invoke-Check -Name "backend-status" -Method "GET" -Url "$BaseUrl/api/backend/status" -ExpectedStatus 200

# UI entrypoints
Invoke-Check -Name "home" -Method "GET" -Url "$BaseUrl/" -ExpectedStatus 200
Invoke-Check -Name "marketplace" -Method "GET" -Url "$BaseUrl/marketplace" -ExpectedStatus 200

# Public APIs
Invoke-Check -Name "agents-list" -Method "GET" -Url "$BaseUrl/api/agents" -ExpectedStatus 200
Invoke-Check -Name "sim-status" -Method "GET" -Url "$BaseUrl/api/sim/status" -ExpectedStatus 200

# Validation behavior checks
$badX402 = '{"from":"0x123","to":"0x456","value":0,"validBefore":1,"nonce":0,"v":27,"r":"0x0","s":"0x0","agentId":9999}'
Invoke-Check -Name "x402-invalid-rejected" -Method "POST" -Url "$BaseUrl/api/x402/pay" -ExpectedStatus 400 -Body $badX402

$badSimSpeed = '{"tickRealSeconds":0.01}'
Invoke-Check -Name "sim-speed-invalid-rejected" -Method "POST" -Url "$BaseUrl/api/sim/speed" -ExpectedStatus 400 -Body $badSimSpeed

Write-Output "Smoke checks complete."
