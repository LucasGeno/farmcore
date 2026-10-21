"""Corrections replace canonical facts once, retaining original evidence and intent."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core import logs
from farmcore.core.identity import principal_for
from farmcore.core.models.canonical import Log, Parcel, Quantity
from farmcore.core.models.corrections import OperationCorrection
from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession
from farmcore.core.timeline import post_event
from farmcore.web import is_admin

RecordId = Annotated[int, Field(gt=0, lt=2**63)]
# PostgreSQL unconstrained numeric storage limits (not agronomic dose limits):
# https://www.postgresql.org/docs/current/datatype-numeric.html
NUMERIC_INTEGER_DIGITS = 131072
NUMERIC_FRACTION_DIGITS = 16383
# New intent has a bounded fixed-notation rendering cost. Historical rows remain exact.
NEW_DOSE_TEXT_LIMIT = 4096


class Dose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: RecordId
    measure: str = "rate"
    value: Decimal = Field(gt=0, allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=40)
    label: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def input_size(cls, value):
        if (
            isinstance(value, str)
            and len(value) > NUMERIC_INTEGER_DIGITS + NUMERIC_FRACTION_DIGITS + 2
        ):
            raise ValueError("Dose representation exceeds numeric storage capacity.")
        return value

    @field_validator("measure")
    @classmethod
    def rate(cls, value):
        if value != "rate":
            raise ValueError("Spray doses must be rates.")
        return value

    @field_validator("unit")
    @classmethod
    def unit_known(cls, value):
        if value not in {"kg/ha", "L/ha", "l/ha", "g/ha", "ml/ha"}:
            raise ValueError("Supply an explicit spray rate unit.")
        return value


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantities: list[Dose] | None = Field(default=None, min_length=1, max_length=50)
    parcel_ids: list[RecordId] | None = Field(default=None, min_length=1, max_length=100)
    ts_start: datetime | None = None
    ts_end: datetime | None = None
    notes: str | None = Field(default=None, max_length=10000)
    bbch: int | None = Field(default=None, ge=0, le=99)

    @field_validator("ts_start", "ts_end")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Supply a timezone.")
        return value


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4000)
    changes: Changes

    @field_validator("request_key", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank.")
        return value


@dataclass(frozen=True)
class Actor:
    key: str
    label: str
    identity_id: int | None = None


def authority(request, repo):
    """Revalidate after farm lock, including the persisted session's current authority."""
    logs.lock_farm(repo.session, repo.farm_id)
    if is_admin(request):
        return Actor("operator", "Installation operator")
    return member_authority(repo, principal_for(request))


def member_authority(repo, principal):
    logs.lock_farm(repo.session, repo.farm_id)
    if principal is None:
        # HTTP owner dependencies reject anonymous callers before this locked revalidation.
        raise HTTPException(403, "Farm owner required.")  # pragma: no cover
    now = datetime.now(UTC)
    identity = repo.session.scalar(
        select(LocalIdentity)
        .join(FarmMembership, FarmMembership.identity_id == LocalIdentity.id)
        .join(LocalSession, LocalSession.identity_id == LocalIdentity.id)
        .where(
            LocalIdentity.id == principal.identity_id,
            LocalIdentity.disabled_at.is_(None),
            FarmMembership.farm_id == repo.farm_id,
            FarmMembership.role == "owner",
            FarmMembership.revoked_at.is_(None),
            LocalSession.token_hash == principal.session_hash,
            LocalSession.revoked_at.is_(None),
            LocalSession.expires_at > now,
        )
        .with_for_update(of=(LocalIdentity, FarmMembership, LocalSession))
    )
    if identity is None:
        raise HTTPException(403, "Farm owner required.")
    return Actor(f"identity:{identity.id}", identity.login, identity.id)


def validate_new_dose(value: Decimal) -> None:
    integer_digits = max(value.adjusted() + 1, 1)
    fraction_digits = max(-value.as_tuple().exponent, 0)
    rendered_length = integer_digits + (1 + fraction_digits if fraction_digits else 0)
    if (
        integer_digits > NUMERIC_INTEGER_DIGITS
        or fraction_digits > NUMERIC_FRACTION_DIGITS
        or rendered_length > NEW_DOSE_TEXT_LIMIT
    ):
        raise HTTPException(422, "Dose exceeds supported numeric storage or rendering capacity.")


