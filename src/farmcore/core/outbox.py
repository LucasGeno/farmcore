"""The outbox: deliveries outside the installation, recorded with their cause, sent by the worker.

A job that wants to tell the outside world something (today the Telegram advice digest,
`chat.push_advice`) writes an intent with `add` in its own transaction: if that transaction rolls
back there is no intent, and once it commits the intent outlives any process. `dispatch` (run by
`farmcore worker` every tick, outside the step lock and any long transaction, and once by
`farmcore replay` after its step) delivers them through the providers that installed apps declare
(`Manifest.delivery_providers`: kind -> factory(settings), None when unconfigured).

Each send is claimed first, in a committed transaction of its own (`FOR UPDATE SKIP LOCKED`, so
competing dispatchers never take one intent twice), and only then sent. So no intent is sent twice
once its provider accepted it; the price is that a send whose outcome is unknown is never retried:
- accepted: `sent`, with the provider's receipt;
- refused, and the provider says so (`Undelivered`: an HTTP answer, a connection never made):
  `pending` again after `RETRY_SECONDS` (or the delay the provider asked for: `retry_after`),
  `failed` after `MAX_ATTEMPTS` refusals; a refusal that only asks to wait (Telegram's flood
  limit) is not counted, the intent's expiry bounds it;
- unknown (`OutcomeUnknown`, any other error, or a claim older than `CLAIM_SECONDS` whose worker
  died): `ambiguous`, listed by `ambiguous` and `farmcore outbox` until the operator settles it
  (`resolve`). Never reported as delivered, never resent on its own.
A pending intent past its expiry is marked `expired` and never sent.

Nothing is claimed while the installation's external effects are paused (a restored copy until
its owner runs `farmcore effects enable`), nor while the app that recorded it is switched off:
intents wait, and expire on time, so enabling sends the fresh ones and never a stale backlog.
`may_dispatch` is the admission check for each intent. A restore turns the unexpired intents it
inherits from the source, which the source may have sent, into `ambiguous`, and the expired ones
into `expired` (`HOLD_INHERITED`). A worker stopping sets `dispatch`'s `stop`: no claim is taken
after it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import Row, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core.manifest import Manifest, import_string
from farmcore.core.models.kernel import OutboxIntent
from farmcore.core.startup import EFFECTS_ENABLED, read_external_effects
from farmcore.db import session_scope
from farmcore.settings import Settings

logger = logging.getLogger(__name__)

# Counted refusals of one intent before it is failed for good.
MAX_ATTEMPTS = 3
# A refused intent is offered again after this long.
RETRY_SECONDS = 60
# A claim whose send reported nothing for this long is ambiguous (its worker died). Longer than
# any provider's own timeout (Telegram: 15 s), so a slow send is never declared lost.
CLAIM_SECONDS = 120
# Intents one `dispatch` call handles at most; the rest wait for the next tick.
BATCH = 100

RESOLUTIONS = ("sent", "resend", "drop")


@dataclass(frozen=True)
class Intent:
    id: int
    farm_id: int
    source_app: str
    kind: str
    payload: dict[str, Any]
    attempt: int  # this claim's number


class Undelivered(Exception):
    """The provider definitely did not deliver (it answered with a refusal, or was never
    reached). The message must not quote the payload. `retry_after`: seconds the provider asked
    to wait; such a refusal is not counted against `MAX_ATTEMPTS`."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OutcomeUnknown(Exception):
    """The provider may or may not have delivered (no answer after the request left)."""


class Provider(Protocol):
    def deliver(self, intent: Intent) -> dict[str, Any] | None:
        """Send `intent`; its receipt (JSON) when accepted. Raises `Undelivered` or
        `OutcomeUnknown`. May have a `close()`, called after each `dispatch`."""
        ...


