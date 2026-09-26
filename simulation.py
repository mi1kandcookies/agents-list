"""
simulation.py — full production-grade on-chain simulation.

When FACILITATOR_PRIVATE_KEY / GATEKEEPER_PRIVATE_KEY are not set (demo mode),
this module replaces the live-chain reads with a deterministic, persistent
simulation backed by the DB. It mirrors the contract surface 1:1 so the UI
and API callers see the same shapes regardless of mode.

Core mechanics:
  - Surge pricing:   surge = clamp(1 + 0.5·util + 0.3·demand + time_factor, 1.0, 3.0)
  - Tier gates:      T2 ≥ score 700 + 50 tasks · T3 ≥ score 900 + 200 tasks
  - Slashing:        25/75/100% of stake on incidents 1/2/3; ban on 3rd
  - Stake tiers:     100 / 500 / 2000 USDC minimums
  - Auction window:  5m quick · 15m default · 30m standard · 2h batch (max 7d)
  - Clearing:        first-qualifying agent (mirrors AuctionMarket.claimBid)
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from extensions import db
from models import (
    Agent, OnchainProfile, ChainTransaction, PricePoint, AuctionBid,
    Order, Payout,
)


# ── Economic constants (match Solidity) ──────────────────────────────────────

START_SCORE = 500
MAX_SCORE = 1000
DECAY_TARGET = 500
DECAY_PERIOD = 7 * 24 * 3600          # 1 pt / week toward 500
VOLUME_PER_POINT = 1_000_000          # 1 USDC in micro-units
MAX_POINTS_PER_TASK = 10

TIER1_MIN = 100 * 1_000_000
TIER2_MIN = 500 * 1_000_000
TIER3_MIN = 2000 * 1_000_000

SLASH_PCT = (25, 75, 100)             # incidents 1,2,3
SLASH_SPLIT_USER_BPS = 6000            # 60%
SLASH_SPLIT_INSURANCE_BPS = 4000       # 40%

AUCTION_WINDOWS = {
    "quick":    5 * 60,
    "default":  15 * 60,
    "standard": 30 * 60,
    "batch":    2 * 3600,
}
AUCTION_MIN = 5 * 60
AUCTION_MAX = 7 * 24 * 3600

SURGE_MIN = 1.0
SURGE_MAX = 3.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rng_for_agent(agent_id: int) -> random.Random:
    """Deterministic RNG so repeated seeding produces identical data."""
    return random.Random(f"agenthire-sim-{agent_id}")


def _mock_wallet(agent_id: int) -> str:
    """Realistic-looking 0x wallet address seeded from agent id."""
    h = hashlib.sha256(f"agent-wallet-{agent_id}".encode()).hexdigest()
    return "0x" + h[:40]


def _mock_tx_hash(agent_id: int, nonce: int) -> str:
    h = hashlib.sha256(f"tx-{agent_id}-{nonce}".encode()).hexdigest()
    return "0x" + h


def _tier_for(score: int, tasks: int) -> int:
    if score >= 900 and tasks >= 200:
        return 3
    if score >= 700 and tasks >= 50:
        return 2
    return 1


def _stake_target(score: int, tasks: int) -> int:
    """Agents stake in line with their tier — no one stakes 2kUSDC at 500 rep."""
    tier = _tier_for(score, tasks)
    if tier == 3:
        return 2500 * 1_000_000
    if tier == 2:
        return 750 * 1_000_000
    return 150 * 1_000_000


# ── Surge pricing ────────────────────────────────────────────────────────────

def compute_surge(
    utilization: float,
    demand: float,
    now_utc: datetime | None = None,
) -> float:
    """
    Surge formula (off-chain price signal):
      surge = 1 + 0.5·util + 0.3·demand + time_factor
      time_factor = +0.2 during peak (9am–5pm UTC), −0.1 off-peak
      clamped [1.0, 3.0]

    `utilization` ∈ [0,1] — active sessions / agent's hourly capacity.
    `demand` ∈ [0,1] — (open bids matching agent) / rolling window.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    hour = now_utc.hour
    time_factor = 0.2 if 9 <= hour < 17 else -0.1
    raw = 1.0 + 0.5 * utilization + 0.3 * demand + time_factor
    return max(SURGE_MIN, min(SURGE_MAX, round(raw, 3)))


