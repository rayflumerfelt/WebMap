"""Alembic environment.

Migrations connect as the **privileged** role, not the application role
(`02-data-model.md` §4). The application role must lack `BYPASSRLS`, which
also means it cannot create policies — so the two are deliberately different
credentials and `WEBMAP_MIGRATION_DATABASE_URL` is a separate setting.

There is no `target_metadata`: the schema is defined by the DDL in `02` and
written as explicit SQL, not reflected from SQLAlchemy models. Autogenerate
against a partial model set would silently propose dropping every table it
does not know about.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from webmap_core.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().migration_database_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a connection. Useful for review."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
            # One transaction for the whole upgrade. A migration set that
            # half-applies leaves a database nobody can reason about, and the
            # roadmap's "apply and roll back cleanly" criterion assumes it.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
