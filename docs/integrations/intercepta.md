# Payment screening

Agent-to-agent payments use the Intercepta REST API as a risk gate. It is a
screening service, not a facilitator: settlement still happens through the
Ethereum Sepolia x402/escrow path.

## Flow

1. The buyer requests x402 requirements and the official Python x402 client
   validates network, token, amount, recipient and EIP-712 domain.
2. Before signing, `app/screening/presign.py` makes three live provider calls
   through the official-client bridge in `chain/x402_official.py`:
   Quick Scan on the mainnet payee representation, Token Risks on mainnet
   USDC, and Scan Message on the exact authorization that will be signed.
3. `PAY` proceeds, `CAP` proceeds only within the configured cap,
   `ASK_HUMAN` pauses for approval, and `REFUSE` or any provider/configuration
   error stops before the signer. The resource server then deep-scans the
   payer before accepting the job and screens the payee plus exact
   authorization again after receipt, so a client cannot bypass either side
   of the gate.
4. `app/screening/service.py` persists the resource-side verdict and raw
   provider response with latency. `app/screening/intercepta.py` is the only
   REST transport and logs request path, status, latency and response without
   logging the API key. Successful address evidence is cached for five minutes
   to conserve the request quota; failures are never cached, and each hop still
   gets its own stored verdict and local policy evaluation.

The provider supports mainnet risk data, not Ethereum Sepolia. Every Sepolia
payee and payer therefore needs an explicit `Agent.screening_address` or
`SCREENING_ADDRESS_MAP` entry. A missing mapping is a fail-closed hold.

## Local smoke test

Keep all credentials outside the repository:

```bash
INTERCEPTA_API_KEY=... \
X402_PAYER_PRIVATE_KEY=... \
X402_MANDATE_TOKEN=... \
python scripts/intercepta_x402_smoke.py \
  http://127.0.0.1:8090/api/agents/AGT-XXXX-XXXX-X/tasks \
  --task 'Produce the API test plan' \
  --screening-address 0x<mainnet-risk-address>
```

The command obtains the 402 requirements, lets the official x402 client build
the exact EIP-3009 payload, runs all three provider scans in its pre-sign
hooks, and only sends the second request if the gate allows it. A blocked run
prints the verdict and reasons and exits before signing or submitting payment.

## Named specialist runner

The protected purchase runner resolves an active ENS agent record first, uses
its `payout` record as the expected x402 `payTo`, enforces the buyer's atomic
maximum, and then invokes the official client before signing:

```bash
python scripts/hire_agent.py \
  --agent quartz-qa-test-planner.agentslist-app.eth \
  --task-file demo/api-spec.txt \
  --max-usdc 0.10
```

It requires `X402_PAYER_PRIVATE_KEY`, `X402_MANDATE_TOKEN`, and a
`SCREENING_ADDRESS_MAP` (or `--screening-address`) in the process environment.
The response includes the resolved name, payment receipt, pre-sign verdict and
the source-hashed QA test plan. A missing name, changed payee, over-limit
amount, blocked screening result or missing map stops before the signer.

## Provider feedback

Live read-only validation was run on 2026-09-26 with the credential kept only
in the process environment. The key is intentionally not recorded here:

```text
GET .../account/<address>/quick-scan        HTTP 200  {"toxicScore":0,"traits":[]}
GET .../token-intelligence/token/<mainnet-USDC>/risks?chainId=1
                                             HTTP 200  {"action":"info","trust":"whitelist","riskLevel":"neutral","detectors":[]}
POST .../analysis/signature                 HTTP 200  {"riskGroup":"Low","detectors":[],"addresses":[]}
GET .../account/<address>/toxic-score       TIMEOUT at 4.0s
```

The first three responses were accepted and mapped to `PAY` by the local
policy. The deep-scan timeout was mapped to `TIMEOUT` and fail-closed
`REFUSE`; no signer or settlement was attempted for that request. These are
provider observations, not a claim that a Sepolia payment was executed.
