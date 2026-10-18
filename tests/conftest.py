"""Shared fixtures: one throwaway PostGIS database per test session, one rolled-back
connection per test.

The session database is ``farmcore_test_<8 hex>`` (unique, so parallel worktrees never collide),
created through ``test_database_url`` (an admin URL on the dev server), migrated to head and
dropped at session end. Tests never read ``.env``, and every exported setting other than the
two database URLs is removed for the session, so a developer shell cannot change test behaviour.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from farmcore.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
# The committed open demo data; a local `data/raw` (with optional third-party datasets) wins.
LOCAL_DATA_DIR = REPO_ROOT / "data" / "raw"
DATA_DIR = LOCAL_DATA_DIR if LOCAL_DATA_DIR.is_dir() else REPO_ROOT / "sample-data"
# Lets a test pin the session database name (used to prove the database is dropped).
TEST_DB_ENV = "FARMCORE_TEST_DB"


# Env overrides tests honour: where the dev/CI database lives. Every other setting is pinned.
_ALLOWED_ENV = {"database_url", "test_database_url"}


@pytest.fixture(scope="session", autouse=True)
def _hermetic_env() -> Iterator[None]:
    """Drop exported setting env vars (any case) for the session, except the database URLs."""
    fields = set(Settings.model_fields) - _ALLOWED_ENV
    with pytest.MonkeyPatch.context() as mp:
        for key in list(os.environ):
            if key.lower() in fields:
                mp.delenv(key)
        yield


def _admin_url() -> URL:
    return make_url(Settings(_env_file=None).test_database_url)


@pytest.fixture(scope="session")
def _test_database(_hermetic_env: None) -> Iterator[URL]:
    from alembic import command

    from farmcore.core.commands.migrate import alembic_config

    admin_url = _admin_url()
    name = os.environ.get(TEST_DB_ENV) or f"farmcore_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.exit(
            f"dev PostGIS not reachable at {admin_url.host}:{admin_url.port} "
            f"({type(exc.orig).__name__}). "
            "Start it: docker compose -f docker-compose.dev.yml up -d db",
            returncode=3,
        )
    url = admin_url.set(database=name)
    try:
        migrated = create_engine(url)
        with migrated.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        migrated.dispose()
        command.upgrade(alembic_config(url.render_as_string(hide_password=False)), "head")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def settings(_test_database: URL) -> Settings:
    """Test settings: no `.env`, no secrets, bound to the session test database."""
    return Settings(
        _env_file=None,
        database_url=_test_database.render_as_string(hide_password=False),
        telegram_bot_token=None,
        gemini_api_key=None,
        vertex_access_token=None,
        admin_token=None,
        extractor="fake",
        vertex_project="example-project",
        data_dir=DATA_DIR,
        display_tz="Europe/Paris",
        clock_mode="replay",
        replay_start=datetime(2024, 4, 1, 6, 0),
        default_farm_key="chateau",
        public_base_url="http://localhost:8000",
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    from farmcore.db import make_engine

    eng = make_engine(settings)
    yield eng
    eng.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One connection per test; its outer transaction is rolled back at teardown."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    """Sessions bound to the test connection; `commit()` only releases a savepoint."""
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """HTTP client over the real app, sharing the test connection (sees the test's rows)."""
    from farmcore.main import create_app

    with TestClient(create_app(settings, session_factory=session_factory)) as c:
        yield c


@pytest.fixture
def committed_session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Plain sessions on the engine, for tests that need real commits across sessions.

    Every table except `alembic_version` (and PostGIS's `spatial_ref_sys`) is truncated after.
    """
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"
            )
        ).scalars()
        names = ", ".join(tables)
        if names:
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def data_dir() -> Path:
    """The demo datasets (`sample-data`, or a local `data/raw`); skips when absent."""
    if not DATA_DIR.is_dir():
        pytest.skip(f"{DATA_DIR} is absent")
    return DATA_DIR


# Optional Spanish datasets for `demo_es`: not bundled (see sample-data/README.md).
SPANISH_FILES = (
    "sigpac/47180_Valbuena_de_Duero_2024.zip",
    "inforiego/VA07_2024_03_10_halfhourly.csv",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spanish = all((DATA_DIR / f).is_file() for f in SPANISH_FILES)
    for item in items:
        if item.get_closest_marker("data") is not None and not DATA_DIR.is_dir():
            item.add_marker(pytest.mark.skip(reason=f"{DATA_DIR} is absent"))
        elif item.get_closest_marker("spanish_data") is not None and not spanish:
            item.add_marker(pytest.mark.skip(reason="SIGPAC / InfoRiego files are not in DATA_DIR"))
