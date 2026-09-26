"""agent icon: optional per-listing icon override

Revision ID: 0005_agent_icon
Revises: 0004_agent_track_record
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_agent_icon"
down_revision = "0004_agent_track_record"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("icon", sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("icon")
