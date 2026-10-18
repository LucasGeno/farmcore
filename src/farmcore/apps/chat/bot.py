"""Telegram bot runtime: python-telegram-bot long polling mapped onto 14's chat handlers.

`BotRuntime` turns updates into the transport-free DTOs (`Incoming`, `Callback`), runs the
synchronous handlers in a worker thread (they use the database and the extractor) and sends each
`Outgoing` through a `TelegramSender` (Bot API `sendMessage`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from farmcore.apps.chat import handlers, push
from farmcore.apps.chat.dto import Callback, Incoming, Outgoing
from farmcore.apps.chat.handlers import ChatDeps
from farmcore.apps.chat.telegram import TelegramSender
from farmcore.apps.chat.transcribe import Transcriber
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core.access import (
    FarmNotSeeded,
    ScopedRepository,
    default_farm,
    ensure_owner_grant,
)
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.db import session_scope

logger = logging.getLogger(__name__)

PURPOSE = "chat"
REPLY_PRIVATE_DEMO = "This is a private bot. Ask the farm administrator for access."
REPLY_NO_TRANSCRIPT = "Sorry, I could not transcribe that voice note. Please type it instead."
REPLY_NOTHING_HEARD = "🎤 (no words heard)"
# A reset drops the database, and with it the chat binding `/start` wrote.
REPLY_START_FIRST = "Send /start first: this chat is not linked to the farm yet."
REPLY_HOW_TO = (
    "Send a voice note or a text with what you sprayed, "
    'e.g. "P1 copper, 2 kg, this morning". '
    "Check the draft, then tap Confirm or send /confirm. /cancel starts again. "
    "Advice changes arrive here."
)


def _parcel_key(parcel: Parcel) -> tuple[int, str]:
    m = re.fullmatch(r"[Pp](\d+)", parcel.alias)
    return (int(m.group(1)), parcel.alias) if m else (10**9, parcel.alias)


def _repo(session: Session, deps: ChatDeps, chat_id: int) -> ScopedRepository:
    farm = default_farm(session, deps.settings)
    grant = ensure_owner_grant(session, farm)
    return ScopedRepository(session, farm.id, grant, f"chat:{chat_id}", PURPOSE)


@dataclass(frozen=True)
class BotRuntime:
    deps: ChatDeps
    transcriber: Transcriber
    sender: TelegramSender
    # Chats already told they are not on the allowlist (one reply each per runtime).
    refused: set[int] = field(default_factory=set)

    def _send(self, chat_id: int, out: Outgoing) -> None:
        self.sender.send(chat_id, out.text, buttons=out.buttons, url_buttons=out.url_buttons)

    async def _allowed(self, update: Update) -> bool:
        """Gate for every handler, before any Whisper, Vertex or database work. A stranger gets
        one polite reply per process; ids are never logged with message content."""
        user = update.effective_user
        if user is not None and user.id in self.deps.settings.allowed_telegram_users():
            return True
        chat_id = update.effective_chat.id
        if chat_id not in self.refused:
            self.refused.add(chat_id)
            logger.warning("refused update from a user not on the allowlist")
            await asyncio.to_thread(self._send, chat_id, Outgoing(text=REPLY_PRIVATE_DEMO))
        return False

    async def on_text(self, update: Update, context: Any) -> None:
        if not await self._allowed(update):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        incoming = Incoming(
            chat_id=chat_id, text=message.text, transcript=None, message_id=message.message_id
        )

        def work() -> None:
            if not self._bound(chat_id):
                self._send(chat_id, Outgoing(text=REPLY_START_FIRST))
                return
            self._send(chat_id, handlers.on_message(self.deps, incoming))

        await asyncio.to_thread(work)

    async def on_voice(self, update: Update, context: Any) -> None:
        """Download the voice note to a temp file, transcribe it locally (hints: parcel and
        product names), delete the file, then handle the transcript; the reply shows the
        transcript first. Raw audio never leaves this host."""
        if not await self._allowed(update):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        if not await asyncio.to_thread(self._bound, chat_id):
            await asyncio.to_thread(self._send, chat_id, Outgoing(text=REPLY_START_FIRST))
            return
        voice = message.voice or message.audio
        with tempfile.TemporaryDirectory(prefix="farmcore-voice-") as tmp:
            path = Path(tmp) / "voice.ogg"
            try:
                file = await context.bot.get_file(voice.file_id)
                await file.download_to_drive(custom_path=path)
                transcript = await asyncio.to_thread(self._transcribe, chat_id, path)
            except Exception as exc:
                # The type only: messages could quote audio metadata or the Bot API URL.
                logger.error("voice note failed (%s)", type(exc).__name__)
                transcript = None
            finally:
                path.unlink(missing_ok=True)
        if transcript is None:
            await asyncio.to_thread(self._send, chat_id, Outgoing(text=REPLY_NO_TRANSCRIPT))
            return
        incoming = Incoming(
            chat_id=chat_id, text=None, transcript=transcript, message_id=message.message_id
        )

        def work() -> None:
            out = handlers.on_message(self.deps, incoming)
            heard = f"🎤 “{transcript}”" if transcript.strip() else REPLY_NOTHING_HEARD
            self._send(
                chat_id,
                Outgoing(
                    text=f"{heard}\n{out.text}", buttons=out.buttons, url_buttons=out.url_buttons
                ),
            )

        await asyncio.to_thread(work)

    async def on_callback(self, update: Update, context: Any) -> None:
        if not await self._allowed(update):
            return
        query = update.callback_query
        chat_id = update.effective_chat.id
        try:  # stops the button's spinner; harmless if it fails
            await context.bot.answer_callback_query(query.id)
        except Exception as exc:
            logger.warning("answer_callback_query failed (%s)", type(exc).__name__)
        callback = Callback(chat_id=chat_id, data=query.data or "")

        def work() -> None:
            self._send(chat_id, handlers.on_callback(self.deps, callback))

        await asyncio.to_thread(work)

    async def on_start(self, update: Update, context: Any) -> None:
        """Bind this chat to the demo farm (farm props `chat.telegram_chat_ids`, once) and
        reply with the parcel list."""
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        await asyncio.to_thread(lambda: self._send(chat_id, self._start(chat_id)))

    def _start(self, chat_id: int) -> Outgoing:
        try:
            with session_scope(self.deps.session_factory) as session:
                repo = _repo(session, self.deps, chat_id)
                # Row lock: the push job writes the same props.
                farm = session.get(Farm, repo.farm_id, with_for_update=True, populate_existing=True)
                chats = [int(c) for c in push.chat_props(farm).get(push.CHATS_KEY) or []]
                if chat_id not in chats:
                    push.set_chat_props(farm, **{push.CHATS_KEY: [*chats, chat_id]})
                parcels = repo.list(Parcel, scope="fields:read")
                lines = [f"{p.alias} {p.name}" for p in sorted(parcels, key=_parcel_key)]
                farm_name = farm.name
        except FarmNotSeeded:
            return Outgoing(text=handlers.REPLY_NO_FARM)
        return Outgoing(
            text="\n".join(
                [f"This chat is linked to {farm_name}. Parcels:", *lines, "", REPLY_HOW_TO]
            )
        )

    def _bound(self, chat_id: int) -> bool:
        """Whether `/start` linked this chat to the farm. Before the seed there is no farm:
        True, so the handler answers with its own "seed first" reply."""
        try:
            with session_scope(self.deps.session_factory) as session:
                farm = default_farm(session, self.deps.settings)
                chats = push.chat_props(farm).get(push.CHATS_KEY) or []
                return chat_id in {int(c) for c in chats}
        except FarmNotSeeded:
            return True

    def _hints(self, chat_id: int) -> list[str]:
        """Parcel and product names that bias Whisper's spelling; none before the seed."""
        try:
            with session_scope(self.deps.session_factory) as session:
                repo = _repo(session, self.deps, chat_id)
                parcels = repo.list(Parcel, scope="fields:read", order_by=Parcel.id)
                products = products_for_farm(repo, scope="logs:write")
                return [p.name for p in parcels] + [p.name for p in products]
        except FarmNotSeeded:
            return []

    def _transcribe(self, chat_id: int, path: Path) -> str:
        return self.transcriber.transcribe(path, self._hints(chat_id)).text


async def _log_error(update: object, context: Any) -> None:
    # The type only: PTB and httpx errors can quote the Bot API URL, which carries the token.
    error = getattr(context, "error", None)
    logger.error("update failed (%s)", type(error).__name__)


def build_application(runtime: Any, token: str) -> Application:
    """PTB application for long polling: `/start`, text, voice/audio and button taps.

    New messages only: an edit keeps its message_id, so 14's dedupe would answer it with the
    old draft; channel posts are not a farmer's chat."""
    app = Application.builder().token(token).build()
    new = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("start", runtime.on_start, filters=new))
    app.add_handler(CommandHandler("cancel", runtime.on_text, filters=new))
    app.add_handler(CommandHandler("confirm", runtime.on_text, filters=new))
    app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, runtime.on_text))
    app.add_handler(MessageHandler(new & (filters.VOICE | filters.AUDIO), runtime.on_voice))
    app.add_handler(CallbackQueryHandler(runtime.on_callback))
    app.add_error_handler(_log_error)
    return app
