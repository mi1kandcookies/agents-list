"""agent ens label: the operator-chosen label for the agent's name

Revision ID: 0006_agent_ens_label
Revises: 0005_agent_icon
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_agent_ens_label"
down_revision = "0005_agent_icon"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("ens_label", sa.String(length=63), nullable=True))


def downgrade():
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("ens_label")
