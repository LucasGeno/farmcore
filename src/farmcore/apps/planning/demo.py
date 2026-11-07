"""Demo reset removes the demo farm's planned jobs before core deletes their assessments.

Revision rows refuse UPDATE but allow DELETE, so an explicit delete keeps the demo reset
working without weakening production immutability.
"""

from __future__ import annotations

from sqlalchemy import delete

from farmcore.apps.planning.models import PlanningJob, PlanningJobParcel, PlanningJobRevision


def reset(session, farm) -> None:
    session.execute(delete(PlanningJobRevision).where(PlanningJobRevision.farm_id == farm.id))
    session.execute(delete(PlanningJobParcel).where(PlanningJobParcel.farm_id == farm.id))
    session.execute(delete(PlanningJob).where(PlanningJob.farm_id == farm.id))
