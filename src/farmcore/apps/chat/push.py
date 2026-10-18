"""Job `chat.push_advice`: one Telegram digest per farm per run from `advice.changed` events.

Per farm: the `advice.changed` events with `event.id` above the farm's push cursor (farm props
`chat.push_cursor`, the last pushed `event.id`) become one message to every bound chat (farm
props `chat.telegram_chat_ids`, written by `/start`): a summary line ("10 parcels → spray"), the
parcels that need action with their headline, and one "Open card" button per such parcel (a
local `public_base_url` puts the link in the text: Telegram rejects such buttons). The cursor
moves to the last event id and is committed (releasing the farm row lock that `/start` also
takes) before anything is sent, so delivery is at most once: a chat Telegram refuses (blocked
bot, deleted chat, network blip) is logged by chat id and error type and skipped, and never
makes the next run resend the digest to the others. Farms are independent: one farm's failure
is logged and the run goes on. Event ids only grow, so stepping the clock back is safe: the next
evaluation's events are new ids.

The event payload is 13's contract: `alias`, `name`, `old`, `new`, `headline`, `card_path`.
A parcel's latest event in the batch wins. Events dated after the clock's now (left over from a
run before the clock was stepped back) are skipped but still move the cursor.

The job does nothing when `telegram_bot_token` is unset, so replay steps in tests and the CLI
never call Telegram. Tests inject a sender with `set_sender`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from farmcore.apps.advisor import priority
from farmcore.apps.chat.telegram import HttpxTelegramSender, TelegramSender, TelegramSendError
from farmcore.core.access import repo_for_job
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice, Event, Farm, Parcel
from farmcore.core.urls import with_farm
from farmcore.settings import Settings

JOB_NAME = "chat.push_advice"
KIND = "advice.changed"
PROPS_KEY = "chat"
CHATS_KEY = "telegram_chat_ids"
CURSOR_KEY = "push_cursor"
ACTION = "spray"  # the decision that needs the farmer to act
# Summary order: the decisions a farmer acts on first. Others follow alphabetically.
DECISION_ORDER = ("spray", "wait", "protected", "none")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})

logger = logging.getLogger(__name__)

_sender: TelegramSender | None = None


def set_sender(sender: TelegramSender | None) -> None:
    """Use `sender` for every push (tests); `None` restores the Bot API sender."""
    global _sender
    _sender = sender


def get_sender(settings: Settings) -> TelegramSender | None:
    if _sender is not None:
        return _sender
    if settings.telegram_bot_token is None:
        return None
    return HttpxTelegramSender(settings.telegram_bot_token.get_secret_value())


@contextmanager
def _run_sender(settings: Settings) -> Iterator[TelegramSender]:
    """The sender for one run; a Bot API sender built here is closed afterwards."""
    sender = get_sender(settings)
    assert sender is not None
    try:
        yield sender
    finally:
        if sender is not _sender:
            close = getattr(sender, "close", None)
            if close is not None:
                close()


def chat_props(farm: Farm) -> dict[str, Any]:
    return dict((farm.props or {}).get(PROPS_KEY) or {})


def set_chat_props(farm: Farm, **values: Any) -> None:
    # Reassign: `props` is plain JSONB, so in-place edits are not tracked.
    farm.props = {**(farm.props or {}), PROPS_KEY: {**chat_props(farm), **values}}


def _alias_key(alias: str) -> tuple[int, str]:
    m = re.fullmatch(r"[Pp](\d+)", alias)
    return (int(m.group(1)), alias) if m else (10**9, alias)


def _decision_key(decision: str) -> tuple[int, str]:
    order = DECISION_ORDER.index(decision) if decision in DECISION_ORDER else len(DECISION_ORDER)
    return (order, decision)


def _plural(n: int) -> str:
    return f"{n} parcel" if n == 1 else f"{n} parcels"


def digest(
    changes: list[dict[str, Any]], base_url: str, farm_id: int
) -> tuple[str, list[tuple[str, str]]]:
    """Message text and "Open card" URL buttons for one farm's latest change per parcel."""
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["new"]] = counts.get(c["new"], 0) + 1
    summary = ", ".join(f"{_plural(counts[d])} → {d}" for d in sorted(counts, key=_decision_key))
    # Priority first (from the stored advice; `advisor.priority`), then alias.
    action = sorted(
        (c for c in changes if c["new"] == ACTION),
        key=lambda c: (-(c.get("priority") or 0), _alias_key(c["alias"])),
    )
    lines = [summary]
    buttons = []
    as_buttons = button_ok(base_url)
    for c in action:
        label = f"{c['alias']} {c['name']}".strip()
        line = f"{label}: {c['headline']}" if c["headline"] else label
        url = base_url.rstrip("/") + with_farm(c["card_path"], farm_id)
        if as_buttons:
            buttons.append((f"Open card {c['alias']}", url))
        else:
            line += f"\n  {url}"
        lines.append(line)
    return "\n".join(lines), buttons


