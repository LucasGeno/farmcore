"""Transport-free message shapes: the bot runtime maps Telegram updates to these."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Incoming:
    """One chat message: a typed `text`, or a voice note already turned into `transcript`."""

    chat_id: int
    text: str | None
    transcript: str | None
    message_id: int


@dataclass(frozen=True)
class Callback:
    """A tapped inline button; `data` is `confirm:<log_id>` or `cancel:<log_id>`."""

    chat_id: int
    data: str


@dataclass(frozen=True)
class Outgoing:
    """A reply; `buttons` are inline `(label, callback data)` pairs and `url_buttons` inline
    `(label, url)` pairs, each in order (callback buttons first)."""

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    url_buttons: list[tuple[str, str]] = field(default_factory=list)
