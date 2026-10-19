"""Saved spray weather through the real entry points: the web upload, the operations CLI and
the bot's command-built dependencies with the voice ✅ callback. Each composes the station reader
from the discovered apps; Weather's live state is read inside the writing transaction.
Synthetic farm, station and readings only; fake extractor, no bot or model call."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from farmcore.apps.chat import commands as chat_commands
from farmcore.apps.chat.dto import Callback
from farmcore.apps.chat.handlers import REPLY_ALREADY_LOGGED, ChatDeps, on_callback, on_message
from farmcore.apps.operations import isoxml_demo, spray_weather
from farmcore.apps.stations.capture import read_slot
from farmcore.cli import cli
from farmcore.core import clock
from farmcore.core.models.canonical import LogSource, Parcel
from farmcore.core.models.kernel import AppModule
from farmcore.settings import get_settings
from tests.apps.chat.test_handlers import CHAT, NOW_LOCAL, fake, logs_of, seed_farm, voice
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm
from tests.apps.operations.test_spray_weather_capture import add_station, hours

farm = isoxml_farm.farm  # fixture
NS = spray_weather.NAMESPACE


def _sources(session, source_type: str) -> list[LogSource]:
    return list(
        session.scalars(
            select(LogSource)
            .where(LogSource.source_type == source_type)
            .order_by(LogSource.id)
            .execution_options(populate_existing=True)
        )
    )


def _weather(session, state: str) -> None:
    session.merge(AppModule(name="stations", state=state, version="0"))


# --- web ----------------------------------------------------------------------------------


def _upload(client):
    data = isoxml_demo.demo_taskdata()
    return client.post(
        "/operations/isoxml", files={"file": ("TASKDATA.XML", data, "application/xml")}
    )


def test_web_upload_composes_the_reader_and_saves_weather(client, session, settings, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    clock.set_replay(session, local(CLOCK), settings)
    session.commit()

    assert client.app.state.spray_weather_reader is read_slot
    assert _upload(client).status_code == 200

    assert [s.raw_payload[NS]["status"] for s in _sources(session, "isoxml")] == [
        "captured",
        "captured",
    ]


def test_web_off_at_start_then_on_captures_without_a_restart(settings, session_factory, session):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    f = make_farm(session, settings)
    add_station(session, f.repo.farm_id, hours(5, 6))
    clock.set_replay(session, local(CLOCK), settings)
    _weather(session, "uninstalled")
    session.commit()
    with TestClient(create_app(settings, session_factory=session_factory)) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert all(m.name != "stations" for m in c.app.state.manifests)  # off at start
        assert c.app.state.spray_weather_reader is read_slot  # but installed software
        _weather(session, "installed")
        session.commit()

        assert _upload(c).status_code == 200

    assert {s.raw_payload[NS]["status"] for s in _sources(session, "isoxml")} == {"captured"}


def test_web_on_then_off_suppresses_capture_at_once(client, session, settings, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    clock.set_replay(session, local(CLOCK), settings)
    _weather(session, "uninstalled")
    session.commit()

    assert _upload(client).status_code == 200

    reasons = {s.raw_payload[NS]["reason"] for s in _sources(session, "isoxml")}
    assert reasons == {spray_weather.NO_CAPABILITY}


# --- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_env(committed_session_factory, settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_cli_import_composes_the_reader_and_saves_weather(
    cli_env, committed_session_factory, settings, tmp_path
):
    with committed_session_factory() as s:
        f = make_farm(s, settings)
        add_station(s, f.repo.farm_id, hours(5, 6))
        clock.set_replay(s, local(CLOCK), settings)
        s.commit()
    path = tmp_path / "TASKDATA.XML"
    path.write_bytes(isoxml_demo.demo_taskdata())

    result = CliRunner().invoke(cli, ["operations", "import-isoxml", str(path)])

    assert result.exit_code == 0, result.output
    with committed_session_factory() as s:
        statuses = [src.raw_payload[NS]["status"] for src in _sources(s, "isoxml")]
    assert statuses == ["captured", "captured"]


# --- bot: command-built deps + voice ✅ ---------------------------------------------------


def _bot_deps(settings, session_factory, extractor) -> ChatDeps:
    discovered, active = chat_commands._manifests(session_factory, ("farmcore.apps",))
    built = chat_commands.bot_deps(settings, session_factory, discovered, active)
    return ChatDeps(
        built.session_factory,
        extractor,
        built.settings,
        built.manifests,
        weather_reader=built.weather_reader,
    )


def _voice_confirm(session_factory, session, settings, deps, ids):
    echo = on_message(deps, voice("copper + sulphur, 2 kg, this morning"))
    return on_callback(deps, Callback(chat_id=CHAT, data=echo.buttons[0][1]))


def test_bot_deps_carry_the_reader_and_a_confirm_saves_the_drafts_own_weather(
    session_factory, session, settings
):
    ids = seed_farm(session, settings)
    farm_id = session.get(Parcel, ids["P1"]).farm_id
    reading = {"t_c": 7.0, "ff_kmh": 4.0, "rr1_mm": 0.0}
    add_station(session, farm_id, {datetime(2024, 4, 10, 3, tzinfo=UTC): reading})
    session.commit()
    d = _bot_deps(settings, session_factory, fake())
    assert d.weather_reader is read_slot

    out = _voice_confirm(session_factory, session, settings, d, ids)

    assert out.text.startswith("✅ Logged")
    [src] = _sources(session, "voice")
    evidence = src.raw_payload[NS]
    assert evidence["status"] == "captured"
    assert evidence["slot"] == "2024-04-10T03:00:00+00:00"  # 05:00 Paris draft time
    assert evidence["values"] == {"t_c": 7.0, "ff_kmh": 4.0, "rr1_mm": 0.0}
    assert evidence["parcel_ids"] == [ids["P1"]]
    assert "transcript" in src.raw_payload  # the original receipt is kept
    assert not any(k in evidence for k in ("transcript", "chat_id", "external_key"))


def test_a_pending_draft_or_a_cancel_saves_nothing(session_factory, session, settings):
    ids = seed_farm(session, settings)
    d = _bot_deps(settings, session_factory, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))
    [src] = _sources(session, "voice")
    assert NS not in src.raw_payload  # pending

    on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[1][1]))  # ❌

    assert all(NS not in s.raw_payload for s in _sources(session, "voice"))
    assert ids  # farm seeded


def test_a_repeated_confirm_never_overwrites(session_factory, session, settings):
    seed_farm(session, settings)
    d = _bot_deps(settings, session_factory, fake())
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))
    on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    [src] = _sources(session, "voice")
    first = dict(src.raw_payload)

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert out.text == REPLY_ALREADY_LOGGED
    assert _sources(session, "voice")[0].raw_payload == first


def test_a_merged_confirm_keeps_the_drafts_time_and_parcels(session_factory, session, settings):
    from decimal import Decimal

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
        quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids["Copper Shield 20 WP"])],
        source=SourceInput("seed", "test-seed", {}, None, logs.RANK_SEED),
        actor="seed",
        occurred_at=NOW_LOCAL,
    )
    session.commit()
    d = _bot_deps(settings, session_factory, fake())

    out = _voice_confirm(session_factory, session, settings, d, ids)

    assert out.text.startswith("✅ Added to the spray already logged")
    [log] = logs_of(session)
    by_type = {s.source_type: s for s in log.sources}
    assert NS not in by_type["seed"].raw_payload  # no backfill of the older record
    evidence = by_type["voice"].raw_payload[NS]
    saved_at = datetime.fromisoformat(evidence["source_time"])
    assert saved_at == NOW_LOCAL.replace(hour=5)  # the draft's own time (voice outranks seed)
    assert evidence["parcel_ids"] == [ids["P1"]]
    assert evidence["reason"] == spray_weather.NO_STATION


def test_a_capture_error_rolls_the_confirm_back_not_already_logged(
    session_factory, session, settings
):
    seed_farm(session, settings)

    def broken(repo, slot):
        raise ValueError("station payload invalid")

    base = _bot_deps(settings, session_factory, fake())
    d = ChatDeps(
        base.session_factory, base.extractor, base.settings, base.manifests, weather_reader=broken
    )
    echo = on_message(d, voice("copper + sulphur, 2 kg, this morning"))

    with pytest.raises(ValueError, match="station payload invalid"):
        on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    [log] = logs_of(session)
    assert log.status == "pending"  # rolled back, and a clean retry still works
    ok = ChatDeps(base.session_factory, base.extractor, base.settings, base.manifests)
    out = on_callback(ok, Callback(chat_id=CHAT, data=echo.buttons[0][1]))
    assert out.text.startswith("✅ Logged")
    [src] = _sources(session, "voice")
    assert src.raw_payload[NS]["reason"] == spray_weather.NO_CAPABILITY


# --- the real `farmcore bot` command (Telegram, Whisper and polling are test boundaries) ----

from tests.apps.chat.test_commands import bot_boundaries  # noqa: E402, F401, F811  (fixture)


def test_the_bot_command_injects_the_reader_from_the_discovered_apps(
    committed_session_factory,
    bot_boundaries,  # noqa: F811  (the imported fixture)
    monkeypatch,
):
    # (Stations off at start also switches Chat off through its dependencies: no bot then.)
    seen = []
    real = chat_commands.build_application

    def recording(runtime, token):
        seen.append(runtime.deps)
        return real(runtime, token)

    monkeypatch.setattr(chat_commands, "build_application", recording)

    result = CliRunner().invoke(cli, ["bot"])

    assert result.exit_code == 0, result.output
    [deps] = seen
    assert deps.weather_reader is read_slot
    assert "chat" in {m.name for m in deps.manifests}  # jobs keep the active list


def test_a_voice_note_without_a_stated_time_saves_time_unknown_without_a_station_read(
    session_factory, session, settings
):
    from farmcore.apps.chat.extractor import RegexExtractor

    ids = seed_farm(session, settings)
    farm_id = session.get(Parcel, ids["P1"]).farm_id
    reading = {"t_c": 7.0, "ff_kmh": 4.0, "rr1_mm": 0.0}
    add_station(session, farm_id, {datetime(2024, 4, 10, 4, tzinfo=UTC): reading})  # replay hour
    session.commit()
    calls = []
    base = _bot_deps(settings, session_factory, RegexExtractor())
    d = ChatDeps(
        base.session_factory,
        base.extractor,
        base.settings,
        base.manifests,
        weather_reader=lambda repo, slot: calls.append(slot) or read_slot(repo, slot),
    )
    echo = on_message(d, voice("P1 copper 2 kg"))  # no time said: the draft uses now
    assert "No time was said" in echo.text

    out = on_callback(d, Callback(chat_id=CHAT, data=echo.buttons[0][1]))

    assert out.text.startswith("✅ Logged")
    [src] = _sources(session, "voice")
    evidence = src.raw_payload[NS]
    assert evidence["status"] == "unavailable" and evidence["reason"] == spray_weather.NO_TIME
    assert evidence["source_time"] is None and evidence["slot"] is None
    assert calls == []  # never the replay hour's reading
