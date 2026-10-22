"""`present_farm_advice` is the one farm-advice summary behind the Today hero and the
`/advisor/` home. Pure: rows in, summary out, no database."""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape

from farmcore.apps.advisor.presenter import FarmAdvice, ParcelRow, parcel_row, present_farm_advice
from farmcore.core.models.canonical import Advice, Parcel
from farmcore.web import templates
from tests.apps.advisor.scenes import local

TZ = "Europe/Paris"
NOW = local("2024-04-10 06:00")


def row(
    n: int,
    decision: str,
    products: tuple[str, ...] = ("copper",),
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    open_ended: bool = False,
    advised: bool = True,
    until: datetime | None = None,
    reason: str = "",
    horizon: int | None = 72,
) -> ParcelRow:
    return ParcelRow(
        id=n,
        alias=f"P{n}",
        name=f"Parcel {n}",
        decision=decision,
        has_advice=advised,
        products=products if decision in ("spray", "wait") else (),
        window_start=start,
        window_end=end,
        window_open_ended=open_ended,
        protected_until=until,
        reason=reason,
        window_horizon_h=horizon,
    )


def spray(n: int, products: tuple[str, ...] = ("copper",), **kw) -> ParcelRow:
    kw.setdefault("start", local("2024-04-10 06:00"))
    kw.setdefault("end", local("2024-04-10 11:00"))
    return row(n, "spray", products, **kw)


