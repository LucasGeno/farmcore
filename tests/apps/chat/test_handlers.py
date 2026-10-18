"""Chat confirm flow through `on_message` / `on_callback` with plain DTOs and a seeded test DB.

Synthetic parcels and products only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from farmcore.apps.chat.audit import DbAuditSink
from farmcore.apps.chat.dto import Callback, Incoming
from farmcore.apps.chat.extractor import DraftOperation, FakeExtractor, RegexExtractor
from farmcore.apps.chat.handlers import ChatDeps, build_deps, on_callback, on_message
from farmcore.core.clock import set_replay
from farmcore.core.manifest import JobContext, JobSpec, Manifest
from farmcore.core.models.canonical import Event, Log, LogSource, Parcel, Product, Quantity
from farmcore.core.seed import ensure_default_farm

PARIS = ZoneInfo("Europe/Paris")
# Replay "now" of the hero scene: 2024-04-10 06:00 local.
NOW_LOCAL = datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
COPPER = "Copper Shield 20 WP"
SULPHUR = "Sulfo Jet 80 WG"
CHAT = 4242
HOUR = timedelta(hours=1)


def seed_farm(session, settings, now_local: datetime = NOW_LOCAL) -> dict[str, int]:
    """Demo farm with P1, P5, P6, a copper + sulphur shortlist and the replay clock at `now`."""
    farm = ensure_default_farm(session, settings)
    parcels = {
        alias: Parcel(farm_id=farm.id, alias=alias, name=name, area_ha=Decimal("1"), geom=SQUARE)
        for alias, name in (("P1", "Le Plateau"), ("P5", "Les Granges"), ("P6", "Peyrot"))
    }
    products = {
        COPPER: Product(
            name=COPPER,
            authorisation_no="TEST-1",
            active_substance="copper",
            product_type="contact",
            targets=["downy"],
        ),
        SULPHUR: Product(
            name=SULPHUR,
            authorisation_no="TEST-2",
            active_substance="sulphur",
            product_type="contact",
            targets=["powdery"],
        ),
    }
    session.add_all([*parcels.values(), *products.values()])
    set_replay(session, now_local, settings)
    session.commit()
    return {**{k: p.id for k, p in parcels.items()}, **{k: p.id for k, p in products.items()}}


def fake(**draft: object) -> FakeExtractor:
    base: dict[str, object] = {
        "type": "spray",
        "parcels": ["P1"],
        "products": [COPPER, SULPHUR],
        "dose_value": 2,
        "dose_unit": "kg/ha",
        "ts_start": NOW_LOCAL.replace(hour=5),
        "confidence": 0.9,
        "adapter": "fake",
    }
    base.update(draft)
    return FakeExtractor(DraftOperation(**base))


def deps(session_factory, settings, extractor, manifests=()) -> ChatDeps:
    return ChatDeps(
        session_factory=session_factory,
        extractor=extractor,
        settings=settings,
        manifests=tuple(manifests),
    )


def voice(transcript: str, message_id: int = 1) -> Incoming:
    return Incoming(chat_id=CHAT, text=None, transcript=transcript, message_id=message_id)


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def logs_of(session) -> list[Log]:
    return list(
        session.scalars(select(Log).order_by(Log.id).execution_options(populate_existing=True))
    )


def events(session, kind: str) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.kind == kind).order_by(Event.id)))


def test_voice_note_on_p1_becomes_a_pending_log_echoed_with_confirm_and_cancel(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    d = deps(session_factory, settings, fake())

    out = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    [log] = logs_of(session)
    assert (log.type, log.status) == ("spray", "pending")
    assert [p.id for p in log.parcels] == [ids["P1"]]
    assert out.text == (
        "P1 Le Plateau · copper + sulphur · 2 kg/ha · 10 Apr 05:00 — confirm?\n"
        "Copper Shield 20 WP: 2 kg/ha\nSulfo Jet 80 WG: 2 kg/ha\n"
        "Checks: Warning: Copper at least 0 kg Cu/ha this year (incomplete)\n"
        "Preview only; checks run again after confirmation."
    )
    assert out.buttons == [("✅ Confirm", f"confirm:{log.id}"), ("❌ Cancel", f"cancel:{log.id}")]


def test_confirm_turns_the_pending_log_done_with_quantities_voice_source_and_event(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    d = deps(session_factory, settings, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))
    confirm_data = echo.buttons[0][1]

    out = on_callback(d, Callback(chat_id=CHAT, data=confirm_data))

    [log] = logs_of(session)
    assert log.status == "done"
    assert sorted((q.product_id, q.value, q.unit, q.measure) for q in log.quantities) == sorted(
        [
            (ids[COPPER], Decimal("2"), "kg/ha", "rate"),
            (ids[SULPHUR], Decimal("2"), "kg/ha", "rate"),
        ]
    )
    [src] = log.sources
    assert src.source_type == "voice"
    assert src.raw_payload["transcript"] == "copper + sulphur, 2 kg, this morning"
    assert src.confidence == pytest.approx(0.9)
    [confirmed] = events(session, "log.confirmed")
    assert (confirmed.res_model, confirmed.res_id) == ("parcel", ids["P1"])
    assert confirmed.source_app == "chat"
    assert out.text.startswith("✅ Logged: P1 Le Plateau · copper + sulphur · 2 kg/ha")
    assert out.buttons == []


# What the fake `advisor.evaluate` job saw: the chat log statuses, read in its own session.
SEEN_BY_JOB: list[list[str]] = []


def fake_evaluate(ctx: JobContext) -> None:
    SEEN_BY_JOB.append(list(ctx.session.scalars(select(Log.status).where(Log.type == "spray"))))


FAKE_ADVISOR = Manifest(
    name="advisor",
    version="0.0.0",
    jobs=(JobSpec(name="advisor.evaluate", func=f"{__name__}:fake_evaluate", interval=HOUR),),
)


def test_confirm_commits_before_advisor_evaluate_runs_in_its_own_session(
    committed_session_factory, settings
):
    with committed_session_factory() as s:
        seed_farm(s, settings)
    SEEN_BY_JOB.clear()
    d = deps(committed_session_factory, settings, fake(), manifests=[FAKE_ADVISOR])
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert SEEN_BY_JOB == [["done"]]
    assert "Advice updated." in out.text


def test_confirm_without_an_advisor_job_still_confirms(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake(), manifests=[])
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert [log.status for log in logs_of(session)] == ["done"]
    assert "Advice" not in out.text


def test_cancel_discards_the_pending_log_and_posts_log_cancelled(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    d = deps(session_factory, settings, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[1][1]))

    assert count(session, Log) == 0
    assert count(session, Quantity) == 0
    assert count(session, LogSource) == 0
    [cancelled] = events(session, "log.cancelled")
    assert (cancelled.res_model, cancelled.res_id) == ("parcel", ids["P1"])
    assert out.text == "❌ Cancelled. Nothing was logged."
    assert out.buttons == []


def test_a_second_tap_or_a_stale_button_replies_plainly_and_changes_nothing(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake())
    first = on_message(d, voice("copper + sulphur, 2 kg, this morning", message_id=1))
    second = on_message(d, voice("copper + sulphur, 2 kg, this morning", message_id=2))
    on_callback(d, Callback(chat_id=CHAT, data=first.buttons[0][1]))
    on_callback(d, Callback(chat_id=CHAT, data=second.buttons[1][1]))

    again = on_callback(d, Callback(chat_id=CHAT, data=first.buttons[0][1]))
    cancel_done = on_callback(d, Callback(chat_id=CHAT, data=first.buttons[1][1]))
    gone = on_callback(d, Callback(chat_id=CHAT, data=second.buttons[0][1]))
    junk = on_callback(d, Callback(chat_id=CHAT, data="confirm:abc"))

    assert again.text == "This spray is already logged."
    assert cancel_done.text == "This spray is already logged."
    assert gone.text == "This spray was cancelled or no longer exists."
    assert junk.text == "I did not understand that button."
    assert [log.status for log in logs_of(session)] == ["done"]
    assert len(events(session, "log.confirmed")) == 1


def test_a_button_for_another_farms_log_finds_nothing(session_factory, session, settings):
    from farmcore.core.models.canonical import Farm

    seed_farm(session, settings)
    other = Farm(name="Other")
    session.add(other)
    session.flush()
    parcel = Parcel(farm_id=other.id, alias="P1", name="X", area_ha=Decimal("1"), geom=SQUARE)
    log = Log(farm_id=other.id, type="spray", status="pending", name="x", ts_start=NOW_LOCAL)
    log.parcels.append(parcel)
    session.add_all([parcel, log])
    session.commit()
    d = deps(session_factory, settings, fake())

    out = on_callback(d, Callback(chat_id=CHAT, data=f"confirm:{log.id}"))

    assert out.text == "This spray was cancelled or no longer exists."
    assert session.get(Log, log.id, populate_existing=True).status == "pending"


def test_this_morning_at_a_0600_replay_clock_is_dated_on_the_replay_day_and_clamped(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    out = on_message(d, voice("P1 copper + sulphur, 2 kg, this morning"))

    [log] = logs_of(session)
    assert log.ts_start == NOW_LOCAL  # 10 Apr 2024 06:00 local, not today's date
    assert log.ts_start.astimezone(PARIS).date().isoformat() == "2024-04-10"
    assert "time clamped to now" in log.sources[0].raw_payload["notes"]
    assert out.text.splitlines()[0].endswith("· 10 Apr 06:00 — confirm?")
    assert "Note: The time you said is later than now, so I used now." in out.text


def test_the_request_carries_the_replay_date_and_a_later_parsed_time_is_clamped(
    session_factory, session, settings
):
    seed_farm(session, settings)
    extractor = fake(ts_start=NOW_LOCAL.replace(hour=15))
    d = deps(session_factory, settings, extractor)

    out = on_message(d, voice("copper + sulphur, 2 kg, this afternoon"))

    [req] = extractor.requests
    assert req.today.isoformat() == "2024-04-10"
    assert req.now_local == NOW_LOCAL
    [log] = logs_of(session)
    assert log.ts_start == NOW_LOCAL
    assert log.sources[0].raw_payload["notes"] == ["time clamped to now"]
    assert "Note: The time you said is later than now, so I used now." in out.text


SPEC_SENTENCE = "Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos, this morning"


def test_spec_sentence_logs_one_spray_on_p5_and_p6_and_confirms_on_both(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    echo = on_message(d, voice(SPEC_SENTENCE))

    [log] = logs_of(session)
    assert sorted(p.id for p in log.parcels) == sorted([ids["P5"], ids["P6"]])
    assert sorted(q.product_id for q in log.quantities) == sorted([ids[COPPER], ids[SULPHUR]])
    assert echo.text.startswith("P5 Les Granges, P6 Peyrot · copper + sulphur · 2 kg/ha")

    on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert [log.status for log in logs_of(session)] == ["done"]
    confirmed = events(session, "log.confirmed")
    assert sorted(e.res_id for e in confirmed) == sorted([ids["P5"], ids["P6"]])
    assert all(e.payload["source_types"] == ["voice"] for e in confirmed)


@pytest.mark.parametrize(
    ("extractor", "transcript", "reply_start"),
    [
        (RegexExtractor(), "P42 copper, 2 kg, this morning", "I don't know parcel P42."),
        (fake(parcels=["P42"]), "P42 copper", "I don't know parcel P42."),
        (fake(parcels=[]), "copper, 2 kg", "Which parcel did you spray?"),
        (fake(products=["Mystery Mix"]), "P1 mystery mix", "I don't know the product Mystery Mix."),
        (fake(products=[]), "P1, 2 kg", "Which product did you spray?"),
        (fake(dose_value=None, dose_unit=None), "P1 copper", "How much did you use?"),
        (fake(dose_value=2, dose_unit=None), "P1 copper 2", "How much did you use?"),
        (fake(dose_value=50), "P1 copper 50 kg", "A dose of 50 kg/ha looks wrong."),
        (fake(dose_value=0.05), "P1 copper 0.05 kg", "A dose of 0.05 kg/ha looks wrong."),
    ],
)
def test_a_draft_that_cannot_be_logged_gets_a_clarifying_reply_and_no_log(
    session_factory, session, settings, extractor, transcript, reply_start
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, extractor)

    out = on_message(d, voice(transcript))

    assert out.text.startswith(reply_start)
    assert out.buttons == []
    assert count(session, Log) == 0


def test_unknown_parcel_reply_lists_the_farms_parcels(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    out = on_message(d, voice("P42 copper, 2 kg, this morning"))

    assert "P1 Le Plateau, P5 Les Granges, P6 Peyrot" in out.text


def test_a_message_without_words_asks_for_the_spray(session_factory, session, settings):
    seed_farm(session, settings)
    extractor = fake()
    d = deps(session_factory, settings, extractor)

    out = on_message(d, Incoming(chat_id=CHAT, text="  ", transcript=None, message_id=1))

    assert out.text.startswith("Tell me what you sprayed")
    assert extractor.requests == []
    assert count(session, Log) == 0


def test_a_text_message_is_logged_with_its_text_as_transcript(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, RegexExtractor())

    on_message(d, Incoming(chat_id=CHAT, text=SPEC_SENTENCE, transcript=None, message_id=7))

    [log] = logs_of(session)
    [src] = log.sources
    assert src.raw_payload["transcript"] == SPEC_SENTENCE
    assert src.raw_payload["input"] == "text"
    assert src.external_key == f"tg:{CHAT}:7"


def test_before_the_farm_is_seeded_the_bot_says_so(session_factory, session, settings):
    d = deps(session_factory, settings, fake())

    message = on_message(d, voice("P1 copper, 2 kg"))
    button = on_callback(d, Callback(chat_id=CHAT, data="confirm:1"))

    assert message.text == button.text == "The farm is not set up yet. Run `farmcore seed` first."
    assert count(session, Log) == 0


def test_build_deps_wires_the_configured_extractor_with_the_db_audit_sink(
    session_factory, settings
):
    d = build_deps(settings.model_copy(update={"extractor": "regex"}), session_factory, [])

    assert isinstance(d.extractor, RegexExtractor)
    assert d.manifests == ()
    vertex = build_deps(settings.model_copy(update={"extractor": "vertex"}), session_factory, [])
    assert isinstance(vertex.extractor.primary._audit, DbAuditSink)


def test_confirming_next_to_a_logged_spray_merges_and_says_so(session_factory, session, settings):
    from farmcore.core import logs
    from farmcore.core.logs import QuantityInput, SourceInput

    ids = seed_farm(session, settings)
    logs.record_operation(
        session,
        farm_id=session.get(Parcel, ids["P1"]).farm_id,
        type="spray",
        parcel_ids=[ids["P1"]],
        ts_start=NOW_LOCAL.replace(hour=4),
        ts_end=None,
        quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids[COPPER])],
        source=SourceInput("seed", "test-seed", {}, None, logs.RANK_SEED),
        actor="seed",
        occurred_at=NOW_LOCAL,
    )
    session.commit()
    d = deps(session_factory, settings, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    [log] = logs_of(session)
    assert sorted(s.source_type for s in log.sources) == ["seed", "voice"]
    assert out.text.startswith("✅ Added to the spray already logged: P1 Le Plateau")


def test_confirm_after_the_clock_stepped_back_before_the_draft_asks_to_send_again(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake())  # the draft starts at 05:00
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))
    set_replay(session, NOW_LOCAL.replace(hour=4), settings)
    session.commit()

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert out.text.startswith("This spray is dated after the current time")
    assert "send it again" in out.text
    assert out.buttons == []
    assert count(session, Log) == 0
    assert events(session, "log.confirmed") == []


def test_confirm_does_not_merge_into_a_spray_logged_later_than_now(
    session_factory, session, settings
):
    from farmcore.core import logs
    from farmcore.core.logs import QuantityInput, SourceInput

    ids = seed_farm(session, settings)
    later, _ = logs.record_operation(
        session,
        farm_id=session.get(Parcel, ids["P1"]).farm_id,
        type="spray",
        parcel_ids=[ids["P1"]],
        ts_start=NOW_LOCAL.replace(hour=11),
        ts_end=None,
        quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids[COPPER])],
        source=SourceInput("voice", "tg:1:99", {}, None, logs.RANK_VOICE),
        actor="chat:1",
        occurred_at=NOW_LOCAL.replace(hour=11),
    )
    session.commit()
    d = deps(session_factory, settings, fake())  # clock at 06:00, draft at 05:00
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert "11:00" in out.text and "later than the current time" in out.text
    assert out.buttons == [echo.buttons[1]]  # cancel stays possible
    statuses = {log.id: log.status for log in logs_of(session)}
    assert statuses[later.id] == "done"
    assert sorted(statuses.values()) == ["done", "pending"]
    assert len(session.get(Log, later.id, populate_existing=True).sources) == 1
    assert events(session, "log.merged") == []


def test_extractor_notes_are_echoed_in_plain_words_and_kept_raw(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake(notes=["fallback: regex (HTTP 500)"]))

    out = on_message(
        d, Incoming(chat_id=CHAT, text="P1 copper 2 kg", transcript=None, message_id=3)
    )

    assert "fallback:" not in out.text and "HTTP 500" not in out.text
    assert "Please check the details" in out.text
    [log] = logs_of(session)
    assert log.sources[0].raw_payload["notes"] == ["fallback: regex (HTTP 500)"]


def test_a_button_from_another_chat_does_not_touch_the_pending_log(
    session_factory, session, settings
):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    confirm = on_callback(d, Callback(chat_id=CHAT + 1, data=echo.buttons[0][1]))
    cancel = on_callback(d, Callback(chat_id=CHAT + 1, data=echo.buttons[1][1]))

    assert confirm.text == cancel.text == "This spray was cancelled or no longer exists."
    assert [log.status for log in logs_of(session)] == ["pending"]
    assert events(session, "log.confirmed") == events(session, "log.cancelled") == []


def test_a_redelivered_message_echoes_the_same_pending_log_without_a_second(
    session_factory, session, settings
):
    seed_farm(session, settings)
    extractor = fake()
    d = deps(session_factory, settings, extractor)
    first = on_message(d, voice("copper + sulphur, 2 kg, this morning", message_id=5))

    again = on_message(d, voice("copper + sulphur, 2 kg, this morning", message_id=5))

    assert count(session, Log) == 1
    assert len(extractor.requests) == 1
    assert again.buttons == first.buttons
    assert again.text.startswith("P1 Le Plateau · copper + sulphur · 2 kg/ha · 10 Apr 05:00")
    on_callback(d, Callback(chat_id=CHAT, data=first.buttons[0][1]))
    done = on_message(d, voice("copper + sulphur, 2 kg, this morning", message_id=5))
    assert done.text == "This spray is already logged."
    assert done.buttons == []
    assert count(session, Log) == 1


def test_confirm_reports_the_saved_register_checks_once_across_parcels(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    product = session.get(Product, ids[SULPHUR])
    product.props = {"label_max_dose_kg_ha": "1", "phi_days": 7}
    session.commit()
    d = deps(session_factory, settings, fake(parcels=["P5", "P6"], products=[SULPHUR]))
    echo = on_message(d, voice("P5 P6 sulphur 2 kg"))
    assert "Checks: Warning: Sulphur dose 2 kg/ha, above the label max 1" in echo.text

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert (
        "Checks: Warning: Sulphur dose 2 kg/ha, above the label max 1; "
        "Note: Harvest not before 17 Apr" in out.text
    )
    assert out.text.count("Sulphur dose") == 1
    assert out.text.count("Harvest not before") == 1
    again = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    assert again.text == "This spray is already logged."


def test_invalid_label_checks_do_not_undo_saved_spray_or_skip_advisor(
    committed_session_factory, settings
):
    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        session.get(Product, ids[SULPHUR]).props = {"phi_days": "unavailable"}
        session.commit()
    SEEN_BY_JOB.clear()
    d = deps(committed_session_factory, settings, fake(products=[SULPHUR]), [FAKE_ADVISOR])
    echo = on_message(d, voice("P1 sulphur 2 kg"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert out.text.startswith("✅ Logged:")
    assert "Checks unavailable; review the register." in out.text
    assert "OK:" not in out.text
    assert SEEN_BY_JOB == [["done"]]
    assert "Advice updated." in out.text


@pytest.mark.parametrize("base", ["https://agri.example.test", "http://localhost:8000"])
def test_confirmation_actions_pin_the_saved_farm(session_factory, session, settings, base):
    ids = seed_farm(session, settings)
    farm_id = session.get(Parcel, ids["P1"]).farm_id
    d = deps(session_factory, settings.model_copy(update={"public_base_url": base}), fake())
    echo = on_message(d, voice("P1 copper 2 kg"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    expected = [
        ("Open map", f"{base}/fields/?farm={farm_id}"),
        ("Open register", f"{base}/operations/register?farm={farm_id}"),
    ]
    if base.startswith("https:"):
        assert out.url_buttons == expected
    else:
        assert out.url_buttons == []
        assert all(f"{label}: {url}" in out.text for label, url in expected)


def test_confirm_empty_checks_are_unavailable_not_ok(session_factory, session, settings):
    seed_farm(session, settings)
    d = deps(session_factory, settings, fake(products=[SULPHUR]))
    echo = on_message(d, voice("P1 sulphur 2 kg"))
    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    assert "Checks unavailable; review the register." in out.text
    assert "OK:" not in out.text


def test_confirm_merged_spray_uses_canonical_checks_and_excludes_future_history(
    session_factory, session, settings
):
    from farmcore.core import logs
    from farmcore.core.logs import QuantityInput, SourceInput

    ids = seed_farm(session, settings)
    farm_id = session.get(Parcel, ids["P1"]).farm_id
    session.get(Product, ids[SULPHUR]).props = {"label_max_applications": 1}
    for key, when in (
        ("earlier", NOW_LOCAL.replace(hour=4)),
        ("future", NOW_LOCAL + timedelta(days=2)),
    ):
        logs.record_operation(
            session,
            farm_id=farm_id,
            type="spray",
            parcel_ids=[ids["P1"]],
            ts_start=when,
            ts_end=None,
            quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids[SULPHUR])],
            source=SourceInput("seed", key, {}, None, logs.RANK_SEED),
            actor="seed",
            occurred_at=NOW_LOCAL,
        )
    session.commit()
    d = deps(session_factory, settings, fake(products=[SULPHUR]))
    echo = on_message(d, voice("P1 sulphur 2 kg"))

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert out.text.startswith("✅ Added to the spray already logged:")
    assert "Checks: OK: 1 of 1 sulphur applications this season" in out.text
    assert "2 sulphur applications" not in out.text
    assert "Checks unavailable" not in out.text
