"""The Météo-France portal, faked from recorded response shapes: token, DPObs hourly and the
DPClim hourly order. No network."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from farmcore.apps.weather_meteofrance import ingest
from farmcore.apps.weather_meteofrance import provider as live
from farmcore.core import seed as core_seed

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT_ID = "recorded-client-id"
CLIENT_SECRET = "recorded-client-secret-0123456789"
ORDER = "768920711487"


def _hour(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


class Portal:
    """Answers like the portal; `fail` names an endpoint (`token`, `archive`) that errs."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.pending = 1  # the archive file is not ready on the first poll

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/token":
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if self.fail == "token" or request.headers.get("authorization") != f"Basic {basic}":
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, content=(FIXTURES / "token.json").read_bytes())
        token = json.loads((FIXTURES / "token.json").read_text())["access_token"]
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401)
        if path == "/public/DPObs/v2/station/horaire":
            rows = json.loads((FIXTURES / "dpobs-station-horaire-33394002.json").read_text())
            rows[0]["validity_time"] = params["date"]
            return httpx.Response(200, json=rows)
        if path == "/public/DPClim/v1/commande-station/horaire":
            self.period = _hour(params["date-deb-periode"]), _hour(params["date-fin-periode"])
            return httpx.Response(
                202, json={"elaboreProduitAvecDemandeResponse": {"return": ORDER}}
            )
        if path == "/public/DPClim/v1/commande/fichier" and params["id-cmde"] == ORDER:
            if self.fail == "archive":
                return httpx.Response(500)
            if self.pending:
                self.pending -= 1
                return httpx.Response(204)
            header, *rows = (
                (FIXTURES / "dpclim-commande-horaire-33394002.csv").read_text().splitlines()
            )
            start, end = self.period
            hours = [start + timedelta(hours=i) for i in range(len(rows))]
            assert hours[-1] == end.replace(minute=0, second=0)
            lines = [
                ";".join([r.split(";")[0], f"{h:%Y%m%d%H}", *r.split(";")[2:]])
                for r, h in zip(rows, hours, strict=True)
            ]
            return httpx.Response(201, text="\n".join([header, *lines]) + "\n")
        return httpx.Response(404)


@pytest.fixture
def portal(monkeypatch) -> Callable[..., Portal]:
    """Route the provider's HTTP to a fake portal; the archive polls without waiting."""
    monkeypatch.setattr(live, "FIRST_POLL_SECONDS", 0.0)
    monkeypatch.setattr(live, "POLL_SECONDS", 0.0)
    # Two archive hours before the 24-hour window keep the order small.
    monkeypatch.setattr(ingest, "BACKFILL", timedelta(hours=26))
    real = httpx.Client

    def install(fail: str | None = None) -> Portal:
        fake = Portal(fail)
        monkeypatch.setattr(
            live.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake), **kw)
        )
        return fake

    return install


@pytest.fixture
def credentials(settings):
    return settings.model_copy(
        update={
            "meteofrance_client_id": SecretStr(CLIENT_ID),
            "meteofrance_client_secret": SecretStr(CLIENT_SECRET),
        }
    )


@pytest.fixture
def station(session, settings, monkeypatch, tmp_path):
    """The demo farm and its Météo-France station (33394002), seeded by the stations app."""
    from farmcore.apps.stations.seed import seed

    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    core_seed.seed(session, scene=None, data_dir=tmp_path)
    seed(session, scene=None, data_dir=tmp_path)
