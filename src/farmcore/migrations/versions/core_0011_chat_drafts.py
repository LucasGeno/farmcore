"""core: chat and assistant capture keep their pre-confirm state only as drafts

Every pending `log` becomes a `draft` and is deleted, so no committed log is pending:

- A chat or web-assistant capture (its one source keyed `tg:<chat>:<message>`,
  `web:farmer:<message>` or `web:farm:<farm>:<actor>:<message>`) of a spray, irrigation or
  observation with its parcels, time and dose or fact becomes an **open** draft of its proposer
  (`chat:<chat>`, `operator` or `identity:<id>`), expiring seven days after the upgrade (the
  default draft TTL), that its chat confirms as before.
- Any other pending log becomes an **expired** draft holding the same record data, so nothing
  is dropped: it can be read but never confirmed.

Unfinished chat clarifications (`farm.props.chat.clarifications`, 15-minute state) are removed.
The counts of open drafts, expired drafts and cleared clarifications are logged.

Downgrade changes no row: drafts stay drafts (core_0010 owns the table) and no pending log is
recreated; an unconfirmed chat capture of an upgraded database is proposed again after a
downgrade.

Revision ID: core_0011
Revises: core_0010
Create Date: 2026-11-19
"""

import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision: str = "core_0011"
down_revision: str | None = "core_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

KINDS = {"spray": "spray", "irrigate": "irrigation", "observation": "observation"}
OPEN_TTL = timedelta(days=7)  # core.drafts.DEFAULT_TTL
UPGRADE = "upgrade:core_0011"
CHAT_KEY = re.compile(
    r"^(?:tg:(?P<chat>-?\d+)"
    r"|web:farmer"
    r"|web:farm:\d+:(?P<actor>operator|identity:\d+)):\d+$"
)


def _actor(external_key: str) -> str | None:
    match = CHAT_KEY.match(external_key)
    if match is None:
        return None
    if match["chat"] is not None:
        return f"chat:{match['chat']}"
    return match["actor"] or "operator"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _payload(db, log) -> dict:
    parcels = db.execute(
        sa.text("SELECT parcel_id FROM log_parcel WHERE log_id = :id ORDER BY parcel_id"),
        {"id": log.id},
    ).scalars()
    payload = {"parcel_ids": list(parcels), "ts_start": _iso(log.ts_start)}
    if log.ts_end is not None:
        payload["ts_end"] = _iso(log.ts_end)
    props = log.props or {}
    if log.type == "spray":
        rates = db.execute(
            sa.text(
                "SELECT product_id, value, unit, label FROM quantity "
                "WHERE log_id = :id AND measure = 'rate' ORDER BY id"
            ),
            {"id": log.id},
        )
        payload["doses"] = [
            {"product_id": r.product_id, "value": str(r.value), "unit": r.unit, "label": r.label}
            for r in rates
            if r.product_id is not None
        ]
    elif log.type == "irrigate" and props.get("irrigation"):
        payload["fact"] = props["irrigation"]
    elif log.type == "observation" and props.get("observation"):
        payload["fact"] = props["observation"]
    return payload


def _complete(kind: str | None, payload: dict) -> bool:
    if kind is None or not payload["parcel_ids"] or payload["ts_start"] is None:
        return False
    return bool(payload.get("doses") if kind == "spray" else payload.get("fact"))


def upgrade() -> None:
    db = op.get_bind()
    now = datetime.now(UTC)
    opened = expired = 0
    pending = db.execute(
        sa.text(
            "SELECT id, farm_id, type, ts_start, ts_end, props FROM log "
            "WHERE status = 'pending' ORDER BY id"
        )
    ).all()
    for log in pending:
        sources = db.execute(
            sa.text(
                "SELECT source_type, external_key, raw_payload, confidence FROM log_source "
                "WHERE log_id = :id ORDER BY id"
            ),
            {"id": log.id},
        ).all()
        kind = KINDS.get(log.type)
        payload = _payload(db, log)
        actor = _actor(sources[0].external_key) if len(sources) == 1 else None
        details = {"migrated_from_log": log.id, "log_type": log.type}
        if actor is not None and _complete(kind, payload):
            source = sources[0]
            raw = source.raw_payload or {}
            transcript = str(raw.get("transcript") or "")[:2000]
            payload["capture"] = {
                "input": "text" if raw.get("input") == "text" else "voice",
                "transcript": transcript,
                "adapter": str(raw.get("adapter") or "")[:200],
                "confidence": source.confidence,
                "notes": [str(n) for n in raw.get("notes") or []][:20],
                "messages": {source.external_key: transcript},
                "operation": {},
                "history": [],
                "question": None,
                "epoch": "",
            }
            status, request_key, expires_at, finalized_at = (
                "open",
                source.external_key,
                now + OPEN_TTL,
                None,
            )
            opened += 1
        else:
            # Not a chat capture (or not a complete one): kept readable, never confirmable.
            details["sources"] = [
                {"source_type": s.source_type, "external_key": s.external_key} for s in sources
            ]
            kind = kind or log.type[:32]
            actor, status, request_key, expires_at, finalized_at = (
                UPGRADE,
                "expired",
                f"log:{log.id}",
                now,
                now,
            )
            expired += 1
        source_type = sources[0].source_type if sources else "voice"
        db.execute(
            sa.text(
                "INSERT INTO draft (farm_id, actor, kind, request_key, payload, provenance, "
                "status, revision, expires_at, finalized_at, finalized_by) VALUES (:farm, "
                ":actor, :kind, :key, CAST(:payload AS jsonb), CAST(:provenance AS jsonb), "
                ":status, 1, :expires, :finalized, :finalized_by)"
            ),
            {
                "farm": log.farm_id,
                "actor": actor,
                "kind": kind,
                "key": request_key,
                "payload": json.dumps(payload, default=str),
                "provenance": json.dumps({"source_type": source_type, "details": details}),
                "status": status,
                "expires": expires_at,
                "finalized": finalized_at,
                "finalized_by": UPGRADE if finalized_at is not None else None,
            },
        )
        db.execute(
            sa.text("DELETE FROM ext_id WHERE model = 'log' AND res_id = :id"), {"id": log.id}
        )
        db.execute(sa.text("DELETE FROM log WHERE id = :id"), {"id": log.id})

    cleared = 0
    farms = db.execute(
        sa.text("SELECT id, props FROM farm WHERE props -> 'chat' ? 'clarifications'")
    ).all()
    for farm in farms:
        chat = dict(farm.props["chat"])
        cleared += len(chat.pop("clarifications") or {})
        db.execute(
            sa.text(
                "UPDATE farm SET props = jsonb_set(props, '{chat}', CAST(:chat AS jsonb)) "
                "WHERE id = :id"
            ),
            {"chat": json.dumps(chat), "id": farm.id},
        )
    logger.warning(
        "core_0011: %d pending logs became open drafts, %d became expired drafts; "
        "%d unfinished chat clarifications cleared",
        opened,
        expired,
        cleared,
    )


def downgrade() -> None:
    """No row changes: drafts stay drafts and no pending log is recreated."""
