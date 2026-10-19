"""Season export (D4): the register CSV/JSON and NGSI-LD narrowed to one season (local calendar
year) and optionally one parcel, from the parcel History and from Sharing. Same exporters, same
access_log rows, same grant scoping.

Values are synthetic, except the live-loop scene test."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from farmcore.apps.share import service
from farmcore.core import logs
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import AccessLog, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

LAST_SEASON = datetime(2023, 6, 1, 6, 0, tzinfo=UTC)
PARIS = ZoneInfo("Europe/Paris")


def _src(key: str) -> logs.SourceInput:
    return logs.SourceInput("voice", f"tg:season:{key}", {"transcript": "synthetic"}, 0.9, 50)


def _csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _ops(body: list[dict]) -> list[dict]:
    return [e for e in body if e["type"] != "AgriParcel"]


def _parcel_names(body: list[dict]) -> set[str]:
    return {e["name"] for e in body if e["type"] == "AgriParcel"}


def _access_count(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(AccessLog))


@pytest.fixture
def two_seasons(demo, spray):
    """P1+P2 sprayed in 2024 (T_SPRAY), P1 only in 2023."""
    spray(source=_src("2024"))
    spray(ts=LAST_SEASON, parcels=[demo.parcels[0]], source=_src("2023"))
    return demo


def test_csv_is_narrowed_to_the_season_and_parcel(admin, two_seasons):
    p2 = two_seasons.parcels[1]

    r = admin.get("/operations/register.csv", params={"season": 2024, "parcel": p2.id})

    assert r.status_code == 200
    rows = _csv(r.text)
    assert {row["parcel_alias"] for row in rows} == {"P2"}
    assert {row["date"][:4] for row in rows} == {"2024"}
    assert 'filename="spray-register-2024-P2.csv"' in r.headers["content-disposition"]
    all_seasons = _csv(admin.get("/operations/register.csv").text)
    assert {row["date"][:4] for row in all_seasons} == {"2023", "2024"}


def test_last_season_and_a_season_with_nothing(admin, two_seasons):
    last = _csv(admin.get("/operations/register.csv", params={"season": 2023}).text)
    assert {(row["parcel_alias"], row["date"]) for row in last} == {("P1", "2023-06-01")}

    empty = admin.get("/operations/register.csv", params={"season": 2022})
    assert empty.status_code == 200 and _csv(empty.text) == []
    assert empty.text.startswith("product_name,")  # the header still opens as a register


def test_json_and_ngsi_ld_take_the_same_filter(admin, two_seasons):
    p1 = two_seasons.parcels[0]
    params = {"season": 2023, "parcel": p1.id}

    rows = admin.get("/operations/register.json", params=params).json()
    body = admin.get("/share/ngsi-ld.json", params=params).json()

    assert [(r["parcel_alias"], r["date"]) for r in rows] == [("P1", "2023-06-01")]
    assert len(_ops(body)) == 1 and _parcel_names(body) == {"P1"}
    assert _ops(admin.get("/share/ngsi-ld.json", params={"season": 2022}).json()) == []


def test_season_end_is_bounded_by_the_clock(admin, demo, spray):
    spray(ts=datetime(2024, 4, 20, 6, tzinfo=UTC), source=_src("after-now"))  # NOW is 12 Apr

    assert _csv(admin.get("/operations/register.csv", params={"season": 2024}).text) == []
    assert _ops(admin.get("/share/ngsi-ld.json", params={"season": 2024}).json()) == []


def test_unknown_parcel_and_bad_season_are_refused(admin, demo):
    for path in ("/operations/register.csv", "/operations/register.json", "/share/ngsi-ld.json"):
        assert admin.get(path, params={"parcel": 999_999}).status_code == 404
        assert admin.get(path, params={"season": "x"}).status_code == 422
        assert admin.get(path, params={"season": 1}).status_code == 422


def test_parcel_ids_beyond_bigint_are_422_not_500(admin, grantee_client, session, owner_repo, demo):
    grant = _grant(owner_repo, session, "adviser_logs", grantee_client.identity_id)
    paths = (
        "/operations/register.csv",
        "/operations/register.json",
        "/share/ngsi-ld.json",
        f"/share/as/{grant.id}/register.json",
        f"/share/as/{grant.id}/ngsi-ld.json",
    )
    for path in paths:
        client = grantee_client if "/as/" in path else admin
        for bad in (2**63, 0, -1):
            assert client.get(path, params={"parcel": bad}).status_code == 422, (path, bad)
        assert client.get(path, params={"parcel": 2**63 - 1}).status_code == 404, path
    assert admin.get("/share/", params={"parcel": 2**63}).status_code == 200


def test_sharing_ignores_non_ascii_digits(admin, demo):
    for params in ({"parcel": "²"}, {"season": "²²²²"}, {"parcel": "٣"}, {"season": "２０２４"}):
        r = admin.get("/share/", params=params)
        assert r.status_code == 200, params
        assert "/operations/register.csv?season=2024" in _links(r.text)


def test_new_year_midnight_falls_in_exactly_one_season(admin, settings, session, demo, spray):
    from farmcore.core.clock import set_replay

    set_replay(session, datetime(2025, 3, 1, 6, tzinfo=UTC), settings)
    session.commit()
    spray(ts=datetime(2025, 1, 1, 0, 0, tzinfo=PARIS), source=_src("new-year"))

    in_2024 = _csv(admin.get("/operations/register.csv", params={"season": 2024}).text)
    in_2025 = _csv(admin.get("/operations/register.csv", params={"season": 2025}).text)
    ld_2024 = admin.get("/share/ngsi-ld.json", params={"season": 2024}).json()
    ld_2025 = admin.get("/share/ngsi-ld.json", params={"season": 2025}).json()

    assert in_2024 == [] and _ops(ld_2024) == []
    assert {r["date"] for r in in_2025} == {"2025-01-01"} and _ops(ld_2025)


def test_other_farm_parcel_is_404(admin, session, demo, other_farm):
    foreign = session.scalar(select(Parcel).where(Parcel.farm_id == other_farm.id))

    for path in ("/operations/register.csv", "/share/ngsi-ld.json"):
        assert admin.get(path, params={"parcel": foreign.id}).status_code == 404


def test_season_exports_write_access_log(admin, session, two_seasons):
    p1 = two_seasons.parcels[0]
    for path in ("/operations/register.csv", "/share/ngsi-ld.json"):
        before = _access_count(session)
        assert admin.get(path, params={"season": 2024, "parcel": p1.id}).status_code == 200
        assert _access_count(session) > before


def _grant(owner_repo, session, preset, recipient_id):
    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset=preset,
        recipient_identity_id=recipient_id,
        purpose="season export",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    return grant


def test_grantee_exports_take_the_filter_and_keep_their_scope(
    grantee_client, session, owner_repo, two_seasons
):
    p1 = two_seasons.parcels[0]
    grant = _grant(owner_repo, session, "adviser_logs", grantee_client.identity_id)
    params = {"season": 2023, "parcel": p1.id}

    rows = grantee_client.get(f"/share/as/{grant.id}/register.json", params=params).json()
    body = grantee_client.get(f"/share/as/{grant.id}/ngsi-ld.json", params=params).json()

    assert [(r["parcel_alias"], r["date"]) for r in rows] == [("P1", "2023-06-01")]
    assert len(_ops(body)) == 1 and _parcel_names(body) == {"P1"}

    weather = _grant(owner_repo, session, "station", grantee_client.identity_id)
    for path in ("register.json", "ngsi-ld.json"):
        r = grantee_client.get(f"/share/as/{weather.id}/{path}", params=params)
        assert r.status_code == 403


def test_grantee_cannot_name_another_farms_parcel(
    grantee_client, session, owner_repo, two_seasons, other_farm
):
    grant = _grant(owner_repo, session, "adviser_logs", grantee_client.identity_id)
    foreign = session.scalar(select(Parcel).where(Parcel.farm_id == other_farm.id))

    for path in ("register.json", "ngsi-ld.json"):
        r = grantee_client.get(f"/share/as/{grant.id}/{path}", params={"parcel": foreign.id})
        assert r.status_code == 404


def _links(page: str) -> list[str]:
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', page)]


def test_sharing_offers_a_season_selector_defaulting_to_the_current_one(admin, two_seasons):
    page = admin.get("/share/").text

    select_html = page.split('name="season"', 1)[1].split("</select>", 1)[0]
    assert re.search(r'<option value="2024" selected>', select_html)
    assert '<option value="2023">' in select_html
    assert 'name="parcel"' in page and ">All parcels<" in page
    links = _links(page)
    assert "/operations/register.csv?season=2024" in links
    assert "/share/ngsi-ld.json?season=2024" in links
    assert '<option value="all">All seasons</option>' in select_html
    every = admin.get("/share/", params={"season": "all"}).text
    assert re.search(r'<option value="all" selected>All seasons', every)
    assert "/operations/register.csv" in _links(every)
    assert "/share/ngsi-ld.json" in _links(every)
    assert 'download="spray-register.csv"' in every
    all_parcels = admin.get("/share/", params={"season": 2023, "parcel": ""})  # the form's "All"
    assert all_parcels.status_code == 200
    assert "/operations/register.csv?season=2023" in _links(all_parcels.text)


def test_sharing_links_carry_the_chosen_season_and_parcel(admin, two_seasons):
    p1 = two_seasons.parcels[0]

    page = admin.get("/share/", params={"season": 2023, "parcel": p1.id}).text

    links = _links(page)
    assert f"/operations/register.csv?season=2023&parcel={p1.id}" in links
    assert f"/share/ngsi-ld.json?season=2023&parcel={p1.id}" in links
    assert 'download="spray-register-2023-P1.csv"' in page
    assert re.search(rf'<option value="{p1.id}" selected>P1', page)


# --- the demo: the parcel History downloads, on the live-loop scene ---------------------------


@pytest.fixture
def live_loop(session, settings, seeded, step):
    from farmcore.apps.advisor.demo import apply_live_loop

    seeded("B")
    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    return farm


def _by_alias(session, farm, alias):
    return session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == alias))


@pytest.mark.data
def test_history_offers_this_seasons_downloads(client, session, live_loop):
    p5 = _by_alias(session, live_loop, "P5")

    html = client.get(f"/advisor/parcels/{p5.id}/history").text

    assert "Download this season" in html
    links = _links(html)
    assert f"/operations/register.csv?season=2024&parcel={p5.id}&farm={live_loop.id}" in links
    assert f"/share/ngsi-ld.json?season=2024&parcel={p5.id}&farm={live_loop.id}" in links


@pytest.mark.data
def test_p5_2024_download_is_non_empty_and_opens(client, session, live_loop, step):
    p5 = _by_alias(session, live_loop, "P5")
    params = {"season": 2024, "parcel": p5.id}
    step("2024-05-15 06:00")  # the Scene B copper pass on 9 May covers P5 too

    r = client.get("/operations/register.csv", params=params)
    rows = _csv(r.text)
    body = client.get("/share/ngsi-ld.json", params=params).json()

    assert r.status_code == 200 and rows
    assert {row["parcel_alias"] for row in rows} == {"P5"}
    assert {row["date"] for row in rows} == {"2024-05-09"}
    assert _ops(body) and _parcel_names(body) == {"P5"}
