"""`GET /kpi/` and `/kpi/kpis.json` on a synthetic station file in `tmp_path` (invented values)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.kpi import config, season
from farmcore.apps.kpi.config import Assumptions
from farmcore.apps.operations import seed as ops_seed
from farmcore.core.logs import SourceInput, record_operation
from farmcore.core.models.canonical import AccessLog, Asset
from tests.apps.kpi.synth import seed_farm as _farm
from tests.apps.kpi.synth import write_station


def _spray(session, farm, parcels, ts, source_type, key, *, status="done", payload=None):
    record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[p.id for p in parcels],
        ts_start=ts,
        ts_end=None,
        quantities=[],
        source=SourceInput(source_type, key, payload or {}, None, 1),
        actor="test",
        occurred_at=ts,
        status=status,
    )


def _web_reads(session) -> set[tuple[str, str]]:
    rows = session.scalars(select(AccessLog).where(AccessLog.actor == "owner"))
    return {(r.resource, r.scope) for r in rows}


def _year(body, year):
    return next(y for y in body["years"] if y["year"] == year)


def _kpi(body, year, key):
    return next(k for k in _year(body, year)["kpis"] if k["key"] == key)


def test_json_has_both_years_with_model_calendar_agreste_and_kpis(
    kpi_client, kpi_settings, session
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    resp = kpi_client.get("/kpi/kpis.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["station"] == "33394002"
    assert body["area_ha"] == pytest.approx(25.59)
    assert [y["year"] for y in body["years"]] == [2024, 2025]
    results = season.season_results(season.station_file(kpi_settings.data_dir))
    for y in body["years"]:
        r = results[y["year"]]
        assert y["model"]["passes"] == r.model.passes
        assert y["calendar"]["passes"] == r.calendar.passes
        assert y["agreste_treatments"] == 20.1
        assert _kpi(body, y["year"], "treatments_avoided")["formula"]
    assert body["banner"] == config.BANNER


def test_page_shows_banner_labels_and_every_assumption(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    resp = kpi_client.get("/kpi/")

    assert resp.status_code == 200
    html = resp.text
    assert "one season, one station, replayed" in html
    assert config.CALENDAR_LABEL in html
    assert config.RETROSPECTIVE_LABEL in html
    assert 'data-year="2024"' in html and 'data-year="2025"' in html
    a = Assumptions()
    for name in Assumptions.names():
        field = getattr(a, name)
        assert f'name="{name}"' in html
        assert f'value="{field.value:g}"' in html
        assert field.unit in html and field.label in html
    nav = re.search(r'<nav id="navbar".*?</nav>', html, re.S)
    assert nav and re.search(r'<a href="/kpi/".*?Season &amp; impact</span>', nav.group(0), re.S)


def test_page_shows_the_sourced_diesel_and_gnr_factors(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    diesel = re.search(r'<label[^>]*data-assumption="diesel_l_ha_pass".*?</label>', html, re.S)
    assert diesel and 'value="8.7"' in diesel.group(0)
    assert "L/ha/pass" in diesel.group(0)
    assert "6.4" in diesel.group(0) and "10.4" in diesel.group(0)
    assert "Fédération Cuma Bourgogne 2013–14" in diesel.group(0)
    co2 = re.search(r'<label[^>]*data-assumption="co2_kg_per_l".*?</label>', html, re.S)
    assert co2 and 'value="3.17"' in co2.group(0)
    assert "kg CO₂e/L" in co2.group(0) and "GNR" in co2.group(0)
    assert "Arrêté du 26 avril 2017" in co2.group(0)


def test_copper_counts_are_any_copper_product_next_to_the_e_phy_label_cap(
    kpi_client, kpi_settings, session
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text
    body = kpi_client.get("/kpi/kpis.json").json()

    # The year tables and the lead block both name what is counted; the label cap is said once,
    # next to the lead copper figure (it was repeated under every table).
    for year in ('data-year="2024"', 'data-year="2025"'):
        section = re.search(rf"<section[^>]*{year}.*?</section>", html, re.S).group(0)
        assert "Copper treatments (any copper product)" in section
    lead = re.search(r"<section[^>]*data-lead[^>]*>.*?</section>", html, re.S).group(0)
    assert "copper treatments (any copper product)" in lead.lower()
    assert "max 5 applications per season" in lead
    assert html.count("max 5 applications per season") == 1
    # E-Phy label of the shortlist copper product (operations seed).
    cap = body["copper_label_cap"]
    assert cap == {
        "product": "BOUILLIE BORDELAISE RSR DISPERSS NC",
        "authorisation_no": "9800474",
        "max_applications": 5,
        "source": "https://ephy.anses.fr/ppp/bouillie-bordelaise-rsr-disperss-nc",
        "note": config.COPPER_CAP_NOTE,
    }


def test_the_label_cap_names_the_seeded_copper_product():
    copper = next(p for p in ops_seed.SHORTLIST if p.active_substance == "copper")
    assert (
        copper.name,
        copper.authorisation_no,
        copper.source,
    ) == (config.COPPER_LABEL_PRODUCT, config.COPPER_LABEL_AMM, config.COPPER_LABEL_SOURCE)


def test_large_farm_totals_render_in_fixed_notation(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings, areas=("90000", "9000"))

    body = kpi_client.get("/kpi/kpis.json").json()
    html = kpi_client.get("/kpi/").text

    co2_farm = _kpi(body, 2024, "co2_kg_farm")["value"]
    assert abs(co2_farm) >= 1_000_000  # big enough for %g to switch to e-notation
    assert re.search(r"\de[+-]\d", html) is None
    assert "99,000 ha" in html
    assert f"{co2_farm:,.0f}" in html  # farm totals show whole units


def test_editing_pass_cost_changes_eur_ha_and_marks_the_field(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    base = kpi_client.get("/kpi/kpis.json").json()
    edited = kpi_client.get("/kpi/kpis.json", params={"pass_cost_eur_ha": 40}).json()

    for year in (2024, 2025):
        passes = _kpi(base, year, "passes_avoided")["value"]
        delta = _kpi(edited, year, "eur_ha")["value"] - _kpi(base, year, "eur_ha")["value"]
        assert delta == pytest.approx(passes * 10)
    assert edited["assumptions"]["pass_cost_eur_ha"]["edited"] is True
    assert edited["assumptions"]["form_minutes"]["edited"] is False
    html = kpi_client.get("/kpi/", params={"pass_cost_eur_ha": 40}).text
    assert "edited (default 30)" in html
    assert 'value="40"' in html


@pytest.mark.parametrize("query", [{"pass_cost_eur_ha": "abc"}, {"form_minutes": "-1"}])
@pytest.mark.parametrize("url", ["/kpi/", "/kpi/kpis.json"])
def test_bad_assumption_is_422(kpi_client, kpi_settings, session, query, url):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    assert kpi_client.get(url, params=query).status_code == 422


def test_two_gets_simulate_once_per_year(kpi_client, kpi_settings, session, monkeypatch):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)
    calls: list[int] = []
    real = season.simulate

    def spy(series, cfg, start, end):
        calls.append(cfg.year)
        return real(series, cfg, start, end)

    monkeypatch.setattr(season, "simulate", spy)

    kpi_client.get("/kpi/")
    assert sorted(calls) == [2024, 2025]
    kpi_client.get("/kpi/", params={"pass_cost_eur_ha": 40})
    assert sorted(calls) == [2024, 2025]  # the second response needed no simulation


def test_voice_count_is_clock_bounded_and_ignores_seed_and_file(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    farm, parcels = _farm(session, kpi_settings)
    _spray(session, farm, parcels[:1], datetime(2024, 4, 10, 8, tzinfo=UTC), "voice", "v1")
    _spray(session, farm, parcels[1:], datetime(2024, 4, 20, 8, tzinfo=UTC), "voice", "v2")
    _spray(session, farm, parcels[:1], datetime(2024, 5, 3, 8, tzinfo=UTC), "voice", "later")
    _spray(session, farm, parcels[1:], datetime(2024, 4, 12, 8, tzinfo=UTC), "seed", "s1")
    _spray(session, farm, parcels[:1], datetime(2024, 4, 14, 8, tzinfo=UTC), "file", "f1")
    _spray(
        session,
        farm,
        parcels[1:],
        datetime(2024, 4, 15, 8, tzinfo=UTC),
        "voice",
        "p",
        status="pending",
    )

    body = kpi_client.get("/kpi/kpis.json").json()

    assert _year(body, 2024)["voice_ops"] == 2
    assert _year(body, 2025)["voice_ops"] == 0
    assert _kpi(body, 2024, "minutes_saved")["value"] == pytest.approx(2 * 2.5)


def test_typed_chat_messages_are_not_counted_as_voice(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    farm, parcels = _farm(session, kpi_settings)
    ts = datetime(2024, 4, 10, 8, tzinfo=UTC)
    # The chat handler stores typed text as a `voice` source marked `input: text`.
    _spray(session, farm, parcels[:1], ts, "voice", "said", payload={"input": "voice"})
    _spray(session, farm, parcels[1:], ts, "voice", "typed", payload={"input": "text"})

    body = kpi_client.get("/kpi/kpis.json").json()

    assert _year(body, 2024)["voice_ops"] == 1


@pytest.mark.parametrize("url", ["/kpi/", "/kpi/kpis.json"])
def test_farm_without_a_station_gets_the_message_and_no_numbers(
    kpi_client, kpi_settings, session, url
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings, station=False)

    resp = kpi_client.get(url)

    assert resp.status_code == 200
    assert config.NO_STATION in resp.text
    assert "33394002" not in resp.text  # no station sentence naming one it does not have
    if url.endswith(".json"):
        assert resp.json()["years"] == []
    else:
        assert "data-year" not in resp.text


def test_a_station_that_is_not_33394002_gets_the_message(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    farm, _ = _farm(session, kpi_settings, station=False)
    session.add(Asset(farm_id=farm.id, kind="station", name="Elsewhere", props={}))
    session.flush()

    body = kpi_client.get("/kpi/kpis.json").json()

    assert body["message"] == config.NO_STATION
    assert body["years"] == []


def test_missing_station_file_names_the_file(kpi_client, kpi_settings, session):
    _farm(session, kpi_settings)

    resp = kpi_client.get("/kpi/")

    assert resp.status_code == 200
    assert "demo_station_33394002_2024_2025.csv" in resp.text
    assert "data-year" not in resp.text


@pytest.mark.parametrize("url", ["/kpi/", "/kpi/kpis.json"])
def test_each_get_writes_access_log_rows_with_its_scopes(kpi_client, kpi_settings, session, url):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    assert kpi_client.get(url).status_code == 200

    reads = _web_reads(session)
    assert ("asset", "stations:read") in reads
    assert ("parcel", "fields:read") in reads
    assert ("log", "logs:read") in reads


def test_json_leads_with_2024_copper_vs_two_labelled_calendars(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    body = kpi_client.get("/kpi/kpis.json").json()

    lead = body["lead"]
    assert lead["year"] == 2024
    assert lead["cu_kg_ha_treatment"] == Assumptions().cu_kg_ha_treatment.value
    plain, rapid = lead["calendars"]  # the conservative one first: "at least"
    assert (plain["key"], plain["at_least"]) == ("plain_10d", True)
    assert (rapid["key"], rapid["at_least"]) == ("rapid_growth", False)
    downy = AdvisorConfig.for_region_year(config.REGION, 2024).calendar.downy_interval_days
    assert plain["label"] == config.plain_calendar_label(downy.value)
    assert rapid["label"] == config.CALENDAR_LABEL
    y2024 = _year(body, 2024)
    # Same model run as the year table; the rapid-growth calendar is the year table's calendar.
    assert plain["model_copper"] == rapid["model_copper"] == y2024["model"]["copper"]
    assert rapid["calendar_copper"] == y2024["calendar"]["copper"]
    for c in (plain, rapid):
        assert c["avoided"] == c["calendar_copper"] - c["model_copper"]
        assert c["kg_cu_ha"] == pytest.approx(c["avoided"] * 0.33)


def test_page_shows_the_lead_figure_at_least_first(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    lead = re.search(r"<section[^>]*data-lead[^>]*>.*?</section>", html, re.S)
    assert lead, "lead block missing"
    block = lead.group(0)
    assert "at least" in block
    assert block.index("plain_10d") < block.index("rapid_growth")
    assert "0.33 kg Cu" in block
    assert "`" not in block  # labels are plain text, not markdown
    assert html.index("data-lead") < html.index('data-year="2024"')  # top of the page


def test_lead_kg_follows_the_edited_copper_dose(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    body = kpi_client.get("/kpi/kpis.json?cu_kg_ha_treatment=1").json()

    for c in body["lead"]["calendars"]:
        assert c["kg_cu_ha"] == pytest.approx(c["avoided"])


def test_no_station_has_no_lead(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings, station=False)

    body = kpi_client.get("/kpi/kpis.json").json()

    assert body["lead"] is None


# --- Season & impact page -----------------------------------------------------------------

INTERNAL_NOTES = re.compile(r"R13|review-20|spec §|\bticket\b|KPI sheet", re.I)


def _visible(html: str) -> str:
    """The page's visible text: no script/style, no tags or attributes, entities decoded."""
    import html as htmllib

    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def _tile(html: str, attr: str, key: str) -> str:
    m = re.search(rf'<article[^>]*{attr}="{key}".*?</article>', html, re.S)
    assert m, f"{attr}={key} tile missing"
    return m.group(0)


