"""The delivery outbox (`core/outbox.py`): the advice push records intents in its own transaction
and dispatchers deliver them to Telegram, faked at the HTTP boundary (`httpx.MockTransport`
behind the real Bot API sender), which records every request it receives.

Failure model (what these tests are for):
- an intent written although its cause rolled back, or lost when a worker dies after the commit;
- two dispatchers, or a retry after a crash, sending one intent twice once Telegram accepted it;
- a send whose outcome is unknown (crash after the request, read timeout) retried blindly or
  reported as delivered instead of listed for reconciliation;
- a refusal Telegram answered (flood limit, blocked bot) never retried, or retried forever;
- anything sent while external effects are paused, or a stale digest sent when they resume;
- an ambiguous delivery the operator cannot see or settle.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from farmcore.apps.chat import push
from farmcore.apps.chat.manifest import MANIFEST
from farmcore.apps.chat.telegram import HttpxTelegramSender
from farmcore.core import jobs, outbox
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.timeline import post_event

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
CHAT = 4242
TOKEN = "123:synthetic-token"


class Telegram:
    """The Bot API's `sendMessage`, recorded; `answer(chat_id, n)` decides the n-th reply to a
    chat (default: accepted, with a message id)."""

    def __init__(self, answer: Callable[[int, int], httpx.Response] | None = None) -> None:
        self.requests: list[int] = []
        self._answer = answer
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        chat_id = json.loads(request.content)["chat_id"]
        with self._lock:
            self.requests.append(chat_id)
            n = self.requests.count(chat_id)
            message_id = len(self.requests)
        if self._answer is not None:
            return self._answer(chat_id, n)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


@pytest.fixture
def telegram() -> Iterator[Callable[..., Telegram]]:
    def install(answer=None) -> Telegram:
        fake = Telegram(answer)
        client = httpx.Client(transport=httpx.MockTransport(fake.handler))
        push.set_sender(HttpxTelegramSender(TOKEN, client=client))
        return fake

    yield install
    push.set_sender(None)


@pytest.fixture
def bot_settings(settings):
    return settings.model_copy(update={"telegram_bot_token": SecretStr(TOKEN)})


def dispatch(factory, settings) -> None:
    outbox.dispatch(factory, settings, [MANIFEST])


def states(factory) -> dict[int, str]:
    with factory() as s:
        return dict(s.execute(text("SELECT (payload->>'chat_id')::int, state FROM outbox")).all())


def add_farm(factory, chats: tuple[int, ...]) -> int:
    with factory.begin() as s:
        farm = Farm(name="Synthetic farm", props={"chat": {"telegram_chat_ids": list(chats)}})
        s.add(farm)
        s.flush()
        return farm.id


def intents(factory, settings, chats: tuple[int, ...]) -> int:
    """One pending intent per chat, as the push records them."""
    farm_id = add_farm(factory, chats)
    with factory.begin() as s:
        for chat in chats:
            outbox.add(
                s,
                farm_id=farm_id,
                source_app="chat",
                kind=push.DELIVERY_KIND,
                dedupe_key=f"test:{chat}",
                payload={"chat_id": chat, "text": "1 parcel → spray", "url_buttons": []},
                ttl=push.DIGEST_TTL,
            )
    return farm_id


def test_an_intent_exists_only_if_its_cause_commits_and_outlives_a_worker_that_died_first(
    session_factory, session, bot_settings, telegram
):
    sent = telegram()
    farm_id = add_farm(session_factory, (CHAT,))
    parcel = Parcel(farm_id=farm_id, alias="P5", name="Granges", area_ha=Decimal(1), geom=SQUARE)
    session.add(parcel)
    set_replay(session, NOW, bot_settings)
    session.flush()
    post_event(
        session,
        farm_id=farm_id,
        res_model="parcel",
        res_id=parcel.id,
        kind="advice.changed",
        body="Advice changed: none → spray",
        payload={"alias": "P5", "name": "Granges", "old": "none", "new": "spray"},
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=NOW,
    )
    session.commit()

    with session_factory() as cause:  # its cause's transaction fails after the intent
        outbox.add(
            cause,
            farm_id=farm_id,
            source_app="chat",
            kind=push.DELIVERY_KIND,
            dedupe_key="rolled back",
            payload={"chat_id": CHAT, "text": "never", "url_buttons": []},
            ttl=push.DIGEST_TTL,
        )
        cause.rollback()
    assert states(session_factory) == {}

    # Committed by the push job; the worker that would have sent it died: a new one sends it.
    assert jobs.run_now(session_factory, [MANIFEST], push.JOB_NAME, bot_settings).ok
    assert sent.requests == []
    dispatch(session_factory, bot_settings)
    dispatch(session_factory, bot_settings)

    assert sent.requests == [CHAT]
    with session_factory() as s:
        assert s.execute(text("SELECT state, receipt FROM outbox")).one() == (
            "sent",
            {"message_id": 1},
        )


def test_competing_dispatchers_deliver_each_intent_once(
    committed_session_factory, bot_settings, telegram
):
    chats = tuple(range(1001, 1025))
    started = threading.Barrier(4)

    def accept_after_all_started(chat_id: int, n: int) -> httpx.Response:
        # Every dispatcher holds a claim before any send returns.
        with contextlib.suppress(threading.BrokenBarrierError):
            started.wait(timeout=5)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": chat_id}})

    sent = telegram(accept_after_all_started)
    intents(committed_session_factory, bot_settings, chats)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            dispatch(committed_session_factory, bot_settings)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(sent.requests) == list(chats)
    assert set(states(committed_session_factory).values()) == {"sent"}


class WorkerKilled(BaseException):
    """The process dies right after Telegram accepted the message."""


def _accepted_then_killed(chat_id: int, n: int) -> httpx.Response:
    raise WorkerKilled


def _read_timeout(chat_id: int, n: int) -> httpx.Response:
    raise httpx.ReadTimeout("no answer")


@pytest.mark.parametrize(
    "answer", [_accepted_then_killed, _read_timeout], ids=["killed-after-send", "read-timeout"]
)
def test_a_send_with_an_unknown_outcome_is_never_retried_and_is_listed_for_reconciliation(
    session_factory, bot_settings, telegram, monkeypatch, answer
):
    sent = telegram(answer)
    intents(session_factory, bot_settings, (CHAT,))

    with contextlib.suppress(WorkerKilled):
        dispatch(session_factory, bot_settings)
    # The killed worker's claim lapses at once; any later dispatcher sees it.
    monkeypatch.setattr(outbox, "CLAIM_SECONDS", 0)
    dispatch(session_factory, bot_settings)
    dispatch(session_factory, bot_settings)

    assert sent.requests == [CHAT]
    with session_factory() as s:
        assert [row.payload["chat_id"] for row in outbox.ambiguous(s)] == [CHAT]


BLOCKED = 666


FLOODED = 777


def _flood(retry_after: int) -> httpx.Response:
    return httpx.Response(
        429,
        json={
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": retry_after},
        },
    )


def _floods_and_blocked(chat_id: int, n: int) -> httpx.Response:
    if chat_id == BLOCKED:
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})
    if chat_id == FLOODED:
        return _flood(3600)
    if n <= 4:  # more flood answers than refusals allowed: a flood is not a refusal
        return _flood(0)
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


def test_a_refused_send_is_retried_a_flood_waits_as_told_and_a_refusing_chat_ends_failed(
    session_factory, bot_settings, telegram, monkeypatch
):
    monkeypatch.setattr(outbox, "RETRY_SECONDS", 0)
    sent = telegram(_floods_and_blocked)
    intents(session_factory, bot_settings, (CHAT, BLOCKED, FLOODED))

    for _ in range(5):
        dispatch(session_factory, bot_settings)

    assert sent.requests.count(CHAT) == 5  # four floods, then delivered once
    assert sent.requests.count(BLOCKED) == 3  # three refusals, then given up
    assert sent.requests.count(FLOODED) == 1  # told to wait an hour: not asked again yet
    assert states(session_factory) == {CHAT: "sent", BLOCKED: "failed", FLOODED: "pending"}
    with session_factory() as s:
        assert s.execute(
            text(
                "SELECT available_at > now() + interval '59 minutes' FROM outbox "
                "WHERE payload @> :p"
            ),
            {"p": json.dumps({"chat_id": FLOODED})},
        ).scalar_one()


def test_a_stopping_worker_takes_no_new_claim_after_the_send_in_flight(
    session_factory, bot_settings, telegram
):
    """Shutdown cancels the worker's loop but not a pass already running in its thread."""
    stop = threading.Event()

    def accept_then_stop(chat_id: int, n: int) -> httpx.Response:
        stop.set()  # SIGTERM arrives while this send is in flight
        return httpx.Response(200, json={"ok": True, "result": {"message_id": chat_id}})

    sent = telegram(accept_then_stop)
    intents(session_factory, bot_settings, (1, 2, 3))

    outbox.dispatch(session_factory, bot_settings, [MANIFEST], stop=stop)

    assert sent.requests == [1]
    assert states(session_factory) == {1: "sent", 2: "pending", 3: "pending"}


