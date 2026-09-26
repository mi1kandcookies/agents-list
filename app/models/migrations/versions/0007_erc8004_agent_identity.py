"""store the ERC-8004 identity token for each marketplace agent

Revision ID: 0007_erc8004_agent_identity
Revises: 0006_agent_ens_label
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_erc8004_agent_identity"
down_revision = "0006_agent_ens_label"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("erc8004_agent_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("erc8004_registry", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("erc8004_registry")
        batch.drop_column("erc8004_agent_id")
