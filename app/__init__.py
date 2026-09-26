"""
Agent's List Flask application factory.

    from app import create_app
    app = create_app()            # config from FLASK_ENV (default: development)
    app = create_app("testing")   # explicit config name

Blueprints:
    catalog  /, /marketplace, /agent/<id>, /checkout/<id>, /order/<id>, jobs pages
    seller   /seller/*
    admin    /admin/*
    api      /api/* (JSON)
    chain    /config.js, /api/x402/*, /api/onchain/*, legacy contract reads

Custody-chain blueprints (docs/decisions/0001-custody-chain.md):
    identity /login, /auth/world/*          approvals /approvals/*, /api/approvals/*
    engagements /api/engagements/*          screening /api/screening/*
    mandates /api/mandates/*, /api/engagements/<id>/chain
    humans   /humans/*
    names    /api/names/*, /names
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = str(Path(__file__).resolve().parent / "models" / "migrations")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no python-dotenv dependency). Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


# Load .env before app.config is imported: config classes read os.environ at import.
_load_dotenv(_ROOT / ".env")

from app.config import config as _config_map, validate_runtime_config  # noqa: E402
from app.extensions import cors, db, limiter, migrate  # noqa: E402

log = logging.getLogger("agents_list")


def create_app(config_name: str | None = None, **overrides) -> Flask:
    """Build and configure a Flask app instance. Keyword overrides are applied
    on top of the selected config class (handy in tests)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    app = Flask(__name__, instance_path=str(_ROOT / "instance"))
    name = config_name or os.environ.get("FLASK_ENV", "development")
    config_cls = _config_map.get(name, _config_map["default"])
    app.config.from_object(config_cls)
    app.config.update(overrides)
    if hasattr(config_cls, "init_app"):
        config_cls.init_app(app)
    validate_runtime_config(app)

    db.init_app(app)
    migrate.init_app(app, db, directory=MIGRATIONS_DIR)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})
    limiter.init_app(app)

    # Import models so their tables are registered on db.metadata before
    # migrations or create_all() run.
    from app import models  # noqa: F401

    from app.admin import bp as admin_bp
    from app.api import bp as api_bp
    from app.catalog import bp as catalog_bp
    from app.chain import bp as chain_bp
    from app.seller import bp as seller_bp

    from app.intake import bp as intake_bp

    # intake first: its guided flow serves /new ahead of the catalog placeholder.
    for bp in (intake_bp, catalog_bp, seller_bp, admin_bp, api_bp, chain_bp):
        app.register_blueprint(bp)

    # Custody chain (docs/decisions/0001-custody-chain.md); routes land per area.
    from app.approvals import bp as approvals_bp
    from app.engagements import bp as engagements_bp
    from app.humans import bp as humans_bp
    from app.identity import bp as identity_bp
    from app.mandates import bp as mandates_bp
    from app.names import bp as names_bp
    from app.screening import bp as screening_bp

    for bp in (identity_bp, approvals_bp, engagements_bp, screening_bp, mandates_bp,
               humans_bp, names_bp):
        app.register_blueprint(bp)

    from chain.config import explorer_url, get_address, get_chain_config

    app.jinja_env.globals["enumerate"] = enumerate
    app.jinja_env.globals["explorer_url"] = explorer_url

    @app.context_processor
    def _inject_chain():
        return {"chain": get_chain_config(), "usdc_address": get_address("USDC")}
    _register_request_logging(app)
    _register_error_handlers(app)
    _register_cli(app)
    _init_database(app)
    return app


def _register_request_logging(app: Flask) -> None:
    @app.before_request
    def _log_request():
        log.debug("%s %s", request.method, request.path)

    @app.after_request
    def _log_response(response):
        log.info("%s %s -> %s", request.method, request.path, response.status_code)
        return response


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found", "code": "NOT_FOUND"}), 404
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error", "code": "INTERNAL"}), 500
        return render_template("500.html"), 500


def _register_cli(app: Flask) -> None:
    @app.cli.command("seed")
    def seed_command():
        """Load sample agents for local development."""
        from app.models import Agent
        from app.sample_data import seed_sample_agents
        added = seed_sample_agents(db, Agent)
        print(f"Added {added} sample agents.")

    @app.cli.command("seed-stamps")
    def seed_stamps_command():
        """DEVELOPMENT ONLY: mark the sample agents operator-stamped (simulated,
        no World ID approval) so there are hireable listings to try."""
        import sys
        from app.seller.stamp import seed_sample_stamps, seed_stamps_refusal
        refusal = seed_stamps_refusal(app)
        if refusal:
            print(f"Refusing: {refusal}", file=sys.stderr)
            sys.exit(1)
        names = seed_sample_stamps()
        print(f"SIMULATED dev stamps written for {len(names)} sample agents: {', '.join(names) or '-'}")


def _init_database(app: Flask) -> None:
    """Bring the database schema to head when AUTO_MIGRATE is on.

    A database created by the pre-migration create_all() bootstrap (tables but
    no alembic_version) is stamped at the initial revision first so upgrade
    does not try to recreate existing tables.
    """
    if not app.config.get("AUTO_MIGRATE"):
        return
    from flask_migrate import stamp, upgrade
    from sqlalchemy import inspect

    with app.app_context():
        try:
            tables = set(inspect(db.engine).get_table_names())
            if "agents" in tables and "alembic_version" not in tables:
                log.info("stamping pre-migration database at the initial revision")
                stamp(directory=MIGRATIONS_DIR, revision=INITIAL_REVISION)
            upgrade(directory=MIGRATIONS_DIR)
        except Exception as exc:  # never block boot on the dev DB
            log.warning("database migration skipped: %s", exc)


# First Alembic revision (app/models/migrations/versions); used to stamp
# databases that predate migrations.
INITIAL_REVISION = "0001_initial"
