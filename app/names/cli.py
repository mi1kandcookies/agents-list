"""``flask names publish-agents``: give every hireable listed agent its ENS
name, or bring an existing one up to date: records, the agent's grants on its
own endpoint records, and the link from its registry to the root.

New names go to hireable agents only (listed, valid operator stamp, payout).
Every agent that already has a name is kept in sync as well, since its
records follow its state: the payee disappears if screening refuses it, the
manifest hash while the stamp is invalid. Names the operator already holds
on-chain, e.g. issued from another host, are adopted by the sidecar instead
of deployed again. ``--plan`` prints what would be sent and sends nothing; a
live sidecar also needs ``--confirm-live``.
"""
from __future__ import annotations

import click
from flask import current_app
from flask.cli import AppGroup

names_cli = AppGroup("names", help="ENS names for agents and jobs.")


def candidates(public_ids=()) -> list[tuple]:
    """``(agent, reason, named)`` for every listed agent and every agent with
    an active name, oldest first. ``reason`` is why the agent is not hireable
    (see ``unhireable_reason``), else None."""
    from app.extensions import db
    from app.models import Agent, EnsName
    from app.names.service import unhireable_reason
    from app.services import listed_agents_query
    named = {row.agent_id for row in EnsName.query.filter_by(kind="agent", status="active").all()}
    listed = listed_agents_query().with_entities(Agent.id)
    query = Agent.query.filter(db.or_(Agent.id.in_(listed), Agent.id.in_(named)))
    if public_ids:
        query = query.filter(Agent.public_id.in_(public_ids))
    return [(agent, unhireable_reason(agent), agent.id in named) for agent in query.order_by(Agent.id)]


def _check_sidecar(client, *, confirm_live: bool) -> dict:
    from app.names.client import SidecarError
    from app.names.service import root_name
    try:
        health = client.health()
    except SidecarError as exc:
        raise click.ClickException(f"names sidecar: {exc} ({exc.code})") from None
    if health.get("mode") != "dry_run" and not confirm_live:
        raise click.ClickException("the sidecar is live: pass --confirm-live to send Sepolia transactions")
    if not health.get("ok"):
        problems = "; ".join((health.get("address_check") or {}).get("problems") or []) or "not run"
        raise click.ClickException(f"the sidecar refuses writes: {problems}")
    if health.get("root") != root_name():
        raise click.ClickException(f"the sidecar issues names under {health.get('root')}, "
                                   f"but ENS_ROOT_NAME is {root_name()}")
    return health


@names_cli.command("publish-agents")
@click.option("--agent", "public_ids", multiple=True, metavar="AGT-ID", help="Only this agent (repeatable).")
@click.option("--plan", is_flag=True, help="Print the names and records; send nothing.")
@click.option("--confirm-live", is_flag=True, help="Allow Sepolia transactions when the sidecar is live.")
@click.option("--setup-root", is_flag=True,
              help="Register the root name if it is not registered yet (mints MockUSDC, about 70 s).")
@click.option("--timeout", type=float, default=600.0, show_default=True,
              help="Seconds per sidecar call; a live agent name waits for several blocks.")
def publish_agents(public_ids: tuple, plan: bool, confirm_live: bool, setup_root: bool, timeout: float):
    """Name every hireable listed agent and keep existing names in sync (idempotent)."""
    from app.names import service
    from app.names.client import SidecarError, get_client

    rows = candidates(public_ids)
    todo = [agent for agent, reason, named in rows if reason is None or named]
    for agent, reason, named in rows:
        if reason and not named:
            click.echo(f"skip  {agent.public_id}  {agent.name}: {reason}")
    if plan:
        for agent in todo:
            name, status, records = service.plan_agent(agent)
            click.echo(f"plan  {agent.public_id}  {name} ({status})")
            for field, value in records.items():
                click.echo(f"        {field}: {value}")
        return
    if not todo:
        click.echo("No hireable listed agents to name.")
        return

    config = current_app.config
    config["ENS_SIDECAR_TIMEOUT"] = max(float(config.get("ENS_SIDECAR_TIMEOUT") or 15), timeout)
    client = get_client()
    if not client.configured:
        raise click.ClickException("set ENS_SIDECAR_URL and ENS_SIDECAR_TOKEN first")
    health = _check_sidecar(client, confirm_live=confirm_live)
    if health.get("root_status") != "active":
        click.echo(f"root  {health['root']}: " + ("registering or adopting" if setup_root else "adopting"))
        try:
            client.setup_root(register=setup_root)
        except SidecarError as exc:
            if exc.code == "ROOT_NOT_REGISTERED":
                raise click.ClickException(f"{health['root']} is not registered yet: re-run with "
                                           "--setup-root to register it") from None
            raise click.ClickException(f"root setup failed: {exc} ({exc.code})") from None

    reasons = {agent.id: reason for agent, reason, _ in rows}
    failed = 0
    for agent in todo:
        row, error, sent = service.publish_agent(agent)
        note = f"; not hireable: {reasons[agent.id]}" if reasons[agent.id] else ""
        if error:
            failed += 1
            click.echo(f"FAIL  {agent.public_id}  {row.name} ({row.status}{note}): {error}")
        else:
            click.echo(f"ok    {agent.public_id}  {row.name} ({row.status}, {sent} tx{note})")
    skipped = len(rows) - len(todo)
    click.echo(f"{len(todo) - failed} of {len(todo)} names published; "
               f"{skipped} agent{'' if skipped == 1 else 's'} skipped.")
    if failed:
        raise SystemExit(1)
