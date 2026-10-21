"""Engine, session factory and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core.tour import is_tour
from farmcore.settings import Settings

# A request transaction left idle this long is ended by Postgres, releasing its locks: a stuck
# request then fails alone instead of holding farm locks until every pooled connection waits.
# Far above any legitimate request transaction: slow external calls (a recording's
# transcription, the model) run after the request ends its transaction.
REQUEST_IDLE_IN_TRANSACTION_TIMEOUT = "5min"


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request from `app.state.session_factory`."""
    with request.app.state.session_factory() as session:
        session.info["tour"] = is_tour(request)
        event.listen(session, "after_begin", _bound_idle_transaction)
        yield session


def _bound_idle_transaction(session: Session, transaction, connection) -> None:
    connection.execute(
        text("SELECT set_config('idle_in_transaction_session_timeout', :t, true)"),
        {"t": REQUEST_IDLE_IN_TRANSACTION_TIMEOUT},
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on error, always close."""
    with factory() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
