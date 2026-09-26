# Payment screening

Agent-to-agent payments use the Intercepta REST API as a risk gate. It is a
screening service, not a facilitator: settlement still happens through the
Ethereum Sepolia x402/escrow path.

## Flow

1. The buyer requests x402 requirements and validates network, token, amount,
   recipient and EIP-712 domain.
2. Before signing, `app/screening/presign.py` makes three live provider calls:
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

The command obtains the 402 requirements, runs all three provider scans, and
only sends the second request if the pre-sign gate allows it. A blocked run
prints the verdict and reasons and exits before signing or submitting payment.

## Provider feedback

The first credential supplied for local validation was rejected by the live
provider; the key is intentionally not recorded here:

```text
GET /api/public/v2/extension/account/<address>/quick-scan
HTTP 403
{"status":403,"response":"This authentication key is incorrect or doesn’t exist"}
```

The client treats that response as `HTTP_ERROR` → fail-closed `REFUSE`. A
valid sandbox key is required for the successful live-payment recording.
