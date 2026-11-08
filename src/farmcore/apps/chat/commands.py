"""`farmcore bot`: the Telegram bot process (long polling), separate from the web app.

Installed apps' manifests come from the registry, as for `farmcore replay`, so a confirmed spray
re-runs `advisor.evaluate` with `run_now`. The advice digest push is a manifest job
(`chat.push_advice`) run by replay steps and the web process's live scheduler, not here.
"""

from __future__ import annotations

import logging
from time import sleep

import typer
from sqlalchemy.orm import Session, sessionmaker
from telegram import Update

from farmcore.apps.chat.bot import BotRuntime, build_application
from farmcore.apps.chat.handlers import ChatDeps, build_deps
from farmcore.apps.chat.telegram import HttpxTelegramSender
from farmcore.apps.chat.transcribe import WhisperTranscriber
from farmcore.composition import compose_spray_weather_reader
from farmcore.core import registry
from farmcore.core.manifest import Manifest
from farmcore.core.startup import EFFECTS_ENABLED, read_external_effects
from farmcore.db import make_engine, make_session_factory
from farmcore.settings import Settings, get_settings

logger = logging.getLogger("farmcore.bot")
# How often a paused bot looks again whether the owner enabled external effects.
EFFECTS_RECHECK_SECONDS = 30
# These log every request URL at INFO, and Bot API URLs carry the token.
_URL_LOGGERS = ("httpx", "httpcore", "telegram", "apscheduler")


def _manifests(
    factory: sessionmaker[Session], packages: tuple[str, ...]
) -> tuple[list[Manifest], list[Manifest]]:
    """`(discovered, active)` from one discovery and one state read: active drives the bot's
    start and jobs; the discovered software composes optional readers (saved spray weather)."""
    ordered = registry.discover(packages=packages)
    with factory() as session:
        states = registry.read_states(session, ordered)
    return ordered, registry.active_manifests(ordered, states)


def bot_deps(
    settings: Settings,
    factory: sessionmaker[Session],
    discovered: list[Manifest],
    active: list[Manifest],
) -> ChatDeps:
    """The bot's `ChatDeps`: active apps for jobs, the saved-weather reader from the discovered."""
    return build_deps(
        settings,
        factory,
        active,
        weather_reader=compose_spray_weather_reader(discovered),
    )


def _wait_for_effects(factory: sessionmaker[Session]) -> None:
    """Hold off polling while external effects are paused (a restored copy, until its owner
    enables them): a second poller on the original's token would make both fail."""
    announced = False
    while True:
        with factory() as session:
            if read_external_effects(session) == EFFECTS_ENABLED:
                return
        if not announced:
            typer.echo(
                "External effects are paused for this installation; the bot will not poll "
                "Telegram until the owner runs `farmcore effects enable`."
            )
            announced = True
        sleep(EFFECTS_RECHECK_SECONDS)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def register(cli: typer.Typer) -> None:
    @cli.command()
    def bot(
        app_package: list[str] = typer.Option(
            ["farmcore.apps"], "--app-package", hidden=True, help="App packages to discover."
        ),
    ) -> None:
        """Run the Telegram bot (long polling) until interrupted."""
        settings = get_settings()
        if settings.telegram_bot_token is None:
            typer.echo("TELEGRAM_BOT_TOKEN is not set; the bot cannot start.", err=True)
            raise typer.Exit(code=1)
        token = settings.telegram_bot_token.get_secret_value()
        configure_logging()
        engine = make_engine(settings)
        try:
            factory = make_session_factory(engine)
            discovered, manifests = _manifests(factory, tuple(app_package))
            if not any(m.name == "chat" for m in manifests):
                typer.echo("Chat is off; the bot will not start.")
                return
            _wait_for_effects(factory)
            sender = HttpxTelegramSender(token)
            try:
                runtime = BotRuntime(
                    deps=bot_deps(settings, factory, discovered, manifests),
                    transcriber=WhisperTranscriber(settings),
                    sender=sender,
                )
                application = build_application(runtime, token)
                logger.info(
                    "polling Telegram for updates (extractor=%s, whisper=%s, apps=%s)",
                    settings.extractor,
                    settings.whisper_model,
                    ",".join(m.name for m in manifests),
                )
                application.run_polling(allowed_updates=Update.ALL_TYPES)
            finally:
                sender.close()
        finally:
            engine.dispose()
