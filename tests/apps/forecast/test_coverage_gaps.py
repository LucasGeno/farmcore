"""Coverage for the forecast busy form without a stored clock row in live mode."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from farmcore.core.models.kernel import ClockState
from farmcore.main import create_app


def test_live_mode_without_a_clock_row_uses_wall_now(settings, session_factory, session, farm):
    session.query(ClockState).delete()
    session.flush()
    live = settings.model_copy(update={"clock_mode": "live"})
    app = create_app(live, session_factory=session_factory)
    with TestClient(
        app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}
    ) as client:
        response = client.post(
            "/forecast/busy",
            params={"farm": farm.id},
            data={
                "day": (date.today() + timedelta(days=1)).isoformat(),
                "busy": "0",
                "next": "/forecast/today",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
