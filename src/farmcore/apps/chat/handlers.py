"""Chat handlers: a message becomes a draft spray, irrigation or observation (`core/drafts.py`,
actor `chat:<id>`, or the signed-in member in the web panel); a button confirms or cancels it
through the same draft service as `/drafts/<id>`. No pending log is ever committed, and an
unfinished capture is the open draft that names what it still misses.

Transport-free: `on_message(deps, Incoming) -> Outgoing`, `on_callback(deps, Callback) ->
Outgoing`. The bot runtime maps Telegram updates to these DTOs and sends the replies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from farmcore.apps.chat import assistant, conversation, push, service
from farmcore.apps.chat.assistant import fallback, router
from farmcore.apps.chat.audit import DbAuditSink, audit_ref
from farmcore.apps.chat.dto import Callback, Incoming, Outgoing
from farmcore.apps.chat.extractor import Extractor, RegexExtractor, get_extractor
from farmcore.apps.chat.extractor.base import NOTE_TIME_CLAMPED
from farmcore.apps.operations import corrections, facts, spray_items, spray_weather
from farmcore.apps.operations import drafts as ops_drafts
from farmcore.apps.operations.checks import summary as checks_summary
from farmcore.apps.operations.manifest import MANIFEST as OPERATIONS
from farmcore.apps.operations.register import preview_checks, register_rows
from farmcore.core import drafts, jobs, logs
from farmcore.core.access import (
    FarmNotSeeded,
    ScopedRepository,
    default_farm,
    ensure_owner_grant,
)
from farmcore.core.clock import Clock, get_clock
from farmcore.core.farm_location import time_zone_of
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import AccessLog, DataGrant, Event, Farm, Log
from farmcore.core.models.drafts import Draft
from farmcore.core.urls import with_farm
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

SOURCE_APP = "chat"
PURPOSE = "chat"
ADVISOR_JOB = "advisor.evaluate"
# Live mode: how long a confirm waits for the worker's new evaluation before answering anyway.
ADVICE_WAIT_SECONDS = 3.0
CONFIRM = "confirm"
CANCEL = "cancel"
# The signed-in farmer in the web assistant panel: the same draft → ✅ path as a Telegram chat,
# under its own key. Telegram never uses chat id 0.
WEB_CHAT_ID = 0
WEB_ACTOR = "web:farmer"

REPLY_EMPTY = (
    "Tell me what you sprayed, irrigated or observed: the parcel, what happened and when. "
    'For example: "P1 copper, 2 kg, this morning", "P1 20 mm irrigation" '
    'or "P5 powdery mildew this morning".'
)
REPLY_CANCELLED = "❌ Cancelled. Nothing was logged."
REPLY_ALREADY_LOGGED = "This spray is already logged."
REPLY_ALREADY_RECORDED = "This record is already logged."
REPLY_GONE = "This spray was cancelled or no longer exists."
REPLY_GONE_RECORD = "This record was cancelled or no longer exists."
REPLY_KEY_CONFLICT = (
    "This message is already saved as a different record. Nothing was changed; "
    "send the new note as a new message."
)
REPLY_UNKNOWN_BUTTON = "I did not understand that button."
REPLY_NO_FARM = "The farm is not set up yet. Run `farmcore seed` first."
REPLY_DEMO_CHANGED = "The demo changed while I was reading. Please send this spray again."
REPLY_SEND_AGAIN = "Nothing was saved; please send it again."
REPLY_DATED_LATER = (
    "This spray is dated after the current time (the clock was moved back), "
    "so nothing was logged. Please send it again."
)
REPLY_DATED_LATER_RECORD = (
    "This record is dated after the current time (the clock was moved back), "
    "so nothing was logged. Please send it again."
)
NO_STOCK = {
    "irrigate": "No spray stock is used and no advice is calculated for irrigation.",
    "observation": "No spray stock is used and no advice is calculated for observations.",
}
NO_STOCK_CONFIRMED = "No spray stock was used and no advice was calculated."
FACT_LINKS: tuple[tuple[str, str], ...] = (("Open operations", "/operations/"),)

# After ✅: where the farmer sees the result (`public_base_url` + path).
CONFIRM_LINKS: tuple[tuple[str, str], ...] = (
    ("Open map", "/fields/"),
    ("Open register", "/operations/register"),
)

# The extractor's operation type -> the Records app's draft kind (`operations.drafts`). Chat
# requires Records, so its kinds are always there.
KINDS = {"spray": "spray", "irrigate": "irrigation", "observation": "observation"}
DRAFT_MANIFESTS = (OPERATIONS,)

# The extractor's notes in plain words for the farmer; the raw notes stay in raw_payload.
NOTE_FALLBACK = "fallback:"  # 06's FallbackExtractor: "fallback: <adapter> (<reason>)"
PLAIN_NOTES = {
    NOTE_TIME_CLAMPED: "The time you said is later than now, so I used now.",
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

    @property
    def actor_key(self):
        """The draft actor: the same principal `/drafts/<id>` reviews as."""
        return "operator" if self.operator else f"identity:{self.principal.identity_id}"

    @property
    def prefix(self):
        return f"web:farm:{self.farm_id}:{self.actor_key}:"


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
    """Prefix of every draft request key this chat writes (`tg:<chat>:<message>`; the web
    assistant writes `web:farm:<farm>:<actor>:<message>`)."""
    if deps is not None and deps.capture_binding is not None:
        return deps.capture_binding.prefix
    return "web:farmer:" if chat_id == WEB_CHAT_ID else f"tg:{chat_id}:"


def _echo(summary: str, draft: Draft, notes: Sequence[str] = (), *, details="") -> Outgoing:
    text = f"{summary} — confirm?"
    if notes:
        text += "\nNote: " + " ".join(dict.fromkeys(_plain_note(n) for n in notes))
    if details:
        text += "\n" + details
    button = f"{draft.id}:{draft.revision}"
    return Outgoing(
        text=text,
        buttons=[("✅ Confirm", f"{CONFIRM}:{button}"), ("❌ Cancel", f"{CANCEL}:{button}")],
    )


def _preview(repo, log, now, tz):
    if log.type in NO_STOCK:
        return f"{NO_STOCK[log.type]}\nPreview only; nothing is recorded until you confirm."
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


def _render(repo: ScopedRepository, draft: Draft, now: datetime, tz: str) -> tuple[str, str]:
    """`(summary, preview)` of a complete draft, from its record staged in a savepoint that is
    rolled back: nothing of it is committed, but the preview's reads stay in `access_log`."""
    session = repo.session
    before = session.scalar(select(func.max(AccessLog.id))) or 0
    nested = session.begin_nested()
    try:
        log, _record, _held = ops_drafts.stage(session, draft, actor=draft.actor, now=now)
        summary = service.log_summary(log, ZoneInfo(tz))
        details = _preview(repo, log, now, tz)
        reads = [
            (a.actor, a.farm_id, a.grant_id, a.resource, a.scope, a.purpose)
            for a in session.scalars(select(AccessLog).where(AccessLog.id > before))
        ]
    finally:
        nested.rollback()
    for actor, farm_id, grant_id, resource, scope, purpose in reads:
        session.add(
            AccessLog(
                actor=actor,
                farm_id=farm_id,
                grant_id=grant_id,
                resource=resource,
                scope=scope,
                purpose=purpose,
            )
        )
    session.flush()
    return summary, details


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
    """Extract a spray, irrigation or observation from a voice transcript or a text and keep it
    as a draft: a complete one is echoed with ✅ / ❌, an incomplete one stays an open draft
    that names what is missing while the reply asks for it."""
    try:
        return _on_message(deps, incoming)
    except FarmNotSeeded:
        return Outgoing(text=REPLY_NO_FARM)


