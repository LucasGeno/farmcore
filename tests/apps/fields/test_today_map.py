"""`GET /fields/today`: the Today map fragment (no page wrapper), sharing `map.js`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from farmcore.apps.fields.seed import seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import AccessLog, Parcel

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)


@pytest.fixture
def seeded(session, synthetic_data_dir, settings):
    seed(session, scene=None, data_dir=synthetic_data_dir)
    set_replay(session, NOW, settings)
    session.commit()
    return session


def _access_rows(session) -> int:
    return session.scalar(select(func.count()).select_from(AccessLog))


def test_today_fragment_is_a_map_container_without_a_page_wrapper(client, seeded):
    before = _access_rows(seeded)

    resp = client.get("/fields/today")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    page = resp.text
    assert "<html" not in page and "<body" not in page and 'id="navbar"' not in page
    from sqlalchemy import select

    farm_id = seeded.scalar(select(Parcel.farm_id).limit(1))
    assert "data-parcel-map" in page
    assert f'data-geojson-url="/fields/parcels.geojson?farm={farm_id}"' in page
    assert "ORTHOIMAGERY.ORTHOPHOTOS" in page and "data-basemap-toggle" in page
    assert "/static/core/vendor/maplibre-gl-4.7.1.js" in page
    assert '<script src="/static/fields/map.js"></script>' in page
    assert 'data-icon="spray"' in page  # the legend travels with the fragment
    assert 'id="parcel-map"' not in page  # unique id when embedded next to other content
    assert _access_rows(seeded) >= before + 1


def test_today_fragment_logs_one_cheap_scoped_read_not_the_whole_collection(client, seeded):
    # The fragment's JS fetches /fields/parcels.geojson itself; the HTML only needs the audit row.
    before = _access_rows(seeded)

    client.get("/fields/today")

    rows = seeded.scalars(select(AccessLog).order_by(AccessLog.id.desc()).limit(5)).all()
    assert _access_rows(seeded) == before + 1
    assert rows[0].scope == "fields:read" and rows[0].resource == "farm"


# --- The Today mini map is desktop only (QA F1, modular-apps.md: order 50, full) -------------


def test_fields_declares_the_desktop_mini_map_widget() -> None:
    from farmcore.apps.fields.manifest import MANIFEST
    from farmcore.core.manifest import Widget

    assert MANIFEST.widgets == (Widget("/fields/today?desktop=1", 30, "half"),)


def test_the_preview_shell_loads_on_phone_with_independent_retry(client, seeded):
    body = client.get("/fields/today", params={"desktop": "1"}).text
    assert "matchMedia" not in body and "hidden lg:block" not in body
    farm_id = seeded.scalar(select(Parcel.farm_id).limit(1))
    assert f'hx-get="/fields/today?compact=1&amp;farm={farm_id}"' in body
    assert 'hx-trigger="load"' in body
    assert 'hx-sync="closest [data-fragment]:drop"' in body
    assert "Map could not load." in body and "Retry Map" in body
    assert "Open map" in body
    assert "maplibre" not in body and "data-parcel-map" not in body


def test_the_desktop_slot_and_its_map_keep_an_explicit_farm(client, seeded, session):
    from farmcore.core.models.canonical import Farm

    farm_id = session.scalar(select(Farm.id).order_by(Farm.id).limit(1))
    today = client.get("/today/", params={"farm": farm_id}).text
    assert f'hx-get="/fields/today?desktop=1&amp;farm={farm_id}"' in today
    shell = client.get("/fields/today", params={"desktop": "1", "farm": farm_id}).text
    assert f'hx-get="/fields/today?compact=1&amp;farm={farm_id}"' in shell
    full = client.get("/fields/today", params={"farm": farm_id}).text
    assert f'data-geojson-url="/fields/parcels.geojson?farm={farm_id}"' in full
