"""`GET /operations/register`: the spray register as a phone-friendly HTML table.

Same rows and columns as `register.csv` (compared cell by cell with the CSV, so the real
authorisation numbers need no change here), Paris dates, placeholder flags visible, CSV download
and inline JSON viewer links. Synthetic parcels and products only.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape

from sqlalchemy import select

from farmcore.apps.operations import register
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, logs
from farmcore.core.models.canonical import AccessLog, Product

# 10 Apr 2024 21:30 UTC = 23:30 Paris (CEST): the Paris date is still 10 Apr, the time 23:30.
T_LATE = datetime(2024, 4, 10, 21, 30, tzinfo=UTC)
CLOCK_12_APR = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
VOICE = logs.SourceInput("voice", "tg:9", {"transcript": "synthetic"}, 0.9, logs.RANK_VOICE)

OWNER = [*register.CSV_FIELDS, register.SAVED_WEATHER]  # owner exports: saved weather last


def _spray(session, demo, products, ts=T_LATE):
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in demo.parcels],
        ts_start=ts,
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal("2"), "kg/ha", product_id=products[k].id)
            for k in ("copper-contact", "sulphur-wettable")
        ],
        source=VOICE,
        actor="owner",
        occurred_at=ts,
    )
    return log


def _at(session, settings, t):
    clock.set_replay(session, t, settings)
    session.commit()


def _table(html: str) -> tuple[list[str], list[list[str]]]:
    """(header cells, body rows) of the page's register table, tags stripped."""
    [table] = re.findall(r"<table[^>]*data-register[^>]*>(.*?)</table>", html, re.S)

    def cells(row: str, tag: str) -> list[str]:
        found = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", row, re.S)
        return [unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in found]

    [head] = re.findall(r"<thead>(.*?)</thead>", table, re.S)
    [body] = re.findall(r"<tbody>(.*?)</tbody>", table, re.S)
    rows = [cells(r, "td") for r in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)]
    return cells(head, "th"), [r for r in rows if r]


def test_register_page_is_an_html_table_with_the_csv_rows_and_columns(
    client, session, settings, demo
):
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register", headers={"Accept": "text/html"})
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/operations/register.csv").text)))

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'name="viewport"' in page.text  # base.html: phone width
    header, rows = _table(page.text)
    assert header == OWNER
    assert len(rows) == len(csv_rows) == 4
    assert rows == [[r[name] for name in OWNER] for r in csv_rows]
    # Paris date and time, not UTC.
    assert {(r[4], r[5]) for r in rows} == {("2024-04-10", "23:30")}


def test_register_page_links_the_csv_download_and_json_viewer_and_logs_access(
    client, session, settings, demo
):
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    assert page.status_code == 200
    assert 'href="/operations/register.csv"' in page.text
    assert 'href="/operations/register.json"' in page.text
    session.expire_all()
    scopes = {(a.resource, a.scope) for a in session.scalars(select(AccessLog))}
    assert ("log", "register:read") in scopes


