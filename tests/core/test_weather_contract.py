"""Weather provider contract and the normalised weather schema (expand step, core_0008).

Failure modes owned here (one test each):
- Golden: adding the normalised schema, or mirroring legacy weather into it, changes what the
  stations, forecast, advisor or replay consumers produce. Frozen in
  `snapshots/weather_golden.json` before any schema change.
- Licence: a receipt without a licence is stored as redistributable (or is refused instead of
  stored as `unknown`), or a raw SQL writer can mark unknown-licence data redistributable.
- Credential: a provider credential named in `auth_schema()` is stored in a receipt's request.
- Units and values: an observation or forecast value is stored as a float, NaN, or in a unit
  other than its CF variable's unit; a legacy float gains binary noise when mirrored.
- Registry: a second provider under a taken name silently replaces the first, or an object
  missing a contract method registers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from farmcore.apps.forecast import store as forecast_store
from farmcore.apps.stations import service as stations
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Advice, Event, Parcel
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor import scenes

GOLDEN = Path(__file__).parent / "snapshots" / "weather_golden.json"
manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
NOW = "2024-05-15 06:00"


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _without_ids(value):
    """Drop generated row ids (sequences are not rolled back between tests)."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "id" and not k.endswith("_id")}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def _semantics(session, settings) -> dict:
    """What the weather consumers produce at NOW, without generated ids or wall-clock times."""
    farm = ensure_default_farm(session, settings)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "golden")
    now = scenes.local(NOW)
    station = stations.station_for_farm(repo)
    hourly = stations.load_hourly(
        repo, station.id, now - timedelta(hours=24), now + timedelta(hours=72)
    )
    run = forecast_store.latest_run(repo, now)
    advice = session.execute(
        select(
            Parcel.alias,
            Advice.evaluated_at,
            Advice.decision,
            Advice.reasons,
            Advice.next_window_start,
            Advice.next_window_end,
            Advice.protected_until,
            Advice.products,
            Advice.props,
        )
        .join(Parcel, Parcel.id == Advice.parcel_id)
        .order_by(Advice.evaluated_at, Parcel.alias)
    ).all()
    events = session.execute(
        select(Event.kind, Event.occurred_at, Event.body, Event.payload).order_by(
            Event.occurred_at, Event.id
        )
    ).all()
    return _without_ids(
        json.loads(
            json.dumps(
                {
                    "station_hourly": [{k: _iso(v) for k, v in row.items()} for row in hourly],
                    "forecast": None
                    if run is None
                    else {
                        "source": run.source,
                        "issued_at": _iso(run.issued_at),
                        "available_at": _iso(run.available_at),
                        "label": run.label,
                        "as_issued": run.as_issued,
                        "series": [
                            [_iso(h.ts), h.rr1_mm, h.t_c, h.rh_pct, h.ff_kmh] for h in run.series
                        ],
                        "props": run.props,
                    },
                    "freshness": forecast_store.freshness(repo, now),
                    "advice": [[_iso(v) for v in row] for row in advice],
                    "events": [[_iso(v) for v in row] for row in events],
                },
                default=_iso,
                sort_keys=True,
            )
        )
    )


def _digests(semantics: dict) -> dict:
    """Readable decisions plus a SHA-256 per section of the canonical JSON (the full output is
    megabytes of advice evidence); a changed section names itself."""
    return {
        "decisions": [row[:3] for row in semantics["advice"]],
        **{
            name: sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()
            for name, part in semantics.items()
        },
    }


@pytest.mark.data
def test_golden_stations_forecast_advisor_and_replay_keep_their_output(
    session, settings, seeded, step
):
    seeded()
    step("2024-05-14 06:00")
    step(NOW)

    actual = _digests(_semantics(session, settings))

    assert actual == json.loads(GOLDEN.read_text())
