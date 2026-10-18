"""End-to-end hero loop: the demo as the farmer and a visitor see it, on the real apps.

migrate (session fixture) → `farmcore seed --scene B` → `/start` → replay 8 Apr 06:00 (all
`none`, nothing pushed) → replay 10 Apr 06:00 (all `spray`, one digest) → voice note "Sprayed Les
Granges and Peyrot this morning with Bouillie bordelaise ..." (fake transcriber, regex extractor)
→ ✅ (reply with "Open map" / "Open register" links) → P5 and P6 `protected` with no clock step,
2 register rows (copper only: on 10 Apr the engine advises copper; sulphur starts at bloom) →
replay 15 May 06:00 (Scene B: every parcel `spray`, washed off, not expired).

The copper product is found by its seed key (`copper-contact`), never by its literal name:
the seed renames the shortlist to the E-Phy names. The spoken "Bouillie bordelaise" matches the
product by its distinctive first word, before and after that rename.

Real seeds (IGN parcels, open Météo-France station CSV, product shortlist), real manifest jobs,
real chat handlers behind the real bot adapter. Telegram is faked on both sides: the PTB `Update`
objects are built from Bot API JSON (no network), replies and the advice digest go to fake
senders, and the bot token in the test settings is a dummy. Needs the demo datasets (marked `data`).
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Callable, Iterator
from datetime import datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from telegram import Update

from farmcore.apps.chat import push
from farmcore.apps.chat.bot import BotRuntime
from farmcore.apps.chat.extractor import RegexExtractor
from farmcore.apps.chat.handlers import ChatDeps
from farmcore.apps.chat.telegram import FakeTelegramSender
from farmcore.apps.chat.transcribe import FakeTranscriber, Transcript
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import ext_ids, jobs
from farmcore.core.access import default_farm
from farmcore.core.models.canonical import Parcel, Product
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

pytestmark = pytest.mark.data

# Shared fixtures (tests/apps/advisor/scenes.py): `farmcore seed [--scene B]` on the test DB.
manifests, seeded = scenes.manifests, scenes.seeded

# demo.md's voice phrases (copper only: the 10 Apr advice is copper).
SPEC_SENTENCE = (
    "Sprayed Les Granges and Peyrot this morning with Bouillie bordelaise, 2 kilos per hectare"
)
FR_SENTENCE = "Traité Les Granges et Peyrot ce matin à la bouillie bordelaise, 2 kilos par hectare"
COPPER_KEY = "copper-contact"  # operations seed shortlist key
CHAT = 4242  # synthetic Telegram chat id
ALIASES = [f"P{n}" for n in range(1, 11)]
SPRAYED = ("P5", "P6")  # Les Granges, Peyrot
_ids = count(1)


# --- fake Telegram side: Bot API JSON → PTB `Update` objects, a bot that serves one voice file ---


def _message(**extra) -> dict:
    return {
        "message_id": next(_ids),
        "date": 1712721600,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": CHAT, "is_bot": False, "first_name": "Farmer"},
        **extra,
    }


def start_update() -> Update:
    entities = [{"type": "bot_command", "offset": 0, "length": len("/start")}]
    return Update.de_json(
        {"update_id": next(_ids), "message": _message(text="/start", entities=entities)}, None
    )


def voice_update() -> Update:
    voice = {"file_id": "voice-1", "file_unique_id": "u-voice-1", "duration": 5}
    return Update.de_json({"update_id": next(_ids), "message": _message(voice=voice)}, None)


def callback_update(data: str) -> Update:
    query = {
        "id": f"cb-{next(_ids)}",
        "from": {"id": CHAT, "is_bot": False, "first_name": "Farmer"},
        "chat_instance": "ci",
        "data": data,
        "message": _message(text="echo"),
    }
    return Update.de_json({"update_id": next(_ids), "callback_query": query}, None)


class FakeFile:
    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(b"OggS fake voice bytes")
        return Path(custom_path)


class FakeBot:
    async def get_file(self, file_id: str) -> FakeFile:
        return FakeFile()

    async def answer_callback_query(self, callback_query_id: str, *args, **kwargs) -> bool:
        return True


# --- the demo loop ---


class Demo:
    """One farmer's phone (bot replies) plus a visitor's browser (HTTP client) on one database."""

    def __init__(
        self, client, session, session_factory, manifests, settings, sentence: str
    ) -> None:
        self.sentence = sentence
        self.client = client
        self.session = session
        self.session_factory = session_factory
        self.manifests = manifests
        # A dummy token: `chat.push_advice` only pushes when one is set; the sender is fake.
        self.settings = settings.model_copy(
            update={
                "telegram_bot_token": SecretStr("123:dummy-token"),
                "extractor": "regex",
                "telegram_allowed_user_ids": str(CHAT),
            }
        )
        self.digests = FakeTelegramSender()  # `chat.push_advice`
        self.replies = FakeTelegramSender()  # the bot's answers to the farmer
        self.runtime = BotRuntime(
            deps=ChatDeps(
                session_factory=session_factory,
                extractor=RegexExtractor(),
                settings=self.settings,
                manifests=tuple(manifests),
            ),
            transcriber=FakeTranscriber(Transcript(text=sentence, language="en", seconds=5.0)),
            sender=self.replies,
        )
        self.context = SimpleNamespace(bot=FakeBot())
        farm_id = default_farm(session, settings).id
        self.ids = {
            p.alias: p.id
            for p in session.scalars(
                select(Parcel).where(Parcel.farm_id == farm_id).order_by(Parcel.id)
            )
        }

    def step(self, to: str) -> None:
        """The navbar replay step (`farmcore replay --to <local>`): every due job succeeds."""
        runs = jobs.step_to(self.session_factory, self.manifests, local(to), self.settings)
        assert all(r.ok for r in runs), runs

    def start(self) -> None:
        asyncio.run(self.runtime.on_start(start_update(), self.context))

    def send_voice_note(self) -> str:
        """Returns the ✅ button's callback data from the echo."""
        asyncio.run(self.runtime.on_voice(voice_update(), self.context))
        echo = self.replies.sent[-1]
        assert echo.text.startswith(f"🎤 “{self.sentence}”"), echo.text
        # At a 06:00 replay clock "this morning" is clamped to now (the echo adds a note).
        summary = "P5 Les Granges, P6 Peyrot · copper · 2 kg/ha · 10 Apr 06:00"
        assert f"{summary} — confirm?" in echo.text, echo.text
        [(confirm_label, confirm), _cancel] = echo.buttons
        assert confirm_label == "✅ Confirm"
        return confirm

    def tap(self, data: str) -> str:
        asyncio.run(self.runtime.on_callback(callback_update(data), self.context))
        return self.replies.sent[-1].text

    def copper(self) -> Product:
        """The seeded copper product, by its shortlist key (its name may change)."""
        product = ext_ids.lookup(self.session, ops_seed.PRODUCT_SOURCE, COPPER_KEY)
        assert isinstance(product, Product), product
        return product

    def advice(self, alias: str) -> dict:
        resp = self.client.get(f"/advisor/parcels/{self.ids[alias]}.json")
        assert resp.status_code == 200
        return resp.json()

    def map_decisions(self) -> dict[str, str]:
        resp = self.client.get("/fields/parcels.geojson")
        assert resp.status_code == 200
        return {
            f["properties"]["alias"]: f["properties"]["decision"] for f in resp.json()["features"]
        }

    def register_rows(self) -> list[dict[str, str]]:
        resp = self.client.get("/operations/register.csv")
        assert resp.status_code == 200
        return list(csv.DictReader(io.StringIO(resp.text)))


