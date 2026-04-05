import os
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from app.database import CHECKSUM_SUFFIX, compute_db_checksum


def create_engine_for(db_path):
    return create_engine(f"sqlite:///{db_path}")


def create_cli_workspace(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "cli_workspace"
    shutil.copytree(repo_root / "app", workspace / "app")
    db_path = workspace / "data" / "vocabulary.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return workspace, db_path


def write_checksum_file(db_path: Path) -> None:
    checksum = compute_db_checksum(str(db_path))
    db_path.with_name(db_path.name + CHECKSUM_SUFFIX).write_text(checksum)


def run_verify_db_cli(workspace: Path, db_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "WORD_APP_DATABASE_PATH": str(db_path),
        "WORD_APP_DATABASE_URL": f"sqlite:///{db_path}",
        "PYTHONPATH": str(workspace),
    }
    return subprocess.run(
        [sys.executable, "-m", "app.tools.verify_db"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )


def test_valid_db_returns_exit_0(tmp_path):
    workspace, db_path = create_cli_workspace(tmp_path)
    engine = create_engine_for(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    write_checksum_file(db_path)

    result = run_verify_db_cli(workspace, db_path)

    assert result.returncode == 0
    assert "Database verification" in result.stdout
    assert "Status: VALID" in result.stdout


def test_missing_required_tables_returns_exit_1(tmp_path):
    workspace, db_path = create_cli_workspace(tmp_path)
    engine = create_engine_for(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))

    result = run_verify_db_cli(workspace, db_path)

    assert result.returncode == 1
    assert "ERROR: Required tables missing" in result.stdout
    assert "Status:" in result.stdout
    assert "Status: VALID" not in result.stdout


def test_checksum_mismatch_returns_exit_1(tmp_path):
    workspace, db_path = create_cli_workspace(tmp_path)
    engine = create_engine_for(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    write_checksum_file(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE extra_table (id INTEGER)"))

    result = run_verify_db_cli(workspace, db_path)

    assert result.returncode == 1
    assert "Checksum: MISMATCH" in result.stdout
