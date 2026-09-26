"""deliveries: completed work shown in the buyer's Inbox

Revision ID: 0007_deliveries
Revises: 0006_agent_ens_label
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_deliveries"
down_revision = "0006_agent_ens_label"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deliveries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("engagement_id", sa.String(length=32), sa.ForeignKey("engagements.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("buyer_human_id", sa.Integer(), sa.ForeignKey("humans.id"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("attestation_json", sa.Text(), nullable=False),
        sa.Column("logs_json", sa.Text(), nullable=False),
        sa.Column("milestones_json", sa.Text(), nullable=False),
        sa.Column("pdf_path", sa.String(length=255), nullable=True),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_deliveries_engagement_id", "deliveries", ["engagement_id"])
    op.create_index("ix_deliveries_buyer_human_id", "deliveries", ["buyer_human_id"])


def downgrade():
    op.drop_index("ix_deliveries_buyer_human_id", table_name="deliveries")
    op.drop_index("ix_deliveries_engagement_id", table_name="deliveries")
    op.drop_table("deliveries")
