"""`chat.push_advice`: one Telegram digest per farm per run from `advice.changed` events.

Driven through the chat manifest with `jobs.run_now`, a fake sender (`set_sender`) and inserted
fake events (the push does not need the advisor app; a stub `advisor.evaluate` checks the step
order). Synthetic parcels only.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import traceback
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from farmcore.apps.chat import push
from farmcore.apps.chat.manifest import MANIFEST
from farmcore.apps.chat.telegram import FakeTelegramSender, HttpxTelegramSender, TelegramSendError
from farmcore.core import jobs, registry
from farmcore.core.clock import set_replay
from farmcore.core.manifest import JobSpec
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.seed import ensure_default_farm
from farmcore.core.timeline import post_event

PARIS = ZoneInfo("Europe/Paris")
NOW_LOCAL = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
CHAT = 4242
BASE = "https://agri.example.test"


@pytest.fixture
def sender() -> Iterator[FakeTelegramSender]:
    fake = FakeTelegramSender()
    push.set_sender(fake)
    yield fake
    push.set_sender(None)


@pytest.fixture
def bot_settings(settings):
    return settings.model_copy(
        update={"telegram_bot_token": SecretStr("123:test-token"), "public_base_url": BASE}
    )


def seed(session, settings, *, chats=(CHAT,)) -> dict[str, int]:
    farm = ensure_default_farm(session, settings)
    farm.props = {**farm.props, "chat": {"telegram_chat_ids": list(chats)}}
    parcels = {
        alias: Parcel(farm_id=farm.id, alias=alias, name=name, area_ha=Decimal("1"), geom=SQUARE)
        for alias, name in (("P1", "Le Plateau"), ("P5", "Les Granges"), ("P6", "Peyrot"))
    }
    session.add_all(parcels.values())
    set_replay(session, NOW_LOCAL, settings)
    session.commit()
    return {"farm": farm.id, **{k: p.id for k, p in parcels.items()}}


def changed(session, farm_id: int, parcel_id: int, alias: str, name: str, new: str, headline: str):
    """An `advice.changed` event shaped like 13's contract."""
    event = post_event(
        session,
        farm_id=farm_id,
        res_model="parcel",
        res_id=parcel_id,
        kind="advice.changed",
        body=f"Advice changed: none → {new}",
        payload={
            "alias": alias,
            "name": name,
            "old": "none",
            "new": new,
            "headline": headline,
            "card_path": f"/advisor/parcels/{parcel_id}",
        },
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=NOW_LOCAL,
    )
    session.commit()
    return event


def cursor(session, farm_id: int) -> int | None:
    session.expire_all()
    return session.get(Farm, farm_id).props.get("chat", {}).get("push_cursor")


def run(session_factory, settings):
    result = jobs.run_now(session_factory, [MANIFEST], "chat.push_advice", settings)
    assert result is not None and result.ok, result
    return result


