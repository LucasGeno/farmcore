"""Recent records on `/operations/` and the parcel timeline say what
was applied (product, dose and unit) and on which parcels, in the register cards' words."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, logs

T_SPRAY = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
CLOCK = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
COPPER = "BOUILLIE BORDELAISE RSR DISPERSS NC"


@pytest.fixture
def products(session):
    return ops_seed.ensure_products(session)


def _record(session, demo, *, parcels, quantities, type="spray", source=None, at=T_SPRAY):
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type=type,
        parcel_ids=[p.id for p in parcels],
        ts_start=at,
        ts_end=None,
        quantities=quantities,
        source=source or logs.SourceInput("voice", f"tg:{type}:{at}", {}, 0.9, logs.RANK_VOICE),
        actor="owner",
        occurred_at=at,
    )
    return log


def _rate(value: str, unit: str, **kw) -> logs.QuantityInput:
    return logs.QuantityInput("rate", Decimal(value), unit, **kw)


def _get(client, session, settings, path: str) -> str:
    clock.set_replay(session, CLOCK, settings)
    session.commit()
    return client.get(path).text


def _record_html(html: str, log_id: int) -> str:
    """The `<li data-record=…>` of one recent record."""
    start = html.index(f'data-record="{log_id}"')
    return html[start : html.index("</li>", start)]


def _summary(html: str) -> str:
    """The text of the first product summary in `html`, tags stripped."""
    m = re.search(r"<[^>]*data-products[^>]*>(.*?)</(?:p|div|dl)>", html, re.S)
    assert m, "no product summary"
    return _text(m.group(1))


def _text(html: str) -> str:
    """`html` as words: tags stripped, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def test_dose_and_unit_stay_on_one_line_on_a_phone(client, session, settings, demo, products):
    """Records, timeline and register cards keep "2 kg/ha" together at 375 px."""
    _record(
        session,
        demo,
        parcels=demo.parcels[:1],
        quantities=[_rate("2", "kg/ha", product_id=products["copper-contact"].id)],
    )
    nowrap = re.compile(r'<span class="whitespace-nowrap">\s*2 kg/ha</span>')

    for path in (
        "/operations/",
        f"/operations/parcels/{demo.parcels[0].id}/timeline",
        "/operations/register",
    ):
        assert nowrap.search(_get(client, session, settings, path)), path


def test_recent_spray_shows_product_dose_unit_and_every_parcel(
    client, session, settings, demo, products
):
    log = _record(
        session,
        demo,
        parcels=demo.parcels,
        quantities=[_rate("2", "kg/ha", product_id=products["copper-contact"].id)],
    )

    record = _record_html(_get(client, session, settings, "/operations/"), log.id)

    assert f"{COPPER} · 2 kg/ha" in _summary(record)
    assert "P1 Test parcel 1" in record and "P2 Test parcel 2" in record


def test_recent_record_without_dose_shows_the_product_name_only(
    client, session, settings, demo, products
):
    log = _record(session, demo, parcels=demo.parcels[:1], quantities=[])
    log.props = {"products": {"copper": {"name": COPPER}}}  # a named product, no rate
    session.flush()

    summary = _summary(_record_html(_get(client, session, settings, "/operations/"), log.id))

    assert summary == COPPER
    assert "·" not in summary and "None" not in summary


def test_recent_record_lists_each_product_of_a_tank_mix(client, session, settings, demo, products):
    log = _record(
        session,
        demo,
        parcels=demo.parcels,
        quantities=[
            _rate("2", "kg/ha", product_id=products["copper-contact"].id),
            _rate("0.45", "kg/ha", product_id=products["cymoxanil-penetrant"].id),
        ],
    )

    record = _record_html(_get(client, session, settings, "/operations/"), log.id)

    assert f"{COPPER} · 2 kg/ha" in _text(record)
    assert "PAJO · 0.45 kg/ha" in _text(record)