def _names_the_farm(transcript: str, ctx: service.FarmContext) -> bool:
    """Whether the message names a parcel (alias or name) or a product (name or substance)."""
    phrases = [p.alias for p in ctx.parcels] + [p.name for p in ctx.parcels]
    phrases += [p.name for p in ctx.products] + [p.active_substance for p in ctx.products]
    return router.mentions(transcript, phrases)


def names_a_parcel(transcript: str, ctx: service.FarmContext) -> bool:
    """Whether the message names one of the farm's parcels (alias or name)."""
    phrases = [p.alias for p in ctx.parcels] + [p.name for p in ctx.parcels]
    return router.mentions(transcript, phrases)


def _done_reply(kind: str) -> Outgoing:
    return Outgoing(text=REPLY_ALREADY_LOGGED if kind == "spray" else REPLY_ALREADY_RECORDED)


def _replayed(repo, draft: Draft, ref: str, transcript: str, now, tz) -> Outgoing:
    """The answer to a message this draft already took (Telegram re-delivers unanswered ones):
    never a second draft, and never a rewrite of the one the key holds."""
    capture = draft.payload.get("capture") or {}
    stored = (capture.get("messages") or {}).get(ref)
    if stored is not None and stored != transcript:
        return Outgoing(text=REPLY_KEY_CONFLICT, status=409)
    if draft.status == "confirmed":
        return _done_reply(draft.kind)
    problem = drafts.refusal(draft)
    if draft.status != "open" or problem is not None:
        return Outgoing(text=problem.reason if problem else REPLY_GONE_RECORD)
    if draft.payload.get("missing"):
        return Outgoing(text=capture.get("question") or REPLY_EMPTY)
    summary, details = _render(repo, draft, now, tz)
    return _echo(summary, draft, capture.get("notes") or (), details=details)


