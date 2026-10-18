"""Bot CLI startup against a disposable database; Telegram and Whisper never run."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event
from telegram import Update
from telegram.ext import Application
from typer.testing import CliRunner

from farmcore.apps.chat import commands
from farmcore.apps.chat.transcribe import FakeTranscriber
from farmcore.cli import cli
from farmcore.core.models.kernel import AppModule


@pytest.fixture
def bot_boundaries(monkeypatch, settings):
    calls = []

    class Sender:
        def __init__(self, token):
            calls.append("sender")

        def close(self):
            calls.append("sender closed")

    def transcriber(settings):
        calls.append("transcriber")
        return FakeTranscriber()

    def poll(application, *, allowed_updates):
        assert allowed_updates == Update.ALL_TYPES
        calls.append("polling")

    def disposed(engine):
        calls.append("engine disposed")

    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("123:fake")})
    monkeypatch.setattr(commands, "get_settings", lambda: configured)
    monkeypatch.setattr(commands, "HttpxTelegramSender", Sender)
    monkeypatch.setattr(commands, "WhisperTranscriber", transcriber)
    monkeypatch.setattr(Application, "run_polling", poll)
    event.listen(Engine, "engine_disposed", disposed)
    try:
        yield calls
    finally:
        event.remove(Engine, "engine_disposed", disposed)


@pytest.mark.parametrize("disabled_app", ["chat", "advisor"])
def test_bot_exits_cleanly_when_chat_is_off_before_constructing_transports_or_models(
    committed_session_factory, bot_boundaries, disabled_app
):
    with committed_session_factory() as session:
        session.add(AppModule(name=disabled_app, version="0.1.0", state="uninstalled"))
        session.commit()

    result = CliRunner().invoke(cli, ["bot"])

    assert result.exit_code == 0, result.output
    assert bot_boundaries == ["engine disposed"]
    assert result.output.strip() == "Chat is off; the bot will not start."


@pytest.mark.parametrize("app_state", [None, ("chat", "installed"), ("kpi", "uninstalled")])
def test_enabled_bot_polls_and_closes_resources(
    committed_session_factory, bot_boundaries, app_state
):
    if app_state:
        name, state = app_state
        with committed_session_factory() as session:
            session.add(AppModule(name=name, version="0.1.0", state=state))
            session.commit()

    result = CliRunner().invoke(cli, ["bot"])

    assert result.exit_code == 0, result.output
    assert bot_boundaries == [
        "sender",
        "transcriber",
        "polling",
        "sender closed",
        "engine disposed",
    ]
    assert "Chat is off" not in result.output


def test_bot_polling_failure_still_closes_resources(
    committed_session_factory, bot_boundaries, monkeypatch
):
    def fail_polling(application, *, allowed_updates):
        raise RuntimeError("synthetic polling failure")

    monkeypatch.setattr(Application, "run_polling", fail_polling)

    result = CliRunner().invoke(cli, ["bot"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert bot_boundaries == ["sender", "transcriber", "sender closed", "engine disposed"]


def test_bot_model_startup_failure_still_closes_resources(
    committed_session_factory, bot_boundaries, monkeypatch
):
    def fail_transcriber(settings):
        raise RuntimeError("synthetic model startup failure")

    monkeypatch.setattr(commands, "WhisperTranscriber", fail_transcriber)

    result = CliRunner().invoke(cli, ["bot"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert bot_boundaries == ["sender", "sender closed", "engine disposed"]
