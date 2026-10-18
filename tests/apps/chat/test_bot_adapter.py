"""Bot adapter: real PTB `Update` objects (built from Bot API JSON, no network) reach 14's
handlers, and replies go out through a fake sender. Never polls Telegram.

Synthetic parcels and products only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from telegram import Update

from farmcore.apps.chat import bot as bot_module
from farmcore.apps.chat import push
from farmcore.apps.chat.bot import REPLY_START_FIRST, BotRuntime, build_application
from farmcore.apps.chat.extractor import DraftOperation, FakeExtractor
from farmcore.apps.chat.handlers import ChatDeps
from farmcore.apps.chat.telegram import FakeTelegramSender
from farmcore.apps.chat.transcribe import FakeTranscriber, Transcript
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Log, Parcel, Product
from farmcore.core.seed import ensure_default_farm

PARIS = ZoneInfo("Europe/Paris")
NOW_LOCAL = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
COPPER = "Copper Shield 20 WP"
CHAT = 4242
OGG = b"OggS fake voice bytes"
_ids = count(1)


def seed_farm(session, settings, *, bound: bool = True) -> dict[str, int]:
    """Synthetic farm; `bound`: CHAT is linked to it, as `/start` leaves it."""
    farm = ensure_default_farm(session, settings)
    if bound:
        push.set_chat_props(farm, **{push.CHATS_KEY: [CHAT]})
    parcels = [
        Parcel(farm_id=farm.id, alias=a, name=n, area_ha=Decimal("1"), geom=SQUARE)
        for a, n in (("P1", "Le Plateau"), ("P5", "Les Granges"))
    ]
    copper = Product(
        name=COPPER,
        authorisation_no="TEST-1",
        active_substance="copper",
        product_type="contact",
        targets=["downy"],
    )
    session.add_all([*parcels, copper])
    set_replay(session, NOW_LOCAL, settings)
    session.commit()
    return {"farm": farm.id, **{p.alias: p.id for p in parcels}}


def draft() -> DraftOperation:
    return DraftOperation(
        type="spray",
        parcels=["P1"],
        products=[COPPER],
        dose_value=2,
        dose_unit="kg/ha",
        ts_start=NOW_LOCAL.replace(hour=5),
        confidence=0.9,
        adapter="fake",
    )


# --- fake Telegram side: Bot API JSON → PTB objects, and a bot that serves one voice file ---


def _message(**extra) -> dict:
    return {
        "message_id": next(_ids),
        "date": 1712721600,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": CHAT, "is_bot": False, "first_name": "Farmer"},
        **extra,
    }


def text_update(text: str, bot=None) -> Update:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text)}] * text.startswith("/")
    return Update.de_json(
        {"update_id": next(_ids), "message": _message(text=text, entities=entities)}, bot
    )


def voice_update(bot=None) -> Update:
    voice = {"file_id": "voice-1", "file_unique_id": "u-voice-1", "duration": 4}
    return Update.de_json({"update_id": next(_ids), "message": _message(voice=voice)}, bot)


def callback_update(data: str, bot=None) -> Update:
    query = {
        "id": f"cb-{next(_ids)}",
        "from": {"id": CHAT, "is_bot": False, "first_name": "Farmer"},
        "chat_instance": "ci",
        "data": data,
        "message": _message(text="echo"),
    }
    return Update.de_json({"update_id": next(_ids), "callback_query": query}, bot)


class FakeFile:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(OGG)
        self.bot.downloaded.append(Path(custom_path))
        return Path(custom_path)


class FakeBot:
    def __init__(self) -> None:
        self.downloaded: list[Path] = []
        self.answered: list[str] = []

    async def get_file(self, file_id: str) -> FakeFile:
        assert file_id == "voice-1"
        return FakeFile(self)

    async def answer_callback_query(self, callback_query_id: str, *args, **kwargs) -> bool:
        self.answered.append(callback_query_id)
        return True


class RecordingTranscriber(FakeTranscriber):
    """Records whether the downloaded file existed (with its bytes) while being transcribed."""

    def __init__(self, transcript: Transcript) -> None:
        super().__init__(transcript)
        self.saw_bytes: list[bytes] = []

    def transcribe(self, path, hints):
        self.saw_bytes.append(Path(path).read_bytes())
        return super().transcribe(path, hints)


class FailingTranscriber(RecordingTranscriber):
    def transcribe(self, path, hints):
        super().transcribe(path, hints)
        raise RuntimeError("model crashed")


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def sender() -> FakeTelegramSender:
    return FakeTelegramSender()


def runtime(
    session_factory, settings, sender, transcriber=None, extractor=None, allowed=str(CHAT)
) -> BotRuntime:
    deps = ChatDeps(
        session_factory=session_factory,
        extractor=extractor or FakeExtractor(draft()),
        settings=settings.model_copy(update={"telegram_allowed_user_ids": allowed}),
    )
    return BotRuntime(
        deps=deps,
        transcriber=transcriber or FakeTranscriber(),
        sender=sender,
    )


def handle(coro) -> None:
    asyncio.run(coro)


def pending_logs(session) -> list[Log]:
    session.expire_all()
    return list(session.query(Log).order_by(Log.id))


def test_text_message_reaches_the_handler_and_the_echo_is_sent_with_buttons(
    session_factory, session, settings, sender, bot
):
    ids = seed_farm(session, settings)
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    [log] = pending_logs(session)
    assert (log.status, [p.id for p in log.parcels]) == ("pending", [ids["P1"]])
    [msg] = sender.sent
    assert msg.chat_id == CHAT
    assert msg.text.splitlines()[0].endswith("— confirm?")
    assert f"{COPPER}: 2 kg/ha" in msg.text
    assert msg.buttons == [("✅ Confirm", f"confirm:{log.id}"), ("❌ Cancel", f"cancel:{log.id}")]


def test_voice_note_is_transcribed_from_a_temp_file_that_is_then_deleted(
    session_factory, session, settings, sender, bot
):
    seed_farm(session, settings)
    said = "P1 copper 2 kg this morning"
    transcriber = RecordingTranscriber(Transcript(text=said, language="en", seconds=4.0))
    rt = runtime(session_factory, settings, sender, transcriber=transcriber)

    handle(rt.on_voice(voice_update(), SimpleNamespace(bot=bot)))

    [path] = bot.downloaded
    assert transcriber.saw_bytes == [OGG]
    assert not path.exists()
    [(_, hints)] = transcriber.calls
    assert {"Le Plateau", "Les Granges", COPPER} <= set(hints)
    [log] = pending_logs(session)
    assert log.sources[0].raw_payload["transcript"] == said
    [msg] = sender.sent
    first, *rest = msg.text.splitlines()
    assert said in first  # the transcript comes first
    assert rest[0].endswith("— confirm?")
    assert f"{COPPER}: 2 kg/ha" in msg.text
    assert msg.buttons[0] == ("✅ Confirm", f"confirm:{log.id}")


def test_voice_temp_file_is_deleted_when_transcription_fails(
    session_factory, session, settings, sender, bot
):
    seed_farm(session, settings)
    transcriber = FailingTranscriber(Transcript(text="", language="en", seconds=0.0))
    rt = runtime(session_factory, settings, sender, transcriber=transcriber)

    handle(rt.on_voice(voice_update(), SimpleNamespace(bot=bot)))

    [path] = bot.downloaded
    assert not path.exists()
    assert pending_logs(session) == []
    [msg] = sender.sent
    assert "type" in msg.text.lower()  # asks the farmer to type it instead


def test_confirm_button_reaches_the_callback_handler_and_answers_the_query(
    session_factory, session, settings, sender, bot
):
    seed_farm(session, settings)
    rt = runtime(session_factory, settings, sender)
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))
    confirm = sender.sent[0].buttons[0][1]
    update = callback_update(confirm)

    handle(rt.on_callback(update, SimpleNamespace(bot=bot)))

    [log] = pending_logs(session)
    assert log.status == "done"
    assert bot.answered == [update.callback_query.id]
    assert sender.sent[-1].chat_id == CHAT
    assert sender.sent[-1].text.startswith("✅ Logged")


def test_confirm_reply_carries_open_map_and_open_register_url_buttons(
    session_factory, session, settings, sender, bot
):
    ids = seed_farm(session, settings)
    public = settings.model_copy(update={"public_base_url": "https://agri.example.org/"})
    rt = runtime(session_factory, public, sender)
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))
    echo = sender.sent[-1]
    assert echo.url_buttons == []  # the echo only asks ✅ / ❌

    handle(rt.on_callback(callback_update(echo.buttons[0][1]), SimpleNamespace(bot=bot)))

    reply = sender.sent[-1]
    assert reply.text.startswith("✅ Logged")
    assert reply.buttons == []
    assert reply.url_buttons == [
        ("Open map", f"https://agri.example.org/fields/?farm={ids['farm']}"),
        ("Open register", f"https://agri.example.org/operations/register?farm={ids['farm']}"),
    ]


def test_confirm_reply_on_a_local_base_url_puts_the_links_in_the_text(
    session_factory, session, settings, sender, bot
):
    """Telegram rejects inline URL buttons to local hosts (the laptop dev stack)."""
    ids = seed_farm(session, settings)
    rt = runtime(session_factory, settings, sender)  # public_base_url http://localhost:8000
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    handle(rt.on_callback(callback_update(sender.sent[-1].buttons[0][1]), SimpleNamespace(bot=bot)))

    reply = sender.sent[-1]
    assert reply.url_buttons == []
    assert f"Open map: http://localhost:8000/fields/?farm={ids['farm']}" in reply.text
    assert (
        f"Open register: http://localhost:8000/operations/register?farm={ids['farm']}" in reply.text
    )


def test_cancel_reply_has_no_url_buttons(session_factory, session, settings, sender, bot):
    seed_farm(session, settings)
    public = settings.model_copy(update={"public_base_url": "https://agri.example.org"})
    rt = runtime(session_factory, public, sender)
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    handle(rt.on_callback(callback_update(sender.sent[-1].buttons[1][1]), SimpleNamespace(bot=bot)))

    assert sender.sent[-1].text.startswith("❌ Cancelled")
    assert sender.sent[-1].url_buttons == []


def test_an_unbound_chats_text_gets_the_start_first_hint_and_logs_nothing(
    session_factory, session, settings, sender, bot
):
    """A reset drops the farm, and with it the chat binding `/start` wrote."""
    seed_farm(session, settings, bound=False)
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    [msg] = sender.sent
    assert msg.text == REPLY_START_FIRST
    assert "/start" in msg.text
    assert pending_logs(session) == []


def test_an_unbound_chats_voice_note_gets_the_hint_without_downloading_it(
    session_factory, session, settings, sender, bot
):
    seed_farm(session, settings, bound=False)
    said = "P1 copper 2 kg this morning"
    transcriber = RecordingTranscriber(Transcript(text=said, language="en", seconds=4.0))
    rt = runtime(session_factory, settings, sender, transcriber=transcriber)

    handle(rt.on_voice(voice_update(), SimpleNamespace(bot=bot)))

    assert bot.downloaded == []
    assert transcriber.saw_bytes == []
    [msg] = sender.sent
    assert msg.text == REPLY_START_FIRST
    assert pending_logs(session) == []


def test_after_start_the_same_chat_is_heard(session_factory, session, settings, sender, bot):
    seed_farm(session, settings, bound=False)
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_start(text_update("/start"), SimpleNamespace(bot=bot)))
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    assert sender.sent[-1].text.splitlines()[0].endswith("— confirm?")
    assert f"{COPPER}: 2 kg/ha" in sender.sent[-1].text
    [log] = pending_logs(session)
    assert log.status == "pending"


def test_text_before_the_farm_is_seeded_still_says_seed_first(
    session_factory, session, settings, sender, bot
):
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), SimpleNamespace(bot=bot)))

    [msg] = sender.sent
    assert "farmcore seed" in msg.text


def test_start_binds_the_chat_once_and_replies_with_the_parcel_list(
    session_factory, session, settings, sender, bot
):
    ids = seed_farm(session, settings, bound=False)
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_start(text_update("/start"), SimpleNamespace(bot=bot)))
    handle(rt.on_start(text_update("/start"), SimpleNamespace(bot=bot)))

    session.expire_all()
    farm = session.get(Farm, ids["farm"])
    assert farm.props["chat"]["telegram_chat_ids"] == [CHAT]
    assert len(sender.sent) == 2
    assert "P1 Le Plateau" in sender.sent[0].text
    assert "P5 Les Granges" in sender.sent[0].text


def test_start_before_the_farm_is_seeded_says_so_and_binds_nothing(
    session_factory, session, settings, sender, bot
):
    rt = runtime(session_factory, settings, sender)

    handle(rt.on_start(text_update("/start"), SimpleNamespace(bot=bot)))

    [msg] = sender.sent
    assert "farmcore seed" in msg.text
    assert session.query(Farm).count() == 0


class RoutingRecorder:
    """Stands in for `BotRuntime`: records which entry point each update reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_start(self, update, context):
        self.calls.append("start")

    async def on_text(self, update, context):
        self.calls.append("text")

    async def on_voice(self, update, context):
        self.calls.append("voice")

    async def on_callback(self, update, context):
        self.calls.append("callback")


