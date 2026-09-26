#!/usr/bin/env python3
"""
demo_subhire.py - print a step-by-step, human-driven walkthrough of the
sub-hire chain against a locally running server (not run in CI).

    python scripts/demo_subhire.py
    python scripts/demo_subhire.py --base http://127.0.0.1:8091 --category Development \\
        --agent AGT-… --sub-agent AGT-… --sub-sub-agent AGT-…

It sends nothing and never approves anything: funding and ASK_HUMAN
sub-hires need a real World ID sign-in by the human on their phone, so the
script only prints the commands (curl + jq) for a person to run in order.
"""
from __future__ import annotations

import argparse
import textwrap


def steps(base: str, category: str, agent: str, sub: str, subsub: str) -> str:
    auth = '-H "Authorization: Bearer $MCP_API_TOKEN"'
    json_h = '-H "Content-Type: application/json"'
    return textwrap.dedent(f"""\
    Sub-hire chain walkthrough (simulated escrow: no on-chain transactions)
    ======================================================================

    0. Prerequisites, in .env (see .env.example):
       - WORLD_ISSUER / WORLD_CLIENT_ID / WORLD_CLIENT_SECRET for the World ID
         sandbox app, so the human can approve on their phone.
       - INTERCEPTA_API_KEY and SCREENING_ADDRESS_MAP mapping each agent's
         Sepolia payout address to a mainnet address. Without a key every hop
         is refused (fail closed), which shows the REFUSE branch only.
         Map one sub-agent to a clean address (PAY) and another to a flagged
         one to see ASK_HUMAN.
       - MCP_API_TOKEN (any random string) so step 5 can read the mandate.
       - Leave ESCROW_PRIVATE_KEY / BUYER_VAULT_PRIVATE_KEY unset: escrow stays
         simulated.

    1. Start the server:
         flask --app wsgi seed
         flask --app wsgi run --port {base.rsplit(':', 1)[-1].rstrip('/')}
       In the shell you will run the next steps from:
         export BASE={base}
         export MCP_API_TOKEN=<the value from .env>

    2. Pick three agents with payout addresses (the hired agent and two
       sub-agents):
         curl -s {auth} "$BASE/api/agents?limit=20" | jq '.agents[] | {{agent_id, name, category}}'
         export A={agent} B={sub} C={subsub}

    3. Scope a job for agent A:
         export ENG=$(curl -s {auth} {json_h} -X POST "$BASE/api/engagements" \\
           -d '{{"agent_id":"'$A'","outcome":"Ship the reporting feature","budget_usdc":"25"}}' \\
           | jq -r .engagement_id); echo $ENG

    4. Hire (fund). The reply has a user_code and a link; the HUMAN opens it on
       their phone and approves with World ID:
         curl -s {auth} {json_h} -X POST "$BASE/api/engagements/$ENG/hire" \\
           -d '{{"flow":"device","confirm_amount_usdc":"25"}}' \\
           | jq '{{approval_id, user_code, verification_uri_complete, screening: .screening.verdict}}'
         export APR=<approval_id from above>
       Poll until "consumed" (this also advances the device flow):
         curl -s {auth} "$BASE/api/approvals/$APR" | jq '{{state, failure_code}}'

    5. The root mandate was minted when the approval was consumed. Read it
       (only API-token callers get mandate_token):
         export MANDATE=$(curl -s {auth} "$BASE/api/engagements/$ENG" | jq -r .mandate_token)
         curl -s "$BASE/api/mandates/$(curl -s {auth} "$BASE/api/engagements/$ENG" | jq -r .mandate.mandate_id)" | jq '{{status, budget_micro, remaining_micro}}'

    6. Agent A sub-hires agent B with its mandate (no API token: the mandate
       is the authority):
         curl -s -H "Authorization: Mandate $MANDATE" {json_h} -X POST "$BASE/api/engagements/$ENG/subhire" \\
           -d '{{"agent_id":"'$B'","outcome":"Write the export tests","budget_usdc":"10","category":"{category}"}}' \\
           | tee /tmp/subhire_b.json | jq '{{engagement_id, status, depth, allocated_micro, capped, screening: .screening.verdict, approval_id, user_code}}'
       - PAY / CAP -> 201: B's job is funded from A's allocation.
           export ENG_B=$(jq -r .engagement_id /tmp/subhire_b.json)
           export MANDATE_B=$(jq -r .mandate_token /tmp/subhire_b.json)
       - ASK_HUMAN -> 202: the ROOT human approves this sub-hire on their phone
         (user_code / verification_uri_complete), then poll it as in step 4.
           export ENG_B=$(jq -r .child_engagement_id /tmp/subhire_b.json)
           export MANDATE_B=$(curl -s {auth} "$BASE/api/engagements/$ENG_B" | jq -r .mandate_token)
       - REFUSE -> 403 SCREENING_REFUSED with the reasons; nothing allocated.

    7. The mandate only narrows. Each of these is refused with 403:
         # more than A has left -> MANDATE_EXCEEDED
         curl -s -H "Authorization: Mandate $MANDATE" {json_h} -X POST "$BASE/api/engagements/$ENG/subhire" \\
           -d '{{"agent_id":"'$C'","outcome":"x","budget_usdc":"100","category":"{category}"}}' | jq '{{code, error}}'
         # a category A was never given -> CATEGORY_NOT_ALLOWED
         curl -s -H "Authorization: Mandate $MANDATE" {json_h} -X POST "$BASE/api/engagements/$ENG/subhire" \\
           -d '{{"agent_id":"'$C'","outcome":"x","budget_usdc":"1","category":"Not-{category}"}}' | jq '{{code, error}}'

    8. Second hop: B sub-hires C with B's own, narrower mandate:
         curl -s -H "Authorization: Mandate $MANDATE_B" {json_h} -X POST "$BASE/api/engagements/$ENG_B/subhire" \\
           -d '{{"agent_id":"'$C'","outcome":"Review the tests","budget_usdc":"4","category":"{category}"}}' \\
           | jq '{{engagement_id, depth, allocated_micro, screening: .screening.verdict}}'
       A third hop from C is refused with DEPTH_EXCEEDED (MANDATE_MAX_DEPTH=2).

    9. See the whole chain, human -> root mandate -> A -> B -> C:
         open "$BASE/jobs/$ENG/chain"
         curl -s "$BASE/api/engagements/$ENG/chain" | jq '.nodes | length'
    """)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--category", default="Development",
                        help="the hired agent's category (the root mandate's only category)")
    parser.add_argument("--agent", default="AGT-XXXX-XXXX-X", help="the agent the human hires")
    parser.add_argument("--sub-agent", default="AGT-YYYY-YYYY-Y", help="the agent A sub-hires")
    parser.add_argument("--sub-sub-agent", default="AGT-ZZZZ-ZZZZ-Z", help="the agent B sub-hires")
    args = parser.parse_args()
    print(steps(args.base.rstrip("/"), args.category,
                args.agent, args.sub_agent, args.sub_sub_agent))


if __name__ == "__main__":
    main()
