"""Terminal job cards retain saved plan resources after releasing live bookings."""

import pytest
from sqlalchemy import func, select

from farmcore.apps.planning.models import PlanningJobAssignment
from tests.apps.planning.test_assignments import _assign, _machine, _operator
from tests.apps.planning.test_completion import _created, completion_body


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_terminal_job_card_shows_saved_resources_without_future_booking_warnings(
    client, session, farm, parcels, products, advice, terminal_status
):
    machine = _machine(session, farm, name="Original sprayer")
    operator = _operator(session, farm, name="Original operator")
    job = _created(client, farm, parcels, products, advice)
    assert _assign(client, farm, job["id"], "machine", machine.id, 1).status_code == 200
    assigned = _assign(client, farm, job["id"], "operator", operator.id, 2)
    assert assigned.status_code == 200
    job = assigned.json()

    planned = client.get("/planning/", params={"farm": farm.id})
    assert planned.status_code == 200
    planned_card = planned.text.split(f'data-planning-job="{job["id"]}"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Resource plan:" in planned_card
    assert "capacity unknown" in planned_card

    if terminal_status == "completed":
        terminal = client.post(
            f"/planning/jobs/{job['id']}/complete",
            params={"farm": farm.id},
            json=completion_body(job),
        )
    else:
        terminal = client.post(
            f"/planning/jobs/{job['id']}/cancel",
            params={"farm": farm.id},
            json={"expected_revision": job["revision"], "reason": "Weather changed"},
        )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == terminal_status
    assert (
        session.scalar(
            select(func.count())
            .select_from(PlanningJobAssignment)
            .where(PlanningJobAssignment.job_id == job["id"])
        )
        == 0
    )

    # Once bookings are released, removing a configured resource must not erase its history.
    session.delete(machine)
    session.delete(operator)
    session.flush()
    page = client.get("/planning/", params={"farm": farm.id})
    assert page.status_code == 200
    card = page.text.split(f'data-planning-job="{job["id"]}"', 1)[1].split("</section>", 1)[0]
    assert "Saved plan resources:" in card
    assert "Original sprayer" in card
    assert "Original operator" in card
    assert "No machine or operator booked" not in card
    assert "Resource plan:" not in card
    assert "capacity unknown" not in card
    assert "no machine is booked" not in card
    assert "no operator is booked" not in card
    assert "Another eligible time:" not in card
    assert "Another configured machine:" not in card
    assert "Another parcel:" not in card
