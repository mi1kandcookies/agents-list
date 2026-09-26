"""agent track record: on-time and repeat-hire rates, demo listing flag

Revision ID: 0004_agent_track_record
Revises: 0003_hire_intents
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_agent_track_record"
down_revision = "0003_hire_intents"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("on_time_rate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("repeat_hire_rate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("demo_listing", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("demo_listing")
        batch.drop_column("repeat_hire_rate")
        batch.drop_column("on_time_rate")
