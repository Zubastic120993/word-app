from pathlib import Path

from app.config import settings
from app.database import engine


def test_database_path_matches_config():
    engine_db_path = Path(engine.url.database).expanduser().resolve()
    settings_db_path = Path(settings.database_path).expanduser().resolve()

    assert engine_db_path == settings_db_path
