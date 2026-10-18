"""The parcel timeline shows one advice entry per evaluation, in plain words, and keeps
the case of dates ("18 Apr", never "18 apr")."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from farmcore.core import clock
from farmcore.core.timeline import post_event

FIRST = datetime(2024, 4, 9, 4, 0, tzinfo=UTC)  # 06:00 Paris
STEP = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
CLOCK = datetime(2024, 4, 12, 4, 0, tzinfo=UTC)


def _advice(session, demo, kind: str, body: str, at: datetime) -> None:
    post_event(
        session,
        farm_id=demo.farm.id,
        res_model="parcel",
        res_id=demo.parcels[0].id,
        kind=kind,
        body=body,
        payload={},
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=at,
    )


def _evaluation(session, demo, old: str, new: str, headline: str, at: datetime) -> None:
    """What the advisor job posts per run: `advice.evaluated`, then `advice.changed`."""
    _advice(session, demo, "advice.evaluated", headline, at)
    _advice(session, demo, "advice.changed", f"P1: {old} → {new}. {headline}", at)


def _timeline(client, session, settings, demo) -> str:
    clock.set_replay(session, CLOCK, settings)
    session.commit()
    return client.get(f"/operations/parcels/{demo.parcels[0].id}/timeline").text


def test_one_entry_per_evaluation_in_plain_words(client, session, settings, demo):
    _evaluation(session, demo, "none", "spray", "Spray copper today before 11:00", FIRST)
    _evaluation(session, demo, "spray", "protected", "Protected until 18 Apr", STEP)

    html = _timeline(client, session, settings, demo)

    entries = re.findall(r'<li class="[^"]*" data-kind="([a-z.]+)"', html)
    assert entries == ["advice.changed", "advice.changed"]  # newest first, no duplicates
    assert "Advice changed: Spray → Protected" in html
    assert "Advice changed: No action → Spray" in html  # `none` is the no-action decision
    assert "Protected until 18 Apr" in html  # the case of the date is kept
    assert "18 apr" not in html
    assert "none →" not in html and "spray → protected" not in html
    assert "10 Apr 2024 06:00" in html and "09 Apr 2024 06:00" in html  # Paris time


def test_an_evaluation_without_a_change_stays_one_plain_entry(client, session, settings, demo):
    _advice(session, demo, "advice.evaluated", "Protected until 18 Apr", STEP)

    html = _timeline(client, session, settings, demo)

    assert re.findall(r'data-kind="([a-z.]+)"', html) == ["advice.evaluated"]
    assert "Advice: Protected until 18 Apr" in html
