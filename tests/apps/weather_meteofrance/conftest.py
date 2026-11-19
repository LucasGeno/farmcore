"""The Météo-France portal, faked from recorded response shapes: token, the DPObs station list,
DPObs hourly and the DPClim hourly order. No network."""

from __future__ import annotations

import base64
import json
import time
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
    """Answers like the portal; `fail` names an endpoint (`token`, `archive`) that errs,
    `pending` how many polls the archive file is not ready for (the first answer a 429 with
    Retry-After), `expired` how many data calls refuse a valid token (it expired), `stall` how
    many seconds the token answer takes."""

    def __init__(
        self, fail: str | None = None, pending: int = 2, expired: int = 0, stall: float = 0.0
    ) -> None:
        self.fail, self.pending, self.expired, self.stall = fail, pending, expired, stall
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/token":
            time.sleep(self.stall)  # a slow portal
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if self.fail == "token" or request.headers.get("authorization") != f"Basic {basic}":
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens += 1
            return httpx.Response(200, content=(FIXTURES / "token.json").read_bytes())
        token = json.loads((FIXTURES / "token.json").read_text())["access_token"]
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401)
        if self.expired:
            self.expired -= 1
            return httpx.Response(401)
        if path == "/public/DPObs/v2/liste-stations":
            return httpx.Response(200, text=(FIXTURES / "dpobs-liste-stations.csv").read_text())
        if path == "/public/DPObs/v2/station/horaire":
            rows = json.loads((FIXTURES / "dpobs-station-horaire-33394002.json").read_text())
            rows[0]["validity_time"] = params["date"]
            return httpx.Response(200, json=rows)
        if path == "/public/DPClim/v1/commande-station/horaire":
            # The documented forms: the start on the hour, the end at hh:59:59.
            if not (
                params["date-deb-periode"].endswith(":00:00Z")
                and params["date-fin-periode"].endswith(":59:59Z")
            ):
                return httpx.Response(400)
            self.period = _hour(params["date-deb-periode"]), _hour(params["date-fin-periode"])
            return httpx.Response(
                202, json={"elaboreProduitAvecDemandeResponse": {"return": ORDER}}
            )
        if path == "/public/DPClim/v1/commande/fichier" and params["id-cmde"] == ORDER:
            if self.fail == "archive":
                return httpx.Response(500)
            if self.pending:
                self.pending -= 1
                if self.pending == 1:
                    return httpx.Response(429, headers={"Retry-After": "0"})
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

    def install(**kw) -> Portal:
        fake = Portal(**kw)
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