def button_ok(base_url: str) -> bool:
    """Telegram rejects inline URL buttons to local hosts (the laptop dev stack), so those
    card links go in the text instead."""
    host = (urlsplit(base_url).hostname or "").lower()
    return not (host in LOCAL_HOSTS or host.endswith((".localhost", ".local")))


def _change(
    event: Event, parcels: dict[int, Parcel], priorities: dict[int, int | None] | None = None
) -> dict[str, Any]:
    p = event.payload or {}
    parcel = parcels.get(event.res_id)
    return {
        "priority": (priorities or {}).get(p.get("advice_id")),
        "alias": p.get("alias") or (parcel.alias if parcel else ""),
        "name": p.get("name") or (parcel.name if parcel else ""),
        "new": str(p.get("new") or "none"),
        "headline": p.get("headline") or "",
        "card_path": p.get("card_path") or f"/advisor/parcels/{event.res_id}",
    }


def push_advice(ctx: JobContext) -> None:
    # The token gates the job even when a test injected a sender.
    if ctx.settings.telegram_bot_token is None:
        return
    farm_ids = ctx.session.scalars(
        select(Farm.id).where(Farm.id.in_(ctx.farm_ids))
        if ctx.farm_ids is not None
        else select(Farm.id).order_by(Farm.id)
    ).all()
    with _run_sender(ctx.settings) as sender:
        for farm_id in farm_ids:
            try:
                outgoing = _advance_farm(ctx, farm_id)
                ctx.session.commit()  # cursor stored, row lock released, before any send
            except Exception as exc:
                ctx.session.rollback()
                # The type only: messages may quote data rows.
                logger.error("push for farm %s failed (%s)", farm_id, type(exc).__name__)
                continue
            if outgoing is not None:
                _deliver(sender, *outgoing)


def _advance_farm(
    ctx: JobContext, farm_id: int
) -> tuple[list[int], str, list[tuple[str, str]]] | None:
    """Move the farm's cursor past its new events; the digest and its chats, if any."""
    # Row lock: `/start` (bot process) writes the same props.
    farm = ctx.session.get(Farm, farm_id, with_for_update=True, populate_existing=True)
    assert farm is not None
    props = chat_props(farm)
    after = int(props.get(CURSOR_KEY) or 0)
    repo = repo_for_job(ctx.session, farm_id, JOB_NAME)
    events = repo.list(
        Event, Event.kind == KIND, Event.id > after, scope="advice:read", order_by=Event.id
    )
    if not events:
        return None
    set_chat_props(farm, **{CURSOR_KEY: events[-1].id})
    latest: dict[int, Event] = {}
    for event in events:
        if event.res_model == "parcel" and event.occurred_at <= ctx.now:
            latest[event.res_id] = event
    chats = [int(c) for c in props.get(CHATS_KEY) or []]
    if not latest or not chats:
        return None
    parcels = {p.id: p for p in repo.list(Parcel, Parcel.id.in_(latest), scope="fields:read")}
    advice_ids = [(e.payload or {}).get("advice_id") for e in latest.values()]
    rows = repo.list(
        Advice, Advice.id.in_([i for i in advice_ids if i is not None]), scope="advice:read"
    )
    priorities = {a.id: getattr(priority.of_advice(a), "level", None) for a in rows}
    changes = [_change(e, parcels, priorities) for e in latest.values()]
    text, buttons = digest(changes, ctx.settings.public_base_url, repo.farm_id)
    return chats, text, buttons


def _deliver(
    sender: TelegramSender, chats: list[int], text: str, buttons: list[tuple[str, str]]
) -> None:
    for chat_id in chats:
        try:
            sender.send(chat_id, text, url_buttons=buttons)
        except TelegramSendError as exc:
            # Chat id and type only, never the digest text.
            logger.warning("push to chat %s failed (%s)", chat_id, type(exc).__name__)