def add(
    session: Session,
    *,
    farm_id: int,
    source_app: str,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    ttl: timedelta,
) -> bool:
    """Record an intent in the session's transaction, expiring `ttl` after it (wall clock).
    False when an intent with `dedupe_key` already exists: one cause, one intent."""
    inserted = session.execute(
        insert(OutboxIntent)
        .values(
            farm_id=farm_id,
            source_app=source_app,
            kind=kind,
            dedupe_key=dedupe_key,
            payload=payload,
            expires_at=func.now() + ttl,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(OutboxIntent.id)
    ).first()
    return inserted is not None


# The intent's app row, locked against a switch until the claim commits (a disable waits for it;
# a claim after the disable reads the new state). No row reads as installed.
_SOURCE = text("SELECT state FROM app_module WHERE name = :a FOR SHARE")


def may_dispatch(session: Session, intent: Intent) -> bool:
    """Whether `intent` may be sent now, checked in the transaction that claims it: the
    installation's external effects are enabled and the app that recorded it is on (a switched
    off app's intents wait, and are sent if it is switched on before they expire). A held intent
    stays pending (and expires on time); the check runs again at the next dispatch."""
    if read_external_effects(session) != EFFECTS_ENABLED:
        return False
    # Only the source app's own row: a switch never leaves a dependant on while its dependency is
    # off (`registry.switch_refusal`); a dependency switched off by hand in SQL is not seen here.
    state = session.execute(_SOURCE, {"a": intent.source_app}).scalar()
    return state is None or state == "installed"


_EXPIRE = text(
    "UPDATE outbox SET state = 'expired', finished_at = clock_timestamp() "
    "WHERE state = 'pending' AND expires_at <= clock_timestamp()"
)
_LAPSE = text(
    "UPDATE outbox SET state = 'ambiguous', error = 'claim lapsed' "
    "WHERE state = 'sending' AND claimed_at <= clock_timestamp() - make_interval(secs => :s)"
)
_NEXT = text(
    "SELECT id, farm_id, source_app, kind, payload, attempts FROM outbox "
    "WHERE state = 'pending' AND available_at <= clock_timestamp() "
    "AND expires_at > clock_timestamp() AND kind = ANY(:k) AND id <> ALL(:h) "
    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
)
_CLAIM = text(
    "UPDATE outbox SET state = 'sending', attempts = attempts + 1, "
    "claimed_at = clock_timestamp() WHERE id = :id RETURNING attempts"
)
# The claim's own number fences each outcome: a resent intent's older claim writes nothing.
_SENT = text(
    "UPDATE outbox SET state = 'sent', receipt = CAST(:r AS jsonb), error = NULL, "
    "finished_at = clock_timestamp() "
    "WHERE id = :id AND attempts = :a AND state IN ('sending', 'ambiguous')"
)
# `:counted` is 1 for a refusal, 0 for one that only asks to wait.
_REFUSED = text(
    "UPDATE outbox SET error = :e, claimed_at = NULL, refusals = refusals + :counted, "
    "state = CASE WHEN refusals + :counted >= :max THEN 'failed' ELSE 'pending' END, "
    "finished_at = CASE WHEN refusals + :counted >= :max THEN clock_timestamp() END, "
    "available_at = clock_timestamp() + make_interval(secs => :retry) "
    "WHERE id = :id AND attempts = :a AND state IN ('sending', 'ambiguous')"
)
_UNKNOWN = text(
    "UPDATE outbox SET state = 'ambiguous', error = :e "
    "WHERE id = :id AND attempts = :a AND state = 'sending'"
)
# Run by `farmcore restore` on the copy: what the source had not settled, it may still send.
HOLD_INHERITED = (
    "UPDATE outbox SET error = 'restored', "
    "state = CASE WHEN state = 'pending' AND expires_at <= clock_timestamp() "
    "THEN 'expired' ELSE 'ambiguous' END, "
    "finished_at = CASE WHEN state = 'pending' AND expires_at <= clock_timestamp() "
    "THEN clock_timestamp() END "
    "WHERE state IN ('pending', 'sending')"
)


def _providers(manifests: Sequence[Manifest], settings: Settings) -> dict[str, Provider]:
    built = {}
    for manifest in manifests:
        for kind, factory in manifest.delivery_providers:
            provider = import_string(factory)(settings)
            if provider is not None:
                built[kind] = provider
    return built


def _claim(
    session_factory: sessionmaker[Session], kinds: list[str], held: list[int]
) -> Intent | None:
    """The next deliverable intent, claimed in a committed transaction; None when none is left.
    Intents `may_dispatch` refuses are added to `held` and skipped for the rest of the pass."""
    while True:
        with session_scope(session_factory) as session:
            row = session.execute(_NEXT, {"k": kinds, "h": held}).first()
            if row is None:
                return None
            intent = Intent(row.id, row.farm_id, row.source_app, row.kind, row.payload, 0)
            if not may_dispatch(session, intent):
                held.append(row.id)
                continue
            attempt = session.execute(_CLAIM, {"id": row.id}).scalar_one()
        return Intent(row.id, row.farm_id, row.source_app, row.kind, row.payload, attempt)


def _deliver(session_factory: sessionmaker[Session], provider: Provider, intent: Intent) -> str:
    """Send one claimed intent, outside any transaction, and record the outcome."""
    keys = {"id": intent.id, "a": intent.attempt}
    try:
        receipt = provider.deliver(intent)
    except Undelivered as exc:
        waits = exc.retry_after is not None
        params = {
            **keys,
            "e": type(exc).__name__,
            "max": MAX_ATTEMPTS,
            "counted": 0 if waits else 1,
            "retry": max(0.0, float(exc.retry_after)) if waits else RETRY_SECONDS,
        }
        statement, outcome = _REFUSED, "deferred" if waits else "refused"
    except Exception as exc:  # the message may have left: never resend it blindly
        statement, params, outcome = _UNKNOWN, {**keys, "e": type(exc).__name__}, "ambiguous"
    else:
        stored = None if receipt is None else json.dumps(receipt)
        statement, params, outcome = _SENT, {**keys, "r": stored}, "sent"
    with session_scope(session_factory) as session:
        session.execute(statement, params)
    if outcome != "sent":
        # Id, kind and error type only: the payload is farm data.
        logger.warning("delivery %s (%s) %s (%s)", intent.id, intent.kind, outcome, params.get("e"))
    return outcome


def dispatch(
    session_factory: sessionmaker[Session],
    settings: Settings,
    manifests: Sequence[Manifest],
    *,
    limit: int = BATCH,
    stop: threading.Event | None = None,
) -> Counter[str]:
    """One pass: expire what is past its expiry, mark lapsed claims ambiguous, then claim and
    send up to `limit` intents one by one, taking no new claim once `stop` is set. Returns
    {outcome: count} (sent, refused, deferred, ambiguous)."""
    outcomes: Counter[str] = Counter()
    with session_scope(session_factory) as session:
        session.execute(_EXPIRE)
        session.execute(_LAPSE, {"s": CLAIM_SECONDS})
        if read_external_effects(session) != EFFECTS_ENABLED:
            return outcomes
    providers = _providers(manifests, settings)
    if not providers:
        return outcomes
    held: list[int] = []
    try:
        while outcomes.total() < limit and not (stop is not None and stop.is_set()):
            intent = _claim(session_factory, list(providers), held)
            if intent is None:
                break
            outcomes[_deliver(session_factory, providers[intent.kind], intent)] += 1
    finally:
        for provider in providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                close()
    return outcomes


def ambiguous(session: Session) -> list[Row]:
    """Intents that may or may not have been delivered, oldest first, for reconciliation."""
    return list(
        session.execute(
            text(
                "SELECT id, farm_id, source_app, kind, payload, attempts, created_at, "
                "claimed_at, expires_at, error FROM outbox WHERE state = 'ambiguous' ORDER BY id"
            )
        ).all()
    )


def resolve(session: Session, intent_id: int, resolution: str) -> str | None:
    """Settle an ambiguous intent: `sent` (the operator saw it arrive), `resend` (it did not:
    offered again, unless it has expired meanwhile) or `drop` (never send it). The intent's new
    state, or None when it is not ambiguous."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution is one of {RESOLUTIONS}")
    state = {
        "sent": "'sent'",
        "resend": "CASE WHEN expires_at > clock_timestamp() THEN 'pending' ELSE 'expired' END",
        "drop": "'failed'",
    }[resolution]
    return session.execute(
        text(
            f"UPDATE outbox SET state = {state}, error = :e, claimed_at = NULL, "
            "available_at = clock_timestamp(), finished_at = CASE WHEN "
            f"{state} = 'pending' THEN NULL ELSE clock_timestamp() END "
            "WHERE id = :id AND state = 'ambiguous' RETURNING state"
        ),
        {"id": intent_id, "e": f"resolved: {resolution}"},
    ).scalar()
