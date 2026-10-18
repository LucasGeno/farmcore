"""The fixtures isolate tests: rows a test writes are visible over HTTP, then rolled back.

Tests run in file order: each `*_gone_*` / `*_truncated_*` test checks the row its predecessor
wrote, and fails (rather than passing vacuously) when that predecessor did not run first in this
process, e.g. under `-k`, a single node id or random ordering. Run the whole module.
"""

from __future__ import annotations

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from farmcore.core.models.kernel import AppModule
from farmcore.db import get_session

PROBE = "isolation_probe"
# Names of writer tests that committed the probe earlier in this process.
_WROTE: set[str] = set()


def _require_writer(name: str) -> None:
    assert name in _WROTE, f"{name} must run first in this process; run the whole module"


def _add_probe_route(client: TestClient) -> None:
    def probe(session: Session = Depends(get_session)) -> dict[str, int]:
        n = session.scalar(select(func.count()).where(AppModule.name == PROBE))
        return {"n": n}

    client.app.add_api_route("/_probe", probe)


def test_row_from_session_is_visible_through_client(session: Session, client: TestClient) -> None:
    session.add(AppModule(name=PROBE, state="installed", version="0.0.1"))
    session.commit()
    _add_probe_route(client)
    assert client.get("/_probe").json() == {"n": 1}
    _WROTE.add("session")


def test_row_is_gone_in_the_next_test(session: Session, client: TestClient) -> None:
    _require_writer("session")
    assert session.get(AppModule, PROBE) is None
    _add_probe_route(client)
    assert client.get("/_probe").json() == {"n": 0}


def test_committed_session_factory_commits_across_sessions(committed_session_factory) -> None:
    with committed_session_factory() as s:
        s.add(AppModule(name=PROBE, state="installed", version="0.0.1"))
        s.commit()
    with committed_session_factory() as other:
        assert other.get(AppModule, PROBE) is not None
    _WROTE.add("committed")


def test_committed_rows_are_truncated_after_the_test(committed_session_factory) -> None:
    _require_writer("committed")
    with committed_session_factory() as s:
        assert s.get(AppModule, PROBE) is None
