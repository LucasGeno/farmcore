"""Crop seasons on a stable parcel. Failure modes owned here, one test each:

1. Editing the current season rewrites an earlier season or the season context an assessment
   saved as evidence.
2. Overlapping seasons are both kept or silently merged instead of rejected.
3. A stale edit, an edit of an earlier season, a non-owner or a selector from another farm or
   parcel changes a season.
4. Unknown crop, end or phenology is invented: from legacy parcel props, or by the upgrade of a
   populated legacy database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

from farmcore.apps.advisor import service as advisor
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.commands.migrate import alembic_config
from farmcore.core.models.canonical import Asset, CropSeason, Farm
from farmcore.core.seed import ensure_default_farm
from tests.apps.share.conftest import make_parcel
from tests.core.test_schema import MULTIPOLYGON_GEOJSON, scratch_db_url  # noqa: F401

SEASON_2023 = {"starts_on": "2023-03-01", "ends_on": "2023-10-31", "crop_eppo": "VITVI"}
SEASON_2024 = {"starts_on": "2024-03-01", "crop_eppo": "VITVI", "crop_name": "Merlot", "bbch": 9}


@pytest.fixture
def farm(session, settings):
    return ensure_default_farm(session, settings)


@pytest.fixture
def parcel(session, farm):
    return make_parcel(session, farm, 1)


def _url(parcel_id, season_id=None):
    base = f"/fields/parcels/{parcel_id}/seasons"
    return base if season_id is None else f"{base}/{season_id}"


def _create(client, farm, parcel_id, body, status=201):
    response = client.post(_url(parcel_id), params={"farm": farm.id}, json=body)
    assert response.status_code == status, response.text
    return response.json()


def _seasons(client, farm, parcel_id):
    response = client.get(_url(parcel_id) + ".json", params={"farm": farm.id})
    assert response.status_code == 200, response.text
    return response.json()


def test_editing_current_season_keeps_prior_season_and_saved_assessment_context(
    client, session, farm, parcel
):
    prior = _create(client, farm, parcel.id, SEASON_2023)
    current = _create(client, farm, parcel.id, SEASON_2024)
    station = Asset(farm_id=farm.id, kind="station", name="Synthetic station")
    session.add(station)
    session.flush()
    repo = _repo(session, farm)

    def evidence(at):
        snapshot = advisor.input_snapshot(
            repo,
            station,
            parcel.id,
            [],
            AdvisorConfig.for_region_year("FRI12", at.year),
            at,
            "Europe/Paris",
            0,
        )
        return snapshot["crop_season"]

    saved_2023 = evidence(datetime(2023, 6, 1, 4, tzinfo=UTC))
    saved_2024 = evidence(datetime(2024, 6, 1, 4, tzinfo=UTC))
    assert (saved_2023["id"], saved_2023["revision"]) == (prior["id"], 1)
    assert (saved_2024["id"], saved_2024["crop_name"]) == (current["id"], "Merlot")
    # A winter day between the two seasons has no season: unknown, never the nearest one.
    assert evidence(datetime(2024, 1, 15, 12, tzinfo=UTC)) == {"status": "unknown"}

    edited = client.patch(
        _url(parcel.id, current["id"]),
        params={"farm": farm.id},
        json={"expected_revision": 1, "crop_name": "Cabernet franc", "bbch": None},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert edited.json()["unknown"] == ["end", "phenology"]

    seasons = _seasons(client, farm, parcel.id)
    assert seasons["seasons"][0] == prior
    assert seasons["current"]["crop_name"] == "Cabernet franc"
    # The saved evidence names the revision it saw; the next evaluation sees the edit.
    assert saved_2024["revision"] == 1
    assert evidence(datetime(2024, 6, 1, 4, tzinfo=UTC))["revision"] == 2


def _repo(session, farm):
    return ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), "test", "crop seasons"
    )


@pytest.mark.parametrize(
    "overlap",
    [
        {"starts_on": "2023-10-31", "crop_eppo": "VITVI"},  # shares the prior season's last day
        {"starts_on": "2023-01-01", "ends_on": "2023-04-01"},  # straddles the prior start
        {"starts_on": "2024-06-01"},  # inside the open current season
    ],
)
def test_overlapping_seasons_are_rejected_not_merged(client, session, farm, parcel, overlap):
    _create(client, farm, parcel.id, SEASON_2023)
    _create(client, farm, parcel.id, SEASON_2024)
    before = _seasons(client, farm, parcel.id)

    _create(client, farm, parcel.id, overlap, status=409)

    assert _seasons(client, farm, parcel.id) == before


def test_stale_prior_foreign_and_non_owner_edits_change_nothing(
    client, settings, session_factory, session, farm, parcel
):
    prior = _create(client, farm, parcel.id, SEASON_2023)
    current = _create(client, farm, parcel.id, SEASON_2024)
    other_parcel = make_parcel(session, farm, 2)
    other_farm = Farm(name="Other farm")
    session.add(other_farm)
    session.flush()
    ensure_owner_grant(session, other_farm)
    foreign_parcel = make_parcel(session, other_farm, 1)
    session.flush()
    before = _seasons(client, farm, parcel.id)
    edit = {"expected_revision": 1, "crop_name": "Syrah"}

    def patch(parcel_id, season_id, body=edit, farm_id=farm.id, http=client):
        return http.patch(_url(parcel_id, season_id), params={"farm": farm_id}, json=body)

    assert patch(parcel.id, current["id"], {**edit, "expected_revision": 2}).status_code == 409
    assert patch(parcel.id, prior["id"]).status_code == 409  # only the current season is edited
    assert patch(other_parcel.id, current["id"]).status_code == 404
    assert patch(foreign_parcel.id, current["id"]).status_code == 404
    assert patch(parcel.id, current["id"], farm_id=other_farm.id).status_code == 404
    assert (
        client.post(_url(foreign_parcel.id), params={"farm": farm.id}, json=SEASON_2023).status_code
        == 404
    )
    from farmcore.main import create_app

    with TestClient(create_app(settings, session_factory=session_factory)) as anonymous:
        assert patch(parcel.id, current["id"], http=anonymous).status_code == 403

    assert _seasons(client, farm, parcel.id) == before
    assert session.scalar(select(func.count()).select_from(CropSeason)) == 2


def test_legacy_parcel_shows_unknown_season_not_its_crop_props(client, farm, parcel):
    # make_parcel carries legacy props (eppo_crop VITVI, crop_label Vigne): no season facts.
    assert _seasons(client, farm, parcel.id) == {
        "parcel_id": parcel.id,
        "current": None,
        "seasons": [],
    }
    panel = client.get(f"/fields/panel/parcels/{parcel.id}", params={"farm": farm.id})
    assert panel.status_code == 200
    assert "Crop season unknown" in panel.text

    unknown = _create(client, farm, parcel.id, {"starts_on": "2024-03-01"})
    assert (unknown["crop_eppo"], unknown["crop_name"], unknown["bbch"]) == (None, None, None)
    assert unknown["unknown"] == ["crop", "end", "phenology"]


def test_populated_legacy_database_upgrades_without_invented_seasons(scratch_db_url):  # noqa: F811
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "planning_0003")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as db:
            farm_id = db.execute(
                text("INSERT INTO farm(name) VALUES ('Legacy vineyard') RETURNING id")
            ).scalar_one()
            db.execute(
                text(
                    "INSERT INTO parcel(farm_id,name,alias,iacs_id,area_ha,geom,props) VALUES "
                    "(:farm,'Old block','P1','FR-1',1.5,"
                    "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom),4326)),"
                    '\'{"eppo_crop": "VITVI", "variety": "Merlot"}\'::jsonb)'
                ),
                {"farm": farm_id, "geom": MULTIPOLYGON_GEOJSON},
            )
        command.upgrade(cfg, "head")
        with engine.begin() as db:
            assert db.execute(text("SELECT count(*) FROM crop_season")).scalar_one() == 0
            assert db.execute(text("SELECT alias, props->>'variety' FROM parcel")).one() == (
                "P1",
                "Merlot",
            )
        command.downgrade(cfg, "planning_0003")
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
