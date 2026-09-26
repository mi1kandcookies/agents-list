"""server-owned immutable task purchase intents"""
from alembic import op
import sqlalchemy as sa


revision = "0003_hire_intents"
down_revision = "0002_custody_chain"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hire_intents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("intent_hash", sa.String(length=66), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("agent_public_id", sa.String(length=16), nullable=False),
        sa.Column("ens_name", sa.String(length=255), nullable=True),
        sa.Column("endpoint_path", sa.String(length=255), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("task_hash", sa.String(length=66), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("token_address", sa.String(length=64), nullable=False),
        sa.Column("pay_to", sa.String(length=64), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("payer_agent_public_id", sa.String(length=16), nullable=True),
        sa.Column("mandate_id", sa.String(length=32), nullable=True),
        sa.Column("payment_nonce", sa.String(length=66), nullable=True),
        sa.Column("payment_entry_id", sa.String(length=32), nullable=True),
        sa.Column("deliverable_json", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["mandate_id"], ["mandates.id"]),
        sa.ForeignKeyConstraint(["payment_entry_id"], ["ledger_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_nonce"),
    )
    with op.batch_alter_table("hire_intents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_hire_intents_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_hire_intents_intent_hash"), ["intent_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_hire_intents_state"), ["state"], unique=False)


def downgrade():
    with op.batch_alter_table("hire_intents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_hire_intents_state"))
        batch_op.drop_index(batch_op.f("ix_hire_intents_intent_hash"))
        batch_op.drop_index(batch_op.f("ix_hire_intents_agent_id"))
    op.drop_table("hire_intents")