def current_price(agent: Agent, *, utilization: float = 0.0, demand: float = 0.0) -> dict:
    """Return the surge-adjusted price envelope for an agent."""
    base = agent.min_price
    surge = compute_surge(utilization, demand)
    price = round(base * surge, 6)
    # Respect agent's own ceiling
    if price > agent.max_price:
        price = agent.max_price
        surge = round(price / base, 3) if base > 0 else 1.0
    return {
        "agentId": agent.id,
        "minPrice": agent.min_price,
        "maxPrice": agent.max_price,
        "currentPrice": price,
        "surgeMultiplier": surge,
        "surgeActive": surge > 1.05,
        "utilization": round(utilization, 3),
        "demand": round(demand, 3),
    }


# ── Auction window helper ────────────────────────────────────────────────────

def resolve_auction_window(window: str | int | None) -> int:
    """Return seconds for a named window or validate a raw seconds value."""
    if window is None:
        return AUCTION_WINDOWS["default"]
    if isinstance(window, str):
        key = window.lower()
        if key in AUCTION_WINDOWS:
            return AUCTION_WINDOWS[key]
        try:
            window = int(window)
        except ValueError:
            return AUCTION_WINDOWS["default"]
    return max(AUCTION_MIN, min(AUCTION_MAX, int(window)))


# ── Slashing simulator (mirrors StakingSlashing.slash) ───────────────────────

def apply_slash(profile: OnchainProfile) -> dict:
    """
    Increment the profile's stake incident count and burn the correct slice.
    Returns {slashedUSDC, toUser, toInsurance, banned}.
    Caller is responsible for committing + logging a ChainTransaction.
    """
    if profile.banned or profile.staked_amount == 0:
        return {"slashedUSDC": 0, "toUser": 0, "toInsurance": 0, "banned": profile.banned}
    profile.stake_incident_count += 1
    pct = SLASH_PCT[min(profile.stake_incident_count, 3) - 1]
    amount = profile.staked_amount * pct // 100
    profile.staked_amount -= amount
    to_user = amount * SLASH_SPLIT_USER_BPS // 10_000
    to_insurance = amount - to_user
    if profile.stake_incident_count >= 3:
        profile.banned = True
        profile.accepting_work = False
    return {
        "slashedUSDC": amount,
        "toUser": to_user,
        "toInsurance": to_insurance,
        "banned": profile.banned,
    }


# ── Simulation seeders ───────────────────────────────────────────────────────

def _build_profile(agent: Agent) -> OnchainProfile:
    rng = _rng_for_agent(agent.id)
    # Seed score roughly from agent's star rating — a 4.8 star agent lives
    # near the top of the band; a 3.5 lives in the middle.
    base_from_rating = int(500 + (agent.rating - 4.0) * 200)
    score = max(400, min(1000, base_from_rating + rng.randint(-40, 40)))
    tasks = agent.tasks_completed or rng.randint(800, 8000)
    tier = _tier_for(score, tasks)
    stake = _stake_target(score, tasks)
    # Most agents are clean; ~15% have 1 incident; ~3% have 2
    roll = rng.random()
    rep_inc = 2 if roll < 0.03 else 1 if roll < 0.18 else 0
    stake_inc = 1 if rep_inc >= 1 and rng.random() < 0.4 else 0

    now = int(time.time())
    return OnchainProfile(
        agent_id=agent.id,
        wallet_address=_mock_wallet(agent.id),
        score=score,
        tier=tier,
        tasks_completed=tasks,
        rep_incident_count=rep_inc,
        last_decay_ts=now - rng.randint(0, DECAY_PERIOD),
        staked_amount=stake,
        stake_incident_count=stake_inc,
        banned=False,
        accepting_work=True,
    )


