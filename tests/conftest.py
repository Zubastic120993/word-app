"""Pytest configuration and fixtures.

Set WORD_APP_TESTING=1 before any app imports so the app lifespan
skips verify_required_tables() and tests can use TestClient with
an overridden in-memory DB without requiring the real vocabulary.db.

WORD_APP_DATABASE_PATH is also forced to a test-only path so the
module-level database.py engine never points to vocabulary.db.
Individual test fixtures create their own in-memory SQLite engines;
this path is never written to during tests.
"""
import os

# Must run before any test module imports app.config / app.database
os.environ.setdefault("WORD_APP_TESTING", "1")
os.environ.setdefault("WORD_APP_DATABASE_PATH", "data/test_module_engine.db")
