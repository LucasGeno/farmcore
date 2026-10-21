"""Chat handlers: a message becomes a pending spray log; a button confirms or cancels it.

Transport-free: `on_message(deps, Incoming) -> Outgoing`, `on_callback(deps, Callback) ->
Outgoing`. The bot runtime maps Telegram updates to these DTOs and sends the replies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from farmcore.apps.chat import assistant, conversation, push, service
from farmcore.apps.chat.assistant import fallback, router
from farmcore.apps.chat.audit import DbAuditSink, audit_ref
from farmcore.apps.chat.dto import Callback, Incoming, Outgoing
from farmcore.apps.chat.extractor import Extractor, RegexExtractor, get_extractor
from farmcore.apps.chat.extractor.base import NOTE_TIME_CLAMPED, NOTE_TIME_MISSING
from farmcore.apps.operations import corrections, spray_weather
from farmcore.apps.operations.checks import summary as checks_summary
from farmcore.apps.operations.register import preview_checks, register_rows
from farmcore.core import jobs, logs
from farmcore.core.access import (
    FarmNotSeeded,
    ScopedRepository,
    default_farm,
    ensure_owner_grant,
)
from farmcore.core.clock import Clock, get_clock
from farmcore.core.logs import QuantityInput, SourceInput
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import DataGrant, Event, Farm, Log, LogSource
from farmcore.core.urls import with_farm
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

SOURCE_APP = "chat"
PURPOSE = "chat"
ADVISOR_JOB = "advisor.evaluate"
CONFIRM = "confirm"
CANCEL = "cancel"
# The signed-in farmer in the web assistant panel: the same draft → ✅ path as a Telegram chat,
# under its own key. Telegram never uses chat id 0.
WEB_CHAT_ID = 0
WEB_ACTOR = "web:farmer"

REPLY_EMPTY = (
    "Tell me what you sprayed: the parcel, the product, the dose and when. "
    'For example: "P1 copper, 2 kg, this morning".'
)
REPLY_CANCELLED = "❌ Cancelled. Nothing was logged."
REPLY_ALREADY_LOGGED = "This spray is already logged."
REPLY_GONE = "This spray was cancelled or no longer exists."
REPLY_UNKNOWN_BUTTON = "I did not understand that button."
REPLY_NO_FARM = "The farm is not set up yet. Run `farmcore seed` first."
REPLY_DEMO_CHANGED = "The demo changed while I was reading. Please send this spray again."
REPLY_DATED_LATER = (
    "This spray is dated after the current time (the clock was moved back), "
    "so nothing was logged. Please send it again."
)

# After ✅: where the farmer sees the result (`public_base_url` + path).
CONFIRM_LINKS: tuple[tuple[str, str], ...] = (
    ("Open map", "/fields/"),
    ("Open register", "/operations/register"),
)

# The extractor's notes in plain words for the farmer; the raw notes stay in raw_payload.
NOTE_FALLBACK = "fallback:"  # 06's FallbackExtractor: "fallback: <adapter> (<reason>)"
PLAIN_NOTES = {
    NOTE_TIME_CLAMPED: "The time you said is later than now, so I used now.",
    NOTE_TIME_MISSING: "No time was said, so I used now.",
}
PLAIN_FALLBACK = (
    "The main reader was not available, so I used the simple one. Please check the details."
)


@dataclass(frozen=True)
class CaptureBinding:
    """Server-derived web authority; never accept this object from request payloads."""

    farm_id: int
    principal: object | None = None
    operator: bool = False
    legacy_operator_web: bool = False

    @property
    def actor_key(self):
        return "operator" if self.operator else f"identity:{self.principal.identity_id}"

    @property
    def prefix(self):
        return f"web:farm:{self.farm_id}:{self.actor_key}:"

    @property
    def prefixes(self):
        if self.operator and self.legacy_operator_web:
            return (self.prefix, "web:farmer:")
        return (self.prefix,)

    @property
    def state_key(self):
        return f"web:farm:{self.farm_id}:{self.actor_key}"


@dataclass(frozen=True)
class ChatDeps:
    session_factory: sessionmaker[Session]
    extractor: Extractor
    settings: Settings
    manifests: Sequence[Manifest] = ()
    # The shared clock, read through a session (replay state lives in the database).
    clock: Callable[[Session, Settings], Clock] = get_clock
    # Saved spray weather: the composed station reader, None when Weather is not installed.
    weather_reader: spray_weather.Reader | None = None
    capture_binding: CaptureBinding | None = None


def _actor(chat_id: int, deps=None) -> str:
    if deps is not None and deps.capture_binding is not None:
        return deps.capture_binding.actor_key
    return WEB_ACTOR if chat_id == WEB_CHAT_ID else f"chat:{chat_id}"


def _plain_note(note: str) -> str:
    if note.startswith(NOTE_FALLBACK):
        return PLAIN_FALLBACK
    return PLAIN_NOTES.get(note, note)


def _chat_key(chat_id: int, deps=None) -> str:
    """Prefix of every `log_source.external_key` this chat writes (`tg:<chat>:<message>`; the
    web assistant writes `web:farmer:<message>`)."""
    if deps is not None and deps.capture_binding is not None:
        return deps.capture_binding.prefix
    return "web:farmer:" if chat_id == WEB_CHAT_ID else f"tg:{chat_id}:"


def _state_key(deps, chat_id):
    return deps.capture_binding.state_key if deps.capture_binding else chat_id


def _from_chat(log: Log, chat_id: int, deps=None) -> bool:
    binding = deps.capture_binding if deps is not None else None
    prefixes = binding.prefixes if binding is not None else (_chat_key(chat_id),)
    return any(s.external_key.startswith(prefixes) for s in log.sources)


def _echo(summary: str, log_id: int, notes: Sequence[str] = (), *, details="") -> Outgoing:
    text = f"{summary} — confirm?"
    if notes:
        text += "\nNote: " + " ".join(dict.fromkeys(_plain_note(n) for n in notes))
    if details:
        text += "\n" + details
    return Outgoing(
        text=text,
        buttons=[("✅ Confirm", f"{CONFIRM}:{log_id}"), ("❌ Cancel", f"{CANCEL}:{log_id}")],
    )


def _preview(repo, log, now, tz):
    lines = [f"{q.label}: {q.value.normalize():f} {q.unit}" for q in log.quantities]
    try:
        checks = preview_checks(repo, log, now, tz=tz)
        lines.append(
            "Checks: " + checks_summary(checks)
            if checks
            else "Checks unavailable; review the register."
        )
    except Exception:
        logger.warning("draft label checks unavailable")
        lines.append("Checks unavailable; review the register.")
    lines.append("Preview only; checks run again after confirmation.")
    return "\n".join(lines)


def _repo(session: Session, deps: ChatDeps, chat_id: int) -> ScopedRepository:
    if deps.capture_binding is not None:
        binding = deps.capture_binding
        logs.lock_farm(session, binding.farm_id)
        farm = session.scalar(select(Farm).where(Farm.id == binding.farm_id).with_for_update())
        if farm is None:
            from fastapi import HTTPException

            raise HTTPException(403, "Farm access denied.")
        repo = ScopedRepository(
            session,
            farm.id,
            ensure_owner_grant(session, farm),
            binding.actor_key,
            PURPOSE,
            private_member=not binding.operator,
        )
        if not binding.operator:
            corrections.member_authority(repo, binding.principal)
        return repo
    farm = default_farm(session, deps.settings)
    return ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), _actor(chat_id), PURPOSE
    )


def build_deps(
    settings: Settings,
    session_factory: sessionmaker[Session],
    manifests: Sequence[Manifest],
    *,
    weather_reader: spray_weather.Reader | None = None,
) -> ChatDeps:
    """Runtime wiring (bot process): the configured extractor, auditing every model
    call to `llm_audit`."""
    extractor = get_extractor(settings, DbAuditSink(session_factory))
    return ChatDeps(
        session_factory, extractor, settings, tuple(manifests), weather_reader=weather_reader
    )


def on_message(deps: ChatDeps, incoming: Incoming) -> Outgoing:
    """Draft a spray from a voice transcript or a text, check it, and keep it as a pending log
    echoed with ✅ / ❌. A draft that cannot be logged gets a clarifying reply and no log."""
    try:
        return _on_message(deps, incoming)
    except FarmNotSeeded:
        return Outgoing(text=REPLY_NO_FARM)


def _names_the_farm(transcript: str, ctx: service.FarmContext) -> bool:
    """Whether the message names a parcel (alias or name) or a product (name or substance)."""
    phrases = [p.alias for p in ctx.parcels] + [p.name for p in ctx.parcels]
    phrases += [p.name for p in ctx.products] + [p.active_substance for p in ctx.products]
    return router.mentions(transcript, phrases)


def _on_message(deps: ChatDeps, incoming: Incoming) -> Outgoing:
    is_voice = incoming.transcript is not None
    transcript = ((incoming.transcript if is_voice else incoming.text) or "").strip()
    if not transcript:
        return Outgoing(text=REPLY_EMPTY)
    if transcript.strip().casefold() == "/confirm":
        with session_scope(deps.session_factory) as session:
            repo = _repo(session, deps, incoming.chat_id)
            pending = [
                log
                for log in repo.list(Log, Log.status == "pending", scope="logs:write")
                if _from_chat(log, incoming.chat_id, deps)
            ]
            if len(pending) != 1:
                return Outgoing(
                    text=(
                        "No single draft to confirm. Use its Confirm button, "
                        "or /cancel and start again."
                    )
                )
            log_id = pending[0].id
        return on_callback(deps, Callback(incoming.chat_id, f"{CONFIRM}:{log_id}"))
    if transcript.casefold().strip() in {"/cancel", "cancel", "start over"}:
        with session_scope(deps.session_factory) as session:
            repo = _repo(session, deps, incoming.chat_id)
            now = deps.clock(session, deps.settings).now()
            conversation.clear(session, repo.farm_id, _state_key(deps, incoming.chat_id))
            pending = repo.list(Log, Log.status == "pending", scope="logs:write")
            for log in pending:
                if _from_chat(log, incoming.chat_id, deps):
                    logs.cancel(
                        session, log.id, _actor(incoming.chat_id, deps), now, source_app=SOURCE_APP
                    )
        return Outgoing(text=REPLY_CANCELLED)
    intent = router.intent(transcript)
    if intent == router.QUESTION:  # read-only tools; logs stay below
        return assistant.answer(deps, incoming.chat_id, transcript)
    ref = f"{_chat_key(incoming.chat_id, deps)}{incoming.message_id}"
    # Read, then close the session before the model call: no transaction waits on the network.
    with session_scope(deps.session_factory) as session:
        repo = _repo(session, deps, incoming.chat_id)
        # Telegram re-delivers a message it thinks went unanswered: answer for the same log.
        [seen, *_] = repo.list(
            LogSource, LogSource.external_key == ref, scope="logs:write", limit=1
        ) or [None]
        if seen is not None:
            if seen.log.status != "pending":
                return Outgoing(text=REPLY_ALREADY_LOGGED)
            tz = ZoneInfo(deps.settings.display_tz)
            return _echo(
                service.log_summary(seen.log, tz),
                seen.log_id,
                details=_preview(repo, seen.log, deps.clock(session, deps.settings).now(), str(tz)),
            )
        now = deps.clock(session, deps.settings).now()
        ctx = service.load_context(repo, now, deps.settings.display_tz)
        state = conversation.load(session, repo.farm_id, _state_key(deps, incoming.chat_id), now)
        epoch = conversation.epoch(session)
        farm_id = repo.farm_id
        local_only = bool((session.get(Farm, farm_id).props or {}).get("no_model"))

    if intent == router.OTHER and not state and not _names_the_farm(transcript, ctx):
        # "hello", "thanks": no spray to draft, so no extractor call and no "Which parcel?".
        lang = router.language(transcript)
        return Outgoing(text=fallback.REPLY_HELP.get(lang, fallback.REPLY_HELP["en"]))
    request = ctx.request(transcript[: conversation.MAX_CHARS])
    history = list(state["history"]) if state else []
    if state:
        request = request.model_copy(
            update={"clarification": {"draft": state["draft"], "history": history}}
        )
    with audit_ref(ref):
        extractor = RegexExtractor() if local_only else deps.extractor
        draft = conversation.continue_draft(extractor.extract(request), state)
    try:
        spray = service.check(draft, ctx)
    except service.Unclear as unclear:
        history += [
            {"role": "user", "content": request.transcript},
            {"role": "assistant", "content": unclear.reply},
        ]
        with session_scope(deps.session_factory) as session:
            if not conversation.unchanged(session, farm_id, epoch):
                return Outgoing(text=REPLY_DEMO_CHANGED)
            _repo(session, deps, incoming.chat_id)
            conversation.save(
                session, farm_id, _state_key(deps, incoming.chat_id), now, draft, history
            )
        return Outgoing(text=unclear.reply)

    with session_scope(deps.session_factory) as session:
        if not conversation.unchanged(session, farm_id, epoch):
            return Outgoing(text=REPLY_DEMO_CHANGED)
        repo = _repo(session, deps, incoming.chat_id)
        log = logs.create_pending(
            session,
            farm_id=repo.farm_id,
            type="spray",
            parcel_ids=[p.id for p in spray.parcels],
            ts_start=spray.ts_start,
            ts_end=None,
            quantities=[
                QuantityInput(
                    measure="rate",
                    value=Decimal(str(value)),
                    unit=unit,
                    product_id=p.id,
                    label=p.name,
                )
                for p, value, unit in spray.product_doses
            ],
            source=SourceInput(
                source_type="voice",
                external_key=ref,
                raw_payload={
                    "transcript": transcript,
                    # log_source.source_type has no `text` value (core_0002), so a typed
                    # message is a `voice` source marked here.
                    "input": "voice" if is_voice else "text",
                    "adapter": draft.adapter,
                    "notes": spray.notes,
                },
                confidence=draft.confidence,
                precision_rank=logs.RANK_VOICE,
            ),
            actor=_actor(incoming.chat_id, deps),
            occurred_at=now,
            source_app=SOURCE_APP,
        )
        log_id = log.id
        details = _preview(repo, log, now, deps.settings.display_tz)
        conversation.clear(session, repo.farm_id, _state_key(deps, incoming.chat_id))

    return _echo(service.summary(spray, ctx.tz), log_id, spray.notes, details=details)


def _later_merge_target(session: Session, pending: Log, now: datetime) -> Log | None:
    """The done log `logs.confirm` would merge `pending` into, when it starts after `now`.

    TODO(07): a `not_after` bound on `logs.confirm` would let this draft log on its own
    instead of waiting; until then confirming is refused rather than merged into the future.
    """
    target = logs._merge_target(
        session,
        farm_id=pending.farm_id,
        type=pending.type,
        parcel_ids=[p.id for p in pending.parcels],
        ts_start=pending.ts_start,
        exclude_id=pending.id,
    )
    return target if target is not None and target.ts_start > now else None


def _parcel_names(log: Log) -> str:
    return ", ".join(f"{p.alias} {p.name}" for p in sorted(log.parcels, key=lambda p: p.id))


def on_callback(deps: ChatDeps, callback: Callback) -> Outgoing:
    """`confirm:<id>` makes the pending log done (or merges it), commits, then re-runs
    `advisor.evaluate` so the advice reflects it at once; `cancel:<id>` discards it."""
    try:
        return _on_callback(deps, callback)
    except FarmNotSeeded:
        return Outgoing(text=REPLY_NO_FARM)


def _on_callback(deps: ChatDeps, callback: Callback) -> Outgoing:
    action, _, raw_id = callback.data.partition(":")
    if action not in (CONFIRM, CANCEL) or not raw_id.isdigit():
        return Outgoing(text=REPLY_UNKNOWN_BUTTON)
    log_id = int(raw_id)
    if not 1 <= log_id < 2**63:
        return Outgoing(text=REPLY_GONE)
    tz = ZoneInfo(deps.settings.display_tz)
    actor = _actor(callback.chat_id, deps)
    with session_scope(deps.session_factory) as session:
        repo = _repo(session, deps, callback.chat_id)
        now = deps.clock(session, deps.settings).now()
        log = repo.get(Log, log_id, scope="logs:write")  # farm-scoped: another farm's is None
        # Log ids are sequential: only the chat that sent the message may act on its draft.
        if log is None or not _from_chat(log, callback.chat_id, deps):
            return Outgoing(text=REPLY_GONE)
        if log.status != "pending":
            return Outgoing(text=REPLY_ALREADY_LOGGED)
        try:
            if action == CANCEL:
                logs.cancel(session, log_id, actor, now, source_app=SOURCE_APP)
                return Outgoing(text=REPLY_CANCELLED)
            # Replay safety (logs.py leaves clock bounds to callers): the clock may have been
            # stepped back since the echo, past the draft or past a spray it would merge into.
            if log.ts_start > now:
                logs.cancel(session, log_id, actor, now, source_app=SOURCE_APP)
                return Outgoing(text=REPLY_DATED_LATER)
            later = _later_merge_target(session, log, now)
            # Saved weather belongs to this draft's own receipt: its time and parcels, before a
            # merge moves the source onto a log with another time and parcel union.
            # A draft with no stated time got "now" (NOTE_TIME_MISSING): its weather time is
            # unknown, never the replay hour.
            receipts = [
                spray_weather.Receipt(
                    s.id,
                    None
                    if NOTE_TIME_MISSING in (s.raw_payload or {}).get("notes", [])
                    else log.ts_start,
                    tuple(p.id for p in log.parcels),
                )
                for s in log.sources
            ]
            if later is not None:
                return Outgoing(
                    text=(
                        f"A spray on {_parcel_names(later)} is logged at "
                        f"{later.ts_start.astimezone(tz):%d %b %H:%M}, later than the current "
                        "time (the clock was moved back). Move the clock forward to confirm "
                        "this one, or cancel it."
                    ),
                    buttons=[("❌ Cancel", f"{CANCEL}:{log_id}")],
                )
            log, merged = logs.confirm(session, log_id, actor, now, source_app=SOURCE_APP)
        except LookupError:  # a concurrent cancel won the row lock
            return Outgoing(text=REPLY_GONE)
        except ValueError:  # a concurrent confirm won the row lock
            return Outgoing(text=REPLY_ALREADY_LOGGED)
        # Outside the race handlers, before this transaction commits: a capture error rolls the
        # confirm back instead of reading as "already logged". Only receipts that survived.
        kept = {s.id for s in log.sources}
        spray_weather.capture(
            repo,
            [r for r in receipts if r.source_id in kept],
            reader=deps.weather_reader,
            data_now=now,
            captured_at=datetime.now(UTC),
        )
        lead = "✅ Added to the spray already logged" if merged else "✅ Logged"
        text = f"{lead}: {service.log_summary(log, tz)}."
        if not merged:
            text += _duplicate_note(repo, log, tz)
        saved_id = log.id
        farm_id = repo.farm_id
        grant_id = repo.grant.id
    # Presentation is best effort, after the save committed, in a separate transaction.
    try:
        with session_scope(deps.session_factory) as session:
            repo = ScopedRepository(
                session, farm_id, session.get(DataGrant, grant_id), actor, PURPOSE
            )
            rows = register_rows(repo, now, tz=deps.settings.display_tz, checks=True)
            saved_row = next((row for row in rows if row["log_id"] == saved_id), None)
            check_line = checks_summary(saved_row["checks"]) if saved_row else ""
    except Exception as exc:
        logger.warning("saved spray checks unavailable (%s)", type(exc).__name__)
        text += "\nChecks unavailable; review the register."
    else:
        text += (
            f"\nChecks: {check_line}"
            if check_line
            else "\nChecks unavailable; review the register."
        )
    # Committed above: the job opens its own session and must see the done log.
    run = jobs.run_now(
        deps.session_factory, deps.manifests, ADVISOR_JOB, deps.settings, farm_ids=(farm_id,)
    )
    if run is not None:
        text += " Advice updated." if run.ok else " Advice will update at the next step."
    return _with_links(text, deps.settings.public_base_url, farm_id)


def _duplicate_note(repo: ScopedRepository, log: Log, tz: ZoneInfo) -> str:
    events = repo.list(
        Event,
        Event.kind == "log.confirmed",
        Event.payload["log_id"].as_integer() == log.id,
        scope="logs:read",
        order_by=Event.id.desc(),
        limit=1,
    )
    if not events or not (target_id := events[0].payload.get("duplicate_of")):
        return ""
    target = repo.get(Log, target_id, scope="logs:read")
    if target is None:
        return ""
    dropped = events[0].payload["dropped_products"]
    names = list(
        dict.fromkeys(
            q.product.active_substance if q.product else (q.label or "product")
            for q in target.quantities
            if (q.product_id if q.product_id is not None else q.label) in dropped
        )
    )
    if not names:
        return ""
    return f"\n{' + '.join(names)} was already logged at {target.ts_start.astimezone(tz):%H:%M}."


def _with_links(text: str, base_url: str, farm_id: int) -> Outgoing:
    """The confirm reply with "Open map" / "Open register" URL buttons; on a local base URL
    (Telegram rejects such buttons, see `push`) the links go in the text instead."""
    links = [
        (label, base_url.rstrip("/") + with_farm(path, farm_id)) for label, path in CONFIRM_LINKS
    ]
    if push.button_ok(base_url):
        return Outgoing(text=text, url_buttons=links)
    return Outgoing(text="\n".join([text, *(f"{label}: {url}" for label, url in links)]))
