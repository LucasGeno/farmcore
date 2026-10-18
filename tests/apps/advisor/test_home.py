"""`/advisor/` is the Crop protection home: the Today hero's summary first, then every
parcel grouped by decision, then the old table in a closed "All advice (expert)" section."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from farmcore.core import jobs
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.advisor.test_card_plain import by_id, parcel_id

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


def _section(page: str, decision: str) -> dict:
    return by_id(page, f"section-{decision}")


def _home_parcels(page: str) -> list[str]:
    """The aliases listed in the by-decision sections (the expert table excluded)."""
    return re.findall(r'data-home-parcel="([^"]+)"', page)


def _section_markup(page: str, decision: str) -> str:
    """The markup of one by-decision section, up to the next section or the section list end."""
    start = page.index(f'id="section-{decision}"')
    ends = [
        i for i in (page.find('id="section-', start + 1), page.find("</section>", start)) if i > 0
    ]
    return page[start : min(ends)]


def _section_aliases(page: str, decision: str) -> list[str]:
    return _home_parcels(_section_markup(page, decision))


def _home_rows(page: str) -> dict[str, str]:
    """Each by-decision row's markup by alias."""
    return dict(re.findall(r'<li\b[^>]*\bdata-home-parcel="([^"]+)"[^>]*>(.*?)</li>', page, re.S))


@pytest.mark.data
def test_today_hero_and_home_render_the_same_summary(client, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")

    today = by_id(client.get("/advisor/today").text, "today-hero")
    home = by_id(client.get("/advisor/").text, "farm-summary")

    assert home["text"] == today["text"]
    assert home["attrs"]["data-decision"] == today["attrs"]["data-decision"] == "spray"
    assert "Spray 10 parcels today" in home["text"] and "with copper" in home["text"]


@pytest.mark.data
def test_home_groups_every_parcel_once_by_decision(
    client, session, session_factory, settings, manifests, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 06:00"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok

    page = client.get("/advisor/").text

    assert sorted(_home_parcels(page)) == sorted(f"P{n}" for n in range(1, 11))
    assert _section_aliases(page, "protected") == ["P5", "P6"]
    assert _section_aliases(page, "spray") == ["P1", "P2", "P3", "P4", "P7", "P8", "P9", "P10"]
    rows = _home_rows(page)
    assert sorted(rows) == sorted(_home_parcels(page))
    for alias, markup in rows.items():
        link = re.search(r'<a\b[^>]*\bhref="([^"]+)"', markup)
        assert link is not None, f"{alias} links to its parcel"
        destination = urlsplit(unescape(link.group(1)))
        expected_id = parcel_id(session, alias)
        assert destination.path == f"/fields/parcels/{expected_id}"
        query = parse_qs(destination.query)
        assert query["view"] == ["protection"]
        [return_to] = query["return_to"]
        origin = urlsplit(return_to)
        assert origin.path == "/advisor/"
        assert origin.fragment == f"advisor-parcel-{expected_id}"


@pytest.mark.data
def test_home_keeps_wash_off_status_at_every_width(client, seeded, step) -> None:
    # Scene B 15 May: the 9 May copper is washed off. Today shows the badge on phones too.
    seeded("B")
    step("2024-05-15 06:00")

    rows = _home_rows(client.get("/advisor/").text)

    washed = {alias: m for alias, m in rows.items() if "Washed off" in m}
    assert washed, "scene B 15 May has washed-off parcels"
    for markup in washed.values():
        badge = re.search(r"<span data-washed-off[^>]*>", markup)
        assert badge is not None and "hidden" not in badge.group(0)


@pytest.mark.data
def test_home_keeps_the_full_table_in_a_closed_expert_section(client, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    assert "all-advice" not in client.get("/advisor/").text  # Expert off: hidden (N4)
    client.cookies.set("expert", "1")

    page = client.get("/advisor/").text

    expert = by_id(page, "all-advice")
    assert expert["tag"] == "details" and "open" not in expert["attrs"]
    assert "All advice (expert)" in expert["text"]
    for n in range(1, 11):
        assert f"P{n}" in expert["text"]


@pytest.mark.data
def test_home_before_any_advice_says_awaiting_not_all_clear(client, seeded) -> None:
    seeded()

    page = client.get("/advisor/").text

    summary = by_id(page, "farm-summary")
    assert "No advice yet" in summary["text"]
    assert "Nothing to spray" not in page
    assert _section(page, "none")["text"].count("No advice yet") >= 10