@pytest.fixture
def demo(
    client, session, session_factory, manifests, settings, seeded
) -> Iterator[Callable[..., Demo]]:
    """`farmcore seed [--scene B]`, then a `Demo` on it; the push sender is restored after."""

    def make(scene: str | None, sentence: str = SPEC_SENTENCE) -> Demo:
        seeded(scene)
        return Demo(client, session, session_factory, manifests, settings, sentence)

    push.set_sender(None)
    yield make
    push.set_sender(None)


def cover(advice: dict, rule: str) -> dict:
    [reason] = [r for r in advice["reasons"] if r["rule"] == rule]
    return reason


def verdict(advice: dict, rule: str) -> str:
    return cover(advice, rule)["verdict"]


def run_to_confirmed_voice_log(d: Demo) -> int:
    """`/start`, 8 Apr, 10 Apr, voice note, ✅. Returns the confirmed log's id."""
    push.set_sender(d.digests)
    d.start()
    assert d.ids.keys() == set(ALIASES)

    d.step("2024-04-08 06:00")
    assert d.map_decisions() == dict.fromkeys(ALIASES, "none")
    assert d.digests.sent == []

    d.step("2024-04-10 06:00")
    assert d.map_decisions() == dict.fromkeys(ALIASES, "spray")
    [digest] = d.digests.sent
    assert digest.chat_id == CHAT
    assert digest.text.splitlines()[0] == "10 parcels → spray"

    confirm = d.send_voice_note()
    reply = d.tap(confirm)
    assert reply.startswith("✅ Logged:"), reply
    assert "Advice updated." in reply
    # Test settings use a localhost base URL: the links ride in the text (no URL buttons).
    assert "Open map: http://localhost:8000/fields/" in reply
    assert "Open register: http://localhost:8000/operations/register" in reply
    return int(confirm.removeprefix("confirm:"))


