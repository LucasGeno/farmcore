"""The operations map panel sections over HTTP.

- `GET /operations/map/farm`: the latest confirmed spray up to the clock (date, parcels, the
  register's products for that log), else "No confirmed spray yet".
- `GET /operations/map/parcels/{id}`: last spray, sprays this season, products and dose only from
  a log that links this parcel alone, copper this year as known / lower bound / unknown.

Each section reads the request's farm (`?farm=` before the cookie) and answers 404 for a parcel
of another farm. Synthetic parcels and products only; the throwaway test database.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from farmcore.apps.operations import seed as ops_seed
from farmcore.apps.operations.manifest import MANIFEST
from farmcore.core import clock, logs
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from tests.apps.operations.conftest import make_parcel

APR_2 = datetime(2024, 4, 2, 6, tzinfo=UTC)
APR_10 = datetime(2024, 4, 10, 6, tzinfo=UTC)
APR_12 = datetime(2024, 4, 12, 6, tzinfo=UTC)
APR_5 = datetime(2024, 4, 5, 6, tzinfo=UTC)
APR_1 = datetime(2024, 4, 1, 6, tzinfo=UTC)


def text(resp) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", resp.text)).split())


def hrefs(resp) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', resp.text)]


def spray(session, farm, parcels, products, ts, key, doses=None):
    log, merged = logs.record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[p.id for p in parcels],
        ts_start=ts,
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal(dose), "kg/ha", product_id=products[k].id)
            for k, dose in (doses or {"copper-contact": "2"}).items()
        ],
        source=logs.SourceInput("voice", f"tg:map:{key}", {}, 0.9, logs.RANK_VOICE),
        actor="owner",
        occurred_at=ts,
    )
    assert not merged
    return log


def at(session, settings, t):
    clock.set_replay(session, t, settings)
    session.commit()


@pytest.fixture
def f1(session, settings, demo, add_parcel):
    """P91 and P92 share one copper+sulphur log (global quantities); P92 also has its own
    copper spray of 2 kg/ha × 0.2 Cu = 0.4 kg Cu/ha."""
    products = ops_seed.ensure_products(session)
    p91, p92 = add_parcel(91), add_parcel(92)
    spray(session, demo.farm, [p92], products, APR_2, "own92")
    spray(
        session,
        demo.farm,
        [p91, p92],
        products,
        APR_10,
        "shared",
        {"copper-contact": "3", "sulphur-wettable": "5"},
    )
    at(session, settings, APR_12)
    return p91, p92


def test_manifest_declares_the_sections() -> None:
    assert MANIFEST.farm_sections == ("/operations/map/farm",)
    assert MANIFEST.parcel_sections == ("/operations/map/parcels/{id}",)


def test_farm_section_without_sprays(client, session, settings, demo) -> None:
    at(session, settings, APR_12)
    resp = client.get("/operations/map/farm")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<section")
    assert "<html" not in resp.text
    assert "No confirmed spray yet" in text(resp)


def test_farm_section_names_the_latest_spray_as_the_register_lists_it(client, f1) -> None:
    body = text(client.get("/operations/map/farm"))
    assert "Last confirmed spray" in body
    assert "Wed 10 Apr 2024" in body
    assert "P91, P92" in body
    assert "BOUILLIE BORDELAISE RSR DISPERSS NC 3 kg/ha" in body
    assert "MICROTHIOL SPECIAL DISPERSS 5 kg/ha" in body
    assert "/operations/register" in hrefs(client.get("/operations/map/farm"))


def test_parcel_section_f1_shared_log_is_never_this_parcels_dose(client, f1) -> None:
    p91, p92 = f1
    one = client.get(f"/operations/map/parcels/{p91.id}")
    assert one.status_code == 200
    body = text(one)
    assert "Last spray Wed 10 Apr 2024" in body
    assert "Sprays this season (2024) 1" in body
    assert "dose for this parcel unknown (logged with P92)" in body
    assert "copper unknown" in body
    assert "at least 0" not in body
    # The register copies the shared log's 3 kg/ha to every linked parcel: not this parcel's.
    rows = client.get("/operations/register.json").json()
    assert {(r["parcel_alias"], r["dose"]) for r in rows if r["date"] == "2024-04-10"} >= {
        ("P91", "3"),
        ("P92", "3"),
    }
    assert "3 kg/ha" not in body and "MICROTHIOL" not in body

    two = text(client.get(f"/operations/map/parcels/{p92.id}"))
    assert "Sprays this season (2024) 2" in two
    assert "dose for this parcel unknown (logged with P91)" in two
    assert "at least 0.4 kg Cu/ha, incomplete" in two


def test_parcel_section_own_spray_lists_products_and_exact_copper(
    client, session, settings, demo
) -> None:
    products = ops_seed.ensure_products(session)
    p1, p2 = demo.parcels
    spray(session, demo.farm, [p1], products, APR_10, "p1")
    spray(session, demo.farm, [p2], products, APR_10, "p2", {"sulphur-wettable": "5"})
    at(session, settings, APR_12)
    one = text(client.get(f"/operations/map/parcels/{p1.id}"))
    two = text(client.get(f"/operations/map/parcels/{p2.id}"))
    assert "BOUILLIE BORDELAISE RSR DISPERSS NC 2 kg/ha" in one and "MICROTHIOL" not in one
    assert "Copper this year 0.4 kg Cu/ha" in one
    assert "MICROTHIOL SPECIAL DISPERSS 5 kg/ha" in two and "BOUILLIE" not in two
    assert "Copper this year no copper recorded" in two


def test_parcel_without_sprays(client, session, settings, demo) -> None:
    at(session, settings, APR_12)
    body = text(client.get(f"/operations/map/parcels/{demo.parcels[0].id}"))
    assert "No confirmed spray yet" in body
    assert "Sprays this season (2024) 0" in body


def test_backward_replay_excludes_later_sprays(client, session, settings, f1) -> None:
    p91, p92 = f1
    at(session, settings, APR_5)
    farm = text(client.get("/operations/map/farm"))
    assert "Tue 2 Apr 2024" in farm and "10 Apr" not in farm
    one = text(client.get(f"/operations/map/parcels/{p91.id}"))
    assert "No confirmed spray yet" in one and "copper unknown" not in one
    two = text(client.get(f"/operations/map/parcels/{p92.id}"))
    assert "Sprays this season (2024) 1" in two
    assert "Copper this year 0.4 kg Cu/ha" in two and "incomplete" not in two
    at(session, settings, APR_1)
    assert "No confirmed spray yet" in text(client.get("/operations/map/farm"))


@pytest.fixture
def farms(client, session, settings, demo):
    """Farm A (the default, cookie) with its demo parcels; farm B with parcel P202 and a spray."""
    second = Farm(name="Map QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    p202 = make_parcel(session, second, 202, area="2", props={})
    products = ops_seed.ensure_products(session)
    spray(session, second, [p202], products, APR_10, "b202")
    at(session, settings, APR_12)
    client.cookies.set(FARM_COOKIE, str(demo.farm.id), domain="testserver.local")
    return demo.farm, second, p202


def test_alien_parcel_is_404(client, farms, demo) -> None:
    first, second, p202 = farms
    assert client.get(f"/operations/map/parcels/{p202.id}").status_code == 404
    alien = client.get(f"/operations/map/parcels/{demo.parcels[0].id}", params={"farm": second.id})
    assert alien.status_code == 404


def test_query_farm_wins_over_the_cookie_and_qualifies_links(client, farms) -> None:
    first, second, p202 = farms
    farm = client.get("/operations/map/farm", params={"farm": second.id})
    assert "P202" in text(farm)
    assert all(f"farm={second.id}" in h for h in hrefs(farm))
    parcel = client.get(f"/operations/map/parcels/{p202.id}", params={"farm": second.id})
    assert parcel.status_code == 200
    assert "Last spray Wed 10 Apr 2024" in text(parcel)
    links = hrefs(parcel)
    assert links and all(f"farm={second.id}" in h for h in links)
    # Cookie farm A alone has no spray.
    assert "No confirmed spray yet" in text(client.get("/operations/map/farm"))


def test_a_shared_logs_products_are_never_read_for_the_parcel(session, f1) -> None:
    from farmcore.apps.operations.map_sections import _spray_logs
    from farmcore.core.access import ScopedRepository

    p91, p92 = f1
    farm = session.get(Farm, p91.farm_id)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    by_log = {lg.parcels: lg for lg in _spray_logs(repo, p92.id, APR_12, "Europe/Paris")}
    assert by_log[("P91", "P92")].products == ()  # F1 first: nothing read from the shared log
    assert [p.name for p in by_log[("P92",)].products] == ["BOUILLIE BORDELAISE RSR DISPERSS NC"]


@pytest.mark.parametrize("bad", ["0", "99999999999999999999"])
def test_out_of_range_parcel_ids_are_rejected_never_a_500(client, demo, bad) -> None:
    for path in ("/operations/map/parcels/", "/advisor/map/parcels/"):
        assert client.get(path + bad).status_code in (404, 422)


def test_products_without_a_catalogue_entry(client, session, settings, demo) -> None:
    from farmcore.core.models.canonical import Log

    p1, p2 = demo.parcels
    for parcel, key, label, entries in (
        (
            p1,
            "props1",
            "Cuivre maison",
            {
                "Cuivre maison": {"name": "Cuivre maison", "active_substance": "copper"},
                "Mystery": {"name": "Mystery"},
            },
        ),
        (p2, "props2", "something", {}),
    ):
        log, _ = logs.record_operation(
            session,
            farm_id=demo.farm.id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=APR_10,
            ts_end=None,
            quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", None, label)],
            source=logs.SourceInput("voice", f"tg:map:{key}", {}, 0.9, logs.RANK_VOICE),
            actor="owner",
            occurred_at=APR_10,
        )
        session.get(Log, log.id).props = {"products": entries}
        session.flush()
    at(session, settings, APR_12)
    one = text(client.get(f"/operations/map/parcels/{p1.id}"))
    assert "Cuivre maison 2 kg/ha" in one and "Mystery (dose unknown)" in one
    assert "Copper this year copper unknown" in one  # copper named, its amount unknown
    two = text(client.get(f"/operations/map/parcels/{p2.id}"))
    assert "something 2 kg/ha" in two
    assert "Copper this year" not in two  # an unidentified product: no copper claim
