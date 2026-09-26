param(
  [string]$BaseUrl = "http://127.0.0.1:5000",
  [decimal]$MinFacilitatorAvax = 0.01,
  [switch]$SkipPredeploy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-True {
  param(
    [bool]$Condition,
    [string]$Message
  )
  if (-not $Condition) {
    throw $Message
  }
}

Write-Output "Running on-chain readiness checks against $BaseUrl ..."

# 0) Ensure app is reachable.
try {
  $health = Invoke-RestMethod -Uri "$BaseUrl/api/health" -Method Get
  Assert-True ($health.status -eq "ok") "Health check failed."
  Write-Output "PASS health check"
} catch {
  Write-Error "Backend is not reachable at $BaseUrl. Start it first (py app.py)."
  exit 1
}

# 1) Run full local quality gate.
if (-not $SkipPredeploy) {
  powershell -ExecutionPolicy Bypass -File "scripts/predeploy.ps1" -BaseUrl $BaseUrl -LiveCheckMode demo
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Predeploy checks failed."
    exit 1
  }
  Write-Output "PASS predeploy suite"
}

# 2) Funding + mode gate.
$chain = Invoke-RestMethod -Uri "$BaseUrl/api/sim/chain-health" -Method Get
$facilitator = [string]$chain.facilitator
$facilitatorAvax = [decimal]$chain.facilitatorAVAX
$mode = [string]$chain.mode

Assert-True ($facilitatorAvax -ge $MinFacilitatorAvax) "Facilitator wallet $facilitator has only $facilitatorAvax AVAX. Need at least $MinFacilitatorAvax AVAX."
Assert-True ($mode -eq "live-write") "Chain mode is '$mode' (expected 'live-write')."
Write-Output "PASS chain funding/mode gate ($facilitator, AVAX=$facilitatorAvax)"

# 3) Force live mode and trigger a direct flow that should be broadcast-capable.
$null = Invoke-RestMethod -Uri "$BaseUrl/api/sim/live-mode" -Method Post -ContentType "application/json" -Body '{"enabled":true}'
$trigger = Invoke-RestMethod -Uri "$BaseUrl/api/sim/trigger-direct" -Method Post -ContentType "application/json" -Body '{"fromId":1,"toId":2,"amountUSDC":0.01,"tokens":1000,"reason":"onchain-ready-gate"}'

Assert-True ([bool]$trigger.ok) "Trigger direct did not return ok=true."
Assert-True ([bool]$trigger.triggered) "Trigger direct did not return triggered=true."
if ($trigger.chain -and $trigger.chain.mode) {
  Assert-True ([string]$trigger.chain.mode -eq "live-write") "Trigger flow returned chain.mode=$($trigger.chain.mode), expected live-write."
}
Write-Output "PASS live write-path trigger"

# 4) Validate a core on-chain read path.
$agent = Invoke-RestMethod -Uri "$BaseUrl/api/sim/agent-onchain/1" -Method Get
Assert-True ([int]$agent.agentId -eq 1) "Agent on-chain read did not return agent 1."
Write-Output "PASS on-chain agent read"

Write-Output ""
Write-Output "ON-CHAIN READY"
Write-Output "- Full predeploy gate passed"
Write-Output "- Facilitator funding gate passed"
Write-Output "- Live write-path trigger passed"
Write-Output "- On-chain reads passed"