@pytest.mark.parametrize("areas", [("12.5", "13.09"), ("90000", "9000")])
def test_visible_numbers_are_rounded_to_at_most_two_decimals(
    kpi_client, kpi_settings, session, areas
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings, areas=areas)

    text = _visible(kpi_client.get("/kpi/").text)

    assert re.findall(r"\d\.\d{3,}", text) == []
    body = kpi_client.get("/kpi/kpis.json").json()
    html = kpi_client.get("/kpi/").text
    for key in ("eur_farm", "diesel_l_farm", "co2_kg_farm"):  # farm totals: whole units
        value = _kpi(body, 2024, key)["value"]
        tile = _visible(_tile(_section(html, 2024), "data-kpi", key))
        assert f"{value:,.0f}" in tile


def _section(html: str, year: int) -> str:
    return re.search(rf'<section[^>]*data-year="{year}".*?</section>', html, re.S).group(0)


def test_no_config_keys_in_visible_text(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    text = _visible(kpi_client.get("/kpi/", params={"pass_cost_eur_ha": 40}).text)

    for key in (*Assumptions.names(), "calendar.downy_interval_days", "downy_interval"):
        assert key not in text


def test_no_internal_team_notes_on_the_page(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    assert INTERNAL_NOTES.findall(_visible(html)) == []
    assert "assumed: awaiting agronomist" in html


def test_three_headline_tiles_lead_the_page(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text
    body = kpi_client.get("/kpi/kpis.json").json()

    tiles = re.findall(r'<article[^>]*data-tile="([a-z0-9_]+)"', html)
    assert tiles == ["passes", "copper", "co2"]
    assert html.index('data-tile="passes"') < html.index('data-year="2024"')
    y = _year(body, config.LEAD_YEAR)
    passes = _visible(_tile(html, "data-tile", "passes"))
    assert str(_kpi(body, config.LEAD_YEAR, "passes_avoided")["value"]) in passes
    assert f"model {y['model']['passes']}" in passes
    assert f"calendar {y['calendar']['passes']}" in passes
    plain = body["lead"]["calendars"][0]
    copper = _visible(_tile(html, "data-tile", "copper"))
    assert f"{plain['kg_cu_ha']:.2f}".rstrip("0").rstrip(".") in copper
    assert "kg Cu/ha" in copper
    co2 = _visible(_tile(html, "data-tile", "co2"))
    assert "kg CO₂e/ha" in co2 and "L/ha" in co2


def test_assumed_pill_marks_every_figure_resting_on_an_assumption(
    kpi_client, kpi_settings, session
):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text
    body = kpi_client.get("/kpi/kpis.json").json()

    pill = re.compile(r"<span[^>]*bg-wait-soft[^>]*>\s*assumed\s*</span>")
    for year in (2024, 2025):
        section = _section(html, year)
        for k in _year(body, year)["kpis"]:
            tile = _tile(section, "data-kpi", k["key"])
            assert bool(pill.search(tile)) is k["assumed"], k["key"]
    assert not _kpi(body, 2024, "treatments_avoided")["assumed"]
    assert _kpi(body, 2024, "eur_ha")["assumed"]
    # The headline tiles compare with an assumed calendar; copper also needs an assumed dose.
    for key in ("passes", "copper", "co2"):
        assert pill.search(_tile(html, "data-tile", key)), key


def test_each_copper_figure_names_its_calendar(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    assert "plain 10-day calendar" in _visible(_tile(html, "data-tile", "copper"))
    for year in (2024, 2025):
        cu = _visible(_tile(_section(html, year), "data-kpi", "cu_kg_ha"))
        assert "rapid-growth calendar" in cu
    lead = re.search(r"<section[^>]*data-lead[^>]*>.*?</section>", html, re.S).group(0)
    plain = re.search(r'data-calendar="plain_10d".*?</li>', lead, re.S).group(0)
    rapid = re.search(r'data-calendar="rapid_growth".*?</li>', lead, re.S).group(0)
    assert "plain 10-day calendar" in plain and "rapid-growth calendar" in rapid


def test_assumptions_form_sits_in_a_collapsed_details_card(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    form = '<form method="get" action="/kpi/"'

    def holder(page: str) -> re.Match:
        blocks = re.finditer(r"<details([^>]*)>(.*?)</details>", page, re.S)
        found = [d for d in blocks if form in d.group(2)]
        assert len(found) == 1, "the assumptions form is not inside a <details>"
        return found[0]

    details = holder(html)
    assert not re.search(r"\bopen\b", details.group(1))
    assert re.search(r"<summary[^>]*>.*?Assumptions", details.group(2), re.S)
    # An edited value opens it so the change stays in view.
    edited = holder(kpi_client.get("/kpi/", params={"pass_cost_eur_ha": 40}).text)
    assert re.search(r"\bopen\b", edited.group(1))


def test_sources_are_listed_once_at_the_bottom(kpi_client, kpi_settings, session):
    write_station(kpi_settings.data_dir)
    _farm(session, kpi_settings)

    html = kpi_client.get("/kpi/").text

    sources = list(re.finditer(r"<section[^>]*data-sources[^>]*>.*?</section>", html, re.S))
    assert len(sources) == 1
    sources = sources[0]
    assert sources.start() > html.index('<form method="get" action="/kpi/"')
    assert config.AGRESTE_TREATMENTS.source in sources.group(0)
    assert config.COPPER_LABEL_SOURCE in sources.group(0)
    assert html.count(config.AGRESTE_TREATMENTS.source) == 1
    for table in re.findall(r"(.{0,200})<table", html, re.S):
        assert "overflow-x-auto" in table  # tables scroll inside their card on a phone
