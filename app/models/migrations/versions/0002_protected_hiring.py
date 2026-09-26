"""protected ENS/x402 hiring intents and validated approvals

Revision ID: 0002_protected_hiring
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_protected_hiring"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hire_intents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.String(length=160), nullable=False),
        sa.Column("payer", sa.String(length=64), nullable=False),
        sa.Column("specialist_name", sa.String(length=255), nullable=False),
        sa.Column("approved_endpoint", sa.String(length=500), nullable=False),
        sa.Column("pay_to", sa.String(length=64), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("network", sa.String(length=80), nullable=False),
        sa.Column("token_address", sa.String(length=64), nullable=False),
        sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("task_hash", sa.String(length=66), nullable=False),
        sa.Column("ens_snapshot", sa.Text(), nullable=False),
        sa.Column("canonical_terms", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        sa.Column("intent_hash", sa.String(length=66), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("screening", sa.Text(), nullable=False),
        sa.Column("payment_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("receipt", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_hash"),
    )
    op.create_index("ix_hire_intents_status", "hire_intents", ["status"], unique=False)
    op.create_table(
        "hire_approvals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("intent_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=160), nullable=False),
        sa.Column("payer", sa.String(length=64), nullable=False),
        sa.Column("intent_hash", sa.String(length=66), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("proof_id", sa.String(length=255), nullable=False),
        sa.Column("action_url", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("denial_reason", sa.Text(), nullable=False),
        sa.Column("validated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["intent_id"], ["hire_intents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_id"),
    )


def downgrade():
    op.drop_table("hire_approvals")
    op.drop_index("ix_hire_intents_status", table_name="hire_intents")
    op.drop_table("hire_intents")
