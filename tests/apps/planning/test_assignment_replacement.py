"""Resource replacement through the service, with only the database session stubbed."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobRevision,
    PlanningMachine,
    PlanningOperator,
)
from farmcore.apps.planning.service import set_assignments
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import DataGrant


@pytest.mark.parametrize(
    "kind, model", [("machine", PlanningMachine), ("operator", PlanningOperator)]
)
def test_replacement_updates_booking_and_revision_snapshot(kind, model):
    now = datetime(2024, 4, 9, 6, tzinfo=UTC)
    job = PlanningJob(
        id=1,
        farm_id=1,
        status="planned",
        revision=2,
        name="Spray block A",
        scheduled_start=now,
        duration_minutes=90,
        props={},
    )
    booking = PlanningJobAssignment(
        farm_id=1,
        job_id=job.id,
        **{f"{kind}_id": 10},
        starts_at=now,
        ends_at=now + timedelta(minutes=90),
    )
    resources = {
        id: model(id=id, farm_id=1, name=name, active=True)
        for id, name in [(10, "Original"), (20, "Replacement")]
    }
    session = MagicMock(spec=Session)
    session.scalar.side_effect = lambda stmt: resources[stmt.compile().params["id_1"]]
    parcel_links = MagicMock()
    parcel_links.all.return_value = []
    session.scalars.side_effect = [
        [booking],  # Current assignment.
        [],  # No conflicting booking for the replacement.
        [],  # No busy day.
        parcel_links,
        [booking],  # Assignment read back for the revision snapshot.
    ]
    repo = ScopedRepository(session, 1, DataGrant(id=1, farm_id=1, scopes=["*"]), "owner", "test")

    result = set_assignments(repo, now, "UTC", job, expected_revision=2, changes={kind: 20})

    assert getattr(booking, f"{kind}_id") == 20
    other_kind = "operator" if kind == "machine" else "machine"
    assert getattr(booking, f"{other_kind}_id") is None
    assert booking.starts_at == now
    assert booking.ends_at == now + timedelta(minutes=90)
    assert result.revision == 3
    revisions = [
        call.args[0]
        for call in session.add.call_args_list
        if isinstance(call.args[0], PlanningJobRevision)
    ]
    assert len(revisions) == 1
    assert revisions[0].revision == 3
    assert revisions[0].snapshot["assignments"] == [
        {
            "kind": kind,
            "id": 20,
            "name": "Replacement",
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(minutes=90)).isoformat(),
        }
    ]