def test_register_page_shows_placeholder_authorisations_plainly(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    copper = products["copper-contact"]
    copper.props = {"placeholder": True}
    sulphur = session.get(Product, products["sulphur-wettable"].id)
    sulphur.props = {"placeholder": False}
    session.flush()
    _spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    _, rows = _table(page.text)
    flags = {(r[0], r[2]) for r in rows}
    assert flags == {(copper.name, "yes"), (sulphur.name, "")}
    assert page.text.count('data-placeholder="yes"') == 2  # one copper row per parcel


def test_empty_register_page_says_so(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    assert page.status_code == 200
    _, rows = _table(page.text)
    assert rows == []
    assert "No confirmed sprays yet" in page.text


def test_operations_index_links_the_register_page(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)

    assert 'href="/operations/register"' in client.get("/operations/").text


def _card_checks(html: str) -> list[list[tuple[str, str, str]]]:
    """Per register card (newest first): (key, status, text) of each check line, tags stripped."""
    cards = html[html.index('id="register-cards"') : html.index("data-register")]
    out = []
    for card in cards.split("data-spray=")[1:]:
        lines = re.findall(
            r'<li data-check="([^"]+)" data-status="([^"]+)"[^>]*>(.*?)</li>', card, re.S
        )
        out.append(
            [(k, s, " ".join(unescape(re.sub(r"<[^>]+>", " ", t)).split())) for k, s, t in lines]
        )
    return out


def test_register_cards_show_each_spray_checks_with_an_icon_and_a_word(
    client, session, settings, demo
):
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    [card] = _card_checks(page.text)
    copper = products["copper-contact"].id
    assert card[0] == ("copper", "ok", "OK Copper 0.4 of 4 kg Cu/ha this year")
    assert (f"applications:{copper}", "ok", "OK 1 of 5 copper applications this season") in card
    assert card[-1] == ("phi", "info", "Note Harvest not before 13 Apr")
    lines = re.findall(r"<li data-check=[^>]*>.*?</li>", page.text, re.S)
    assert all("<svg" in line for line in lines)  # colour is never alone


def test_a_copper_spray_above_4_kg_cu_warns_in_amber_with_the_reason(
    client, session, settings, demo
):
    products = ops_seed.ensure_products(session)
    for day in (1, 2):  # 10 kg/ha of a 20 % copper product: 2 kg Cu/ha each
        log, _ = logs.record_operation(
            session,
            farm_id=demo.farm.id,
            type="spray",
            parcel_ids=[demo.parcels[0].id],
            ts_start=datetime(2024, 4, day, 6, tzinfo=UTC),
            ts_end=None,
            quantities=[
                logs.QuantityInput(
                    "rate", Decimal("10"), "kg/ha", product_id=products["copper-contact"].id
                )
            ],
            source=logs.SourceInput("voice", f"tg:cu{day}", {}, 0.9, logs.RANK_VOICE),
            actor="owner",
            occurred_at=datetime(2024, 4, day, 6, tzinfo=UTC),
        )
    _spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    newest = _card_checks(page.text)[0]
    assert newest[0][:2] == ("copper", "warn")
    assert newest[0][2].startswith("Warning Copper 4.4 of 4 kg Cu/ha this year")
    assert "Highest on P1 (P2 0.4)" in newest[0][2]
    assert "28 kg Cu/ha over 7 years" in newest[0][2]
    [warn] = re.findall(r'<li data-check="copper" data-status="warn"[^>]*>', page.text)
    assert "bg-wait-soft" in warn and "text-wait" in warn


def test_register_page_explains_what_it_checks_with_sources(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)

    page = client.get("/operations/register")

    [box] = re.findall(r"<details[^>]*data-rules[^>]*>(.*?)</details>", page.text, re.S)
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", box)).split())
    assert "What we check" in text
    for words in ("4 kg Cu/ha", "28 kg Cu/ha over 7 years", "2018/1981", "E-Phy", "confirmed"):
        assert words in text, words
    assert 'href="https://eur-lex.europa.eu/eli/reg_impl/2018/1981/oj"' in box
    assert 'href="https://ephy.anses.fr/"' in box


def test_operations_links_download_csv_and_json_without_forcing_attachment_headers(client, demo):
    page = client.get("/operations/").text
    for suffix in ("csv", "json"):
        link = re.search(rf'<a\b[^>]*href="/operations/register\.{suffix}"[^>]*>', page)[0]
        assert f'download="spray-register.{suffix}"' in link
        assert 'target="_blank"' not in link
    response = client.get("/operations/register.json")
    assert response.status_code == 200
    assert "attachment" not in response.headers.get("content-disposition", "")
    operations = client.get("/operations/").text
    assert ">Download JSON</a>" in operations


def test_register_inline_json_view_opens_in_a_separate_tab(client, demo):
    page = client.get("/operations/register").text
    link = re.search(r'<a\b[^>]*href="/operations/register.json"[^>]*>', page)[0]
    assert 'target="_blank"' in link
    assert 'rel="noopener noreferrer"' in link
    assert "download=" not in link
    assert ">Open JSON</a>" in page
