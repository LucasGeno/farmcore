"""One markup for both widths (a table from md up, a card per row on a phone with
each cell naming its column) and 50 rows per page with "Show older"."""

from __future__ import annotations

import html
import re
from datetime import timedelta

from tests.apps.activity.test_columns import _rows
from tests.core.test_activity import NOW, _spray, farm  # noqa: F401  (fixture)


def test_rows_are_cards_on_a_phone_and_table_rows_from_md(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    session.commit()
    page = client.get("/activity/").text
    assert re.search(r'<thead class="[^"]*\bhidden md:table-header-group', page)
    (row,) = _rows(page)
    assert re.search(r'<tr [^>]*class="[^"]*\bblock\b[^"]*md:table-row', row)
    labels = re.findall(r'<span class="[^"]*md:hidden[^"]*">([^<]+)</span>', row)
    assert labels == ["Parcel:", "Product / dose:", "Source:"]


def test_fifty_rows_then_show_older(client, session, farm) -> None:  # noqa: F811
    for m in range(1, 56):
        _spray(session, farm, [f"P{m % 10 + 1}"], NOW - timedelta(minutes=m))
    session.commit()
    page = client.get("/activity/").text
    assert len(_rows(page)) == 50
    link = re.search(r'<a [^>]*href="([^"]*before=[^"]*)"[^>]*>\s*Show older', page)
    assert link, "no Show older link"
    older = client.get(html.unescape(link.group(1))).text
    assert len(_rows(older)) == 5
    assert "Show older" not in older


def test_a_bad_before_value_is_a_400(client, farm) -> None:  # noqa: F811
    assert client.get("/activity/", params={"before": "yesterday"}).status_code in (400, 422)


def test_desktop_header_sticks_under_the_top_bar_on_an_opaque_ground(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    session.commit()
    head = client.get("/activity/").text.split("<thead", 1)[1].split("</thead>", 1)[0]
    ths = re.findall(r'<th\b[^>]*class="([^"]*)"', head)
    assert ths
    for cls in ths:  # the top bar is h-14, lg:h-16 (core/base.html)
        assert {"md:sticky", "md:top-14", "lg:top-16", "bg-ground"} <= set(cls.split()), cls
