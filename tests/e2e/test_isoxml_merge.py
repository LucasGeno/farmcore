"""End-to-end demo step 3: the hero loop to the confirmed 10 Apr voice log, then the sprayer's
TaskData file uploaded through `/operations/isoxml` merges into it.

`farmcore seed --scene B` → `/start` → replay 10 Apr 06:00 → voice note "Sprayed Les Granges and
Peyrot ..." (fake transcriber, regex extractor, real chat handlers behind the real bot adapter) →
✅ → replay 10 Apr 10:00 → upload the synthetic demo TaskData → one log with 🎤 + 🚜, machine time
(07:12:05) and doses (1.94 / 2.06 kg/ha) → the next advisor evaluation still has P5 and P6
`protected`. Telegram is faked as in `test_hero_loop.py` (helpers copied, not imported from a
`test_*.py` module). Needs the demo datasets (marked `data`).
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from decimal import Decimal
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
from farmcore.apps.operations import isoxml_demo
from farmcore.core import jobs
from farmcore.core.models.canonical import Log, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

pytestmark = pytest.mark.data

manifests, seeded = scenes.manifests, scenes.seeded

SENTENCE = "Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos, this morning"
CHAT = 4343  # synthetic Telegram chat id
SPRAYED = ("P5", "P6")
_ids = count(1)


# --- fake Telegram side (copied from test_hero_loop.py) ---


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


@pytest.fixture
def phone(session_factory, manifests, settings):
    """The farmer's phone: the real bot runtime with fake Telegram I/O, and a replay stepper."""
    test_settings = settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("123:dummy-token"),
            "extractor": "regex",
            "telegram_allowed_user_ids": str(CHAT),
        }
    )
    replies = FakeTelegramSender()
    runtime = BotRuntime(
        deps=ChatDeps(
            session_factory=session_factory,
            extractor=RegexExtractor(),
            settings=test_settings,
            manifests=tuple(manifests),
        ),
        transcriber=FakeTranscriber(Transcript(text=SENTENCE, language="en", seconds=5.0)),
        sender=replies,
    )
    context = SimpleNamespace(bot=FakeBot())

    def step(to: str) -> None:
        runs = jobs.step_to(session_factory, manifests, local(to), test_settings)
        assert all(r.ok for r in runs), runs

    push.set_sender(FakeTelegramSender())
    yield SimpleNamespace(runtime=runtime, replies=replies, context=context, step=step)
    push.set_sender(None)


def confirmed_voice_log(phone) -> int:
    asyncio.run(phone.runtime.on_start(start_update(), phone.context))
    phone.step("2024-04-10 06:00")
    asyncio.run(phone.runtime.on_voice(voice_update(), phone.context))
    [(label, confirm), _cancel] = phone.replies.sent[-1].buttons
    assert label == "✅ Confirm"
    asyncio.run(phone.runtime.on_callback(callback_update(confirm), phone.context))
    assert phone.replies.sent[-1].text.startswith("✅ Logged:"), phone.replies.sent[-1].text
    return int(confirm.removeprefix("confirm:"))


def decisions(client) -> dict[str, str]:
    features = client.get("/fields/parcels.geojson").json()["features"]
    return {f["properties"]["alias"]: f["properties"]["decision"] for f in features}


def test_voice_log_then_taskdata_upload_is_one_record_with_two_sources(
    client, session, seeded, phone
):
    seeded("B")
    log_id = confirmed_voice_log(phone)
    phone.step("2024-04-10 10:00")

    resp = client.post(
        "/operations/isoxml",
        files={"file": ("TASKDATA.XML", isoxml_demo.demo_taskdata(), "application/xml")},
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.text.count("time and dose from the machine") == 1  # the first task replaces
    assert resp.text.count("merged into") == 2
    session.expire_all()
    ids = {p.alias: p.id for p in session.scalars(select(Parcel))}
    spray_logs = session.scalars(
        select(Log).where(Log.type == "spray", Log.ts_start >= local("2024-04-10 00:00"))
    ).all()
    spray_logs = [log for log in spray_logs if log.ts_start < local("2024-04-11 00:00")]
    [log] = spray_logs
    assert log.id == log_id
    assert {p.alias for p in log.parcels} == set(SPRAYED)
    assert sorted(s.source_type for s in log.sources) == ["isoxml", "isoxml", "voice"]
    assert log.ts_start == local("2024-04-10 07:12:05")
    assert sorted(q.value for q in log.quantities) == [Decimal("1.94"), Decimal("2.06")]

    index = client.get("/operations/").text
    [record] = re.findall(rf'<li[^>]*data-record="{log.id}"[^>]*>.*?</li>', index, re.S)
    assert 'data-source="voice"' in record and 'data-source="isoxml"' in record
    timeline = client.get(f"/operations/parcels/{ids['P5']}/timeline").text
    assert re.search(
        r'data-kind="log.merged".*?data-source="isoxml".*?Records merged', timeline, re.S
    )

    rows = [r for r in client.get("/operations/register.json").json() if r["log_id"] == log_id]
    assert sorted((r["parcel_alias"], r["active_substance"], r["dose"]) for r in rows) == [
        ("P5", "copper", "1.94"),
        ("P5", "sulphur", "2.06"),
        ("P6", "copper", "1.94"),
        ("P6", "sulphur", "2.06"),
    ]
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == {
        ("P5", "07:12"),
        ("P6", "08:03"),
    }
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/operations/register.csv").text)))
    assert {
        (r["parcel_alias"], r["start_time"]) for r in csv_rows if int(r["log_id"]) == log_id
    } == {("P5", "07:12"), ("P6", "08:03")}
    entities = client.get("/share/ngsi-ld.json").json()
    for alias, start, end in (
        ("P5", "2024-04-10T05:12:05Z", "2024-04-10T05:58:40Z"),
        ("P6", "2024-04-10T06:03:10Z", "2024-04-10T06:31:55Z"),
    ):
        relevant = [
            e
            for e in entities
            if e["type"] == "AgriParcelOperation"
            and e["hasAgriParcel"] == f"urn:ngsi-ld:AgriParcel:farmcore-{ids[alias]}"
            and e["id"].startswith(f"urn:ngsi-ld:AgriParcelOperation:farmcore-{log_id}-")
        ]
        assert len(relevant) == 2
        assert {(e["startedAt"], e["endedAt"]) for e in relevant} == {(start, end)}

    phone.step("2024-04-10 11:00")  # the next advisor evaluation
    after = decisions(client)
    assert {a: after[a] for a in SPRAYED} == dict.fromkeys(SPRAYED, "protected")
