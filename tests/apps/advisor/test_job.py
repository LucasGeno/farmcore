"""`advisor.evaluate`: one advice row and an `advice.evaluated` event per parcel, `advice.changed`
when the decision differs from the latest advice before the clock now.

Data tests run the real seeds (IGN parcels, St-Émilion station, product shortlist) and the real
manifest jobs through `jobs.step_to` / `jobs.run_now`, like `farmcore seed` + `farmcore replay`.
Expected decisions come from the hero-scene acceptance criteria.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from farmcore.apps.chat import push
from farmcore.apps.chat.telegram import FakeTelegramSender
from farmcore.core import jobs
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Advice, Event, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

# Shared fixtures (scenes.py): seed the demo farm, step the replay clock.
manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


def latest_decisions(session, at: datetime) -> dict[str, str]:
    """alias -> decision of the advice rows evaluated at `at` (latest row per parcel)."""
    rows = session.execute(
        select(Parcel.alias, Advice.decision)
        .join(Parcel, Parcel.id == Advice.parcel_id)
        .where(Advice.evaluated_at == at)
        .order_by(Advice.id)
    )
    return dict(rows.all())


def changed_events(session, at: datetime) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.kind == "advice.changed", Event.occurred_at == at)
            .order_by(Event.id)
        )
    )


@pytest.mark.data
def test_8_april_is_all_none_then_10_april_is_all_spray(session, client, seeded, step) -> None:
    seeded()

    step("2024-04-08 06:00")
    t8 = local("2024-04-08 06:00")
    rows = session.scalars(select(Advice).where(Advice.evaluated_at == t8)).all()
    assert len(rows) == 10
    assert {r.decision for r in rows} == {"none"}
    assert changed_events(session, t8) == []

    step("2024-04-10 06:00")
    t10 = local("2024-04-10 06:00")
    decisions = latest_decisions(session, t10)
    assert len(decisions) == 10
    assert set(decisions.values()) == {"spray"}
    geo = client.get("/fields/parcels.geojson").json()
    assert {f["properties"]["decision"] for f in geo["features"]} == {"spray"}
    changed = changed_events(session, t10)
    assert len(changed) == 10
    assert {(e.payload["old"], e.payload["new"]) for e in changed} == {("none", "spray")}


@pytest.mark.data
def test_confirmed_copper_spray_then_run_now_protects_only_p1(
    session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded()
    step("2024-04-08 06:00")
    step("2024-04-10 06:00")
    t10 = local("2024-04-10 06:00")
    before = len(changed_events(session, t10))

    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
    run = jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    assert run is not None and run.ok

    decisions = latest_decisions(session, t10)
    assert decisions["P1"] == "protected"
    others = {a: d for a, d in decisions.items() if a != "P1"}
    assert others == {f"P{n}": "spray" for n in range(2, 11)}
    new = changed_events(session, t10)[before:]
    assert [(e.payload["alias"], e.payload["old"], e.payload["new"]) for e in new] == [
        ("P1", "spray", "protected")
    ]


def advice_at(session, at: datetime) -> list[Advice]:
    return list(session.scalars(select(Advice).where(Advice.evaluated_at == at)))


@pytest.mark.data
def test_scene_b_15_may_is_spray_washed_off_not_expired(session, seeded, step) -> None:
    seeded("B")
    step("2024-05-15 06:00")

    rows = advice_at(session, local("2024-05-15 06:00"))
    assert len(rows) == 10
    for row in rows:
        assert row.decision == "spray"
        assert "washed_off" in row.props["flags"]
        assert "expired" not in row.props["flags"]


@pytest.mark.data
def test_step_back_compares_with_the_latest_advice_before_the_new_now(
    session, seeded, step
) -> None:
    seeded()
    step("2024-04-08 06:00")
    step("2024-04-10 06:00")
    step("2024-05-15 06:00")
    t10 = local("2024-04-10 06:00")
    first_visit = len(changed_events(session, t10))

    step("2024-04-10 06:00")  # back: the 8 Apr `none` is the latest advice before now
    back = changed_events(session, t10)[first_visit:]
    assert len(back) == 10
    assert {(e.payload["old"], e.payload["new"]) for e in back} == {("none", "spray")}

    step("2024-04-10 07:00")  # still spray: compared with this visit's 06:00 row
    t7 = local("2024-04-10 07:00")
    assert set(latest_decisions(session, t7).values()) == {"spray"}
    assert changed_events(session, t7) == []


@pytest.mark.data
def test_second_evaluation_with_unchanged_inputs_posts_no_change(
    session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    t10 = local("2024-04-10 06:00")
    assert len(changed_events(session, t10)) == 10

    run = jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)

    assert run is not None and run.ok
    assert len(advice_at(session, t10)) == 20
    assert len(changed_events(session, t10)) == 10
    evaluated = session.scalars(
        select(Event).where(Event.kind == "advice.evaluated", Event.occurred_at == t10)
    ).all()
    assert len(evaluated) == 20


@pytest.mark.data
def test_revisiting_a_later_now_compares_with_the_advice_just_before_it(
    session, settings, session_factory, manifests, seeded, step
) -> None:
    """Second demo rehearsal: 15 May rows from the first pass are older than this pass's
    protected 10 Apr row, so stepping to 15 May again must post P1 protected -> spray."""
    seeded("B")
    for to in ("2024-04-08 06:00", "2024-04-10 06:00", "2024-05-15 06:00"):
        step(to)
    step("2024-04-08 06:00")
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok
    assert latest_decisions(session, local("2024-04-10 06:00"))["P1"] == "protected"
    t15 = local("2024-05-15 06:00")
    first_pass = len(changed_events(session, t15))

    step("2024-05-15 06:00")

    assert latest_decisions(session, t15)["P1"] == "spray"
    new = changed_events(session, t15)[first_pass:]
    assert ("P1", "protected", "spray") in [
        (e.payload["alias"], e.payload["old"], e.payload["new"]) for e in new
    ]


@pytest.mark.data
def test_spray_without_a_known_product_gives_no_cover_and_is_flagged(
    session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    t10 = local("2024-04-10 06:00")

    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), None)
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok

    rows = session.execute(
        select(Parcel.alias, Advice.decision, Advice.props)
        .join(Parcel, Parcel.id == Advice.parcel_id)
        .where(Advice.evaluated_at == t10)
        .order_by(Advice.id)
    ).all()
    latest = {alias: (decision, props["flags"]) for alias, decision, props in rows}
    assert latest["P1"][0] == "spray"
    flagged = sorted(a for a, (_, flags) in latest.items() if "spray_without_product" in flags)
    assert flagged == ["P1"]


@pytest.mark.data
def test_the_chat_push_turns_real_advice_changes_into_one_digest(
    session, settings, session_factory, manifests, seeded
) -> None:
    """13 -> 16 contract on the real jobs: the replay step's `advice.changed` events, as the
    advisor posts them, reach `chat.push_advice` (fake Telegram sender, synthetic chat id)."""
    base = "https://agri.example.test"
    bot = settings.model_copy(
        update={"telegram_bot_token": SecretStr("123:test-token"), "public_base_url": base}
    )
    seeded()
    farm = core_seed.ensure_default_farm(session, settings)
    farm.props = {**farm.props, "chat": {"telegram_chat_ids": [4242]}}
    session.commit()
    p1 = session.scalar(select(Parcel).where(Parcel.alias == "P1"))
    fake = FakeTelegramSender()
    push.set_sender(fake)
    try:
        runs = jobs.step_to(session_factory, manifests, local("2024-04-10 06:00"), bot)
        assert all(r.ok for r in runs), runs
        [first] = fake.sent
        assert first.chat_id == 4242
        assert first.text.splitlines()[0] == "10 parcels → spray"
        assert (
            f"{p1.alias} {p1.name}: Spray copper from today 06 h: infection risk after 10.3 mm "
            "rain, not protected"
        ) in first.text
        assert len(first.url_buttons) == 10
        assert (
            "Open card P1",
            f"{base}/advisor/parcels/{p1.id}?farm={farm.id}",
        ) in first.url_buttons

        confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
        assert jobs.run_now(session_factory, manifests, "advisor.evaluate", bot).ok
        assert jobs.run_now(session_factory, manifests, "chat.push_advice", bot).ok
        [second] = fake.sent[1:]
        assert second.text == "1 parcel → protected"
        assert second.url_buttons == []
    finally:
        push.set_sender(None)
