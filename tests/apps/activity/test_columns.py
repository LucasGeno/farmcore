"""The Activity page's columns. Default set Time, What, Parcel, Product / dose,
Source; each cell shows only what the data has, "—" otherwise; rows stop at clock now."""

from __future__ import annotations

import html
import re
from datetime import timedelta

from farmcore.core.timeline import post_event
from tests.core.test_activity import NOW, _advice, _spray, farm  # noqa: F401  (fixture)

DASH = "—"
DEFAULT = ["time", "what", "parcel", "product", "source"]


def _text(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(fragment)).split())


def _rows(page: str) -> list[str]:
    return re.findall(r"<tr\b[^>]*data-activity-row[^>]*>.*?</tr>", page, re.S)


def _cells(row: str) -> dict[str, str]:
    """Cell text without the phone-only column label (`md:hidden`)."""
    row = re.sub(r'<span class="[^"]*md:hidden[^"]*">.*?</span>', "", row, flags=re.S)
    return {
        col: _text(body)
        for col, body in re.findall(r'<td\b[^>]*data-col="([a-z]+)"[^>]*>(.*?)</td>', row, re.S)
    }


def _header(page: str) -> list[str]:
    head = page.split("<thead", 1)[1].split("</thead>", 1)[0]
    return re.findall(r'<th\b[^>]*data-col="([a-z]+)"', head)


def test_default_columns_in_order(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    session.commit()
    page = client.get("/activity/").text
    assert _header(page) == DEFAULT


def test_spray_row_cells_come_from_the_data(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P2", "P1"], NOW - timedelta(hours=1))
    session.commit()
    (row,) = _rows(client.get("/activity/").text)
    cells = _cells(row)
    assert list(cells) == DEFAULT
    assert cells["parcel"] == "P1, P2"
    assert cells["product"] == "copper 2 kg/ha"
    assert cells["source"] == "Voice note"
    assert cells["what"] == "Spray logged on P1, P2"
    assert "06:40" in cells["time"]


def test_rows_without_parcel_product_or_source_show_a_dash(client, session, farm) -> None:  # noqa: F811
    post_event(
        session,
        farm_id=farm.id,
        res_model="data_grant",
        res_id=5,
        kind="share.granted",
        body="Granted Coop read access: spray records, until 01 Jun 2024",
        payload={"grantee": "Coop"},
        actor="owner",
        source_app="share",
        occurred_at=NOW - timedelta(hours=2),
    )
    _advice(session, farm, "P4", "advice.changed", NOW - timedelta(hours=1), new="wait")
    session.commit()
    advice, share = (_cells(r) for r in _rows(client.get("/activity/").text))
    assert (share["parcel"], share["product"], share["source"]) == (DASH, DASH, DASH)
    assert share["what"].startswith("Granted Coop read access")
    assert advice["parcel"] == "P4"
    assert (advice["product"], advice["source"]) == (DASH, DASH)
    assert "Wait" in advice["what"]


def test_rows_after_the_clock_are_hidden(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    _spray(session, farm, ["P2"], NOW + timedelta(hours=1))
    session.commit()
    (row,) = _rows(client.get("/activity/").text)
    assert _cells(row)["parcel"] == "P1"