def test_two_changes_send_one_digest_listing_both_parcels_then_nothing_on_the_next_run(
    session_factory, session, bot_settings, sender
):
    ids = seed(session, bot_settings)
    changed(session, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray today 06–10 h")
    last = changed(session, ids["farm"], ids["P6"], "P6", "Peyrot", "spray", "Spray today 06–10 h")

    run(session_factory, bot_settings)

    [msg] = sender.sent
    assert msg.chat_id == CHAT
    assert msg.text.splitlines()[0] == "2 parcels → spray"
    assert "P5 Les Granges: Spray today 06–10 h" in msg.text
    assert "P6 Peyrot: Spray today 06–10 h" in msg.text
    assert msg.url_buttons == [
        ("Open card P5", f"{BASE}/advisor/parcels/{ids['P5']}?farm={ids['farm']}"),
        ("Open card P6", f"{BASE}/advisor/parcels/{ids['P6']}?farm={ids['farm']}"),
    ]
    assert cursor(session, ids["farm"]) == last.id

    run(session_factory, bot_settings)

    assert len(sender.sent) == 1
    assert cursor(session, ids["farm"]) == last.id


def test_without_a_bot_token_the_push_sends_nothing_and_keeps_the_cursor(
    session_factory, session, settings, sender
):
    assert settings.telegram_bot_token is None
    ids = seed(session, settings)
    changed(session, ids["farm"], ids["P1"], "P1", "Le Plateau", "spray", "Spray today")

    run(session_factory, settings)

    assert sender.sent == []
    assert cursor(session, ids["farm"]) is None


def test_no_new_events_sends_nothing(session_factory, session, bot_settings, sender):
    seed(session, bot_settings)

    run(session_factory, bot_settings)

    assert sender.sent == []


def test_only_parcels_to_spray_get_a_line_and_a_card_button(
    session_factory, session, bot_settings, sender
):
    ids = seed(session, bot_settings)
    changed(session, ids["farm"], ids["P1"], "P1", "Le Plateau", "protected", "Protected to 18 Apr")
    changed(session, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray today 06–10 h")

    run(session_factory, bot_settings)

    [msg] = sender.sent
    assert msg.text == "1 parcel → spray, 1 parcel → protected\nP5 Les Granges: Spray today 06–10 h"
    assert msg.url_buttons == [
        ("Open card P5", f"{BASE}/advisor/parcels/{ids['P5']}?farm={ids['farm']}")
    ]


def test_a_change_dated_after_now_is_skipped_but_passed_by_the_cursor(
    session_factory, session, bot_settings, sender
):
    """Left over from a run before the clock was stepped back: never pushed."""
    ids = seed(session, bot_settings)
    stale = changed(session, ids["farm"], ids["P1"], "P1", "Le Plateau", "spray", "Spray 15 May")
    stale.occurred_at = datetime(2024, 5, 15, 6, 0, tzinfo=PARIS)
    session.commit()

    run(session_factory, bot_settings)

    assert sender.sent == []
    assert cursor(session, ids["farm"]) == stale.id


def test_a_localhost_base_url_puts_card_links_in_the_text_not_in_buttons(
    session_factory, session, bot_settings, sender
):
    """Telegram rejects inline URL buttons that point at localhost (laptop dev stack)."""
    local = bot_settings.model_copy(update={"public_base_url": "http://localhost:8000"})
    ids = seed(session, local)
    changed(session, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray today 06–10 h")

    run(session_factory, local)

    [msg] = sender.sent
    assert msg.url_buttons == []
    assert f"http://localhost:8000/advisor/parcels/{ids['P5']}" in msg.text


TOKEN = "123:secret-token"


def mock_sender(handler) -> HttpxTelegramSender:
    return HttpxTelegramSender(TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_bot_api_sender_posts_send_message_with_one_keyboard_row_per_button():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    mock_sender(handler).send(
        CHAT, "hi", buttons=[("✅ Confirm", "confirm:1")], url_buttons=[("Open card P1", BASE)]
    )

    [req] = seen
    assert str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(req.content) == {
        "chat_id": CHAT,
        "text": "hi",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Confirm", "callback_data": "confirm:1"}],
                [{"text": "Open card P1", "url": BASE}],
            ]
        },
    }


def _unauthorized(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot reach {request.url}", request=request)


@pytest.mark.parametrize("handler", [_unauthorized, _unreachable], ids=["http", "transport"])
def test_bot_api_sender_errors_never_quote_the_token(handler):
    sender = mock_sender(handler)

    with pytest.raises(TelegramSendError) as info:
        sender.send(CHAT, "hi")

    # What a logged traceback would print, chained exceptions included.
    assert TOKEN not in "".join(traceback.format_exception(info.value))
    assert TOKEN not in repr(sender)


BLOCKED = 666


class OneChatBlockedSender(FakeTelegramSender):
    """Telegram refuses chat `BLOCKED` (the user blocked the bot) and delivers the rest."""

    def send(self, chat_id, text, buttons=(), url_buttons=()):
        if chat_id == BLOCKED:
            raise TelegramSendError("sendMessage failed: HTTP 403 Forbidden: bot was blocked")
        super().send(chat_id, text, buttons, url_buttons)


def test_a_chat_that_refuses_the_send_does_not_block_the_others_or_the_cursor(
    session_factory, session, bot_settings, caplog
):
    blocked = OneChatBlockedSender()
    push.set_sender(blocked)
    try:
        ids = seed(session, bot_settings, chats=(BLOCKED, CHAT))
        last = changed(session, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray now")

        with caplog.at_level(logging.WARNING, logger=push.__name__):
            run(session_factory, bot_settings)
        run(session_factory, bot_settings)
    finally:
        push.set_sender(None)

    assert [m.chat_id for m in blocked.sent] == [CHAT]
    assert cursor(session, ids["farm"]) == last.id
    logged = caplog.text
    assert str(BLOCKED) in logged and "TelegramSendError" in logged
    assert "Spray now" not in logged and "Les Granges" not in logged


def test_sends_happen_after_the_cursor_is_committed_and_the_farm_row_is_unlocked(
    committed_session_factory, bot_settings
):
    """`/start` takes the same row lock; a slow Telegram round trip must not hold it."""
    seen: list[int | None] = []

    class ProbingSender(FakeTelegramSender):
        def send(self, chat_id, text, buttons=(), url_buttons=()):
            # Another connection, as the bot's `/start` would use; NOWAIT fails if locked.
            with committed_session_factory() as other:
                props = other.execute(
                    select(Farm.props).where(Farm.id == farm_id).with_for_update(nowait=True)
                ).scalar_one()
                seen.append(props["chat"].get("push_cursor"))
            super().send(chat_id, text, buttons, url_buttons)

    with committed_session_factory() as s:
        ids = seed(s, bot_settings)
        last = changed(s, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray now")
    farm_id = ids["farm"]
    probe = ProbingSender()
    push.set_sender(probe)
    try:
        run(committed_session_factory, bot_settings)
    finally:
        push.set_sender(None)

    assert len(probe.sent) == 1
    assert seen == [last.id]


class ClosableSender(FakeTelegramSender):
    built: list[ClosableSender] = []

    def __init__(self, token: str) -> None:
        super().__init__()
        self.closed = False
        ClosableSender.built.append(self)

    def close(self) -> None:
        self.closed = True


def test_a_run_closes_the_bot_api_sender_it_built(
    session_factory, session, bot_settings, monkeypatch
):
    monkeypatch.setattr(push, "HttpxTelegramSender", ClosableSender)
    ClosableSender.built.clear()
    ids = seed(session, bot_settings)
    changed(session, ids["farm"], ids["P5"], "P5", "Les Granges", "spray", "Spray now")

    run(session_factory, bot_settings)

    [built] = ClosableSender.built
    assert len(built.sent) == 1 and built.closed


def test_bot_api_sender_close_closes_its_own_client_only():
    own = HttpxTelegramSender(TOKEN)
    own.close()
    assert own._client.is_closed

    shared = httpx.Client()
    HttpxTelegramSender(TOKEN, client=shared).close()
    assert not shared.is_closed
    shared.close()


def stub_evaluate(ctx) -> None:
    """Stands in for 13's `advisor.evaluate`: one `advice.changed` event at the step's now."""
    parcel = ctx.session.scalars(select(Parcel).where(Parcel.alias == "P5")).one()
    post_event(
        ctx.session,
        farm_id=parcel.farm_id,
        res_model="parcel",
        res_id=parcel.id,
        kind="advice.changed",
        body="Advice changed: none → spray",
        payload={
            "alias": "P5",
            "name": parcel.name,
            "old": "none",
            "new": "spray",
            "headline": "Spray today 06–10 h",
            "card_path": f"/advisor/parcels/{parcel.id}",
        },
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=ctx.now,
    )


def test_a_replay_step_pushes_the_advice_its_own_evaluate_just_changed(
    session_factory, session, bot_settings, sender
):
    """Load order from the real manifests: chat depends on advisor, so within one step the
    push runs after `advisor.evaluate` and sees its events (README seams)."""
    evaluate = JobSpec(
        name="advisor.evaluate", func=f"{__name__}:stub_evaluate", interval=timedelta(hours=1)
    )
    manifests = [
        dataclasses.replace(m, jobs=(evaluate,))
        if m.name == "advisor"
        else m
        if m.name == "chat"
        else dataclasses.replace(m, jobs=())
        for m in registry.discover()
    ]
    ordered = registry.resolve_order(list(reversed(manifests)))
    seed(session, bot_settings)

    runs = jobs.step_to(session_factory, ordered, NOW_LOCAL, bot_settings)

    assert [(r.name, r.ok) for r in runs] == [
        ("advisor.evaluate", True),
        ("chat.push_advice", True),
    ]
    [msg] = sender.sent
    assert "P5 Les Granges: Spray today 06–10 h" in msg.text


@pytest.mark.parametrize("base", [BASE, "http://localhost:8000"])
def test_each_farms_digest_pins_its_own_links_and_preserves_query_fragment(
    session_factory, session, bot_settings, sender, base
):
    settings = bot_settings.model_copy(update={"public_base_url": base})
    ids = seed(session, settings)
    other = Farm(name="Synthetic second farm", props={"chat": {"telegram_chat_ids": [9898]}})
    session.add(other)
    session.flush()
    parcel = Parcel(farm_id=other.id, alias="P1", name="Other", area_ha=Decimal("1"), geom=SQUARE)
    session.add(parcel)
    session.commit()
    first = changed(session, ids["farm"], ids["P1"], "P1", "Le Plateau", "spray", "Now")
    first.payload = {
        **first.payload,
        "card_path": f"/advisor/parcels/{ids['P1']}?view=detail&farm=999#spray",
    }
    session.commit()
    changed(session, other.id, parcel.id, "P1", "Other", "spray", "Later")

    run(session_factory, settings)

    sent = {msg.chat_id: msg for msg in sender.sent}
    expected = {
        CHAT: f"{base}/advisor/parcels/{ids['P1']}?view=detail&farm={ids['farm']}#spray",
        9898: f"{base}/advisor/parcels/{parcel.id}?farm={other.id}",
    }
    assert set(sent) == {CHAT, 9898}
    for chat, url in expected.items():
        if base.startswith("https:"):
            assert sent[chat].url_buttons == [("Open card P1", url)]
        else:
            assert sent[chat].url_buttons == []
            assert url in sent[chat].text
    session.refresh(first)
    assert first.payload["card_path"].endswith("?view=detail&farm=999#spray")


def test_digest_lists_the_higher_priority_parcel_first_from_the_stored_advice(
    session_factory, session, bot_settings, sender
):
    from farmcore.core.models.canonical import Advice

    ids = seed(session, bot_settings)
    now = NOW_LOCAL
    event = now - timedelta(hours=30)
    risk = [
        {
            "rule": "downy_3_10",
            "verdict": "risk",
            "inputs": {"event_at": event.isoformat(), "active_events": [event.isoformat()]},
        },
        {"rule": "protection_downy", "verdict": "no cover", "inputs": {}},
        {
            "rule": "downy_incubation",
            "inputs": {"pct": 60.0, "expected_symptoms": (now + timedelta(days=9)).isoformat()},
            "threshold": {"incub_alert_pct": 50.0},
        },
    ]
    advice = {}
    for alias, reasons in (("P5", []), ("P6", risk)):  # P5 → "Due", P6 → "Urgent"
        row = Advice(
            farm_id=ids["farm"],
            parcel_id=ids[alias],
            evaluated_at=now,
            decision="spray",
            reasons=reasons,
            next_window_start=now,
        )
        session.add(row)
        session.flush()
        advice[alias] = row.id
    for alias, name in (("P5", "Les Granges"), ("P6", "Peyrot")):
        e = changed(session, ids["farm"], ids[alias], alias, name, "spray", "Spray today 06–10 h")
        e.payload = {**e.payload, "advice_id": advice[alias]}
    session.commit()

    run(session_factory, bot_settings)

    [msg] = sender.sent
    assert msg.text.splitlines() == [
        "2 parcels → spray",
        "P6 Peyrot: Spray today 06–10 h",
        "P5 Les Granges: Spray today 06–10 h",
    ]
    assert [label for label, _ in msg.url_buttons] == ["Open card P6", "Open card P5"]
