"""custody-chain engagement and approval foundation

Revision ID: 0003_custody_chain
Revises: 0002_protected_hiring
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_custody_chain"
down_revision = "0002_protected_hiring"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("public_id", sa.String(length=20), nullable=True))
    op.add_column("agents", sa.Column("payout_address", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("screening_address", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("manifest", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("agents", sa.Column("ens_name", sa.String(length=255), nullable=True))
    op.create_index("ix_agents_public_id", "agents", ["public_id"], unique=True)
    with op.batch_alter_table("agents", recreate="always") as batch_op:
        batch_op.create_unique_constraint("uq_agents_ens_name", ["ens_name"])

    op.create_table(
        "humans",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("world_sub", sa.String(length=255), nullable=False),
        sa.Column("wallet", sa.String(length=64), nullable=True),
        sa.Column("auth_time", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("world_sub"),
    )
    op.create_table(
        "engagements",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("human_id", sa.String(length=40), nullable=False),
        sa.Column("parent_engagement_id", sa.String(length=32), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("budget_atomic", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("network", sa.String(length=80), nullable=False),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("sow_hash", sa.String(length=66), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["human_id"], ["humans.id"]),
        sa.ForeignKeyConstraint(["parent_engagement_id"], ["engagements.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_engagements_status", "engagements", ["status"], unique=False)
    op.create_table(
        "milestones",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("engagement_id", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("auto_release_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_hash", sa.String(length=66), nullable=True),
        sa.Column("deliverable", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_milestones_status", "milestones", ["status"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("human_id", sa.String(length=40), nullable=False),
        sa.Column("action_type", sa.String(length=48), nullable=False),
        sa.Column("action_hash", sa.String(length=66), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("auth_time", sa.DateTime(), nullable=True),
        sa.Column("jti", sa.String(length=255), nullable=True),
        sa.Column("device_code", sa.String(length=255), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("denial_reason", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["human_id"], ["humans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_hash"),
        sa.UniqueConstraint("jti"),
        sa.UniqueConstraint("device_code"),
    )
    op.create_index("ix_approvals_state", "approvals", ["state"], unique=False)
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_events_approval_id", "approval_events", ["approval_id"], unique=False)
    op.create_table(
        "screenings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("engagement_id", sa.String(length=32), nullable=True),
        sa.Column("action_type", sa.String(length=48), nullable=False),
        sa.Column("subject_address", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("verdict_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("request_hash", sa.String(length=66), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("engagement_id", sa.String(length=32), nullable=False),
        sa.Column("parent_entry_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("from_address", sa.String(length=64), nullable=True),
        sa.Column("to_address", sa.String(length=64), nullable=True),
        sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approval_id", sa.String(length=32), nullable=True),
        sa.Column("screening_id", sa.String(length=32), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"]),
        sa.ForeignKeyConstraint(["parent_entry_id"], ["ledger_entries.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.ForeignKeyConstraint(["screening_id"], ["screenings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledger_entries_status", "ledger_entries", ["status"], unique=False)
    op.create_table(
        "mandates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("parent_mandate_id", sa.String(length=32), nullable=True),
        sa.Column("engagement_id", sa.String(length=32), nullable=True),
        sa.Column("issuer_human_id", sa.String(length=40), nullable=False),
        sa.Column("subject_agent_id", sa.Integer(), nullable=True),
        sa.Column("budget_atomic", sa.BigInteger(), nullable=False),
        sa.Column("categories_json", sa.Text(), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False),
        sa.Column("per_tx_max_atomic", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("token_jwt", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=66), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["parent_mandate_id"], ["mandates.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"]),
        sa.ForeignKeyConstraint(["issuer_human_id"], ["humans.id"]),
        sa.ForeignKeyConstraint(["subject_agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_mandates_state", "mandates", ["state"], unique=False)
    op.create_table(
        "ens_names",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("engagement_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("parent_name", sa.String(length=255), nullable=True),
        sa.Column("resolver", sa.String(length=64), nullable=True),
        sa.Column("address", sa.String(length=64), nullable=True),
        sa.Column("endpoint", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["engagements.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "used_id_token_jtis",
        sa.Column("jti", sa.String(length=255), nullable=False),
        sa.Column("human_id", sa.String(length=40), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["human_id"], ["humans.id"]),
        sa.PrimaryKeyConstraint("jti"),
    )


def downgrade():
    op.drop_table("used_id_token_jtis")
    op.drop_table("ens_names")
    op.drop_index("ix_mandates_state", table_name="mandates")
    op.drop_table("mandates")
    op.drop_index("ix_ledger_entries_status", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("screenings")
    op.drop_index("ix_approval_events_approval_id", table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_index("ix_approvals_state", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_milestones_status", table_name="milestones")
    op.drop_table("milestones")
    op.drop_index("ix_engagements_status", table_name="engagements")
    op.drop_table("engagements")
    op.drop_table("humans")
    with op.batch_alter_table("agents", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_agents_ens_name", type_="unique")
    op.drop_index("ix_agents_public_id", table_name="agents")
    op.drop_column("agents", "ens_name")
    op.drop_column("agents", "manifest")
    op.drop_column("agents", "screening_address")
    op.drop_column("agents", "payout_address")
    op.drop_column("agents", "public_id")
