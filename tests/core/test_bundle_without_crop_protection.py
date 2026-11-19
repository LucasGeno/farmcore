"""A bundle without crop protection (`advisor` and the apps documented as extending it) starts,
navigates parcels, records and the diary, keeps forecast receipts readable, and says which
capabilities are unavailable instead of answering with an empty or all-clear page.

The import half is proven in a fresh process by `tests/core/test_registry.py`. Here the
crop-protection packages are blocked in-process for the test (a meta-path finder refuses them and
they are dropped from `sys.modules`), so a route that reaches them at request time fails."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from farmcore.apps.forecast import sources, store
from farmcore.core import clock, registry
from farmcore.core.access import FARM_COOKIE
from farmcore.core.models.canonical import Advice, DataGrant, Log
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.forecast.conftest import FIXTURE
from tests.apps.operations.conftest import make_parcel
from tests.core.crop_protection import CROP_PROTECTION

NEEDS_CROP_PROTECTION = 'data-needs-app="advisor"'


BLOCKED = tuple(f"farmcore.apps.{name}" for name in sorted(CROP_PROTECTION))


def _blocked(name: str) -> bool:
    return any(name == b or name.startswith(b + ".") for b in BLOCKED)


class _NotInBundle:
    """A meta-path finder that refuses the crop-protection packages, as if never installed."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        if _blocked(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


@pytest.fixture
def bundle_client(settings, session_factory, monkeypatch):
    bundle = [m for m in registry.discover() if m.name not in CROP_PROTECTION]
    monkeypatch.setattr(sys, "meta_path", [_NotInBundle(), *sys.meta_path])
    for name in [n for n in sys.modules if _blocked(n)]:
        monkeypatch.delitem(sys.modules, name)
    app = create_app(settings, manifests=bundle, session_factory=session_factory)
    with TestClient(app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}) as c:
        yield c


def test_a_bundle_without_crop_protection_navigates_and_says_what_is_unavailable(
    bundle_client, session, settings
):
    # Bugs prevented: a page of a non-crop app 500s (or renders "nothing to do") because it
    # reaches the advisor that is not in the bundle; forecast receipts become unreadable.
    farm = ensure_default_farm(session, settings)
    parcel = make_parcel(session, farm, 1, area="1.25", props={"variety": "Merlot"})
    session.add(
        Log(
            farm_id=farm.id,
            type="observation",
            status="done",
            name="Hail damage on the north rows",
            ts_start=datetime(2024, 4, 8, 9, tzinfo=UTC),
            parcels=[parcel],
        )
    )
    # Advice stored while crop protection was installed, shared with a recipient.
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=datetime(2024, 4, 8, 6, tzinfo=UTC),
            decision="spray",
            props={"headline": "Stored before removal"},
        )
    )
    grant = DataGrant(
        farm_id=farm.id, grantee="Adviser", purpose="Advice review", scopes=["advice:read"]
    )
    session.add(grant)
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    bundle_client.cookies.set(FARM_COOKIE, str(farm.id))

    for url in (
        "/",
        "/fields/",
        "/fields/list",
        f"/fields/parcels/{parcel.id}",
        f"/operations/parcels/{parcel.id}",
        "/notifications/",
    ):
        assert bundle_client.get(url).status_code == 200, url
    diary = bundle_client.get("/operations/")
    assert diary.status_code == 200
    assert "Hail damage on the north rows" in diary.text

    geojson = bundle_client.get("/fields/parcels.geojson")
    assert geojson.status_code == 200
    [feature] = geojson.json()["features"]
    assert (feature["properties"]["decision"], feature["properties"]["advised"]) == ("none", False)

    page = bundle_client.get(f"/share/as/{grant.id}/")
    assert page.status_code == 200
    assert "crop protection is not installed" in page.text

    shared = bundle_client.get(f"/share/as/{grant.id}/advice.json")
    assert shared.status_code == 409
    assert "crop protection is not installed" in shared.json()["detail"]

    weather = bundle_client.get("/forecast/home-weather")
    assert weather.status_code == 200
    assert "forecast as issued" in weather.text
    assert weather.text.count('data-weather-day="') == 5

    for url in ("/forecast/today", f"/forecast/parcels/{parcel.id}", "/forecast/plan"):
        page = bundle_client.get(url)
        assert page.status_code == 200, url
        assert NEEDS_CROP_PROTECTION in page.text, url
        assert "not an all-clear" in page.text, url
