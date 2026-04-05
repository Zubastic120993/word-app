from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Import Base and all models for autogenerate support
from app.database import Base
from app.models import (
    LearningUnit,
    LearningProgress,
    LearningSession,
    SessionUnit,
    AudioAsset,
    Settings,
    Vocabulary,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Safety override: always use the application's database URL.
# This forces Alembic to ignore sqlalchemy.url from alembic.ini in both
# offline and online migration contexts.
from app.config import settings
config.set_main_option("sqlalchemy.url", settings.database_url)

# Pre-migration safety log: show the exact database target.
import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
db_url = settings.database_url
if db_url.startswith("sqlite:////"):
    sqlite_path = Path(urlparse(db_url).path).expanduser().resolve()
    logger.warning("Alembic will run against SQLite database file: %s", str(sqlite_path))
else:
    logger.warning("Alembic will run against database URL: %s", db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target_metadata for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Read database URL from app.config.settings
    from app.config import settings
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Read database URL from app.config.settings
    from app.config import settings, ensure_data_dir
    
    # Ensure data directory exists before creating engine
    ensure_data_dir()
    
    # Override sqlalchemy.url in config with value from settings
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