def _payload(checked, missing, capture) -> dict:
    """The draft payload: the checked record, or what is known plus the field still missing."""
    if checked is None:
        return {"missing": [missing], "capture": capture}
    record = {
        "parcel_ids": [p.id for p in checked.parcels],
        "ts_start": checked.ts_start.isoformat(),
        "capture": capture,
    }
    if isinstance(checked, service.CheckedSpray):
        record["doses"] = [
            {"product_id": product.id, "value": str(value), "unit": unit, "label": product.name}
            for product, value, unit in checked.product_doses
        ]
    elif isinstance(checked, service.CheckedIrrigation):
        fact = facts.IrrigationFact(
            amount=facts.amount_text(checked.amount_value),
            unit=checked.amount_unit,
            basis=checked.basis,
        )
        record["fact"] = fact.model_dump(exclude_none=True)
    else:
        record["fact"] = facts.ObservationFact(text=checked.text).model_dump()
    return record


def _on_message(deps: ChatDeps, incoming: Incoming) -> Outgoing:
    is_voice = incoming.transcript is not None
    transcript = ((incoming.transcript if is_voice else incoming.text) or "").strip()
    if not transcript:
        return Outgoing(text=REPLY_EMPTY)
    actor = _actor(incoming.chat_id, deps)
    if transcript.strip().casefold() == "/confirm":
        with session_scope(deps.session_factory) as session:
            repo = _repo(session, deps, incoming.chat_id)
            ready = [
                d
                for d in conversation.open_captures(session, repo.farm_id, actor)
                if not d.payload.get("missing")
            ]
            if len(ready) != 1:
                return Outgoing(
                    text=(
                        "No single draft to confirm. Use its Confirm button, "
                        "or /cancel and start again."
                    )
                )
            data = f"{CONFIRM}:{ready[0].id}:{ready[0].revision}"
        return on_callback(deps, Callback(incoming.chat_id, data))
    if transcript.casefold().strip() in {"/cancel", "cancel", "start over"}:
        with session_scope(deps.session_factory) as session:
            repo = _repo(session, deps, incoming.chat_id)
            for draft in conversation.open_captures(session, repo.farm_id, actor):
                drafts.cancel(session, farm_id=repo.farm_id, draft_id=draft.id, actor=actor)
        return Outgoing(text=REPLY_CANCELLED)
    intent = router.intent(transcript)
    if intent == router.QUESTION:  # read-only tools; logs stay below
        return assistant.answer(deps, incoming.chat_id, transcript)
    ref = f"{_chat_key(incoming.chat_id, deps)}{incoming.message_id}"
    # Read, then close the session before the model call: no transaction waits on the network.
    with session_scope(deps.session_factory) as session:
        repo = _repo(session, deps, incoming.chat_id)
        now = deps.clock(session, deps.settings).now()
        tz = time_zone_of(repo.session, repo.farm_id, deps.settings)
        seen = conversation.held(session, repo.farm_id, actor, ref)
        if seen is not None:
            return _replayed(repo, seen, ref, transcript, now, tz)
        ctx = service.load_context(repo, now, tz)
        open_draft = conversation.load(session, repo.farm_id, actor)
        state = conversation.state(open_draft)
        previous = (
            (open_draft.id, open_draft.kind, open_draft.revision, open_draft.payload["capture"])
            if open_draft is not None
            else None
        )
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
        op = conversation.continue_draft(extractor.extract(request), state)
    checked, missing, question = None, None, None
    try:
        checked = service.check(op, ctx)
    except service.Unclear as unclear:
        question, missing = unclear.reply, unclear.missing
        history += [
            {"role": "user", "content": request.transcript},
            {"role": "assistant", "content": unclear.reply},
        ]
    kind = KINDS.get(op.type)
    if kind is None or (checked is None and missing is None):
        return Outgoing(text=question or REPLY_EMPTY)
    messages = dict(previous[3]["messages"]) if previous else {}
    messages[ref] = transcript
    capture = {
        "input": "voice" if is_voice else "text",
        "transcript": request.transcript,
        "adapter": op.adapter[:200],
        "confidence": min(max(op.confidence, 0.0), 1.0),
        "notes": list(checked.notes if checked is not None else op.notes)[:20],
        "messages": dict(list(messages.items())[-20:]),
        "operation": op.model_dump(mode="json"),
        "history": history[-conversation.MAX_TURNS :],
        "question": question,
        "epoch": epoch,
    }
    payload = _payload(checked, missing, capture)

    with session_scope(deps.session_factory) as session:
        if not conversation.unchanged(session, farm_id, epoch):
            return Outgoing(text=REPLY_DEMO_CHANGED)
        repo = _repo(session, deps, incoming.chat_id)
        try:
            draft, created = _save(session, farm_id, actor, ref, kind, payload, previous)
        except drafts.DraftConflict:
            return Outgoing(text=REPLY_KEY_CONFLICT, status=409)
        except drafts.DraftError as exc:
            session.rollback()
            return Outgoing(text=f"{exc.reason}\n{REPLY_SEND_AGAIN}")
        if not created:  # a concurrent duplicate of this message already holds the draft
            return _replayed(repo, draft, ref, transcript, now, tz)
        if checked is None:
            return Outgoing(text=question)
        summary = service.checked_summary(checked, ctx.tz)
        _summary, details = _render(repo, draft, now, tz)
        return _echo(summary, draft, capture["notes"], details=details)


