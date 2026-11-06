"""Confirmed field observations and irrigation facts through the chat seam.

Synthetic parcels and products only. An irrigation keeps the entered amount, unit and gross/net
basis; an observation keeps what was seen. Neither type is a spray: no label checks, no advisor
run and no stock use.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from farmcore.apps.chat.dto import Callback, Incoming
from farmcore.apps.chat.extractor import (
    ExtractRequest,
    ParcelRef,
    ProductRef,
    RegexExtractor,
)
from farmcore.apps.chat.handlers import on_callback, on_message
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm, Log, Parcel
from tests.apps.chat.test_handlers import (
    CHAT,
    COPPER,
    FAKE_ADVISOR,
    SEEN_BY_JOB,
    SQUARE,
    count,
    deps,
    events,
    fake,
    logs_of,
    seed_farm,
    voice,
)

PARIS = ZoneInfo("Europe/Paris")
NOW_LOCAL = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
TEN_AM = datetime(2024, 4, 10, 10, 0, tzinfo=PARIS)


def request(transcript: str, now_local: datetime = TEN_AM) -> ExtractRequest:
    return ExtractRequest(
        transcript=transcript,
        parcels=[
            ParcelRef(alias="P1", name="Le Plateau"),
            ParcelRef(alias="P5", name="Les Granges"),
        ],
        products=[
            ProductRef(
                name=COPPER,
                active_substance="copper",
                product_type="contact",
                targets=["downy"],
            )
        ],
        today=now_local.date(),
        now_local=now_local,
    )


def test_regex_extractor_reads_an_irrigation_amount_unit_and_basis():
    draft = RegexExtractor().extract(request("Irrigated P5 20 mm gross at 6"))

    assert draft.type == "irrigate"
    assert draft.parcels == ["P5"]
    assert draft.products == []
    assert draft.amount_value == 20.0
    assert draft.amount_unit == "mm"
    assert draft.basis == "gross"
    assert draft.ts_start == datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)


def test_regex_extractor_reads_a_french_net_irrigation_yesterday():
    draft = RegexExtractor().extract(request("J'ai irrigué Les Granges 12 m3 net hier"))

    assert (draft.type, draft.parcels, draft.amount_value) == ("irrigate", ["P5"], 12.0)
    assert (draft.amount_unit, draft.basis) == ("m3", "net")
    assert draft.ts_start == datetime(2024, 4, 9, 8, 0, tzinfo=PARIS)


def test_regex_extractor_reads_an_observation_without_products():
    transcript = "Observed powdery mildew on Les Granges at 6"
    draft = RegexExtractor().extract(request(transcript))

    assert draft.type == "observation"
    assert draft.parcels == ["P5"]
    assert draft.products == []
    assert draft.observation == transcript
    assert draft.ts_start == datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)


def test_irrigation_preview_and_confirm_keep_the_entered_amount_unit_and_basis(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    echo = on_message(d, voice("Irrigated P5 20 mm gross at 6", 11))

    [log] = logs_of(session)
    assert (log.type, log.status) == ("irrigate", "pending")
    assert log.quantities == []
    assert log.props == {"irrigation": {"amount": "20", "unit": "mm", "basis": "gross"}}
    assert echo.text.splitlines()[0] == (
        "P5 Les Granges · irrigation 20 mm gross · 10 Apr 06:00 — confirm?"
    )
    assert "No spray stock is used and no advice is calculated for irrigation." in echo.text
    assert "Checks" not in echo.text
    assert echo.buttons == [
        ("✅ Confirm", f"confirm:{log.id}"),
        ("❌ Cancel", f"cancel:{log.id}"),
    ]

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert session.get(Log, log.id, populate_existing=True).status == "done"
    assert out.text.startswith("✅ Logged: P5 Les Granges · irrigation 20 mm gross · 10 Apr 06:00.")
    assert "No spray stock was used and no advice was calculated." in out.text
    assert "Advice updated" not in out.text and "Checks" not in out.text


def test_irrigation_without_a_stated_basis_says_so_and_never_assumes_one(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    echo = on_message(d, voice("Irrigated P5 12 m3 at 6", 12))

    [log] = logs_of(session)
    assert log.props == {"irrigation": {"amount": "12", "unit": "m3"}}
    assert echo.text.splitlines()[0] == (
        "P5 Les Granges · irrigation 12 m3, basis not stated · 10 Apr 06:00 — confirm?"
    )
    assert "efficiency" not in echo.text.casefold()

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    assert "irrigation 12 m3, basis not stated" in out.text
    assert "efficiency" not in out.text.casefold()


def test_observation_preview_and_confirm_keep_what_was_seen(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    transcript = "Observed powdery mildew on P5 at 6"

    echo = on_message(d, voice(transcript, 13))

    [log] = logs_of(session)
    assert (log.type, log.status) == ("observation", "pending")
    assert log.quantities == []
    assert log.props == {"observation": {"text": transcript}}
    assert echo.text.splitlines()[0] == (
        f"P5 Les Granges · observation: {transcript} · 10 Apr 06:00 — confirm?"
    )
    assert "No spray stock is used and no advice is calculated for observations." in echo.text

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert session.get(Log, log.id, populate_existing=True).status == "done"
    assert out.text.startswith(f"✅ Logged: P5 Les Granges · observation: {transcript}")
    assert "Advice updated" not in out.text


@pytest.mark.parametrize(
    ("draft", "reply_start"),
    [
        (
            dict(type="irrigate", parcels=["P1"], amount_value=None, amount_unit=None),
            "How much water",
        ),
        (
            dict(type="irrigate", parcels=["P1"], amount_value=20, amount_unit="cm"),
            "I only record irrigation in mm, m3 or L",
        ),
        (
            dict(type="irrigate", parcels=[], amount_value=20, amount_unit="mm"),
            "Which parcel did you irrigate?",
        ),
        (
            dict(type="observation", parcels=[], observation="powdery mildew"),
            "Which parcel is this about?",
        ),
        (dict(type="observation", parcels=["P1"], observation=""), "What did you observe?"),
    ],
)
def test_an_irrigation_or_observation_draft_that_cannot_be_logged_asks_before_preview(
    session_factory, session, settings, draft, reply_start
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake(**draft))

    out = on_message(d, voice("Irrigated P1 this morning", 14))

    assert out.text.startswith(reply_start)
    assert out.buttons == []
    assert count(session, Log) == 0


def test_confirmed_irrigation_runs_no_advisor_job_and_uses_no_spray_stock(
    committed_session_factory, settings
):
    from farmcore.apps.inventory import service as inventory
    from farmcore.apps.inventory.models import InventoryStock

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
        session.add(
            InventoryStock(
                farm_id=farm_id,
                product_id=ids[COPPER],
                season=2024,
                unit="kg",
                purchased=Decimal("20"),
            )
        )
        session.commit()
    SEEN_BY_JOB.clear()
    d = deps(committed_session_factory, settings, RegexExtractor(), [FAKE_ADVISOR])
    echo = on_message(d, voice("Irrigated P1 20 mm gross at 6", 15))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert SEEN_BY_JOB == []
    assert "Advice" not in out.text
    with committed_session_factory() as session:
        [log] = session.scalars(select(Log)).all()
        assert (log.type, log.status) == ("irrigate", "done")
        farm = session.get(Farm, farm_id)
        repo = ScopedRepository(session, farm_id, ensure_owner_grant(session, farm), "test", "test")
        [row] = inventory.stock_rows(repo, now=NOW_LOCAL, tz="Europe/Paris")
        assert (row.used, row.sprays) == (Decimal(0), 0)


def test_a_redelivered_irrigation_and_a_second_confirm_change_nothing(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    first = on_message(d, voice("Irrigated P1 20 mm gross at 6", 16))
    again = on_message(d, voice("Irrigated P1 20 mm gross at 6", 16))

    assert count(session, Log) == 1
    assert again.buttons == first.buttons

    on_callback(d, Callback(chat_id=CHAT, data=first.buttons[0][1]))
    second = on_callback(d, Callback(chat_id=CHAT, data=first.buttons[0][1]))
    redelivered = on_message(d, voice("Irrigated P1 20 mm gross at 6", 16))

    assert second.text == redelivered.text == "This record is already logged."
    assert [log.status for log in logs_of(session)] == ["done"]
    assert len(events(session, "log.confirmed")) == 1


def test_cancelling_an_irrigation_leaves_no_record(session_factory, session, settings):
    from farmcore.core.models.canonical import LogSource, Quantity

    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())
    echo = on_message(d, voice("Irrigated P1 20 mm at 6", 17))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[1][1]))

    assert out.text == "❌ Cancelled. Nothing was logged."
    assert count(session, Log) == count(session, LogSource) == count(session, Quantity) == 0


def test_an_irrigation_belongs_to_the_farm_bound_to_the_capture(session_factory, session, settings):
    from dataclasses import replace

    from farmcore.apps.chat.handlers import CaptureBinding

    ids = seed_farm(session, settings)
    farm_id = session.get(Parcel, ids["P1"]).farm_id
    other = Farm(name="Other")
    session.add(other)
    session.flush()
    parcel = Parcel(
        farm_id=other.id,
        alias="P9",
        name="Elsewhere",
        area_ha=Decimal("1"),
        geom=SQUARE,
    )
    session.add(parcel)
    session.flush()
    d = deps(session_factory, settings, RegexExtractor())
    d = replace(d, capture_binding=CaptureBinding(farm_id=other.id, operator=True))

    echo = on_message(d, Incoming(CHAT, "Irrigated P9 20 mm at 6", None, 18))
    on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    [log] = logs_of(session)
    assert log.farm_id == other.id and log.farm_id != farm_id
    assert [p.id for p in log.parcels] == [parcel.id]
    assert log.sources[0].external_key.startswith(f"web:farm:{other.id}:")


def test_an_irrigation_without_a_time_uses_now_and_says_so(session_factory, session, settings):
    from farmcore.apps.chat.extractor.base import NOTE_TIME_MISSING

    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    out = on_message(d, voice("Irrigated P1 20 mm", 19))

    [log] = logs_of(session)
    assert log.ts_start == NOW_LOCAL
    assert NOTE_TIME_MISSING in log.sources[0].raw_payload["notes"]
    assert "No time was said, so I used now." in out.text


def test_an_irrigation_dated_after_the_clock_is_clamped_to_now(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(
        session_factory,
        settings,
        fake(
            type="irrigate",
            amount_value=20,
            amount_unit="mm",
            ts_start=NOW_LOCAL.replace(hour=15),
        ),
    )

    out = on_message(d, Incoming(CHAT, "Irrigated P1 20 mm at 15", None, 20))

    [log] = logs_of(session)
    assert log.ts_start == NOW_LOCAL
    assert "time clamped to now" in log.sources[0].raw_payload["notes"]
    assert "The time you said is later than now, so I used now." in out.text
