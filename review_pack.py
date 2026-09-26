"""
review_pack.py - generate varied, category-aware reviews for the 125
seeded agents. Idempotent: only adds reviews to agents that have none.

Target: 3-12 reviews per agent, each with its own comment, rating, user
wallet, and date. Pulls from a category-specific comment bank so copy
feels like it comes from actual buyers, not a Mad Libs template.
"""
from __future__ import annotations
import hashlib
import random
from datetime import datetime, timedelta

from extensions import db
from models import Agent, Review


# ── Review bank (positive / mixed / negative) per category ───────────────────
# No em-dashes anywhere. Mixed and negative reviews surface real failure modes
# so ratings don't all cluster at 5 stars.

REVIEW_BANK = {
    "Development": {
        5: [
            "Caught a subtle race condition my own tests missed. Worth the price.",
            "Cleaned up a 4-year-old Flask codebase in one pass. No regressions.",
            "Refactor suggestions were pragmatic, not ivory-tower.",
            "Spotted an unused dependency chain that was pulling in 40MB of garbage.",
            "The diff was small, surgical, and actually shipped.",
            "Found a SQL injection in a helper we'd copy-pasted forever.",
            "Generated test coverage we'd been putting off for months.",
            "Reviewed a 2k-line PR faster than my teammates. Better comments too.",
        ],
        4: [
            "Solid on Python and Go, a bit shaky on our internal DSL.",
            "Great findings, noisy output. Need to tune the verbosity.",
            "Correct on the big issues, missed a couple edge cases.",
            "Handles small PRs well. Larger ones take a few passes.",
            "Nice refactors, but some of the test generation was redundant.",
        ],
        3: [
            "Found half of what our linter already catches. Overlap is real.",
            "Good on obvious bugs. Struggled with our unusual build layout.",
            "Helpful baseline, not yet replacing a senior reviewer.",
        ],
        2: [
            "Kept flagging stylistic nits instead of the actual broken tests.",
            "Hallucinated a function that doesn't exist in our codebase.",
            "Surge pricing hit during our release window. Cost us 2x.",
        ],
    },
    "Data & Analytics": {
        5: [
            "Wrote a window function I would not have thought of. Saved 40 minutes.",
            "Caught a schema drift nobody on the team noticed.",
            "Dashboard scaffolding was clean and actually matched our brand.",
            "Explained the query plan like a senior DE would. Very useful.",
            "Handled a gnarly Snowflake semi-structured join on the first try.",
            "Pipeline design was idempotent, which saved us a backfill.",
            "The cohort analysis spotted a retention cliff we'd missed for weeks.",
        ],
        4: [
            "Great on Snowflake, ok on Postgres, avoid for MySQL.",
            "Correct output, but the initial query was too clever. Had to simplify.",
            "Dashboards look good. Minor color palette override needed.",
        ],
        3: [
            "Got the SQL right but the explanation was generic LLM filler.",
            "Slow on warehouse-scale data. Fine for small tables.",
        ],
        2: [
            "Picked the wrong join order. Query ran 8x slower than our handwritten one.",
            "Missed an obvious NULL handling case. Output was subtly off.",
        ],
    },
    "Content": {
        5: [
            "Nailed the brand voice on the third iteration. Publishable.",
            "Draft was tighter than what our junior writer usually ships.",
            "SEO copy actually ranked. Two positions up after a week.",
            "Social threads had genuine hooks. Not the usual LinkedIn slop.",
            "Newsletter open rate beat our 6-month average by 14%.",
            "Good at matching tone across formats. Impressive consistency.",
        ],
        4: [
            "Solid first draft, needed a human pass for voice polish.",
            "Headlines were decent. Body copy was the stronger part.",
            "Occasionally too corporate. Told it once and it adjusted.",
        ],
        3: [
            "Fine for internal comms. Not sharp enough for marketing yet.",
            "Generic phrasing in the intro. Had to rewrite the lead.",
        ],
        2: [
            "Kept inserting buzzwords our style guide explicitly bans.",
            "Fabricated a customer quote. Big red flag for review usage.",
        ],
    },
    "Finance": {
        5: [
            "Flagged a correlation break before our risk dashboard did.",
            "Tax reconciliation saved us two days of manual work.",
            "Back-test was rigorous and included slippage assumptions.",
            "Explained the option greeks in a memo our PM actually read.",
            "Caught a wallet labeling error in our on-chain report.",
            "Treasury sweep was fast and the audit trail was clean.",
        ],
        4: [
            "Good on DeFi, ok on TradFi, weak on derivatives structuring.",
            "Accurate analysis, but the memo format was a bit academic.",
            "Correct numbers. I still want a human to double-check the narrative.",
        ],
        3: [
            "Fine for daily risk runs. Wouldn't trust it for earnings commentary.",
            "Missed a known regime shift. Had to patch the prompt.",
        ],
        2: [
            "Mis-labeled a major stablecoin flow. Would have skewed the report.",
            "Generated a trade idea that ignored our risk limits entirely.",
        ],
    },
    "Research": {
        5: [
            "Literature review was thorough and every citation checked out.",
            "Turned a fuzzy research question into a methodology proposal.",
            "Traced a disputed claim to the original 1974 paper. Rare skill.",
            "Meta-analysis handled heterogeneity reasonably. Nice touch.",
            "Summary kept the nuance instead of flattening it. Refreshing.",
        ],
        4: [
            "Good on ML research. Shaky when it wandered into clinical.",
            "Accurate, but the formatting took two rounds to match our template.",
        ],
        3: [
            "Got the abstract right, missed the subtler points in the conclusion.",
            "Surface-level summary. Fine for a first pass, not a deep read.",
        ],
        2: [
            "Cited three papers that do not exist. Hallucination risk is real.",
            "Conflated two similar-sounding authors into one attribution.",
        ],
    },
    "Security": {
        5: [
            "Flagged a JWT weakness we'd shipped to prod. Legitimately saved us.",
            "CVE triage removed 80% of the noise our scanner produces.",
            "Threat model started from our actual architecture, not a template.",
            "Found a supply-chain issue in a sub-dependency nobody audits.",
            "Wrote the postmortem draft in a tone our SOC could actually publish.",
            "Smart contract review caught a reentrancy path before deploy.",
        ],
        4: [
            "Solid OWASP scans, less useful on custom business-logic flaws.",
            "Good findings, wordy tickets. Could tighten the severity narrative.",
        ],
        3: [
            "Standard static-analysis output. Useful as a second pair of eyes.",
            "Struggled with our homegrown auth layer. Needed more context.",
        ],
        2: [
            "Missed a high-severity SSRF in a file our team flagged the week prior.",
            "False positives in dependency flagging. Trust dropped fast.",
        ],
    },
    "Automation": {
        5: [
            "Stitched four SaaS tools together with retries and backoff. Works.",
            "Replaced a rickety Zapier chain. Cheaper and more observable.",
            "Turned a vague 'automate this' ask into a working pipeline in 20 min.",
            "Calendar coordination across five teams. No double bookings yet.",
            "Scrape and reformat job is running reliably two weeks in.",
        ],
        4: [
            "Handles the happy path well. Edge cases need hand-holding.",
            "Good glue code, mediocre naming. Spent time cleaning variable names.",
        ],
        3: [
            "Works, but the audit log is thin. Hard to debug when it hiccups.",
            "Fine for internal workflows. Not ready for customer-facing ones.",
        ],
        2: [
            "Retry logic spammed the downstream API. Had to hard-stop.",
            "Missed a webhook signature check. Would have been a security issue.",
        ],
    },
}


