"""Sending Telegram messages: a `TelegramSender` protocol, the Bot API `sendMessage` over httpx,
and a fake for tests.

The bot token is part of every Bot API URL, so it never reaches a log or an exception message:
transport and HTTP errors are re-raised as `TelegramSendError` carrying only the status code and
Telegram's own description. A send whose request may have reached Telegram without an answer
coming back raises `TelegramOutcomeUnknown` (a `TelegramSendError`): the message may have arrived.
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
    ) -> int | None:
        """Send `text` to `chat_id`; `buttons` are callback buttons, `url_buttons` open a URL.
        Each button is its own keyboard row, callback buttons first. Returns Telegram's message
        id when it gives one."""
        ...


class TelegramSendError(RuntimeError):
    """A send failed. The message never contains the bot token. `retry_after`: the seconds
    Telegram asked to wait before sending again (its flood limit, HTTP 429), if it said."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramOutcomeUnknown(TelegramSendError):
    """The request may have reached Telegram, but no answer came back: it may have been sent."""


# Transport errors raised before the request could leave: Telegram certainly saw nothing.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


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
    ) -> int | None:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        markup = reply_markup(buttons, url_buttons)
        if markup is not None:
            body["reply_markup"] = markup
        try:
            response = self._client.post(self._url, json=body)
        except _NOT_SENT as exc:
            # str(exc) may quote the request URL, which carries the token.
            raise TelegramSendError(f"sendMessage failed: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise TelegramOutcomeUnknown(f"sendMessage failed: {type(exc).__name__}") from None
        if response.status_code >= 500:
            # A gateway's error may come after Telegram handled the message.
            raise TelegramOutcomeUnknown(f"sendMessage failed: HTTP {response.status_code}")
        if response.status_code != 200:
            try:
                answer = response.json()
                description = str(answer.get("description", ""))
                retry_after = (answer.get("parameters") or {}).get("retry_after")
            except (ValueError, AttributeError):
                description, retry_after = "", None
            raise TelegramSendError(
                f"sendMessage failed: HTTP {response.status_code} {description}".rstrip(),
                retry_after=retry_after if isinstance(retry_after, int | float) else None,
            )
        try:
            message_id = response.json()["result"]["message_id"]
        except (ValueError, KeyError, TypeError):
            return None
        return message_id if isinstance(message_id, int) else None


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
    ) -> int | None:
        self.sent.append(Sent(chat_id, text, list(buttons), list(url_buttons)))
        return None