def test_recent_record_without_a_product_shows_no_product_line(client, session, settings, demo):
    log = _record(session, demo, parcels=demo.parcels[:1], quantities=[], type="observation")

    record = _record_html(_get(client, session, settings, "/operations/"), log.id)

    assert "data-products" not in record
    assert "P1 Test parcel 1" in record


def test_summaries_wrap_on_a_phone(client, session, settings, demo, products):
    long_name = "A" * 120  # one unbroken word, longer than a 375 px card
    log = _record(
        session,
        demo,
        parcels=demo.parcels[:1],
        quantities=[_rate("1", "l/ha", label="custom")],
    )
    log.props = {"products": {"custom": {"name": long_name}}}
    session.flush()

    record = _record_html(_get(client, session, settings, "/operations/"), log.id)

    tag = re.search(r"<[^>]*data-products[^>]*>", record).group(0)
    assert "break-words" in tag and "min-w-0" in tag
    assert f"{long_name} · 1 l/ha" in _text(record)


def _timeline(client, session, settings, parcel) -> str:
    return _get(client, session, settings, f"/operations/parcels/{parcel.id}/timeline")


def test_timeline_spray_entry_shows_product_dose_and_parcels(
    client, session, settings, demo, products
):
    _record(
        session,
        demo,
        parcels=demo.parcels,
        quantities=[_rate("2", "kg/ha", product_id=products["copper-contact"].id)],
    )

    html = _timeline(client, session, settings, demo.parcels[1])

    assert f"{COPPER} · 2 kg/ha" in _summary(html)
    assert re.search(r"data-parcels[^>]*>[^<]*P1, P2", html)


def test_timeline_record_without_a_product_shows_parcels_only(client, session, settings, demo):
    _record(session, demo, parcels=demo.parcels[:1], quantities=[], type="observation")

    html = _timeline(client, session, settings, demo.parcels[0])

    assert "data-products" not in html
    assert re.search(r"data-parcels[^>]*>[^<]*P1", html)


def test_timeline_shows_a_merged_record_once_with_its_current_dose(
    client, session, settings, demo, products
):
    """A voice note, then the machine's pass replaces its dose: the summary is the record's
    current one, so it sits on the newest entry only, never on the older voice entry."""
    copper = products["copper-contact"].id
    _record(
        session, demo, parcels=demo.parcels[:1], quantities=[_rate("2", "kg/ha", product_id=copper)]
    )
    _record(
        session,
        demo,
        parcels=demo.parcels[:1],
        quantities=[_rate("1.8", "kg/ha", product_id=copper)],
        source=logs.SourceInput("isoxml", "tlg:1", {}, None, logs.RANK_MACHINE),
        at=T_SPRAY + timedelta(minutes=20),
    )

    html = _timeline(client, session, settings, demo.parcels[0])

    entries = re.findall(r'data-kind="(log\.[a-z]+)"', html)
    assert entries == ["log.merged", "log.created"]
    assert html.count("data-products") == 1
    merged = html[html.index('data-kind="log.merged"') : html.index('data-kind="log.created"')]
    assert f"{COPPER} · 1.8 kg/ha" in _text(merged)
    assert "2 kg/ha" not in html


def test_timeline_unconfirmed_draft_gets_no_summary(client, session, settings, demo, products):
    """A voice draft not yet confirmed (✅) is not a record: its entry says "logged" only, as the
    recent records and the register leave it out."""
    logs.create_pending(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[demo.parcels[0].id],
        ts_start=T_SPRAY,
        ts_end=None,
        quantities=[_rate("2", "kg/ha", product_id=products["copper-contact"].id)],
        source=logs.SourceInput("voice", "tg:draft", {}, 0.9, logs.RANK_VOICE),
        actor="owner",
        occurred_at=T_SPRAY,
    )

    html = _timeline(client, session, settings, demo.parcels[0])

    assert re.findall(r'data-kind="(log\.[a-z]+)"', html) == ["log.created"]
    assert "data-products" not in html and "data-parcels" not in html