def _save(session, farm_id, actor, ref, kind, payload, previous) -> tuple[Draft, bool]:
    """Revise the clarification this message answers, or propose a new draft (cancelling a
    clarification whose record type the message changed); `(draft, written now)`."""
    if previous is not None:
        draft_id, previous_kind, revision, _capture = previous
        if previous_kind == kind:
            draft = drafts.revise(
                session,
                DRAFT_MANIFESTS,
                farm_id=farm_id,
                draft_id=draft_id,
                actor=actor,
                payload=payload,
                revision=revision,
                ttl=conversation.TTL,
            )
            return draft, True
        drafts.cancel(session, farm_id=farm_id, draft_id=draft_id, actor=actor)
    draft, created = drafts.propose(
        session,
        DRAFT_MANIFESTS,
        farm_id=farm_id,
        actor=actor,
        kind=kind,
        payload=payload,
        provenance={"source_type": "voice"},
        request_key=ref,
        ttl=conversation.TTL,
    )
    return draft, created


def _later_merge_target(session: Session, draft: Draft, now: datetime) -> Log | None:
    """The done spray a confirm would merge this draft into, when it starts after `now`.

    TODO(07): a `not_after` bound on `logs.confirm` would let this draft log on its own
    instead of waiting; until then confirming is refused rather than merged into the future.
    """
    target = logs._merge_target(
        session,
        farm_id=draft.farm_id,
        type="spray",
        parcel_ids=draft.payload["parcel_ids"],
        ts_start=datetime.fromisoformat(draft.payload["ts_start"]),
    )
    return target if target is not None and target.ts_start > now else None