def _fake_wallet(agent_id: int, i: int) -> str:
    h = hashlib.sha256(f"reviewer-{agent_id}-{i}".encode()).hexdigest()
    return "0x" + h[:40]


def seed_reviews(app, *, target_min: int = 4, target_max: int = 14) -> int:
    """Add varied reviews for any agent currently under target_min."""
    created = 0
    rng = random.Random(7)
    with app.app_context():
        agents = Agent.query.all()
        for a in agents:
            existing = Review.query.filter_by(agent_id=a.id).count()
            if existing >= target_min:
                continue
            bank = REVIEW_BANK.get(a.category, REVIEW_BANK["Development"])
            # Rating distribution: skew to the agent's overall rating
            base = a.rating or 4.5
            if base >= 4.6:
                weights = {5: 65, 4: 25, 3: 7, 2: 3}
            elif base >= 4.2:
                weights = {5: 45, 4: 35, 3: 15, 2: 5}
            else:
                weights = {5: 25, 4: 35, 3: 25, 2: 15}
            ratings_pool = [r for r, w in weights.items() for _ in range(w)]

            target = rng.randint(target_min, target_max)
            # Track used comments per agent so no duplicates within a page
            used = set()
            for i in range(target - existing):
                rating = rng.choice(ratings_pool)
                # Fallback if the bucket is empty for this rating level
                comments = bank.get(rating) or bank[5]
                # Pick a comment not yet used on this agent
                fresh = [c for c in comments if c not in used]
                if not fresh:
                    fresh = comments
                comment = rng.choice(fresh)
                used.add(comment)

                days_ago = rng.randint(1, 180)
                date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                wallet = _fake_wallet(a.id, existing + i)
                db.session.add(Review(
                    agent_id=a.id,
                    user=wallet[:8] + "…" + wallet[-4:],
                    rating=rating,
                    comment=comment,
                    date=date,
                ))
                created += 1
            # Also sync the Agent.reviews count to the actual count
            new_count = existing + (target - existing)
            a.reviews = new_count
        db.session.commit()
        app.logger.info("Review seeder: created %d reviews across %d agents", created, len(agents))
    return created
