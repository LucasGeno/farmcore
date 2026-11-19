"""A bundle without crop protection (`advisor` and the apps documented as extending it) starts,
navigates parcels, records and the diary, keeps forecast receipts readable, and says which
capabilities are unavailable instead of answering with an empty or all-clear page.

The import half (no module of another app imports the advisor package) is proven in a
subprocess by `tests/core/test_registry.py`; here the advisor package is importable, so what this
proves is that each route takes its unavailable branch rather than reaching crop protection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from farmcore.apps.forecast import sources, store
from farmcore.core import clock, registry
from farmcore.core.access import FARM_COOKIE
from farmcore.core.models.canonical import Log
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.forecast.conftest import FIXTURE
from tests.apps.operations.conftest import make_parcel
from tests.core.test_registry import CROP_PROTECTION

NEEDS_CROP_PROTECTION = 'data-needs-app="advisor"'


@pytest.fixture
def bundle_client(settings, session_factory):
    bundle = [m for m in registry.discover() if m.name not in CROP_PROTECTION]
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
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    bundle_client.cookies.set(FARM_COOKIE, str(farm.id))

    for url in (
        "/",
        "/fields/",
        f"/fields/parcels/{parcel.id}",
        f"/operations/parcels/{parcel.id}",
        "/notifications/",
    ):
        assert bundle_client.get(url).status_code == 200, url
    diary = bundle_client.get("/operations/")
    assert diary.status_code == 200
    assert "Hail damage on the north rows" in diary.text

    weather = bundle_client.get("/forecast/home-weather")
    assert weather.status_code == 200
    assert "forecast as issued" in weather.text
    assert weather.text.count('data-weather-day="') == 5

    for url in ("/forecast/today", f"/forecast/parcels/{parcel.id}", "/forecast/plan"):
        page = bundle_client.get(url)
        assert page.status_code == 200, url
        assert NEEDS_CROP_PROTECTION in page.text, url
        assert "not an all-clear" in page.text, url
