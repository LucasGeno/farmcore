"""Actor masking: who did or read something, safe to show anyone (share's access log, Activity).

`chat:<id>` and `tg:<chat>:<msg>` → `chat …8777` (a short id shows no digits); any other run of
≥ 6 digits → `…<last 4>`. `owner`, `job:*`, `cli:*`, `seed`, `import:*` and `grant:<id>` are
shown as they are.
"""

from __future__ import annotations

import re

# Trailing chat-id digits shown: the same rule as `chat/routes.py` MASK_KEEP (core imports no
# app, so the value is repeated, not imported).
MASK_KEEP = 4
_LONG_DIGITS = re.compile(r"\d{6,}")  # a chat, phone or account number, never shown whole


def _masked_chat(chat_id: str) -> str:
    return f"chat …{chat_id[-MASK_KEEP:]}" if len(chat_id) > MASK_KEEP else "chat …"


def mask_actor(actor: str) -> str:
    """An actor safe to show anyone (see the module docstring)."""
    prefix, sep, rest = actor.partition(":")
    if sep and prefix == "chat":
        return _masked_chat(rest)
    if sep and prefix == "tg":
        return _masked_chat(rest.split(":", 1)[0])
    if sep and prefix == "grant" and rest.isdigit():
        return actor
    return _LONG_DIGITS.sub(lambda m: f"…{m.group(0)[-MASK_KEEP:]}", actor)
