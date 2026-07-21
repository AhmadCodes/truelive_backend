from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context
import os
import re
import sys

# Add parent directory to path
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))

# Import models and database Base
from app.database import Base
from app.models import *  # Import all models

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate
target_metadata = Base.metadata

# Get database URL from environment variable if available
database_url = os.getenv('DATABASE_URL')
if database_url:
    config.set_main_option('sqlalchemy.url', database_url)


# Monthly partitions of the alerting tables (raw_messages, alerts, alert_media,
# webhook_deliveries) are created at runtime — by revision 007 and afterwards by
# the `rollover_alerting_partitions` celery beat task. They are physical children
# of a declaratively-modelled parent and never appear in Base.metadata, so
# autogenerate would happily propose dropping every one of them and take
# production alerting data with it. Exclude them from all comparisons.
_PARTITION_SUFFIX_RE = re.compile(r'_p\d{4}_\d{2}$')


def include_object(object_, name, type_, reflected, compare_to):
    """Filter objects out of autogenerate comparison.

    Skips runtime-provisioned monthly partition tables (e.g. `alerts_p2026_04`)
    and anything that belongs to them.
    """
    if type_ == 'table' and name and _PARTITION_SUFFIX_RE.search(name):
        return False
    if type_ in ('column', 'index', 'unique_constraint', 'foreign_key_constraint'):
        table = getattr(object_, 'table', None)
        table_name = getattr(table, 'name', None)
        if table_name and _PARTITION_SUFFIX_RE.search(table_name):
            return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