def decimal_text(value: Decimal) -> str:
    """Exact fixed notation; Decimal.normalize() would round through the active context."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def snapshot(log):
    return {
        "version": 1,
        "type": log.type,
        "status": log.status,
        "name": log.name,
        "ts_start": log.ts_start.astimezone(UTC).isoformat(),
        "ts_end": log.ts_end.astimezone(UTC).isoformat() if log.ts_end else None,
        "bbch": log.bbch,
        "notes": log.notes,
        "props": log.props,
        "parcel_ids": sorted(p.id for p in log.parcels),
        "quantities": [
            {
                "id": q.id,
                "product_id": q.product_id,
                "measure": q.measure,
                "value": decimal_text(q.value),
                "unit": q.unit,
                "label": q.label,
            }
            for q in sorted(log.quantities, key=lambda q: q.id)
        ],
        "sources": [
            {
                "id": s.id,
                "source_type": s.source_type,
                "external_key": s.external_key,
                "raw_payload": s.raw_payload,
                "confidence": s.confidence,
                "precision_rank": s.precision_rank,
                "received_at": s.received_at.isoformat(),
            }
            for s in sorted(log.sources, key=lambda s: s.id)
        ],
    }


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def target_id(value):
    if not 1 <= value < 2**63:
        raise HTTPException(404, "Record not found.")
    return value


def _log(repo, log_id):
    target_id(log_id)
    log = repo.session.scalar(
        select(Log)
        .where(Log.id == log_id, Log.farm_id == repo.farm_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if log is None or log.type != "spray" or log.status != "done":
        raise HTTPException(404, "Confirmed spray not found.")
    repo.session.expire(log, ["parcels", "quantities", "sources"])
    return log


def _after(repo, before, changes, *, replay=False):
    result = json.loads(json.dumps(before))
    for key in changes.model_fields_set:
        value = getattr(changes, key)
        if key == "quantities" and value is not None:
            eligible = None
            unused = list(before["quantities"])
            proposed = []
            for dose in value:
                row = {**dose.model_dump(mode="json"), "value": dose.value}
                matched = next(
                    (
                        prior
                        for prior in unused
                        if all(
                            (
                                row[field] == Decimal(prior[field])
                                if field == "value"
                                else row[field] == prior[field]
                            )
                            for field in row
                            if field != "label" or "label" in dose.model_fields_set
                        )
                    ),
                    None,
                )
                if matched is not None:
                    row = dict(matched)
                    unused.remove(matched)
                else:
                    if not replay:
                        if eligible is None:
                            eligible = {p.id for p in products_for_farm(repo, scope="logs:write")}
                        if dose.product_id not in eligible:
                            raise HTTPException(422, "Product not found.")
                    validate_new_dose(dose.value)  # before fixed notation or persisted intent
                    row["value"] = decimal_text(dose.value)
                    if "label" not in dose.model_fields_set:
                        prior = next(
                            (q for q in unused if q["product_id"] == dose.product_id), None
                        )
                        row["label"] = prior["label"] if prior is not None else None
                proposed.append(row)
            value = proposed
        elif key == "parcel_ids" and value is not None:
            value = sorted(set(value))
            if not replay and set(
                repo.session.scalars(
                    select(Parcel.id).where(Parcel.farm_id == repo.farm_id, Parcel.id.in_(value))
                )
            ) != set(value):
                raise HTTPException(422, "Parcel not found.")
        elif isinstance(value, datetime):
            value = value.astimezone(UTC).isoformat()
        if value is None and key not in {"ts_end", "bbch"}:
            raise HTTPException(422, "Required correction value missing.")
        result[key] = value
    if result["ts_end"] and result["ts_end"] < result["ts_start"]:
        raise HTTPException(422, "End precedes start.")
    if not replay and result == before:
        raise HTTPException(422, "Supply changed values.")
    return result


# The corrected facts a reader may see. Source receipts can hold chat transcripts, Telegram chat
# ids and machine files: the snapshot keeps them, views show only each source's type and time.
VISIBLE_FACTS = ("ts_start", "ts_end", "bbch", "notes", "parcel_ids", "quantities")


def visible(facts):
    return {
        **{key: facts[key] for key in VISIBLE_FACTS},
        "sources": [
            {"source_type": s["source_type"], "received_at": s["received_at"]}
            for s in facts["sources"]
        ],
    }


def view(correction):
    return {
        "id": correction.id,
        "log_id": correction.original_log_id,
        "status": correction.status,
        "reason": correction.reason,
        "actor": correction.actor_label,
        "created_at": correction.created_at.isoformat(),
        "finalized_at": correction.finalized_at.isoformat() if correction.finalized_at else None,
        "before": visible(correction.before_snapshot),
        "after": visible(correction.after_snapshot),
    }


def preview(repo, actor, log_id, body):
    target_id(log_id)
    old = repo.session.scalar(
        select(OperationCorrection).where(
            OperationCorrection.farm_id == repo.farm_id,
            OperationCorrection.actor_key == actor.key,
            OperationCorrection.request_key == body.request_key,
        )
    )
    if old is not None:
        if old.status == "pending" and old.live_log_id is None:
            raise HTTPException(409, "Original record changed; preview again.")
        if (
            old.original_log_id != log_id
            or old.reason != body.reason
            or _after(repo, old.before_snapshot, body.changes, replay=True) != old.after_snapshot
        ):
            raise HTTPException(409, "Request key already used for another correction.")
        return old
    log = _log(repo, log_id)
    before = snapshot(log)
    after = _after(repo, before, body.changes)
    correction = OperationCorrection(
        farm_id=repo.farm_id,
        original_log_id=log.id,
        live_log_id=log.id,
        request_key=body.request_key,
        actor_key=actor.key,
        actor_label=actor.label,
        creator_identity_id=actor.identity_id,
        status="pending",
        reason=body.reason,
        base_fingerprint=fingerprint(before),
        before_snapshot=before,
        after_snapshot=after,
    )
    repo.session.add(correction)
    repo.session.flush()
    return correction


def finalize(repo, actor, correction_id, action, now):
    target_id(correction_id)
    correction = repo.session.scalar(
        select(OperationCorrection)
        .where(
            OperationCorrection.id == correction_id,
            OperationCorrection.farm_id == repo.farm_id,
            OperationCorrection.actor_key == actor.key,
        )
        .with_for_update()
    )
    if correction is None:
        raise HTTPException(404, "Correction not found.")
    desired = "confirmed" if action == "confirm" else "cancelled"
    if correction.status != "pending":
        if correction.status != desired:
            raise HTTPException(409, "Correction already finalized.")
        return correction, False
    if correction.live_log_id is None:
        raise HTTPException(409, "Original record changed; preview again.")
    log = _log(repo, correction.live_log_id)
    if action == "confirm":
        if fingerprint(snapshot(log)) != correction.base_fingerprint:
            raise HTTPException(409, "Record changed; preview again.")
        after = correction.after_snapshot
        # Revalidate edits and current parcel ownership.
        # Untouched historical doses retain their exact values and identity.
        _after(
            repo,
            correction.before_snapshot,
            Changes.model_validate(
                {
                    k: (
                        [
                            {field: value for field, value in q.items() if field != "id"}
                            for q in after[k]
                        ]
                        if k == "quantities"
                        else after[k]
                    )
                    for k in ("quantities", "parcel_ids", "ts_start", "ts_end", "notes", "bbch")
                    if k == "parcel_ids" or after[k] != correction.before_snapshot[k]
                }
            ),
        )
        log.parcels = list(
            repo.session.scalars(select(Parcel).where(Parcel.id.in_(after["parcel_ids"])))
        )
        if after["quantities"] != correction.before_snapshot["quantities"]:
            existing = {q.id: q for q in log.quantities}
            log.quantities = [
                existing[q["id"]]
                if "id" in q
                else Quantity(**{k: (Decimal(v) if k == "value" else v) for k, v in q.items()})
                for q in after["quantities"]
            ]
        log.ts_start = datetime.fromisoformat(after["ts_start"])
        log.ts_end = datetime.fromisoformat(after["ts_end"]) if after["ts_end"] else None
        log.notes, log.bbch = after["notes"], after["bbch"]
        for parcel_id in sorted(
            set(correction.before_snapshot["parcel_ids"]) | set(after["parcel_ids"])
        ):
            post_event(
                repo.session,
                farm_id=repo.farm_id,
                res_model="parcel",
                res_id=parcel_id,
                kind="log.corrected",
                body="Spray corrected",
                payload={
                    "log_id": log.id,
                    "correction_id": correction.id,
                },
                actor=actor.key,
                source_app="operations",
                occurred_at=now,
            )
    correction.status = desired
    correction.finalized_at = now
    correction.finalized_actor_key, correction.finalized_actor_label = actor.key, actor.label
    repo.session.flush()
    return correction, action == "confirm"
