"""Crop seasons on a stable parcel: list, add, edit the current one, and the season context an
assessment saves as evidence.

A season is an inclusive date range with an optional crop (EPPO code, name) and BBCH stage;
null means unknown and is reported as such, never filled from legacy parcel props. Only the
current season (the latest start) is edited, with an explicit `expected_revision`, so earlier
seasons and the assessments that saved them keep their context. Overlaps are rejected by the
database exclusion constraint, never merged.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import CropSeason, Parcel

SCOPE = "fields:read"
FIELDS = ("starts_on", "ends_on", "crop_eppo", "crop_name", "bbch")
OVERLAP = "ex_crop_season_parcel_dates"


class SeasonError(ValueError):
    """Invalid season values; the route answers 422."""


class SeasonNotFound(LookupError):
    """No such season on this parcel of this farm; the route answers 404."""


class SeasonConflict(RuntimeError):
    """Overlap, a stale revision or an earlier season; the route answers 409."""


def season_json(row: CropSeason) -> dict[str, Any]:
    unknown = [
        name
        for name, missing in (
            ("crop", row.crop_eppo is None and row.crop_name is None),
            ("end", row.ends_on is None),
            ("phenology", row.bbch is None),
        )
        if missing
    ]
    return {
        "id": row.id,
        "parcel_id": row.parcel_id,
        "starts_on": row.starts_on.isoformat(),
        "ends_on": row.ends_on.isoformat() if row.ends_on else None,
        "crop_eppo": row.crop_eppo,
        "crop_name": row.crop_name,
        "bbch": row.bbch,
        "revision": row.revision,
        "unknown": unknown,
    }


def seasons(repo: ScopedRepository, parcel_id: int) -> list[CropSeason]:
    """The parcel's seasons, oldest first."""
    return repo.list(
        CropSeason,
        CropSeason.parcel_id == parcel_id,
        scope=SCOPE,
        order_by=(CropSeason.starts_on, CropSeason.id),
    )


def seasons_json(repo: ScopedRepository, parcel: Parcel) -> dict[str, Any]:
    rows = seasons(repo, parcel.id)
    return {
        "parcel_id": parcel.id,
        "current": season_json(rows[-1]) if rows else None,
        "seasons": [season_json(row) for row in rows],
    }


def season_on(repo: ScopedRepository, parcel_id: int, day: date) -> CropSeason | None:
    """The season covering `day`, or None: a day between seasons has none."""
    rows = repo.list(
        CropSeason,
        CropSeason.parcel_id == parcel_id,
        CropSeason.starts_on <= day,
        (CropSeason.ends_on.is_(None)) | (CropSeason.ends_on >= day),
        scope=SCOPE,
    )
    return rows[0] if rows else None


def season_evidence(repo: ScopedRepository, parcel_id: int, day: date) -> dict[str, Any]:
    """The season context an assessment saves: a copy with its revision, so a later edit of
    the season never changes what the saved assessment says it saw."""
    row = season_on(repo, parcel_id, day)
    return {"status": "known", **season_json(row)} if row else {"status": "unknown"}


def _check(values: dict[str, Any]) -> None:
    end = values.get("ends_on")
    if end is not None and end < values["starts_on"]:
        raise SeasonError("A season cannot end before it starts.")


def _flush(repo: ScopedRepository) -> None:
    try:
        with repo.session.begin_nested():
            repo.session.flush()
    except IntegrityError as exc:
        if OVERLAP in str(exc.orig):
            raise SeasonConflict("The dates overlap another season of this parcel.") from None
        raise


def create(repo: ScopedRepository, parcel: Parcel, values: dict[str, Any]) -> CropSeason:
    _check(values)
    row = CropSeason(
        farm_id=repo.farm_id, parcel_id=parcel.id, created_by=repo.actor, revision=1, **values
    )
    repo.session.add(row)
    _flush(repo)
    return row


def edit(
    repo: ScopedRepository,
    parcel: Parcel,
    season_id: int,
    *,
    expected_revision: int,
    changes: dict[str, Any],
) -> CropSeason:
    """Edit the parcel's current season. The row lock serializes two devices' edits."""
    row = repo.session.scalar(
        select(CropSeason)
        .where(
            CropSeason.id == season_id,
            CropSeason.parcel_id == parcel.id,
            CropSeason.farm_id == repo.farm_id,
        )
        .with_for_update()
    )
    if row is None:
        raise SeasonNotFound("No such season on this parcel.")
    later = repo.session.scalar(
        select(CropSeason.id).where(
            CropSeason.parcel_id == parcel.id, CropSeason.starts_on > row.starts_on
        )
    )
    if later is not None:
        raise SeasonConflict("Only the current season can be edited; earlier seasons are kept.")
    if expected_revision != row.revision:
        raise SeasonConflict(f"Revision conflict: the season is at revision {row.revision}.")
    values = {name: getattr(row, name) for name in FIELDS} | changes
    _check(values)
    for name, value in changes.items():
        setattr(row, name, value)
    row.revision += 1
    _flush(repo)
    return row
