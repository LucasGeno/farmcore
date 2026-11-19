"""Crop-assignment drafts: an agent proposes crop seasons the farmer confirms.

`validate` checks a proposed assignment against the farm (its parcels, the FarmCore crop list) and
returns the normalised payload. `confirm` appends one crop season per chosen parcel through the
same `crops.assign` the owner's crops page uses, so history is never rewritten and a covered
parcel keeps its existing season. The agent's text (crop keys, parcel ids) is data: a key is
resolved only through `crops.CROPS`, never guessed.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from farmcore.apps.fields import seasons
from farmcore.apps.onboarding import crops
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm, Parcel


class CropAssignments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_on: date
    assignments: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def non_empty(self):
        if not self.assignments:
            raise ValueError("Name at least one parcel and its crop.")
        return self


def validate(session, farm_id, payload):
    data = CropAssignments.model_validate(payload)
    ids: list[int] = []
    for raw_id in data.assignments:
        if not raw_id.isascii() or not raw_id.isdigit() or not 0 < int(raw_id) < 2**63:
            raise ValueError("Parcel ids must be positive numbers.")
        ids.append(int(raw_id))
    if len(set(ids)) != len(ids):
        raise ValueError("Choose each parcel only once.")
    found = set(
        session.scalars(select(Parcel.id).where(Parcel.id.in_(ids), Parcel.farm_id == farm_id))
    )
    if found != set(ids):
        raise ValueError("Some parcels are not on this farm.")
    for key in data.assignments.values():
        if key not in crops.CROPS:
            raise ValueError("Choose a known crop for each parcel.")
    return {
        "starts_on": data.starts_on.isoformat(),
        "assignments": {str(int(key)): value for key, value in data.assignments.items()},
    }


def review_choices(repo, payload):
    """Validate stale drafts and show the same season decisions confirmation uses."""
    payload = validate(repo.session, repo.farm_id, payload)
    day = date.fromisoformat(payload["starts_on"])
    choices = []
    for raw_id, key in payload["assignments"].items():
        parcel_id = int(raw_id)
        covering = seasons.season_on(repo, parcel_id, day)
        choices.append(
            {
                "parcel": repo.session.get(Parcel, parcel_id),
                "crop": crops.CROPS[key],
                "covering": covering,
                "future_overlap": covering is None
                and any(season.starts_on > day for season in seasons.seasons(repo, parcel_id)),
            }
        )
    return choices


def confirm(session, draft, *, actor, now, services):
    farm = session.get(Farm, draft.farm_id)
    repo = ScopedRepository(
        session, draft.farm_id, ensure_owner_grant(session, farm), actor, "drafts"
    )
    # Core holds the farm lock; inspect every parcel before writing any season.
    rows = review_choices(repo, draft.payload)
    if any(row["future_overlap"] for row in rows):
        raise ValueError("The proposed start overlaps a future crop season. Review its dates.")
    day = date.fromisoformat(draft.payload["starts_on"])
    choices = {row["parcel"].id: row["crop"].key for row in rows}
    new_ids = [row["parcel"].id for row in rows if row["covering"] is None]
    result = crops.assign(repo, starts_on=day, choices=choices)
    if result.assigned == 0:
        raise ValueError(
            "Nothing would be recorded: all selected parcels already have a crop season."
        )
    season = seasons.season_on(repo, min(new_ids), day)
    if season is None:
        raise ValueError("No crop season was recorded. Review the proposed dates.")
    return "crop_season", season.id