def _set_effects(session, value: str | None) -> None:
    name = session.execute(text("SELECT current_database()")).scalar_one()
    if value is None:
        session.execute(text(f'ALTER DATABASE "{name}" RESET farmcore.external_effects'))
    else:
        setting = f"farmcore.external_effects = '{value}'"
        session.execute(text(f'ALTER DATABASE "{name}" SET {setting}'))


def test_paused_effects_hold_intents_and_enabling_sends_the_fresh_ones_never_the_expired(
    session_factory, session, bot_settings, telegram
):
    sent = telegram()
    stale = 5150
    intents(session_factory, bot_settings, (CHAT, stale))
    session.execute(
        text("UPDATE outbox SET expires_at = now() - interval '1 minute' WHERE payload @> :p"),
        {"p": json.dumps({"chat_id": stale})},
    )
    _set_effects(session, "paused")
    try:
        dispatch(session_factory, bot_settings)
        assert sent.requests == []
        assert states(session_factory)[CHAT] == "pending"

        _set_effects(session, "enabled")
        dispatch(session_factory, bot_settings)
    finally:
        _set_effects(session, None)

    assert sent.requests == [CHAT]
    assert states(session_factory) == {CHAT: "sent", stale: "expired"}


def test_the_outbox_command_lists_an_ambiguous_delivery_and_resends_it_on_request(
    committed_session_factory, settings, bot_settings, telegram, monkeypatch, tmp_path
):
    from typer.testing import CliRunner

    from farmcore.cli import cli
    from farmcore.settings import get_settings

    sent = telegram(_read_timeout)
    intents(committed_session_factory, bot_settings, (CHAT,))
    dispatch(committed_session_factory, bot_settings)
    with committed_session_factory() as s:
        [held] = outbox.ambiguous(s)
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        listed = runner.invoke(cli, ["outbox"])
        resent = runner.invoke(cli, ["outbox", "resolve", str(held.id), "resend"])
        unknown = runner.invoke(cli, ["outbox", "resolve", str(held.id), "resend"])
        empty = runner.invoke(cli, ["outbox"])
    finally:
        get_settings.cache_clear()

    assert listed.exit_code == 0 and str(held.id) in listed.output
    assert resent.exit_code == 0, resent.output
    assert unknown.exit_code == 1  # no longer ambiguous: nothing to settle
    assert empty.exit_code == 0 and str(held.id) not in empty.output
    assert sent.requests == [CHAT]  # the timed-out send only
    again = telegram()
    dispatch(committed_session_factory, bot_settings)
    assert again.requests == [CHAT]
    assert states(committed_session_factory) == {CHAT: "sent"}
