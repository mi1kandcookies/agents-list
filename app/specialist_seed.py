"""
specialist_seed.py - `flask --app wsgi seed-specialists`: list the first-party
specialists in the catalog.

A specialist is a package under specialists/ whose agent.yaml is its private
runtime spec (agentkit.manifest). This seed reads each agent.yaml, never the
package's code, and turns it into a catalog listing plus the operator
manifest the platform hires against. agentkit is imported only when the
command runs, so the web app never loads it.

Where each field comes from (no schema change):
    listing columns   Manifest.public_listing(): name, summary (description),
                      listing description (about), category, tags,
                      capabilities; billing per_milestone with
                      min_price..max_price = pricing.typical_low..typical_high
                      USDC and current_price = typical_low. The model columns
                      show the primary model.
    manifest_json     app.seller.stamp.build_manifest() from
                      agentkit.manifest.operator_fields() (primary model,
                      tool names, capabilities as skills, no MCP servers,
                      spec_hash) plus the task price and the payout address
    track record      sample_track_record(slug), see below

Two prices: the listing range is what one milestone of an engagement costs.
The manifest's price (price_min_micro = price_max_micro) is the flat price of
one paid agent-to-agent task over x402: listing.pricing.task_price_usdc,
otherwise 1 USDC, and never above X402_MAX_PAYMENT_USDC.

A specialist is listed when its agent.yaml parses, uses a catalog category
and per_milestone USDC pricing, and its name is not taken by another
listing. Anything else is reported and left out, and the command exits
non-zero.

Track record: like seed-demo (app/demo_seed.py), these are sample listings.
Each is marked demo_listing, so the UI tags it "Demo listing", and carries an
illustrative rating, review count and job count derived from its slug (the
same numbers on every run). The rating and review count are written only
while the listing has no reviews, so a re-run never overwrites reviews the
platform recorded; the job count never drops below what the listing shows or
below its review count.

Stamping works as in seed-demo. The seed does not stamp by itself; an
operator stamps each manifest with World ID, and ``--dev-stamp`` writes
simulated stamps in development only (never over a real operator stamp).

Addresses: a new listing gets the SCREENING_ADDRESS_MAP entry at its place in
slug order (wrapping around); without the map, a placeholder nobody holds a
key for, so screening refuses every payment (fail closed). After that the
payout in the listing's manifest is kept, whether an operator set it or an
earlier run did, so a re-run never moves a payout; only a listing still on its
placeholder (or with no valid payout) is assigned again, e.g. once a map is
set. The screening address is always the map's entry for the payout, or none,
so a payout the map does not cover is refused by screening.

Idempotent: a listing is ours when its seller is FIRST_PARTY_OPERATOR and it
has no verification entry (/seller/create always files one; this seed never
does). A re-run refreshes the listing text, prices and manifest. An unchanged
spec hashes the same, so a valid stamp survives; a changed one is listed for
re-stamping. The seed never touches another seller's listing, and never
changes an admin's verification, featuring or suspension.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import click
from flask.cli import with_appcontext

FIRST_PARTY_OPERATOR = "Agent's List"
BILLING = "per_milestone"
DEFAULT_TASK_PRICE_MICRO = 1_000_000   # 1 USDC per x402 task
# agentkit provider ids (agentkit.llm.base.PROVIDERS) as a listing names them.
PROVIDER_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google",
                  "openrouter": "OpenRouter", "ollama": "Ollama", "vllm": "vLLM",
                  "openai_compat": "OpenAI-compatible"}
MAX_NAME = 120   # agents.name
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SpecialistProblem(Exception):
    """A specialist that cannot be listed; the message says why."""


@dataclass
class _Plan:
    listing: dict      # Manifest.public_listing()
    manifest: dict     # keyword arguments for app.seller.stamp.build_manifest
    provider: str
    model: str
    screening: str | None
    row: object = None   # our existing listing, if any
    kept: bool = False   # the payout is the one row's manifest already had


def placeholder_payout_address(slug: str) -> str:
    """Deterministic Sepolia payout placeholder. Derived from a hash, so no
    one holds its key; it is never in SCREENING_ADDRESS_MAP by accident."""
    digest = hashlib.sha256(f"agents-list specialist payout:{slug}".encode()).hexdigest()
    return "0x" + digest[:40]


def sample_track_record(slug: str) -> dict:
    """Illustrative track record for a specialist's demo listing, derived from
    the sha256 of its slug so every run gives the same numbers: a rating from
    3.7 to 4.9 in steps of 0.1, 12 to 180 reviews, and enough jobs that 45 to
    85 percent of them left a review."""
    n = int.from_bytes(
        hashlib.sha256(f"agents-list specialist track record:{slug}".encode()).digest(), "big")
    reviews = 12 + (n // 13) % 169
    share = 45 + (n // (13 * 169)) % 41
    return {"rating": (37 + n % 13) / 10, "reviews": reviews,
            "jobs": -(-reviews * 100 // share)}


def task_price_cap_micro(app) -> int:
    """X402_MAX_PAYMENT_USDC in micro-USDC: the most one paid agent-to-agent
    task may cost (the payer's policy refuses more). Raises ValueError when
    the setting is unreadable."""
    from app.engagements.sow import parse_usdc
    return parse_usdc(app.config.get("X402_MAX_PAYMENT_USDC"), "X402_MAX_PAYMENT_USDC")


def _usdc(micro: int) -> str:
    return f"{micro / 1_000_000:g} USDC"


def _plan(m, *, payout: str, screening: str | None, max_task_micro: int) -> _Plan:
    """Everything needed to list ``m``, or SpecialistProblem. Reads the
    package's files for spec_hash but imports none of its code, and writes
    nothing."""
    from agentkit.llm.base import ModelRef
    from agentkit.manifest import operator_fields, task_price_micro
    from app.seller.stamp import build_manifest
    from app.services import CATEGORIES

    listing = m.public_listing()
    pricing = listing["pricing"]
    if len(listing["name"]) > MAX_NAME:
        raise SpecialistProblem(f"name is longer than {MAX_NAME} characters")
    if listing["category"] not in CATEGORIES:
        raise SpecialistProblem(f"category {listing['category']!r} is not a catalog category "
                                f"({', '.join(CATEGORIES)})")
    if pricing["model"] != BILLING or pricing["currency"] != "USDC":
        raise SpecialistProblem(f"pricing must be {BILLING} in USDC, not "
                                f"{pricing['model']} in {pricing['currency']}")
    if not 0 < pricing["typical_low"] <= pricing["typical_high"]:
        raise SpecialistProblem("pricing needs 0 < typical_low <= typical_high")
    task = task_price_micro(m)
    if task is None:
        task = min(DEFAULT_TASK_PRICE_MICRO, max_task_micro)
    elif task > max_task_micro:
        raise SpecialistProblem(f"task_price_usdc {_usdc(task)} is above "
                                f"X402_MAX_PAYMENT_USDC ({_usdc(max_task_micro)})")

    fields = operator_fields(m)
    manifest = dict(fields, price_min_micro=task, price_max_micro=task, payout_address=payout)
    # Validate before any listing is written (the real call adds the AGT id).
    build_manifest(SimpleNamespace(public_id=None), **manifest)
    ref = ModelRef.parse(fields["model"])
    return _Plan(listing=listing, manifest=manifest,
                 provider=PROVIDER_NAMES.get(ref.provider, ref.provider)[:40],
                 model=ref.model[:80], screening=screening)


def _existing(Agent, names) -> dict:
    """name -> our listing of that name, or None when only other sellers'
    listings have it (see the module docstring for what makes one ours)."""
    from app.models import VerificationEntry
    rows = Agent.query.filter(Agent.name.in_(names)).order_by(Agent.id).all() if names else []
    filed = {v.agent_id for v in VerificationEntry.query.filter(
        VerificationEntry.agent_id.in_([r.id for r in rows]))} if rows else set()
    out: dict = {}
    for row in rows:
        if row.seller == FIRST_PARTY_OPERATOR and row.id not in filed:
            out[row.name] = out.get(row.name) or row
        else:
            out.setdefault(row.name, None)
    return out


def _addresses(row, slug: str, pairs, i: int) -> tuple[str, str | None, bool]:
    """(payout, screening address, kept) for the i-th specialist in slug
    order. ``row``'s manifest payout is kept, with the map's screening address
    for it (or none), unless it is missing or still the placeholder; otherwise
    the i-th map pair (wrapping around) or the placeholder is assigned."""
    from app.engagements.service import payee_address
    from app.seller.stamp import current_manifest

    placeholder = placeholder_payout_address(slug)
    if row is not None:
        current = (current_manifest(row) or {}).get("payout_address")
        if not (isinstance(current, str) and _ADDRESS_RE.match(current)):
            current = payee_address(row)
        if current and current.lower() != placeholder:
            current = current.lower()
            return current, dict(pairs).get(current), True
    payout, screening = pairs[i % len(pairs)] if pairs else (placeholder, None)
    return payout, screening, False


def _plans(manifests, pairs, existing, *, max_task_micro) -> tuple[list[_Plan], list]:
    """(plans, problems) for ``manifests`` (in slug order), given
    ``existing`` from _existing()."""
    from agentkit.errors import AgentKitError

    plans, problems, names = [], [], set()
    for i, m in enumerate(manifests):
        row = existing.get(m.name)
        payout, screening, kept = _addresses(row, m.slug, pairs, i)
        try:
            if m.name in names:
                raise SpecialistProblem(f"another specialist is already named {m.name!r}")
            if m.name in existing and row is None:
                raise SpecialistProblem(f"another seller's listing is already named {m.name!r}")
            plan = _plan(m, payout=payout, screening=screening, max_task_micro=max_task_micro)
        except (SpecialistProblem, AgentKitError, ValueError) as exc:   # incl. app ManifestError
            problems.append((m.slug, str(exc)))
        else:
            plan.row, plan.kept = row, kept
            plans.append(plan)
            names.add(m.name)
    return plans, problems


def seed_specialists(db, Agent, *, root: Path | None = None, address_map: str | None = None,
                     dev_stamp: bool = False, max_task_micro: int | None = None) -> dict:
    """Insert or refresh a listing for every specialist under ``root`` (the
    specialists/ folder by default). With ``dev_stamp`` also write simulated
    operator stamps (the caller must refuse outside development). Returns
    counts, the listings written ([(public_id, name, category, rating,
    reviews)]), the problems that kept a specialist out ([(slug or folder,
    message)]), the listings whose stamp no longer matches (names) and the
    mapping mode."""
    from flask import current_app

    from agentkit.registry import list_specialists
    from app.demo_seed import address_pairs
    from app.seller import stamp

    if max_task_micro is None:
        max_task_micro = task_price_cap_micro(current_app)
    pairs = address_pairs(address_map)
    errors: list = []
    manifests = list_specialists(root, errors=errors)
    existing = _existing(Agent, [m.name for m in manifests])
    plans, problems = _plans(manifests, pairs, existing, max_task_micro=max_task_micro)
    problems = [(path.parent.name, msg) for path, msg in errors] + problems

    added = updated = stamped = 0
    listed, restamp = [], []
    for plan in plans:
        listing, row = plan.listing, plan.row
        if row is None:
            row = Agent(name=listing["name"], category=listing["category"], billing=BILLING,
                        seller=FIRST_PARTY_OPERATOR, verified=False, verification_tier="none",
                        featured=False, rating=0.0, reviews=0, tasks_completed=0,
                        seller_rating=0.0, avg_completion_time=" - ")
            db.session.add(row)
            added += 1
        else:
            updated += 1
        pricing = listing["pricing"]
        row.description = listing["summary"]
        row.long_description = listing["description"] or listing["summary"]
        row.category = listing["category"]
        row.billing = BILLING
        row.min_price = float(pricing["typical_low"])
        row.max_price = float(pricing["typical_high"])
        row.current_price = row.min_price
        row.model_provider, row.model_name = plan.provider, plan.model
        if not plan.kept:   # a kept payout is already the row's (or awaits a stamp)
            row.deployer_wallet = row.payout_address = plan.manifest["payout_address"]
        row.screening_address = plan.screening
        row.tags = list(listing["tags"])
        row.capabilities = list(listing["capabilities"])
        track = sample_track_record(listing["slug"])
        row.demo_listing = True
        if not row.reviews:   # never overwrite reviews the platform recorded
            row.rating, row.reviews = track["rating"], track["reviews"]
        row.tasks_completed = max(row.tasks_completed or 0, track["jobs"], row.reviews)
        db.session.flush()  # assigns public_id, which the manifest carries
        current = stamp.save_manifest(row, stamp.build_manifest(row, **plan.manifest))
        real_stamp = row.manifest_stamp_sub and row.manifest_stamp_sub != stamp.DEV_STAMP_SUB
        if dev_stamp and not real_stamp:
            stamp.dev_stamp(row)
            stamped += 1
        elif row.manifest_stamped_at and row.manifest_hash != current:
            restamp.append(row.name)
        listed.append((row.public_id, row.name, row.category, row.rating, row.reviews))
    db.session.commit()
    return {"found": len(manifests) + len(errors), "added": added, "updated": updated,
            "dev_stamped": stamped, "listed": listed, "restamp": restamp,
            "problems": problems, "mapped": bool(pairs), "map_entries": len(pairs)}


@click.command("seed-specialists")
@click.option("--dev-stamp", is_flag=True,
              help="DEVELOPMENT ONLY: also write simulated operator stamps so the specialists "
                   "can be hired (same rules as `flask seed-stamps`).")
@click.option("--root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Folder holding the specialist packages (default: specialists/).")
@with_appcontext
def seed_specialists_command(dev_stamp: bool, root: Path | None):
    """List or refresh the first-party specialists as demo listings (idempotent)."""
    from flask import current_app

    from app.demo_seed import address_pairs
    from app.extensions import db
    from app.models import Agent
    from app.seller.stamp import seed_stamps_refusal
    if dev_stamp:
        refusal = seed_stamps_refusal(current_app)
        if refusal:
            raise click.ClickException(f"Refusing --dev-stamp: {refusal}")
    try:
        max_task_micro = task_price_cap_micro(current_app)
    except ValueError as exc:
        raise click.ClickException(f"X402_MAX_PAYMENT_USDC is unusable: {exc}") from None
    try:
        address_pairs()
    except (ValueError, OSError) as exc:
        raise click.ClickException(f"SCREENING_ADDRESS_MAP is unreadable: {exc}") from None
    result = seed_specialists(db, Agent, root=root, dev_stamp=dev_stamp,
                              max_task_micro=max_task_micro)
    if not result["found"]:
        click.echo("No specialists found.")
        return
    click.echo(f"Specialists: {result['added']} added, {result['updated']} refreshed "
               "(demo listings with a sample track record).")
    if dev_stamp:
        click.echo(f"SIMULATED dev stamps written for {result['dev_stamped']} specialists.")
    else:
        click.echo("Not stamped: an operator must stamp each manifest with World ID before "
                   "the specialist can be hired (or use --dev-stamp in development).")
    if result["mapped"]:
        click.echo(f"New payouts come from SCREENING_ADDRESS_MAP "
                   f"({result['map_entries']} entr{'y' if result['map_entries'] == 1 else 'ies'}); "
                   "payouts already set are kept.")
    else:
        click.echo("SCREENING_ADDRESS_MAP not set: new payout addresses are placeholders and "
                   "unmapped, so screening refuses every payment to them (fail closed).")
    for public_id, name, category, rating, reviews in result["listed"]:
        click.echo(f"  {public_id}  {name}  ({category}, rated {rating:.1f} "
                   f"from {reviews} reviews)")
    if result["restamp"]:
        click.echo("Re-stamp required (the manifest changed since it was stamped): "
                   + ", ".join(result["restamp"]))
    if result["problems"]:
        click.echo("Not listed:")
        for slug, message in result["problems"]:
            click.echo(f"  {slug}: {message}")
        raise click.ClickException(f"{len(result['problems'])} specialist(s) could not be listed.")
