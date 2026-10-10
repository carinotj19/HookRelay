"""Alembic environment: use the same app factory as API and worker."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from hookrelay import create_app
from hookrelay.extensions import db
from hookrelay import models  # noqa: F401 -- register mapped tables

config = context.config
app = create_app()
with app.app_context():
    database_url = str(db.engine.url).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = db.metadata


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
