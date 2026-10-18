"""App `activity`, the Activity page (`GET /activity/`): every grouped action of the
farm newest first, an "Activity" entry in the menu, switchable on the Apps page; core's Today
feed (`GET /activity/today`) keeps working beside it."""

from __future__ import annotations

import html
import re
from datetime import timedelta

from farmcore.core.models.kernel import AppModule
from tests.core.test_activity import NOW, _spray, farm  # noqa: F401  (fixture)


def _text(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(fragment)).split())


def _rows(page: str) -> list[str]:
    return re.findall(r"<(?:tr|li)\b[^>]*data-activity-row[^>]*>.*?</(?:tr|li)>", page, re.S)


def test_page_lists_actions_newest_first(client, session, farm) -> None:  # noqa: F811
    for h, alias in enumerate(["P1", "P2", "P3", "P4", "P5", "P6", "P7"], start=1):
        _spray(session, farm, [alias], NOW - timedelta(hours=h))  # P1 newest
    session.commit()
    resp = client.get("/activity/")
    assert resp.status_code == 200
    assert "<h1" in resp.text and "Activity" in _text(resp.text.split("<h1", 1)[1])
    rows = _rows(resp.text)
    assert [re.search(r"on (P\d+)", _text(r)).group(1) for r in rows] == [
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
        "P7",
    ]


def test_empty_page_says_so(client, farm) -> None:  # noqa: F811
    assert "Nothing yet" in _text(client.get("/activity/").text)


def test_menu_has_an_activity_entry(client, farm) -> None:  # noqa: F811
    page = client.get("/activity/").text
    bottom = page.split('id="bottom-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/activity/"' in bottom
    assert 'href="/activity/" aria-current="page"' in page


def test_switched_off_app_has_no_page_and_no_menu_entry(client, session, farm) -> None:  # noqa: F811
    session.get(AppModule, "activity").state = "uninstalled"
    session.commit()
    assert client.get("/activity/").status_code == 404
    assert 'href="/activity/"' not in client.get("/today/").text
    assert client.get("/activity/today").status_code == 200  # Today's feed is core's


def test_sharing_time_note_only_appears_while_replay_is_behind_real_time(
    client,
    session,
    settings,
    farm,  # noqa: F811
):
    from datetime import UTC, datetime

    from farmcore.core.clock import set_live, set_replay

    real_now = datetime.now(UTC)
    note = "Sharing changes use real time and appear once the clock reaches them."
    set_replay(session, real_now - timedelta(days=1), settings)
    session.commit()
    assert note in client.get("/activity/").text
    assert note not in client.get("/today/").text
    set_replay(session, real_now + timedelta(days=1), settings)
    session.commit()
    assert note not in client.get("/activity/").text
    set_live(session, settings)
    session.commit()
    assert note not in client.get("/activity/").text