def _generate_transactions(agent: Agent, profile: OnchainProfile, count: int = 120) -> list[ChainTransaction]:
    """Generate a realistic backfill of activity for the last ~30 days."""
    rng = _rng_for_agent(agent.id)
    now = int(time.time())
    span = 30 * 24 * 3600
    txs: list[ChainTransaction] = []

    # Seed the record with the register + initial stake events up front.
    txs.append(ChainTransaction(
        tx_hash=_mock_tx_hash(agent.id, 0),
        block_number=1_000_000 + agent.id * 100,
        ts=now - span - rng.randint(3600, 7200),
        kind="register",
        agent_id=agent.id,
        from_addr=profile.wallet_address,
        to_addr="0x6B71b84Fa3C313ccC43D63A400Ab47e6A0d4BCbB",  # AgentRegistry
        amount_usdc=0,
        meta=json.dumps({"name": agent.name, "endpoint": f"https://agents.agenthire.io/{agent.id}"}),
    ))
    txs.append(ChainTransaction(
        tx_hash=_mock_tx_hash(agent.id, 1),
        block_number=1_000_000 + agent.id * 100 + 1,
        ts=now - span,
        kind="stake",
        agent_id=agent.id,
        from_addr=profile.wallet_address,
        to_addr="0xfc942b4d1Eb363F25886b3F5935394BD4932B896",  # StakingSlashing
        amount_usdc=profile.staked_amount,
        meta=json.dumps({"tier": profile.tier}),
    ))

    # Sessions — deposits that later settle (or refund on cancel).
    for i in range(count):
        ts = now - rng.randint(0, span)
        tokens = rng.choice([250, 500, 1000, 2000, 5000])
        price = agent.min_price * rng.uniform(1.0, 1.8)
        amount = int(tokens * price * 1_000_000)
        buyer = "0x" + hashlib.sha256(f"user-{agent.id}-{i}".encode()).hexdigest()[:40]

        txs.append(ChainTransaction(
            tx_hash=_mock_tx_hash(agent.id, i * 2 + 2),
            block_number=1_000_000 + agent.id * 100 + i + 2,
            ts=ts,
            kind="deposit",
            agent_id=agent.id,
            from_addr=buyer,
            to_addr="0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2",  # EscrowPayment
            amount_usdc=amount,
            meta=json.dumps({"tokenBudget": tokens, "pricePerToken": round(price, 6)}),
        ))
        # 95% settle, 5% cancel
        if rng.random() < 0.95:
            tokens_used = int(tokens * rng.uniform(0.75, 1.0))
            paid = int(tokens_used * price * 1_000_000)
            refund = amount - paid
            txs.append(ChainTransaction(
                tx_hash=_mock_tx_hash(agent.id, i * 2 + 3),
                block_number=1_000_000 + agent.id * 100 + i + 3,
                ts=ts + rng.randint(60, 1800),
                kind="settle",
                agent_id=agent.id,
                from_addr="0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2",
                to_addr=profile.wallet_address,
                amount_usdc=paid,
                meta=json.dumps({"tokensUsed": tokens_used, "refund": refund, "buyer": buyer}),
            ))
        else:
            txs.append(ChainTransaction(
                tx_hash=_mock_tx_hash(agent.id, i * 2 + 3),
                block_number=1_000_000 + agent.id * 100 + i + 3,
                ts=ts + rng.randint(300, 3600),
                kind="refund",
                agent_id=agent.id,
                from_addr="0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2",
                to_addr=buyer,
                amount_usdc=amount,
                meta=json.dumps({"reason": "session cancelled"}),
            ))

    # Incidents → slash events for any agent that actually has stake incidents.
    for i in range(profile.stake_incident_count):
        amt = profile.staked_amount * SLASH_PCT[i] // 100 if i < 3 else 0
        txs.append(ChainTransaction(
            tx_hash=_mock_tx_hash(agent.id, 900 + i),
            block_number=1_000_000 + agent.id * 100 + 900 + i,
            ts=now - rng.randint(5 * 24 * 3600, 20 * 24 * 3600),
            kind="slash",
            agent_id=agent.id,
            from_addr="0x40ef89Ce1E248Df00AF6Dc37f96BBf92A9Bf603A",  # ReputationContract
            to_addr="0xfc942b4d1Eb363F25886b3F5935394BD4932B896",
            amount_usdc=amt,
            meta=json.dumps({
                "incidentNumber": i + 1,
                "severity": 2,
                "slashPct": SLASH_PCT[i] if i < 3 else 100,
            }),
        ))

    return txs


