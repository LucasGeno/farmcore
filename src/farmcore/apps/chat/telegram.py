"""Sending Telegram messages: a `TelegramSender` protocol, the Bot API `sendMessage` over httpx,
and a fake for tests.

The bot token is part of every Bot API URL, so it never reaches a log or an exception message:
transport and HTTP errors are re-raised as `TelegramSendError` carrying only the status code and
Telegram's own description.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 15.0

Buttons = Sequence[tuple[str, str]]  # (label, callback data) or (label, url)


class TelegramSender(Protocol):
    def send(
        self, chat_id: int, text: str, buttons: Buttons = (), url_buttons: Buttons = ()
    ) -> None:
        """Send `text` to `chat_id`; `buttons` are callback buttons, `url_buttons` open a URL.
        Each button is its own keyboard row, callback buttons first."""
        ...


class TelegramSendError(RuntimeError):
    """A send failed. The message never contains the bot token."""


def reply_markup(buttons: Buttons = (), url_buttons: Buttons = ()) -> dict[str, Any] | None:
    rows = [[{"text": label, "callback_data": data}] for label, data in buttons]
    rows += [[{"text": label, "url": url}] for label, url in url_buttons]
    return {"inline_keyboard": rows} if rows else None


class HttpxTelegramSender:
    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._url = f"{API_BASE}/bot{token}/sendMessage"
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=TIMEOUT_S)

    def __repr__(self) -> str:
        return "HttpxTelegramSender(token=***)"

    def close(self) -> None:
        """Close the connection pool this sender opened (an injected client is the caller's)."""
        if self._owns_client:
            self._client.close()

    def send(
        self, chat_id: int, text: str, buttons: Buttons = (), url_buttons: Buttons = ()
    ) -> None:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        markup = reply_markup(buttons, url_buttons)
        if markup is not None:
            body["reply_markup"] = markup
        try:
            response = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            # str(exc) may quote the request URL, which carries the token.
            raise TelegramSendError(f"sendMessage failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            try:
                description = str(response.json().get("description", ""))
            except ValueError:
                description = ""
            raise TelegramSendError(
                f"sendMessage failed: HTTP {response.status_code} {description}".rstrip()
            )


@dataclass(frozen=True)
class Sent:
    chat_id: int
    text: str
    buttons: list[tuple[str, str]]
    url_buttons: list[tuple[str, str]]


@dataclass
class FakeTelegramSender:
    sent: list[Sent] = field(default_factory=list)

    def send(
        self, chat_id: int, text: str, buttons: Buttons = (), url_buttons: Buttons = ()
    ) -> None:
        self.sent.append(Sent(chat_id, text, list(buttons), list(url_buttons)))
