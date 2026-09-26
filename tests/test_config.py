"""App configuration helpers."""
import pytest

from app.config import _database_url


@pytest.mark.parametrize("raw, expected", [
    ("", "sqlite:///agents_list.db"),
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u@h/d", "postgresql+psycopg://u@h/d"),
    ("sqlite:////tmp/x.db", "sqlite:////tmp/x.db"),
])
def test_database_url_normalization(monkeypatch, raw, expected):
    monkeypatch.setenv("DATABASE_URL", raw)
    assert _database_url() == expected