def _generate_price_points(agent: Agent, profile: OnchainProfile, count: int = 288) -> list[PricePoint]:
    """
    One point every ~5 minutes for the last 24 hours (288 samples).
    Utilization follows a diurnal curve with noise; surge and price follow.
    """
    rng = _rng_for_agent(agent.id)
    now = int(time.time())
    points: list[PricePoint] = []
    for i in range(count):
        offset = (count - i) * 5 * 60
        ts = now - offset
        hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        # Diurnal utilization curve: low overnight UTC, peaks midday
        diurnal = 0.35 + 0.45 * max(0.0, 1 - abs(hour - 13) / 8)
        util = max(0.0, min(1.0, diurnal + rng.uniform(-0.12, 0.12)))
        demand = max(0.0, min(1.0, diurnal * 0.7 + rng.uniform(-0.08, 0.15)))
        surge = compute_surge(util, demand, datetime.fromtimestamp(ts, tz=timezone.utc))
        price = round(agent.min_price * surge, 6)
        if price > agent.max_price:
            price = agent.max_price
        points.append(PricePoint(
            agent_id=agent.id, ts=ts, price_per_token=price,
            utilization=round(util, 3), surge=surge,
        ))
    return points


def _generate_bids(agents: list[Agent]) -> list[AuctionBid]:
    """A handful of currently-open auction bids."""
    rng = random.Random("agenthire-bids")
    bids: list[AuctionBid] = []
    now = int(time.time())
    for i in range(8):
        a = rng.choice(agents)
        tokens = rng.choice([500, 1000, 2000, 5000])
        price = a.min_price * rng.uniform(1.1, 1.6)
        deposit = int(tokens * price * 1_000_000)
        ceiling = int(price * 1.2 * 1_000_000)
        bids.append(AuctionBid(
            on_chain_bid_id=f"SIM-{1000 + i}",
            user="0x" + hashlib.sha256(f"bidder-{i}".encode()).hexdigest()[:40],
            deposit_amount=deposit,
            token_budget=tokens,
            max_price_per_token=ceiling,
            category_id=rng.randint(0, 5),
            min_tier=rng.choice([1, 1, 2, 2, 3]),
            expires_at=now + rng.choice([5 * 60, 15 * 60, 30 * 60, 2 * 3600]),
            settled=False,
            cancelled=False,
        ))
    return bids


def seed_simulation(app) -> None:
    """
    Idempotent top-up of simulation data. Runs after the base seed_db()
    so Agent rows already exist.
    """
    with app.app_context():
        agents = Agent.query.all()
        if not agents:
            return

        created_profiles = 0
        created_txs = 0
        created_points = 0
        created_bids = 0

        for agent in agents:
            if not db.session.get(OnchainProfile, agent.id):
                db.session.add(_build_profile(agent))
                created_profiles += 1
        db.session.commit()

        # Refresh and backfill transactions / price history
        for agent in agents:
            profile = db.session.get(OnchainProfile, agent.id)
            if ChainTransaction.query.filter_by(agent_id=agent.id).count() == 0:
                for tx in _generate_transactions(agent, profile):
                    db.session.add(tx)
                    created_txs += 1
            if PricePoint.query.filter_by(agent_id=agent.id).count() == 0:
                for pt in _generate_price_points(agent, profile):
                    db.session.add(pt)
                    created_points += 1
        db.session.commit()

        if AuctionBid.query.count() == 0:
            for bid in _generate_bids(agents):
                db.session.add(bid)
                created_bids += 1
            db.session.commit()

        app.logger.info(
            "Simulation seeded: %d profiles, %d txs, %d price points, %d bids.",
            created_profiles, created_txs, created_points, created_bids,
        )


# ── Read helpers used by the Flask API when real chain is not configured ────

def get_credit_profile(agent_id: int) -> dict | None:
    p = db.session.get(OnchainProfile, agent_id)
    if not p:
        return None
    # Project score after decay without touching the DB
    now = int(time.time())
    elapsed = now - (p.last_decay_ts or now)
    periods = elapsed // DECAY_PERIOD
    projected = p.score
    if periods:
        if projected > DECAY_TARGET:
            projected = max(DECAY_TARGET, projected - periods)
        elif projected < DECAY_TARGET:
            projected = min(DECAY_TARGET, projected + periods)
    return {
        "score": p.score,
        "tier": p.tier,
        "tasksCompleted": p.tasks_completed,
        "incidentCount": p.rep_incident_count,
        "lastDecayTs": p.last_decay_ts,
        "projectedScore": int(projected),
        "simulated": True,
    }


def get_stake(agent_id: int) -> dict | None:
    p = db.session.get(OnchainProfile, agent_id)
    if not p:
        return None
    return {
        "stakedUSDC": str(p.staked_amount),
        "stakedUSDCDisplay": round(p.staked_amount / 1_000_000, 2),
        "incidentCount": p.stake_incident_count,
        "banned": p.banned,
        "unstakeRequest": {"amount": "0", "availableAt": 0},
        "simulated": True,
    }


def get_full_profile(agent_id: int) -> dict | None:
    """Profile + stake + recent activity count — one-shot for the UI.
    Exposes both 'all activity' revenue and 'real on-chain only' revenue
    so the UI can label numbers honestly."""
    p = db.session.get(OnchainProfile, agent_id)
    if not p:
        return None
    recent_txs = (
        ChainTransaction.query
        .filter_by(agent_id=agent_id)
        .order_by(ChainTransaction.ts.desc())
        .limit(5).all()
    )
    total_revenue = db.session.query(db.func.coalesce(db.func.sum(ChainTransaction.amount_usdc), 0)).filter_by(agent_id=agent_id, kind="settle").scalar()
    real_revenue = db.session.query(db.func.coalesce(db.func.sum(ChainTransaction.amount_usdc), 0)).filter(
        ChainTransaction.agent_id == agent_id,
        ChainTransaction.kind.in_(["settle", "a2a_hire"]),
        ChainTransaction.meta.like('%"real": true%'),
    ).scalar()
    real_tx_count = db.session.query(db.func.count(ChainTransaction.id)).filter(
        ChainTransaction.agent_id == agent_id,
        ChainTransaction.meta.like('%"real": true%'),
    ).scalar() or 0
    return {
        **p.to_dict(),
        "totalRevenueUSDC":     round((total_revenue or 0) / 1_000_000, 2),
        "realRevenueUSDC":      round((real_revenue or 0) / 1_000_000, 4),
        "realTxCount":          int(real_tx_count),
        "recentTransactions":   [tx.to_dict() for tx in recent_txs],
        "simulated":            True,
    }


def get_price_history(agent_id: int, hours: int = 24) -> list[dict]:
    cutoff = int(time.time()) - hours * 3600
    pts = (
        PricePoint.query
        .filter(PricePoint.agent_id == agent_id, PricePoint.ts >= cutoff)
        .order_by(PricePoint.ts.asc())
        .all()
    )
    return [pt.to_dict() for pt in pts]


def get_transactions(
    agent_id: int | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = 50,
    real_only: bool = False,
) -> list[dict]:
    q = ChainTransaction.query
    if agent_id is not None:
        q = q.filter_by(agent_id=agent_id)
    if kinds:
        q = q.filter(ChainTransaction.kind.in_(list(kinds)))
    if real_only:
        q = q.filter(ChainTransaction.meta.like('%"real": true%'))
    q = q.order_by(ChainTransaction.ts.desc()).limit(limit)
    return [tx.to_dict() for tx in q.all()]
