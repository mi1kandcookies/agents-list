"""
sim_engine.py — live, tick-driven agent simulation.

Spawns a daemon thread that advances a simulated clock, generates bid
arrivals, matches them to qualifying agents, settles sessions with realistic
variance, and feeds the reputation / surge / ledger state continuously.

Unlike simulation.py (one-shot historical seed), this runs forever once
started and produces fresh events the UI can tail in real time.

Tick cadence (defaults):
  real 2s  ==  sim 60s   (30× acceleration)
  → one full simulated day ≈ 48 minutes of wall clock.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from extensions import db
from models import (
    Agent, OnchainProfile, ChainTransaction, PricePoint, AuctionBid,
)
from simulation import (
    compute_surge, SLASH_PCT, SLASH_SPLIT_USER_BPS,
    VOLUME_PER_POINT, MAX_POINTS_PER_TASK, MAX_SCORE,
    DECAY_TARGET, DECAY_PERIOD, _tier_for, _mock_tx_hash,
)


# ── Contract addresses (stable mock set so tx explorer view is consistent) ──

ADDR_STAKING  = "0xfc942b4d1Eb363F25886b3F5935394BD4932B896"
ADDR_ESCROW   = "0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2"
ADDR_REP      = "0x40ef89Ce1E248Df00AF6Dc37f96BBf92A9Bf603A"
ADDR_AUCTION  = "0xa7AEEca5a76bd5Cd38B15dfcC2c288d3645E53E3"  # matches onchain.ADDRESSES

# Stable category → id mapping so bids can target specific agent types
CATEGORY_IDS = {
    "Development":       0,
    "Data & Analytics":  1,
    "Content":           2,
    "Finance":           3,
    "Research":          4,
    "Security":          5,
    "Automation":        6,
}


def _category_id(agent: Agent) -> int:
    return CATEGORY_IDS.get(agent.category, 6)


@dataclass
class SimEvent:
    id: int
    ts: int           # simulated unix ts
    real_ts: float    # wall clock
    kind: str
    agent_id: int | None
    message: str
    amount_usdc: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "realTs": self.real_ts,
            "kind": self.kind,
            "agentId": self.agent_id,
            "message": self.message,
            "amountUSDC": round(self.amount_usdc, 4),
            "meta": self.meta,
        }


class SimulationEngine:
    """Singleton background simulator."""

    def __init__(self, app, *, tick_real_s: float = 5.0, tick_sim_s: int = 60):
        self.app = app
        self.tick_real_s = tick_real_s
        self.tick_sim_s = tick_sim_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._event_id = 0
        self.events: deque[SimEvent] = deque(maxlen=500)
        self.started_real_ts = 0.0
        self.sim_clock = int(time.time())
        self.tick_count = 0
        # Per-agent in-flight sessions: [{start_sim_ts, end_sim_ts, tokens, price, deposit, buyer, outcome}]
        self._active: dict[int, list[dict]] = {}
        # Per-agent capacity (concurrent sessions before util saturates)
        self._capacity: dict[int, int] = {}
        # Rolling demand per category for surge
        self._recent_bids: deque[tuple[int, int]] = deque(maxlen=120)  # (sim_ts, category)
        # RNG — fresh each start so sims don't look identical across restarts
        self._rng = random.Random(time.time())

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self.started_real_ts = time.time()
            self.sim_clock = int(time.time())
            self._thread = threading.Thread(target=self._run, name="sim-engine", daemon=True)
            self._thread.start()
            self._log_event("system", None, "Simulation engine started")
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False
            self._stop.set()
            self._thread.join(timeout=5)
            self._log_event("system", None, "Simulation engine stopped")
            return True

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "tickCount": self.tick_count,
            "simClock": self.sim_clock,
            "simClockIso": datetime.fromtimestamp(self.sim_clock, tz=timezone.utc).isoformat(),
            "tickRealSeconds": self.tick_real_s,
            "tickSimSeconds": self.tick_sim_s,
            "acceleration": f"{self.tick_sim_s / self.tick_real_s:.1f}×",
            "activeSessions": sum(len(v) for v in self._active.values()),
            "eventBufferSize": len(self.events),
            "startedRealTs": self.started_real_ts,
        }

    def set_speed(self, tick_real_s: float) -> None:
        self.tick_real_s = max(0.1, min(10.0, float(tick_real_s)))

    def events_since(self, since_id: int = 0, limit: int = 100) -> list[dict]:
        return [e.to_dict() for e in list(self.events)[-limit:] if e.id > since_id]

    # ── main loop ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                with self.app.app_context():
                    self._tick()
                    db.session.commit()
            except Exception as e:
                self.app.logger.exception("sim tick failed: %s", e)
                try:
                    with self.app.app_context():
                        db.session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    with self.app.app_context():
                        db.session.remove()
                except Exception:
                    pass
            elapsed = time.time() - t0
            self._stop.wait(max(0.0, self.tick_real_s - elapsed))

    def _tick(self) -> None:
        self.tick_count += 1
        self.sim_clock += self.tick_sim_s
        agents = Agent.query.all()
        if not agents:
            return

        # 0. Ensure capacity map populated
        for a in agents:
            self._capacity.setdefault(a.id, self._rng.choice([3, 4, 5, 6, 8]))
            self._active.setdefault(a.id, [])

        # 1. Progress in-flight sessions → settle / refund / slash
        self._progress_sessions(agents)

        # 2. Generate new bid arrivals (Poisson)
        self._generate_bids(agents)

        # 3. Match open bids to qualifying agents
        self._match_bids(agents)

        # 4. Demo-driven A2A flow. The natural matching loop rarely picks
        # the 4 flagship composable agents out of 125+ competitors, so we
        # also fire an A2A flow on a flagship every ~10 ticks (~20 real
        # sec) to keep agent-to-agent transactions visible on /sim.
        # Fire a flagship A2A cascade every ~20 ticks (100 real sec at 5s
        # cadence). Rare enough to feel notable, frequent enough to always
        # have one visible within a reviewer's attention window.
        if self.tick_count % 20 == 0:
            self._fire_demo_a2a_flow()

        # 5. Periodic maintenance
        # Every 5 sim-minutes: snapshot price points
        if self.tick_count % 5 == 0:
            self._snapshot_prices(agents)
        # Every 60 sim-minutes: decay scores + expire stale bids
        if self.tick_count % 60 == 0:
            self._apply_decay(agents)
            self._expire_bids()

    def fire_direct_a2a(self, from_id: int, to_id: int, amount_usdc: float,
                         tokens: int = 1000, reason: str | None = None) -> dict:
        """Direct agent-to-agent payment: caller picks both sides and the
        exact USDC amount. Used by the /demo picker when the user wants full
        control over sender AND receiver, bypassing A2A_WORKFLOWS.

        Returns the event ids generated so the UI can echo them.
        """
        from_agent = db.session.get(Agent, int(from_id))
        from_profile = db.session.get(OnchainProfile, int(from_id))
        to_agent = db.session.get(Agent, int(to_id))
        to_profile = db.session.get(OnchainProfile, int(to_id))
        if not (from_agent and from_profile and to_agent and to_profile):
            return {"ok": False, "error": "agent or profile missing"}
        if to_profile.banned:
            # Unban the target so the demo stays alive
            to_profile.banned = False
            to_profile.accepting_work = True
            to_profile.stake_incident_count = max(0, to_profile.stake_incident_count - 1)

        amount_micro = int(float(amount_usdc) * 1_000_000)
        if amount_micro <= 0:
            return {"ok": False, "error": "amount must be > 0"}

        reason = reason or f"direct payment: {from_agent.name} → {to_agent.name}"

        # ── If live-writes is on and facilitator has AVAX, fire a REAL Fuji
        #    MockUSDC.transfer from the facilitator to the receiver's wallet.
        #    The real tx hash replaces the synthetic one below so reviewers can
        #    click through to Snowtrace and see a receipt.
        real_tx_hash = None
        real_block_number = None
        live_error = None
        try:
            import os as _os, sys as _sys
            if _os.environ.get("AGENTHIRE_LIVE_WRITES", "").lower() in ("1","true","yes"):
                _sys.path.insert(0, "/Users/nichar/agenthire")
                from onchain import OnChain, ADDRESSES
                from web3 import Web3
                oc = OnChain.from_env()
                if oc and oc.facilitator:
                    # Direct MockUSDC.transfer from facilitator → receiver's
                    # wallet. We previously tried EscrowPayment.depositFunds
                    # first, but sim-seeded agents aren't registered in
                    # AgentRegistry so it always reverts with "not accepting"
                    # — landing a useless approve + a failed deposit on-chain.
                    # The transfer path is 1 tx, always succeeds, moves real
                    # USDC, and gives reviewers a clean Ava Labs explorer link.
                    usdc_abi = [{"type":"function","name":"transfer",
                                 "stateMutability":"nonpayable",
                                 "inputs":[{"name":"to","type":"address"},
                                           {"name":"amt","type":"uint256"}],
                                 "outputs":[{"type":"bool"}]}]
                    usdc = oc.w3.eth.contract(
                        address=Web3.to_checksum_address(ADDRESSES["MockUSDC"]),
                        abi=usdc_abi,
                    )
                    nonce = oc.w3.eth.get_transaction_count(oc.facilitator.address)
                    tx = usdc.functions.transfer(
                        Web3.to_checksum_address(to_profile.wallet_address),
                        amount_micro,
                    ).build_transaction({
                        "from":     oc.facilitator.address,
                        "nonce":    nonce,
                        "chainId":  oc.w3.eth.chain_id,
                        "gasPrice": oc.w3.eth.gas_price,
                        "gas":      120_000,
                    })
                    signed = oc.facilitator.sign_transaction(tx)
                    raw = signed.raw_transaction if hasattr(signed, 'raw_transaction') else signed.rawTransaction
                    h = oc.w3.eth.send_raw_transaction(raw)
                    r = oc.w3.eth.wait_for_transaction_receipt(h, timeout=60)
                    if r.status == 1:
                        real_tx_hash = h.hex() if hasattr(h, 'hex') else str(h)
                        real_block_number = int(r.blockNumber)
                        if real_tx_hash and not real_tx_hash.startswith("0x"):
                            real_tx_hash = "0x" + real_tx_hash
                    else:
                        live_error = f"USDC.transfer reverted tx={h.hex() if hasattr(h,'hex') else h}"
        except Exception as _e:
            live_error = str(_e)[:160]

        # Log as a pseudo-settle on the sender so the demo UI has context
        self._log_event(
            "settle", from_agent.id,
            f"{from_agent.name} initiating direct A2A payment to {to_agent.name}",
            amount=float(amount_usdc),
            meta={"tokensUsed": tokens, "demo": True, "direct": True},
        )

        # Deposit-style tx from sender wallet → escrow
        tx_hash_to_store = real_tx_hash or _mock_tx_hash(from_agent.id, 80_000 + self._event_id)
        block_to_store = real_block_number or (2_000_000 + self.tick_count * 100 + self._event_id)
        db.session.add(ChainTransaction(
            tx_hash=tx_hash_to_store,
            block_number=block_to_store,
            ts=self.sim_clock, kind="a2a_hire",
            agent_id=to_agent.id,
            from_addr=from_profile.wallet_address,
            to_addr=ADDR_ESCROW,
            amount_usdc=amount_micro,
            meta=json.dumps({
                "primaryAgentId": from_agent.id, "primaryAgentName": from_agent.name,
                "subAgentName": to_agent.name, "trigger": reason, "direct": True,
                "real": bool(real_tx_hash),
                **({"liveError": live_error} if live_error else {}),
            }),
        ))
        self._log_event(
            "a2a_hire", from_agent.id,
            f"{from_agent.name} → {to_agent.name} ({reason})",
            amount=float(amount_usdc),
            meta={"subAgentId": to_agent.id, "subAgentName": to_agent.name,
                  "primaryId": from_agent.id, "tokens": tokens,
                  "trigger": reason, "direct": True},
        )
        # Settle-style tx from escrow → receiver
        db.session.add(ChainTransaction(
            tx_hash=_mock_tx_hash(to_agent.id, 90_000 + self._event_id),
            block_number=2_000_000 + self.tick_count * 100 + self._event_id + 1,
            ts=self.sim_clock, kind="a2a_settle",
            agent_id=to_agent.id,
            from_addr=ADDR_ESCROW,
            to_addr=to_profile.wallet_address,
            amount_usdc=amount_micro,
            meta=json.dumps({
                "primaryAgentId": from_agent.id, "primaryAgentName": from_agent.name,
                "subAgentName": to_agent.name, "direct": True,
            }),
        ))
        points = min(MAX_POINTS_PER_TASK // 2 or 1,
                     max(1, amount_micro // VOLUME_PER_POINT))
        to_profile.score = min(MAX_SCORE, to_profile.score + points)
        to_profile.tasks_completed += 1
        to_profile.tier = _tier_for(to_profile.score, to_profile.tasks_completed)
        to_agent.tasks_completed = (to_agent.tasks_completed or 0) + 1
        self._log_event(
            "a2a_settle", to_agent.id,
            f"{to_agent.name} got paid by {from_agent.name} · +{points} score → {to_profile.score}",
            amount=float(amount_usdc),
            meta={"primaryId": from_agent.id, "primaryName": from_agent.name,
                  "score": to_profile.score, "tier": to_profile.tier, "direct": True},
        )
        return {"ok": True, "fromId": from_agent.id, "toId": to_agent.id,
                "amountUSDC": float(amount_usdc),
                "realTxHash":    real_tx_hash,
                "realBlock":     real_block_number,
                "liveError":     live_error}

    def _fire_demo_a2a_flow(self, primary_id: int | None = None,
                              token_budget: int | None = None,
                              price_per_token: float | None = None,
                              force_all: bool = True) -> None:
        """Fire an A2A flow. When called with no args, picks a random
        flagship and generates a random-budget job. All three args can be
        overridden by the /demo UI so reviewers can see any specific agent
        pair and any dollar amount they want.
        """
        try:
            from app import A2A_WORKFLOWS
        except Exception:
            return
        flagship_ids = list(A2A_WORKFLOWS.keys())
        if not flagship_ids:
            return

        # If caller specified a flagship, use it; otherwise round-robin
        ordered: list[int] = []
        if primary_id and primary_id in flagship_ids:
            ordered = [primary_id]
        else:
            self._rng.shuffle(flagship_ids)
            ordered = flagship_ids

        primary = primary_profile = None
        for candidate_id in ordered:
            a = db.session.get(Agent, candidate_id)
            p = db.session.get(OnchainProfile, candidate_id)
            if not a or not p:
                continue
            if p.banned:
                # Rehabilitate: flagships can't stay banned, the A2A story
                # depends on them. Reset incident count and restore stake.
                p.banned = False
                p.accepting_work = True
                p.stake_incident_count = max(0, p.stake_incident_count - 1)
                p.staked_amount = max(p.staked_amount, 2500 * 1_000_000)
                p.score = max(p.score, 700)
            primary, primary_profile = a, p
            break
        if not primary or not primary_profile:
            return

        # Budget + price — caller-driven when provided, randomized otherwise
        tokens = token_budget if token_budget and token_budget > 0 else self._rng.choice([500, 1000, 2000, 5000])
        upstream_price = (price_per_token if price_per_token and price_per_token > 0
                          else primary.min_price)
        tokens_used = int(tokens * self._rng.uniform(0.8, 1.0))
        synth_session = {
            "tokens": tokens,
            "price": upstream_price,
            "deposit": 0,
            "buyer": "0x" + hashlib.sha256(f"demo-buyer-{self.tick_count}-{tokens}".encode()).hexdigest()[:40],
            "bid_id": f"DEMO-{self.tick_count}",
            "price_per_token_override": price_per_token,  # consumed by sub-agent call
        }
        self._log_event(
            "settle", primary.id,
            f"{primary.name} settled a {tokens_used}-token job, now routing sub-calls",
            amount=tokens_used * upstream_price,
            meta={"tokensUsed": tokens_used, "demo": True,
                  "pricePerToken": round(upstream_price, 6)},
        )
        self._fire_a2a_subagent_calls(primary, primary_profile, tokens_used, synth_session,
                                       force_all=force_all)

    # ── tick sub-steps ────────────────────────────────────────────────────

    # Realistic task descriptions per category — keeps bids from feeling
    # like Mad Libs. Each bid pulls one line from the matching bucket.
    TASK_DESCRIPTIONS = {
        0: [  # Development
            "refactor auth middleware",
            "audit PR for memory leaks",
            "generate test cases for the new pricing endpoint",
            "migrate legacy REST to GraphQL",
            "scan deps for CVE-2024-known issues",
            "rewrite CI pipeline in Python",
        ],
        1: [  # Data & Analytics
            "cohort retention analysis Q4",
            "rebuild churn model on new schema",
            "daily revenue dashboard for sales team",
            "debug slow BigQuery join",
            "ETL from Snowflake to Postgres staging",
        ],
        2: [  # Content
            "long-form blog on agent economy",
            "SEO landing page for launch",
            "email drip for new waitlist",
            "press release draft for Series A",
            "social thread on latest product update",
        ],
        3: [  # Finance
            "reconcile DeFi treasury positions",
            "tax lot accounting Q4",
            "portfolio risk analysis for governance vote",
            "on-chain flow trace suspicious wallet",
            "backtest new signal against 2022 data",
        ],
        4: [  # Research
            "literature review on agent pricing mechanisms",
            "meta-analysis of LLM benchmarks",
            "citation graph for published crypto papers",
            "methodology review for our planned study",
        ],
        5: [  # Security
            "pentest the new checkout flow",
            "OWASP scan before production push",
            "threat model for the federated auth service",
            "audit smart contract deploy bytecode",
            "investigate phishing report from ops",
        ],
        6: [  # Automation
            "sync customer updates from Salesforce to Zendesk",
            "auto-respond to low-tier support tickets",
            "scrape competitor pricing daily",
            "calendar orchestration for exec team",
            "route webhooks across our four tools",
        ],
    }

    # Buyer personas — feels like a real marketplace with named customers,
    # not anonymous 0x hashes. Each has a stable deterministic wallet.
    BUYER_PERSONAS = [
        ("Acme Labs Treasury",      "0xAC3e1abs11111111111111111111111111111111"),
        ("Helix Capital",           "0xHe11x0000000000000000000000000000000022"),
        ("Northwind Ventures",      "0x400Thw11d00000000000000000000000000002A"),
        ("Pioneer Audit Co",        "0xP10Nee4Aud17000000000000000000000000003B"),
        ("Stratos AI Desk",         "0x5724705a1deSk00000000000000000000000044"),
        ("Meridian Research",       "0xMer1d14nResearch00000000000000000000055"),
        ("Compass Tax Partners",    "0xC0mpa55Tax000000000000000000000000000066"),
        ("Open Source Consortium",  "0x0p3n50u7C0n50r71um0000000000000000000077"),
        ("Atlas Guild DAO",         "0xA71a5Gu11dDa000000000000000000000000088"),
        ("Pulse Analytics",         "0xPu15eAn4ly71c500000000000000000000000099"),
    ]

    def _generate_bids(self, agents) -> int:
        hour = datetime.fromtimestamp(self.sim_clock, tz=timezone.utc).hour
        peak = 9 <= hour < 17
        # ~1 bid every two sim-minutes at peak — one at a time, like a
        # real order book, not a firehose.
        lam = 0.6 if peak else 0.2
        n = self._poisson(lam)
        created = 0
        for _ in range(n):
            a_template = self._rng.choice(agents)
            category = _category_id(a_template)
            tokens = self._rng.choice([500, 1000, 2000, 5000])
            # Floor price so bids with near-zero seed min_price still have
            # a meaningful dollar value (no $0.00 bids in the feed).
            base_price = max(a_template.min_price, 0.0005)
            price_ceiling = base_price * self._rng.uniform(1.05, 1.8)
            deposit = int(tokens * price_ceiling * 1_000_000)
            min_tier = self._rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
            # Narrative — a real task + a real-looking buyer name
            task = self._rng.choice(self.TASK_DESCRIPTIONS.get(category, ["general task"]))
            buyer_name, buyer_wallet = self._rng.choice(self.BUYER_PERSONAS)
            # Normalize wallet to valid hex (replace junk chars with hex)
            clean_wallet = "0x" + "".join(
                c if c in "0123456789abcdefABCDEF" else format(ord(c) % 16, "x")
                for c in buyer_wallet[2:]
            )[:40].lower()
            # Sequential bid id (looks like "BID-4873") not "LIVE-2-31"
            bid_num = self._event_id + 1
            bid = AuctionBid(
                on_chain_bid_id=f"BID-{bid_num:04d}",
                user=clean_wallet,
                deposit_amount=deposit,
                token_budget=tokens,
                max_price_per_token=int(price_ceiling * 1_000_000),
                category_id=category,
                min_tier=min_tier,
                expires_at=self.sim_clock + self._rng.choice([15*60, 30*60, 2*3600]),
                settled=False, cancelled=False,
            )
            db.session.add(bid)
            self._recent_bids.append((self.sim_clock, category))
            self._log_event(
                "bid_post", None,
                f"{buyer_name} posted BID-{bid_num:04d}: \"{task}\" · {tokens} tokens · T{min_tier}+",
                amount=deposit / 1_000_000,
                meta={"bidId": f"BID-{bid_num:04d}", "tokens": tokens, "minTier": min_tier,
                      "buyerName": buyer_name, "task": task},
            )
            created += 1
        return created

    def _match_bids(self, agents) -> int:
        # Cap matches per tick so a backlog doesn't empty into the feed all
        # at once. Fewer concurrent claims means a calmer, more readable stream.
        open_bids = (
            AuctionBid.query
            .filter_by(settled=False, cancelled=False)
            .filter(AuctionBid.expires_at > self.sim_clock)
            .order_by(AuctionBid.id.desc())
            .limit(1)  # at most one claim per tick keeps the pace natural
            .all()
        )
        matched = 0
        for bid in open_bids:
            # Find qualifying agents: category match, tier gate, accepting, not banned,
            # capacity available, price ceiling respected.
            candidates = []
            for a in agents:
                if _category_id(a) != bid.category_id:
                    continue
                p = db.session.get(OnchainProfile, a.id)
                if not p or p.banned or not p.accepting_work:
                    continue
                if p.tier < bid.min_tier:
                    continue
                if len(self._active[a.id]) >= self._capacity[a.id]:
                    continue
                if int(a.min_price * 1_000_000) > bid.max_price_per_token:
                    continue
                candidates.append((a, p))
            if not candidates:
                continue
            # Clearing rule: highest tier first, then highest reputation, then
            # lowest price. This mirrors how a real buyer would pick among
            # qualified bids — quality before bargain-hunting.
            candidates.sort(key=lambda t: (-t[1].tier, -t[1].score, t[0].min_price))
            agent, profile = candidates[0]
            # Kick off session
            price = agent.min_price * self._rng.uniform(1.0, 1.4)
            if price * 1_000_000 > bid.max_price_per_token:
                price = bid.max_price_per_token / 1_000_000
            deposit = int(bid.token_budget * price * 1_000_000)
            duration = self._rng.randint(90, 600)  # 1.5–10 sim minutes
            # Outcome lottery
            roll = self._rng.random()
            outcome = "settle" if roll < 0.92 else "refund" if roll < 0.97 else "incident"
            session = {
                "start": self.sim_clock,
                "end": self.sim_clock + duration,
                "tokens": bid.token_budget,
                "price": price,
                "deposit": deposit,
                "buyer": bid.user,
                "outcome": outcome,
                "bid_id": bid.on_chain_bid_id,
            }
            self._active[agent.id].append(session)
            # Log deposit tx
            tx_nonce = self._event_id
            db.session.add(ChainTransaction(
                tx_hash=_mock_tx_hash(agent.id, 10_000 + tx_nonce),
                block_number=2_000_000 + self.tick_count * 100 + tx_nonce,
                ts=self.sim_clock,
                kind="deposit",
                agent_id=agent.id,
                from_addr=bid.user,
                to_addr=ADDR_ESCROW,
                amount_usdc=deposit,
                meta=json.dumps({"tokenBudget": bid.token_budget, "pricePerToken": round(price, 6), "bidId": bid.on_chain_bid_id}),
            ))
            bid.settled = True
            bid.tx_hash = _mock_tx_hash(agent.id, 10_000 + tx_nonce)
            self._log_event(
                "bid_claim", agent.id,
                f"{agent.name} claimed {bid.on_chain_bid_id} · {bid.token_budget} tokens @ ${price:.4f}/tok (T{profile.tier})",
                amount=deposit / 1_000_000,
                meta={"bidId": bid.on_chain_bid_id, "duration": duration, "outcome": outcome},
            )
            matched += 1
        return matched

    def _progress_sessions(self, agents) -> None:
        for agent in agents:
            still: list[dict] = []
            profile = db.session.get(OnchainProfile, agent.id)
            if not profile:
                still = self._active[agent.id]
                self._active[agent.id] = still
                continue
            for s in self._active[agent.id]:
                if self.sim_clock < s["end"]:
                    still.append(s)
                    continue
                # Completed — resolve by outcome
                if s["outcome"] == "settle":
                    tokens_used = int(s["tokens"] * self._rng.uniform(0.75, 1.0))
                    paid = int(tokens_used * s["price"] * 1_000_000)
                    refund = s["deposit"] - paid
                    db.session.add(ChainTransaction(
                        tx_hash=_mock_tx_hash(agent.id, 20_000 + self._event_id),
                        block_number=2_000_000 + self.tick_count * 100 + self._event_id,
                        ts=self.sim_clock,
                        kind="settle",
                        agent_id=agent.id,
                        from_addr=ADDR_ESCROW,
                        to_addr=self._wallet_of(profile),
                        amount_usdc=paid,
                        meta=json.dumps({"tokensUsed": tokens_used, "refund": refund, "buyer": s["buyer"], "bidId": s["bid_id"]}),
                    ))
                    # Reputation update
                    points = min(MAX_POINTS_PER_TASK, max(1, paid // VOLUME_PER_POINT))
                    profile.score = min(MAX_SCORE, profile.score + points)
                    profile.tasks_completed += 1
                    # Retier
                    profile.tier = _tier_for(profile.score, profile.tasks_completed)
                    agent.tasks_completed = (agent.tasks_completed or 0) + 1
                    self._log_event(
                        "settle", agent.id,
                        f"{agent.name} settled {tokens_used}/{s['tokens']} tokens · +{points} score → {profile.score} (T{profile.tier})",
                        amount=paid / 1_000_000,
                        meta={"tokensUsed": tokens_used, "score": profile.score, "tier": profile.tier},
                    )
                    # A2A: flagship agents fire sub-agent payments after settling
                    self._fire_a2a_subagent_calls(agent, profile, tokens_used, s)
                elif s["outcome"] == "refund":
                    db.session.add(ChainTransaction(
                        tx_hash=_mock_tx_hash(agent.id, 30_000 + self._event_id),
                        block_number=2_000_000 + self.tick_count * 100 + self._event_id,
                        ts=self.sim_clock,
                        kind="refund",
                        agent_id=agent.id,
                        from_addr=ADDR_ESCROW,
                        to_addr=s["buyer"],
                        amount_usdc=s["deposit"],
                        meta=json.dumps({"reason": "session cancelled", "buyer": s["buyer"]}),
                    ))
                    self._log_event(
                        "refund", agent.id,
                        f"{agent.name} refunded {s['buyer'][:10]}… ({s['tokens']} tokens)",
                        amount=s["deposit"] / 1_000_000,
                    )
                else:  # incident → slash
                    self._do_slash(agent, profile, s)
                    # Refund buyer too
                    db.session.add(ChainTransaction(
                        tx_hash=_mock_tx_hash(agent.id, 40_000 + self._event_id),
                        block_number=2_000_000 + self.tick_count * 100 + self._event_id,
                        ts=self.sim_clock,
                        kind="refund",
                        agent_id=agent.id,
                        from_addr=ADDR_ESCROW,
                        to_addr=s["buyer"],
                        amount_usdc=s["deposit"],
                        meta=json.dumps({"reason": "task failed — slashed", "buyer": s["buyer"]}),
                    ))
            self._active[agent.id] = still

    def _do_slash(self, agent: Agent, profile: OnchainProfile, session: dict) -> None:
        # Flagship composable agents are protected from permanent ban so the
        # A2A story stays intact across long-running demos. They still take
        # the stake + score hit.
        try:
            from app import A2A_WORKFLOWS
            is_flagship = agent.id in A2A_WORKFLOWS
        except Exception:
            is_flagship = False
        profile.stake_incident_count += 1
        idx = min(profile.stake_incident_count, 3) - 1
        pct = SLASH_PCT[idx]
        amount = profile.staked_amount * pct // 100
        profile.staked_amount -= amount
        to_user = amount * SLASH_SPLIT_USER_BPS // 10_000
        if profile.stake_incident_count >= 3 and not is_flagship:
            profile.banned = True
            profile.accepting_work = False
        elif is_flagship and profile.stake_incident_count >= 3:
            # Reset to 2 so one more slash won't immediately re-ban
            profile.stake_incident_count = 2
        # Score penalty
        profile.score = max(0, profile.score - 30)
        profile.rep_incident_count += 1
        profile.tier = _tier_for(profile.score, profile.tasks_completed)
        db.session.add(ChainTransaction(
            tx_hash=_mock_tx_hash(agent.id, 50_000 + self._event_id),
            block_number=2_000_000 + self.tick_count * 100 + self._event_id,
            ts=self.sim_clock,
            kind="slash",
            agent_id=agent.id,
            from_addr=ADDR_REP,
            to_addr=ADDR_STAKING,
            amount_usdc=amount,
            meta=json.dumps({
                "incidentNumber": profile.stake_incident_count,
                "slashPct": pct,
                "toUser": to_user,
                "toInsurance": amount - to_user,
                "banned": profile.banned,
                "buyer": session["buyer"],
            }),
        ))
        banned_tag = " ⚠ BANNED" if profile.banned else ""
        self._log_event(
            "slash", agent.id,
            f"{agent.name} slashed {pct}% · {amount/1_000_000:.2f} USDC · score→{profile.score}{banned_tag}",
            amount=amount / 1_000_000,
            meta={"incidentNumber": profile.stake_incident_count, "banned": profile.banned},
        )

    # ── Agent-to-Agent: a primary agent hires sub-agents after settling ────

    def _fire_a2a_subagent_calls(self, primary_agent: Agent, primary_profile: OnchainProfile,
                                  tokens_used: int, session: dict,
                                  force_all: bool = False) -> None:
        """When a composable agent settles its own work, it programmatically
        pays sub-agents per A2A_WORKFLOWS. No human approval; pure agentic
        commerce on Avalanche.

        Each trigger fires:
          - a2a_hire  tx:  primary wallet → escrow (sub-agent deposit)
          - a2a_settle tx: escrow → sub-agent wallet
        Sub-agent's score + task count bump. Primary earns per-call fee split
        by eating it out of its own settle.
        """
        try:
            from app import A2A_WORKFLOWS
        except Exception:
            return
        wf = A2A_WORKFLOWS.get(primary_agent.id)
        if not wf or not wf.get("composable"):
            return
        sub_agents = wf.get("sub_agents", [])
        triggers = wf.get("trigger_rules", [])
        if not sub_agents:
            return

        for trigger in triggers:
            # Each trigger has a `calls` name; resolve to a sub_agent entry
            target_name = trigger.get("calls")
            sub_spec = next((s for s in sub_agents if s.get("name") == target_name), None)
            if not sub_spec:
                continue
            # Fire probability: "Always -..." triggers always fire; others 55%
            # unless force_all=True (demo button wants the full pipeline).
            always = "always" in (trigger.get("condition") or "").lower()
            if not always and not force_all and self._rng.random() > 0.55:
                continue

            sub_id = int(sub_spec["id"])
            sub_agent = db.session.get(Agent, sub_id)
            sub_profile = db.session.get(OnchainProfile, sub_id)
            if not sub_agent or not sub_profile:
                continue
            if sub_profile.banned:
                # Sub-agents in A2A_WORKFLOWS are first-class demo citizens;
                # auto-rehab them so the pipeline never goes dark.
                sub_profile.banned = False
                sub_profile.accepting_work = True
                sub_profile.stake_incident_count = max(0, sub_profile.stake_incident_count - 1)
                sub_profile.staked_amount = max(sub_profile.staked_amount, 750 * 1_000_000)
                sub_profile.score = max(sub_profile.score, 700)

            # Fee: caller-driven price-per-token when provided, otherwise
            # a random draw from this sub-agent's published cost band.
            override = session.get("price_per_token_override")
            if override and override > 0:
                per_token = float(override)
            else:
                cost_lo = float(sub_spec.get("est_cost_low", 0.003))
                cost_hi = float(sub_spec.get("est_cost_high", 0.01))
                per_token = self._rng.uniform(cost_lo, cost_hi)
            # For sub-agents billed per-minute, use a fixed 2-3 min engagement
            if sub_spec.get("billing") == "per_minute":
                amount_usdc = per_token * self._rng.uniform(2, 4)
            else:
                amount_usdc = per_token * tokens_used
            amount_micro = int(amount_usdc * 1_000_000)
            if amount_micro <= 0:
                continue

            # Deposit tx: primary agent's wallet sends into escrow
            deposit_hash = _mock_tx_hash(primary_agent.id, 60_000 + self._event_id)
            db.session.add(ChainTransaction(
                tx_hash=deposit_hash,
                block_number=2_000_000 + self.tick_count * 100 + self._event_id,
                ts=self.sim_clock, kind="a2a_hire",
                agent_id=sub_id,
                from_addr=primary_profile.wallet_address,
                to_addr=ADDR_ESCROW,
                amount_usdc=amount_micro,
                meta=json.dumps({
                    "primaryAgentId": primary_agent.id,
                    "primaryAgentName": primary_agent.name,
                    "subAgentName": sub_agent.name,
                    "trigger": trigger.get("condition"),
                    "bidId": session.get("bid_id"),
                }),
            ))
            self._log_event(
                "a2a_hire", primary_agent.id,
                f"{primary_agent.name} → {sub_agent.name} ({trigger.get('condition')})",
                amount=amount_usdc,
                meta={"subAgentId": sub_id, "subAgentName": sub_agent.name,
                      "primaryId": primary_agent.id,
                      "tokens": tokens_used, "billing": sub_spec.get("billing")},
            )

            # Settle tx: sub-agent receives funds, score + task count bump
            settle_hash = _mock_tx_hash(sub_id, 70_000 + self._event_id)
            db.session.add(ChainTransaction(
                tx_hash=settle_hash,
                block_number=2_000_000 + self.tick_count * 100 + self._event_id + 1,
                ts=self.sim_clock, kind="a2a_settle",
                agent_id=sub_id,
                from_addr=ADDR_ESCROW,
                to_addr=sub_profile.wallet_address,
                amount_usdc=amount_micro,
                meta=json.dumps({
                    "primaryAgentId": primary_agent.id,
                    "primaryAgentName": primary_agent.name,
                    "subAgentName": sub_agent.name,
                }),
            ))
            # Rep bump for the sub-agent (smaller than direct-user settles)
            points = min(MAX_POINTS_PER_TASK // 2 or 1,
                         max(1, amount_micro // VOLUME_PER_POINT))
            sub_profile.score = min(MAX_SCORE, sub_profile.score + points)
            sub_profile.tasks_completed += 1
            sub_profile.tier = _tier_for(sub_profile.score, sub_profile.tasks_completed)
            sub_agent.tasks_completed = (sub_agent.tasks_completed or 0) + 1
            self._log_event(
                "a2a_settle", sub_id,
                f"{sub_agent.name} got paid by {primary_agent.name} · +{points} score → {sub_profile.score}",
                amount=amount_usdc,
                meta={"primaryId": primary_agent.id,
                      "primaryName": primary_agent.name,
                      "score": sub_profile.score, "tier": sub_profile.tier},
            )

    def _snapshot_prices(self, agents) -> None:
        for a in agents:
            util = min(1.0, len(self._active[a.id]) / max(1, self._capacity[a.id]))
            # Demand ≈ recent bids in the last 15 sim-min / 15 (rough normalization)
            cutoff = self.sim_clock - 15 * 60
            recent = sum(1 for t, _ in self._recent_bids if t >= cutoff)
            demand = min(1.0, recent / 15.0)
            now_dt = datetime.fromtimestamp(self.sim_clock, tz=timezone.utc)
            surge = compute_surge(util, demand, now_dt)
            price = round(a.min_price * surge, 6)
            if price > a.max_price:
                price = a.max_price
            db.session.add(PricePoint(
                agent_id=a.id, ts=self.sim_clock,
                price_per_token=price, utilization=round(util, 3), surge=surge,
            ))
            # Also persist onto Agent row so /marketplace shows live surge.
            # Surging means this specific agent is actually in demand, not
            # that UTC peak hours are adding a flat time-of-day bonus.
            a.current_price = price
            a.surge_multiplier = surge
            # "Surging" is rare on purpose: the agent must have real pressure
            # (util >70% OR demand >60%) AND the clearing price must be
            # meaningfully above base (1.3×+). Keeps the leaderboard honest.
            a.surge_active = (util > 0.7 or demand > 0.6) and surge > 1.3

    def _apply_decay(self, agents) -> None:
        for a in agents:
            p = db.session.get(OnchainProfile, a.id)
            if not p:
                continue
            elapsed = self.sim_clock - (p.last_decay_ts or self.sim_clock)
            periods = elapsed // DECAY_PERIOD
            if not periods:
                continue
            if p.score > DECAY_TARGET:
                p.score = max(DECAY_TARGET, p.score - int(periods))
            elif p.score < DECAY_TARGET:
                p.score = min(DECAY_TARGET, p.score + int(periods))
            p.last_decay_ts = self.sim_clock
            p.tier = _tier_for(p.score, p.tasks_completed)

    def _expire_bids(self) -> None:
        stale = (
            AuctionBid.query
            .filter_by(settled=False, cancelled=False)
            .filter(AuctionBid.expires_at <= self.sim_clock)
            .all()
        )
        for b in stale:
            b.cancelled = True
            self._log_event(
                "bid_cancel", None,
                f"Bid {b.on_chain_bid_id} expired unmatched",
                meta={"bidId": b.on_chain_bid_id},
            )

    # ── helpers ───────────────────────────────────────────────────────────

    def _poisson(self, lam: float) -> int:
        L = 2.718281828 ** -lam
        k = 0
        p = 1.0
        while True:
            k += 1
            p *= self._rng.random()
            if p <= L:
                return k - 1

    def _wallet_of(self, profile: OnchainProfile) -> str:
        return profile.wallet_address

    def _log_event(self, kind: str, agent_id: int | None, message: str, *, amount: float = 0.0, meta: dict | None = None) -> None:
        self._event_id += 1
        # Spread events across the prior sim-minute so many events from the
        # same tick don't all share an identical HH:MM:SS timestamp. The
        # jitter uses event_id as an index so ordering is preserved.
        jitter = (self._event_id * 7) % 57
        ev = SimEvent(
            id=self._event_id, ts=self.sim_clock - jitter, real_ts=time.time(),
            kind=kind, agent_id=agent_id, message=message,
            amount_usdc=amount, meta=meta or {},
        )
        self.events.append(ev)


# ── module-level singleton accessor ──────────────────────────────────────────

_engine: SimulationEngine | None = None


def get_engine(app=None) -> SimulationEngine:
    global _engine
    if _engine is None:
        if app is None:
            raise RuntimeError("Engine not initialized — call get_engine(app) first")
        _engine = SimulationEngine(app)
    return _engine
