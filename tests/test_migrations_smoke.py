import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.db_verification import verify_database
from app.database import REQUIRED_TABLES


def test_migrations_build_valid_schema(tmp_path):
    db_path = tmp_path / "migration_test.db"

    env = {
        **os.environ,
        "WORD_APP_DATABASE_PATH": str(db_path),
        "WORD_APP_DATABASE_URL": f"sqlite:///{db_path}",
    }

    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Alembic migration failed:\n{result.stderr}"

    engine = create_engine(f"sqlite:///{db_path}")

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.integrity_ok
    assert not verification.missing_required_tables
