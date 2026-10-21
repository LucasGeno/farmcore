"""Alembic environment for farmcore.

Rule: ONE linear revision chain. Revision ids and files are `<app>_<NNNN>_<slug>`
(id `core_0001`, file `versions/core_0001_kernel.py`). Only the orchestrator adds revisions.

URL precedence: `-x url=...`, then `sqlalchemy.url` set in code, then settings.database_url.
"""

from __future__ import annotations

import importlib
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import create_engine, pool

from farmcore.core.models import Base
from farmcore.core.registry import discover

config = context.config

# App-owned tables (`Manifest.models`) join the metadata, so autogenerate compares every table
# the chain creates. The registry imports the same modules when it loads the apps.
for _manifest in discover():
    for _module in _manifest.models:
        importlib.import_module(_module)
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Tables owned by PostGIS and its extensions, never by farmcore.
EXCLUDED_SCHEMAS = {"tiger", "tiger_data", "topology"}
EXCLUDED_TABLES = {
    "spatial_ref_sys",
    "geocode_settings",
    "geocode_settings_default",
    "direction_lookup",
    "secondary_unit_lookup",
    "state_lookup",
    "street_type_lookup",
    "place_lookup",
    "county_lookup",
    "countysub_lookup",
    "zip_lookup_all",
    "zip_lookup_base",
    "zip_lookup",
    "zip_state",
    "zip_state_loc",
    "loader_platform",
    "loader_variables",
    "loader_lookuptables",
    "pagc_gaz",
    "pagc_lex",
    "pagc_rules",
    "layer",
    "topology",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and (name in EXCLUDED_TABLES or obj.schema in EXCLUDED_SCHEMAS):
        return False
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url")
    if url:
        return url
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from farmcore.settings import get_settings

    return get_settings().database_url


def _configure_kwargs() -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "include_object": include_object,
        "render_item": alembic_helpers.render_item,
        "process_revision_directives": alembic_helpers.writer,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"}, **_configure_kwargs()
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # `farmcore migrate`'s own connection: it holds the migration lock, so the lock lives
        # exactly as long as the backend that runs the migrations.
        context.configure(connection=connection, **_configure_kwargs())
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, **_configure_kwargs())
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
