"""Farm production mode (N8): conventional (the demo farm) or organic. Organic mode offers only
shortlist products whose organic status has a source (Reg. (EU) 2021/1165 Annex I); the copper
tracker sums this season's logged copper against Reg. (EU) 2018/1981's 28 kg/ha over 7 years."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, logs
from farmcore.main import create_app

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2024, 6, 1, 8, 0, tzinfo=PARIS)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _part(html: str, name: str) -> str:
    return _text(html.split(f'data-production="{name}"', 1)[1].split("</section>", 1)[0])


def _setup(session, settings, demo, *, mode=None):
    products = ops_seed.ensure_products(session)
    if mode:
        demo.farm.props = {**(demo.farm.props or {}), "production": mode}
    clock.set_replay(session, NOW, settings)
    session.commit()
    return products


def _spray(session, demo, product, at, value="2", unit="kg/ha", parcel=0, key="s"):
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[demo.parcels[parcel].id],
        ts_start=at,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal(value), unit, product.id, product.name)],
        source=logs.SourceInput("file", key, {}, None, logs.RANK_FILE),
        actor="test",
        occurred_at=at,
    )
    session.commit()


def test_seeded_shortlist_carries_organic_sources_only_where_annex_i_lists_the_substance(
    session, demo, tmp_path
):
    ops_seed.seed(session, scene=None, data_dir=tmp_path)
    products = ops_seed.ensure_products(session)

    copper = products["copper-contact"].props["organic"]
    sulphur = products["sulphur-wettable"].props["organic"]
    assert "2021/1165" in copper["source"] and "Bordeaux mixture" in copper["source"]
    assert "2021/1165" in sulphur["source"] and "Sulphur" in sulphur["source"]
    assert copper["url"].startswith("https://eur-lex.europa.eu/")
    assert "organic" not in products["cymoxanil-penetrant"].props
    assert demo.farm.props.get("production") == "conventional"


def test_conventional_farm_shows_its_label_and_offers_the_whole_shortlist(
    client, session, settings, demo
):
    _setup(session, settings, demo)

    html = client.get("/operations/production").text

    assert "Conventional farm" in _text(html)
    offered = _part(html, "offered")
    for name in ("BOUILLIE BORDELAISE RSR DISPERSS NC", "MICROTHIOL SPECIAL DISPERSS", "PAJO"):
        assert name in offered
    assert 'data-production="withheld"' not in html


def test_organic_mode_offers_only_sourced_products_and_says_why_others_are_not(
    client, session, settings, demo
):
    _setup(session, settings, demo, mode="organic")

    html = client.get("/operations/production").text

    assert "Organic farm" in _text(html)
    offered = _part(html, "offered")
    assert "BOUILLIE BORDELAISE RSR DISPERSS NC" in offered and "2021/1165" in offered
    assert "MICROTHIOL SPECIAL DISPERSS" in offered
    assert "PAJO" not in offered
    withheld = _part(html, "withheld")
    assert "PAJO" in withheld and "no source" in withheld.lower()


def test_copper_tracker_sums_this_seasons_logged_copper_against_28_over_7_years(
    client, session, settings, demo
):
    products = _setup(session, settings, demo)
    copper = products["copper-contact"]
    _spray(session, demo, copper, datetime(2024, 5, 9, 8, tzinfo=PARIS), key="may")
    _spray(session, demo, copper, datetime(2024, 5, 20, 8, tzinfo=PARIS), value="1.5", key="b")
    _spray(session, demo, copper, datetime(2024, 6, 20, 8, tzinfo=PARIS), key="after-now")
    _spray(session, demo, copper, datetime(2023, 6, 1, 8, tzinfo=PARIS), key="last-year")

    tracker = _part(client.get("/operations/production").text, "copper")

    # 2 kg/ha + 1.5 kg/ha of a product with 20 % copper (stored cu_fraction 0.2) = 0.7 kg Cu/ha
    assert "P1" in tracker and "0.7 kg Cu/ha" in tracker
    assert "28 kg Cu/ha over 7 years" in tracker and "2018/1981" in tracker
    assert "P2" in tracker and "no copper recorded" in tracker


def test_admin_switches_the_mode_and_bad_values_are_refused(client, session, settings, demo):
    _setup(session, settings, demo)

    r = client.post("/operations/production", data={"mode": "organic"}, follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert demo.farm.props["production"] == "organic"
    assert client.post("/operations/production", data={"mode": "biodynamic"}).status_code == 422


def test_mode_switch_needs_the_admin_and_is_blocked_for_tour_visitors(
    settings, session_factory, session, demo
):
    _setup(session, settings, demo)
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr("t0ken")}),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        assert 'name="mode"' not in c.get("/operations/production").text
        assert c.post("/operations/production", data={"mode": "organic"}).status_code == 403
        c.cookies.set("farmcore_tour", "1")
        r = c.post(
            "/operations/production", data={"mode": "organic"}, headers={"X-Admin-Token": "t0ken"}
        )
        assert r.status_code == 403
    session.expire_all()
    assert demo.farm.props.get("production") in (None, "conventional")


def test_crop_protection_page_loads_the_production_section(client, session, settings, demo):
    _setup(session, settings, demo)

    page = client.get("/advisor/").text

    assert "/operations/production" in page


def test_copper_tracker_cites_the_approval_extension_next_to_the_limit(
    client, session, settings, demo
):
    _setup(session, settings, demo)

    html = client.get("/operations/production").text
    tracker = _part(html, "copper")

    assert "28 kg Cu/ha over 7 years" in tracker
    assert "approval extended to 30 June 2029 by Reg. (EU) 2025/1489" in tracker
    assert "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32025R1489" in html
    assert "not verified" not in tracker


def test_copper_long_parcel_names_keep_full_text_and_wrap_contract(client, session, settings, demo):
    # HTTP checks the layout contract; browser reflow remains a required separate check.
    _setup(session, settings, demo)
    name = "UnbrokenParcelName" * 60
    demo.parcels[0].name = name
    session.commit()
    body = client.get("/operations/production").text
    copper = body.split('data-production="copper"', 1)[1].split("</section>", 1)[0]
    row = next(row for row in re.findall(r"<li\b.*?</li>", copper) if name in row)
    name_class = re.search(r'<span class="([^"]+)"><span class="font-semibold">', row)[1]
    assert "min-w-0" in name_class
    assert "[overflow-wrap:anywhere]" in name_class
    assert 'class="shrink-0 tabular-nums"' in row
    assert name in _text(row) and "no copper recorded" in _text(row)
    assert "truncate" not in row and "overflow-hidden" not in row