def test_a_homogeneous_farm_is_one_group_with_its_products_and_deadline() -> None:
    rows = [spray(n, reason="10.3 mm rain at 12.1 °C → downy mildew risk") for n in (1, 2, 3)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert farm.decision == "spray"
    assert farm.coverage == (3, 3)
    assert len(farm.groups) == 1
    group = farm.groups[0]
    assert group.aliases == ("P1", "P2", "P3")
    assert group.products == ("copper",)
    assert group.when == "until Wed 10 Apr · 11:00"
    assert farm.title == "Spray 3 parcels today"
    assert farm.reason == "10.3 mm rain at 12.1 °C → downy mildew risk"


def test_parcels_with_different_products_are_two_groups_never_a_tank_mix() -> None:
    # Codex QA F4: P1 copper and P2 sulphur read as "with copper + sulphur" (a false tank-mix).
    rows = [spray(1, ("copper",)), spray(2, ("sulphur",)), spray(3, ("copper",))]

    farm = present_farm_advice(rows, NOW, TZ)

    assert [(g.aliases, g.products) for g in farm.groups] == [
        (("P1", "P3"), ("copper",)),
        (("P2",), ("sulphur",)),
    ]
    assert farm.title == "Spray 3 parcels today"


def test_the_same_products_in_a_different_window_are_another_group() -> None:
    rows = [spray(1), spray(2, end=local("2024-04-10 15:00"))]

    farm = present_farm_advice(rows, NOW, TZ)

    assert [g.when for g in farm.groups] == [
        "until Wed 10 Apr · 11:00",
        "until Wed 10 Apr · 15:00",
    ]


def test_more_than_three_groups_is_a_bounded_summary_pointing_to_each_parcel() -> None:
    ends = ("08:00", "09:00", "10:00", "11:00")
    rows = [spray(n, end=local(f"2024-04-10 {e}")) for n, e in enumerate(ends, 1)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert len(farm.groups) == 4
    assert farm.bounded and farm.shown == ()
    assert farm.title == "4 parcels need treatment — see each parcel"
    assert farm.link == "/advisor/"


def test_up_to_three_groups_are_all_shown() -> None:
    ends = ("08:00", "09:00", "10:00")
    rows = [spray(n, end=local(f"2024-04-10 {e}")) for n, e in enumerate(ends, 1)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert not farm.bounded and len(farm.shown) == 3
    assert farm.title == "Spray 3 parcels today"


def test_one_evaluated_and_one_unevaluated_parcel_is_never_an_all_clear() -> None:
    # Codex QA F5: "Nothing to spray today" while P2 had no advice at all.
    rows = [row(1, "none"), row(2, "none", advised=False)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert farm.decision == "none"
    assert farm.coverage == (1, 2)
    assert farm.title == "No spray advised on 1 evaluated parcel"
    assert farm.awaiting == "1 awaiting advice"
    assert "Nothing to spray" not in farm.title and farm.label != "Nothing to do"


def test_every_parcel_evaluated_with_nothing_to_spray_is_the_all_clear() -> None:
    farm = present_farm_advice([row(1, "none"), row(2, "none")], NOW, TZ)

    assert (farm.title, farm.label, farm.awaiting) == (
        "Nothing to spray today",
        "Nothing to do",
        "",
    )


def test_no_advice_on_any_parcel_says_so() -> None:
    farm = present_farm_advice([row(1, "none", advised=False)], NOW, TZ)

    assert (farm.title, farm.label, farm.awaiting) == ("No advice yet", "No advice yet", "")


def test_unevaluated_parcels_are_named_whatever_the_lead_decision() -> None:
    farm = present_farm_advice([spray(1), row(2, "none", advised=False)], NOW, TZ)

    assert farm.decision == "spray" and farm.awaiting == "1 awaiting advice"


def test_all_parcels_protected() -> None:
    until = (local("2024-04-14 06:00"), local("2024-04-12 06:00"))
    rows = [row(n, "protected", until=u) for n, u in enumerate(until, 1)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert (farm.decision, farm.label) == ("protected", "Protected")
    assert farm.title == "All 2 parcels protected"
    assert farm.protected_until == "Fri 12 Apr · 06:00"


def test_some_parcels_protected_and_some_awaiting_advice() -> None:
    rows = [row(1, "protected", until=local("2024-04-14 06:00")), row(2, "none", advised=False)]

    farm = present_farm_advice(rows, NOW, TZ)

    assert farm.title == "1 of 2 parcels protected"
    assert farm.awaiting == "1 awaiting advice"


def test_a_window_to_the_forecast_edge_has_no_deadline() -> None:
    edge = local("2024-04-13 06:00")
    open_now = present_farm_advice([spray(1, end=edge, open_ended=True)], NOW, TZ)
    later = present_farm_advice(
        [spray(1, start=local("2024-04-10 09:00"), end=edge, open_ended=True)], NOW, TZ
    )

    assert open_now.groups[0].when == "spray window open now"
    assert later.groups[0].when == "window open from Wed 10 Apr · 09:00"


def test_an_open_window_names_the_rain_it_lets_through() -> None:
    """The brief said "spray window open now" beside the station's "Rain until 07:00": the
    window allows under 0.5 mm in the next 2 h, the station flags 0.2 mm as rain."""
    edge = local("2024-04-13 06:00")
    advice = Advice(
        decision="spray",
        props={},
        products=[{"name": "copper"}],
        next_window_start=NOW,
        next_window_end=edge,
        evaluated_at=NOW,
        reasons=[
            {
                "rule": "spray_window",
                "inputs": {"rain_next_mm": 0.2},
                "threshold": {
                    "window_horizon_h": 72,
                    "window_rain_lookahead_h": 2,
                    "window_rain_next_max_mm": 0.5,
                },
            }
        ],
    )
    parcel = Parcel(id=1, alias="P1", name="Parcel 1", props={}, area_ha=1.0)

    when = present_farm_advice([parcel_row(parcel, advice, TZ)], NOW, TZ).groups[0].when

    assert "0.2 mm rain in next 2 h" in when
    assert "0.5 mm limit" in when


def test_wait_names_the_next_window_or_none() -> None:
    nxt = row(1, "wait", start=local("2024-04-11 08:00"), end=local("2024-04-11 12:00"))

    assert present_farm_advice([nxt], NOW, TZ).groups[0].when == "next window Thu 11 Apr · 08:00"
    assert present_farm_advice([row(1, "wait")], NOW, TZ).groups[0].when == (
        "no spray window in the next 3 days"
    )
    assert present_farm_advice([nxt], NOW, TZ).title == "Wait: 1 parcel at risk"


def test_the_replay_note_follows_the_lead_parcels() -> None:
    replayed = ParcelRow(id=1, alias="P1", decision="spray", has_advice=True, replay=True)

    assert present_farm_advice([replayed, row(2, "none")], NOW, TZ).replay
    assert not present_farm_advice([row(2, "none")], NOW, TZ).replay


def test_sections_hold_every_parcel_exactly_once_in_urgency_order() -> None:
    rows = [
        row(1, "protected", until=local("2024-04-14 06:00")),
        spray(2),
        row(3, "none", advised=False),
        row(4, "wait"),
        row(5, "none"),
        spray(6, ("sulphur",)),
    ]

    farm = present_farm_advice(rows, NOW, TZ)

    assert [(s.decision, s.title, [r.alias for r in s.rows]) for s in farm.sections] == [
        ("spray", "Spray", ["P2", "P6"]),
        ("wait", "Wait", ["P4"]),
        ("protected", "Protected", ["P1"]),
        ("none", "No advice", ["P3", "P5"]),
    ]


def test_empty_sections_are_left_out() -> None:
    farm = present_farm_advice([spray(1)], NOW, TZ)

    assert [s.decision for s in farm.sections] == ["spray"]


def test_only_action_groups_are_bounded() -> None:
    ends = ("08:00", "09:00", "10:00", "11:00")
    rows = [
        row(n, "protected", start=local(f"2024-04-11 {e}"), end=local(f"2024-04-11 {e}"))
        for n, e in enumerate(ends, 1)
    ]

    farm = present_farm_advice(rows, NOW, TZ)

    assert not farm.bounded and farm.link is None
    assert farm.title == "All 4 parcels protected"


def test_no_window_names_the_stored_horizon_not_an_assumed_one() -> None:
    # The card says `_no_window(window_horizon_h)`; the hero must agree, never a hard-coded 72 h.
    short = present_farm_advice([row(1, "wait", horizon=36)], NOW, TZ)
    unknown = present_farm_advice([row(1, "wait", horizon=None)], NOW, TZ)

    assert short.groups[0].when == "no spray window in the next 36 h"
    assert unknown.groups[0].when == "no spray window in the forecast"


def test_parcels_with_no_window_over_different_horizons_are_not_one_group() -> None:
    farm = present_farm_advice([row(1, "wait"), row(2, "wait", horizon=36)], NOW, TZ)

    assert sorted(g.when for g in farm.groups) == [
        "no spray window in the next 3 days",
        "no spray window in the next 36 h",
    ]


def test_a_window_that_opens_later_names_its_start_and_its_deadline() -> None:
    later = spray(1, start=local("2024-04-10 14:00"), end=local("2024-04-10 18:00"))
    overnight = spray(1, start=local("2024-04-10 22:00"), end=local("2024-04-11 02:00"))

    assert present_farm_advice([later], NOW, TZ).groups[0].when == (
        "from Wed 10 Apr · 14:00 until 18:00"
    )
    assert present_farm_advice([overnight], NOW, TZ).groups[0].when == (
        "from Wed 10 Apr · 22:00 until Thu 11 Apr · 02:00"
    )


# The summary macro (advisor/_summary.html) rendered without a database: what the farmer sees.


def _render(farm: FarmAdvice, **kw) -> str:
    macro = templates.env.get_template("advisor/_summary.html").module.farm_summary
    return unescape(str(macro(farm, **kw)))


def _text(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def _groups_shown(page: str) -> list[str]:
    return [_text(li) for li in re.findall(r"<li data-group[^>]*>(.*?)</li>", page, re.S)]


def _title_link(page: str) -> str | None:
    h2 = re.search(r"<h2[^>]*>(.*?)</h2>", page, re.S)
    assert h2 is not None
    href = re.search(r'<a href="([^"]+)"', h2.group(1))
    return href.group(1) if href else None


def test_the_summary_lists_each_product_group_on_its_own_line_never_a_tank_mix() -> None:
    farm = present_farm_advice([spray(1, ("copper",)), spray(2, ("sulphur",))], NOW, TZ)

    page = _render(farm)

    assert 'data-groups="2"' in page
    assert _groups_shown(page) == [
        "P1 · copper until Wed 10 Apr · 11:00",
        "P2 · sulphur until Wed 10 Apr · 11:00",
    ]
    assert "copper + sulphur" not in page and "sulphur + copper" not in page
    assert "with copper" not in page
    assert _title_link(page) is None


def test_a_homogeneous_summary_names_its_products_once() -> None:
    page = _render(present_farm_advice([spray(1), spray(2)], NOW, TZ))

    assert _groups_shown(page) == []
    assert "with copper" in _text(page)


def test_a_bounded_summary_links_to_each_parcel_from_its_title() -> None:
    ends = ("08:00", "09:00", "10:00", "11:00")
    farm = present_farm_advice(
        [spray(n, end=local(f"2024-04-10 {e}")) for n, e in enumerate(ends, 1)], NOW, TZ
    )

    today = _render(farm, el_id="today-hero")
    home = _render(farm, el_id="farm-summary", link="#by-decision")

    for page in (today, home):
        assert "data-group" not in page
        assert "4 parcels need treatment — see each parcel" in _text(page)
    assert 'id="today-hero"' in today and _title_link(today) == "/advisor/"
    assert 'id="farm-summary"' in home and _title_link(home) == "#by-decision"