def _parcel_names(log: Log) -> str:
    return ", ".join(f"{p.alias} {p.name}" for p in sorted(log.parcels, key=lambda p: p.id))


def on_callback(deps: ChatDeps, callback: Callback) -> Outgoing:
    """`confirm:<draft>:<revision>` records the reviewed revision through `drafts.confirm` (the
    same service as `/drafts/<id>`), commits, then re-runs `advisor.evaluate` for a spray so the
    advice reflects it at once; an irrigation or observation is confirmed without advice or
    stock work. `cancel:<draft>:<revision>` discards it."""
    try:
        return _on_callback(deps, callback)
    except FarmNotSeeded:
        return Outgoing(text=REPLY_NO_FARM)


def _migrated_draft(session: Session, farm_id: int, actor: str, log_id: int) -> int | None:
    """The draft the upgrade made from pending log `log_id` for this actor (`core_0011`)."""
    return session.scalar(
        select(Draft.id).where(
            Draft.farm_id == farm_id,
            Draft.actor == actor,
            Draft.provenance["details"]["migrated_from_log"].as_integer() == log_id,
        )
    )


def _on_callback(deps: ChatDeps, callback: Callback) -> Outgoing:
    action, _, rest = callback.data.partition(":")
    raw_id, _, raw_revision = rest.partition(":")
    # A button names the draft and the revision the farmer saw. A button sent before the
    # upgrade to drafts (`confirm:<log id>`) names the pending log its draft was migrated from.
    legacy = raw_revision == ""
    if action not in (CONFIRM, CANCEL) or not raw_id.isdigit():
        return Outgoing(text=REPLY_UNKNOWN_BUTTON)
    if not legacy and not raw_revision.isdigit():
        return Outgoing(text=REPLY_UNKNOWN_BUTTON)
    draft_id, revision = int(raw_id), 1 if legacy else int(raw_revision)
    if not 1 <= draft_id < 2**63 or not 1 <= revision < 2**31:
        return Outgoing(text=REPLY_GONE)
    actor = _actor(callback.chat_id, deps)
    with session_scope(deps.session_factory) as session:
        repo = _repo(session, deps, callback.chat_id)
        tz = ZoneInfo(time_zone_of(repo.session, repo.farm_id, deps.settings))
        now = deps.clock(session, deps.settings).now()
        if legacy:
            draft_id = _migrated_draft(session, repo.farm_id, actor, draft_id)
            if draft_id is None:
                return Outgoing(text=REPLY_GONE)
        try:  # only the chat (or member) that proposed it, on its farm, sees a draft
            draft = drafts.get(session, farm_id=repo.farm_id, draft_id=draft_id, actor=actor)
        except drafts.DraftNotFound:
            return Outgoing(text=REPLY_GONE)
        is_spray = draft.kind == "spray"
        if draft.status == "confirmed":
            return _done_reply(draft.kind)
        if action == CANCEL:
            try:
                drafts.cancel(session, farm_id=repo.farm_id, draft_id=draft_id, actor=actor)
            except drafts.DraftError as exc:
                return Outgoing(text=exc.reason)
            return Outgoing(text=REPLY_CANCELLED)
        if draft.status == "cancelled":
            return Outgoing(text=REPLY_GONE if is_spray else REPLY_GONE_RECORD)
        problem = drafts.refusal(draft, revision)  # expired, or changed since this button
        if problem is not None:
            return Outgoing(text=problem.reason)
        if draft.payload.get("missing"):
            return Outgoing(text=(draft.payload.get("capture") or {}).get("question") or "")
        # Replay safety (logs.py leaves clock bounds to callers): the clock may have been
        # stepped back since the echo, past the draft or past a spray it would merge into.
        if datetime.fromisoformat(draft.payload["ts_start"]) > now:
            try:  # a concurrent confirm or cancel may have closed it meanwhile
                drafts.cancel(session, farm_id=repo.farm_id, draft_id=draft_id, actor=actor)
            except drafts.DraftClosed:
                draft = drafts.get(session, farm_id=repo.farm_id, draft_id=draft_id, actor=actor)
                if draft.status == "confirmed":
                    return _done_reply(draft.kind)
            return Outgoing(text=REPLY_DATED_LATER if is_spray else REPLY_DATED_LATER_RECORD)
        later = _later_merge_target(session, draft, now) if is_spray else None
        if later is not None:
            return Outgoing(
                text=(
                    f"A spray on {_parcel_names(later)} is logged at "
                    f"{later.ts_start.astimezone(tz):%d %b %H:%M}, later than the current "
                    "time (the clock was moved back). Move the clock forward to confirm "
                    "this one, or cancel it."
                ),
                buttons=[("❌ Cancel", f"{CANCEL}:{draft_id}:{revision}")],
            )
        try:
            # A refused confirm rolls back to here: no pending log is ever committed.
            with session.begin_nested():
                draft, recorded = drafts.confirm(
                    session,
                    DRAFT_MANIFESTS,
                    farm_id=repo.farm_id,
                    draft_id=draft_id,
                    actor=actor,
                    revision=revision,
                    now=now,
                    services={ops_drafts.WEATHER_READER: deps.weather_reader},
                )
        except drafts.DraftError as exc:
            return Outgoing(text=exc.reason)
        if not recorded:  # a concurrent or retried confirm won: it recorded once
            return _done_reply(draft.kind)
        log = repo.get(Log, draft.result_id, scope="logs:write")
        merged = any(s.external_key != f"draft:{draft.id}" for s in log.sources)
        saved_id = log.id
        farm_id = repo.farm_id
        grant_id = repo.grant.id
        if is_spray:
            lead = "✅ Added to the spray already logged" if merged else "✅ Logged"
            text = f"{lead}: {service.log_summary(log, tz)}."
            if not merged:
                text += _duplicate_note(repo, log, tz)
        else:
            text = f"✅ Logged: {service.log_summary(log, tz)}.\n{NO_STOCK_CONFIRMED}"
    if not is_spray:
        return _with_links(text, deps.settings.public_base_url, farm_id, FACT_LINKS)
    # Presentation is best effort, after the save committed, in a separate transaction.
    try:
        with session_scope(deps.session_factory) as session:
            repo = ScopedRepository(
                session, farm_id, session.get(DataGrant, grant_id), actor, PURPOSE
            )
            rows = register_rows(
                repo,
                now,
                tz=time_zone_of(repo.session, repo.farm_id, deps.settings),
                checks=True,
            )
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
    # Committed above: the evaluation (here in replay, the worker's task in live) must see the log.
    run = jobs.run_now(
        deps.session_factory,
        deps.manifests,
        ADVISOR_JOB,
        deps.settings,
        farm_ids=(farm_id,),
        wait=ADVICE_WAIT_SECONDS,
    )
    if run is not None:
        if run.ok:
            text += " Advice updated."
        elif run.error == jobs.QUEUED:
            text += " Advice will update in a moment."
        else:
            text += " Advice will update at the next step."
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
            item.active_substance if item else (q.label or "product")
            for q, item in spray_items.named_doses_of_log(target)
            if (q.item_id if q.item_id is not None else q.label) in dropped
        )
    )
    if not names:
        return ""
    return f"\n{' + '.join(names)} was already logged at {target.ts_start.astimezone(tz):%H:%M}."


def _with_links(
    text: str, base_url: str, farm_id: int, links: Sequence[tuple[str, str]] = CONFIRM_LINKS
) -> Outgoing:
    """The confirm reply with "Open map" / "Open register" URL buttons; on a local base URL
    (Telegram rejects such buttons, see `push`) the links go in the text instead. A confirmed
    fact links to operations, where its amount and basis are shown."""
    links = [(label, base_url.rstrip("/") + with_farm(path, farm_id)) for label, path in links]
    if push.button_ok(base_url):
        return Outgoing(text=text, url_buttons=links)
    return Outgoing(text="\n".join([text, *(f"{label}: {url}" for label, url in links)]))