def test_application_routes_each_update_kind_to_its_handler_without_network():
    recorder = RoutingRecorder()
    app = build_application(recorder, "123:not-a-real-token")
    # `/start` matching reads the bot's username, which a real bot only learns from getMe.
    named_bot = SimpleNamespace(username="farmcore_test_bot", defaults=None)
    updates = [
        text_update("/start", named_bot),
        text_update("P1 copper", named_bot),
        voice_update(named_bot),
        callback_update("confirm:1", named_bot),
    ]

    # Dispatch as PTB does (first matching handler of the group) without `initialize()`,
    # which would call the Bot API.
    async def feed() -> None:
        for update in updates:
            handler = next(h for h in app.handlers[0] if h.check_update(update))
            await handler.callback(update, None)

    handle(feed())

    assert recorder.calls == ["start", "text", "voice", "callback"]


def _other_update(kind: str, **message) -> Update:
    """An edited message or a channel post: same content as a message, different update."""
    named_bot = SimpleNamespace(username="farmcore_test_bot", defaults=None)
    return Update.de_json({"update_id": next(_ids), kind: _message(**message)}, named_bot)


@pytest.mark.parametrize("kind", ["edited_message", "channel_post", "edited_channel_post"])
def test_edits_and_channel_posts_reach_no_handler(kind):
    """An edit re-sends the same message_id, which 14's dedupe would answer with the old draft."""
    app = build_application(RoutingRecorder(), "123:not-a-real-token")
    voice = {"file_id": "voice-1", "file_unique_id": "u-voice-1", "duration": 4}
    updates = [
        _other_update(kind, text="P1 copper 2 kg"),
        _other_update(kind, voice=voice),
        _other_update(
            kind, text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]
        ),
    ]

    assert [h for u in updates for h in app.handlers[0] if h.check_update(u)] == []