def test_hero_loop_scene_b_seed_10_april_voice_log_then_15_may_washed_off(demo) -> None:
    d = demo("B")
    log_id = run_to_confirmed_voice_log(d)

    # No clock step: the confirm re-ran the advisor, the map shows P5 and P6 green.
    decisions = d.map_decisions()
    assert {a: decisions[a] for a in SPRAYED} == dict.fromkeys(SPRAYED, "protected")
    others = {a: v for a, v in decisions.items() if a not in SPRAYED}
    assert others == dict.fromkeys(set(ALIASES) - set(SPRAYED), "spray")
    rows = [r for r in d.register_rows() if r["log_id"] == str(log_id)]
    assert len(rows) == 2  # 2 parcels × 1 product
    assert sorted(r["parcel_alias"] for r in rows) == sorted(SPRAYED)
    copper = d.copper()
    assert {(r["product_name"], r["active_substance"]) for r in rows} == {(copper.name, "copper")}
    assert {r["authorisation_no"] for r in rows} == {copper.authorisation_no}
    assert len(d.digests.sent) == 1  # the confirm itself pushes nothing

    d.step("2024-05-15 06:00")
    for alias in ALIASES:
        advice = d.advice(alias)
        assert advice["decision"] == "spray", (alias, advice)
        assert "washed_off" in advice["flags"], (alias, advice["flags"])
        assert verdict(advice, "protection_downy") == "washed off", alias  # the 9 May copper
    for alias in set(ALIASES) - set(SPRAYED):
        assert "expired" not in d.advice(alias)["flags"], alias


def test_scene_b_15_may_voice_logged_parcels_are_not_flagged_expired(demo) -> None:
    d = demo("B")
    run_to_confirmed_voice_log(d)

    d.step("2024-05-15 06:00")
    for alias in SPRAYED:
        advice = d.advice(alias)
        assert verdict(advice, "protection_downy") == "washed off", alias
        assert "expired" not in advice["flags"], (alias, advice["flags"])


def test_without_scene_b_p5_reads_expired_at_15_may(demo) -> None:
    """Why the demo seeds `--scene B`: without the 9 May copper spray, P5's latest downy cover is
    the 10 Apr voice log, whose protection period ended weeks before 15 May: the card says
    "expired" (the rain since 10 Apr also washed it off; "washed off" wins the cover verdict)."""
    d = demo(None)
    run_to_confirmed_voice_log(d)

    d.step("2024-05-15 06:00")
    advice = d.advice("P5")
    assert advice["decision"] == "spray"
    assert "expired" in advice["flags"], advice["flags"]
    downy_until = datetime.fromisoformat(cover(advice, "protection_downy")["inputs"]["until"])
    assert downy_until < local("2024-05-01 00:00")  # 10 Apr + the contact protection period


def test_the_french_voice_note_logs_the_same_spray(demo) -> None:
    """demo.md's French phrase takes the same path (regex fallback) to the same 2 rows."""
    d = demo("B", FR_SENTENCE)
    log_id = run_to_confirmed_voice_log(d)

    decisions = d.map_decisions()
    assert {a: decisions[a] for a in SPRAYED} == dict.fromkeys(SPRAYED, "protected")
    rows = [r for r in d.register_rows() if r["log_id"] == str(log_id)]
    assert sorted((r["parcel_alias"], r["product_name"]) for r in rows) == [
        ("P5", d.copper().name),
        ("P6", d.copper().name),
    ]


def test_the_spoken_product_still_matches_an_e_phy_style_copper_name(demo) -> None:
    """The seed renames the copper product to its E-Phy name (upper case, a longer name that
    still starts with "Bouillie bordelaise"): the demo sentence logs the same 2 rows."""
    d = demo("B")
    copper = d.copper()
    copper.name = "BOUILLIE BORDELAISE EPHY-STYLE TEST NAME"
    d.session.commit()

    log_id = run_to_confirmed_voice_log(d)

    rows = [r for r in d.register_rows() if r["log_id"] == str(log_id)]
    assert {(r["parcel_alias"], r["product_name"]) for r in rows} == {
        ("P5", copper.name),
        ("P6", copper.name),
    }
