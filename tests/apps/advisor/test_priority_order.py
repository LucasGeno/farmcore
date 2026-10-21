"""Parcels that need a pass are listed by priority (Urgent first), then in their existing order:
Crop protection, the Home attention data, the bell and the Telegram digest."""

from __future__ import annotations

import re
from datetime import timedelta

from farmcore.apps.advisor.presenter import ParcelRow, by_priority, present_farm_advice
from farmcore.apps.chat.push import digest
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.advisor.scenes import local
from tests.apps.share.conftest import make_parcel

NOW = local("2024-04-10 06:00")
TZ = "Europe/Paris"


def _row(n: int, priority: int | None, decision: str = "spray", products=("Copper",)):
    return ParcelRow(
        id=n,
        alias=f"P{n}",
        decision=decision,
        has_advice=True,
        products=products,
        priority=priority,
        priority_label=None if priority is None else {1: "Due", 2: "High", 3: "Urgent"}[priority],
    )


def test_by_priority_puts_higher_first_and_keeps_order_within_a_level():
    rows = [_row(1, 1), _row(2, None, "protected"), _row(3, 3), _row(4, 1), _row(5, 2)]

    assert [r.alias for r in by_priority(rows)] == ["P3", "P5", "P1", "P4", "P2"]


def test_sections_and_hero_groups_follow_priority():
    rows = [
        _row(1, 1, products=("Sulphur",)),
        _row(2, 3, products=("Copper",)),
        _row(3, 2, products=("Sulphur",)),
        _row(4, None, "protected"),
    ]

    farm = present_farm_advice(rows, NOW, TZ)

    assert [r.alias for r in farm.sections[0].rows] == ["P2", "P3", "P1"]
    assert farm.groups[0].products == ("Copper",)  # the urgent parcel's group leads
    assert [r.alias for r in farm.groups[1].rows] == ["P3", "P1"]


def _reasons(level: int) -> list[dict]:
    if level == 1:
        return []
    event = NOW - timedelta(hours=30)
    base = {"threshold": {}, "source": "", "note": ""}
    return [
        {**base, **r}
        for r in [
            {
                "rule": "downy_3_10",
                "verdict": "risk",
                "inputs": {"event_at": event.isoformat(), "active_events": [event.isoformat()]},
            },
            {"rule": "protection_downy", "verdict": "no cover", "inputs": {}},
            {
                "rule": "downy_incubation",
                "verdict": "incubating",
                "inputs": {
                    "pct": 55.0 if level == 3 else 1.0,
                    "expected_symptoms": (NOW + timedelta(days=60)).isoformat(),
                },
                "threshold": {"incub_alert_pct": 50.0},
            },
        ]
    ]


def _farm_with_levels(session, settings, levels=(1, 3, 2)):
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    for n, level in enumerate(levels, 1):
        parcel = make_parcel(session, farm, n)
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=NOW,
                decision="spray",
                reasons=_reasons(level),
                next_window_start=NOW,
                props={"headline": f"Stored reason {n}"},
            )
        )
    set_replay(session, NOW, settings)
    session.commit()
    return farm


def _order(text: str, pattern: str) -> list[str]:
    return list(dict.fromkeys(re.findall(pattern, text)))


def test_crop_protection_home_attention_and_bell_list_by_priority(client, session, settings):
    farm = _farm_with_levels(session, settings)

    crop = client.get(f"/advisor/?farm={farm.id}").text
    home = client.get(f"/advisor/today?view=attention&farm={farm.id}").text
    bell = client.get(f"/notifications?farm={farm.id}").text

    for page in (crop, home):
        assert _order(page, r"/fields/parcels/(\d+)")[:3] == [
            str(i) for i in _ids(session, farm, ("P2", "P3", "P1"))
        ]
    assert _order(bell, r"Stored reason (\d)") == ["2", "3", "1"]


def _ids(session, farm, aliases):
    from sqlalchemy import select

    from farmcore.core.models.canonical import Parcel

    by_alias = {
        p.alias: p.id for p in session.scalars(select(Parcel).where(Parcel.farm_id == farm.id))
    }
    return [by_alias[a] for a in aliases]


def _change(alias: str, priority: int | None):
    return {
        "alias": alias,
        "name": f"Parcel {alias}",
        "new": "spray",
        "headline": f"Spray {alias}",
        "card_path": f"/advisor/parcels/{alias[1:]}",
        "priority": priority,
    }


def test_digest_lists_action_lines_by_priority_and_keeps_its_content():
    changes = [_change("P1", 1), _change("P7", 3), _change("P3", None), _change("P5", 2)]

    text, buttons = digest(changes, "https://agri.example", 1)

    lines = text.splitlines()
    assert lines[0] == "4 parcels → spray"
    assert [line.split()[0] for line in lines[1:]] == ["P7", "P5", "P1", "P3"]
    assert lines[1] == "P7 Parcel P7: Spray P7"
    assert [label for label, _ in buttons] == [
        "Open card P7",
        "Open card P5",
        "Open card P1",
        "Open card P3",
    ]