def test_bot_command_without_a_token_exits_1_before_touching_the_database(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from farmcore.cli import cli
    from farmcore.settings import get_settings

    monkeypatch.chdir(tmp_path)  # no .env; the hermetic env already dropped the token
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["bot"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN is not set" in result.output


def test_bot_logging_silences_the_loggers_that_print_token_bearing_urls(monkeypatch):
    """httpx logs every request URL at INFO, and Bot API URLs carry the token."""
    from farmcore.apps.chat import commands

    for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger(), "level", logging.getLogger().level)

    commands.configure_logging()

    for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logger = logging.getLogger(name)
        assert logger.getEffectiveLevel() >= logging.WARNING, name
        assert not logger.isEnabledFor(logging.INFO), name


# --- Allowlist: a stranger's update reaches no handler work (no Whisper, no Vertex, no log) ---


class _CountingTranscriber(FakeTranscriber):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def transcribe(self, path, hints):
        self.calls += 1
        return super().transcribe(path, hints)


@pytest.mark.parametrize("allowed", ["", "999"])
def test_a_user_not_on_the_allowlist_gets_one_private_demo_reply_and_nothing_else(
    session_factory, session, settings, sender, bot, allowed
):
    seed_farm(session, settings, bound=False)
    transcriber = _CountingTranscriber()
    extractor = FakeExtractor(draft())
    rt = runtime(session_factory, settings, sender, transcriber, extractor, allowed=allowed)
    ctx = SimpleNamespace(bot=bot)

    handle(rt.on_start(text_update("/start"), ctx))
    handle(rt.on_text(text_update("P1 copper 2 kg this morning"), ctx))
    handle(rt.on_voice(voice_update(), ctx))
    handle(rt.on_callback(callback_update("confirm:1"), ctx))

    assert pending_logs(session) == []
    assert transcriber.calls == 0
    assert [m.text for m in sender.sent] == [bot_module.REPLY_PRIVATE_DEMO]
    farm = session.scalars(select(Farm)).one()
    session.refresh(farm)
    assert not (farm.props or {}).get("chat", {}).get("telegram_chat_ids")


def test_allowlist_parses_comma_separated_ids(settings) -> None:
    s = settings.model_copy(update={"telegram_allowed_user_ids": " 12, 34 ,,56"})
    assert s.allowed_telegram_users() == frozenset({12, 34, 56})


@pytest.mark.parametrize(
    ("target_covers_p6", "pending_covers_p1"), [(False, False), (True, False), (True, True)]
)
def test_voice_confirm_merge_keeps_second_product_in_reply_register_and_checks(
    session_factory, session, settings, sender, bot, target_covers_p6, pending_covers_p1
):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.apps.operations import register
    from farmcore.core.access import ScopedRepository, ensure_owner_grant

    ids = seed_farm(session, settings)
    p6 = Parcel(farm_id=ids["farm"], alias="P6", name="Peyrot", area_ha=Decimal("1"), geom=SQUARE)
    session.add(p6)
    sulphur = Product(
        name="Sulfo Jet 80 WG",
        authorisation_no="TEST-2",
        active_substance="sulphur",
        product_type="contact",
        targets=["powdery"],
        props={"label_max_applications": 1},
    )
    session.add(sulphur)
    session.commit()
    transcriber = FakeTranscriber(
        Transcript(
            text=(
                "Sprayed Les Granges and Peyrot with copper 2 kg per hectare at 6"
                if target_covers_p6
                else "Sprayed Les Granges with copper 2 kg per hectare at 6"
            ),
            language="en",
            seconds=4.0,
        )
    )
    rt = runtime(session_factory, settings, sender, transcriber, RegexExtractor())
    ctx = SimpleNamespace(bot=bot)
    handle(rt.on_voice(voice_update(), ctx))
    handle(rt.on_callback(callback_update(sender.sent[-1].buttons[0][1]), ctx))
    [target] = pending_logs(session)
    assert target.status == "done"
    assert target.ts_start.astimezone(PARIS) == NOW_LOCAL
    target_id = target.id
    transcriber.transcript = Transcript(
        text=(
            (
                "Sprayed Les Granges and Le Plateau with copper 2 kg per hectare "
                "and sulphur 5 kg per hectare at 6"
            )
            if pending_covers_p1
            else (
                "Sprayed Les Granges with copper 2 kg per hectare and sulphur 5 kg per hectare at 6"
            )
        ),
        language="en",
        seconds=4.0,
    )
    handle(rt.on_voice(voice_update(), ctx))
    handle(rt.on_callback(callback_update(sender.sent[-1].buttons[0][1]), ctx))

    reply = sender.sent[-1].text
    assert "1 of 1 sulphur applications this season" in reply
    saved_logs = pending_logs(session)
    if target_covers_p6:
        parcels_text = "P1 Le Plateau, P5 Les Granges" if pending_covers_p1 else "P5 Les Granges"
        assert reply.splitlines()[0].startswith(f"✅ Logged: {parcels_text} · sulphur · 5 kg/ha")
        assert "copper was already logged at 06:00" in reply
        assert "Peyrot" not in reply
        assert len(saved_logs) == 2
        saved = next(log for log in saved_logs if log.id != target_id)
        assert [(q.product.active_substance, q.value) for q in saved.quantities] == [
            ("sulphur", Decimal("5")),
        ]
    else:
        assert reply.startswith("✅ Added to the spray already logged:")
        assert "copper + sulphur" in reply
        [saved] = saved_logs
        assert saved.id == target_id
        assert sorted((q.product.active_substance, q.value) for q in saved.quantities) == [
            ("copper", Decimal("2")),
            ("sulphur", Decimal("5")),
        ]
    farm = session.get(Farm, ids["farm"])
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    rows = register.register_rows(repo, NOW_LOCAL, checks=True)
    assert {row["product_name"] for row in rows} == {COPPER, sulphur.name}
    if target_covers_p6:
        assert sorted((row["parcel_alias"], row["active_substance"]) for row in rows) == (
            ([("P1", "copper"), ("P1", "sulphur")] if pending_covers_p1 else [])
            + [("P5", "copper"), ("P5", "sulphur"), ("P6", "copper")]
        )
        assert all(
            row["log_id"] == saved.id for row in rows if row["active_substance"] == "sulphur"
        )
    else:
        assert all(row["log_id"] == target_id for row in rows)
    assert any("sulphur" in str(check).lower() for row in rows for check in row["checks"])
