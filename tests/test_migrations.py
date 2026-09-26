"""Alembic migrations must build the same schema the models declare."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade

from app import MIGRATIONS_DIR, create_app
from app.extensions import db


def test_upgrade_matches_models(tmp_path):
    db_file = tmp_path / "m.db"
    app = create_app("testing", SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_file}")
    with app.app_context():
        upgrade(directory=MIGRATIONS_DIR)
        assert db_file.exists()
        with db.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []
