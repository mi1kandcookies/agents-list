"""
extensions.py - Flask extension singletons.

Create extension objects here without binding to an app.
Call extension.init_app(app) inside create_app() or at the bottom of app.py.

This pattern avoids circular imports and makes unit-testing trivial
(just call init_app with a test app).
"""
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# SQLAlchemy - the primary data store
db = SQLAlchemy()

# CORS - allow cross-origin requests to /api/* endpoints
cors = CORS()

# Rate limiter - keyed by remote IP
limiter = Limiter(key_func=get_remote_address)
