"""Alembic environment for Cadence's local-canonical D1.

The database URL is resolved from :mod:`cadence.config` so migrations always target
the same local SQLite file the runtime uses. ``target_metadata`` is the ORM metadata,
enabling ``--autogenerate`` for future revisions.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from cadence.config import get_settings
from cadence.stores.models import Base

config = context.config

# Resolve the runtime DB URL (overrides the ini fallback). Ensure the local storage
# dirs exist first so `alembic upgrade head` works on a clean checkout without a manual
# `mkdir -p var` (SQLite cannot create the DB file if its parent dir is missing).
_settings = get_settings()
_settings.ensure_dirs()
config.set_main_option("sqlalchemy.url", _settings.d1_sqlalchemy_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
