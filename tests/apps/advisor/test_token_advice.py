"""Advisor tools for agent tokens: an explicit `advice:read` reads the stored decision, anything
else keeps "Assessment limited".

Failure model (one owning test each):
- advice:read implied by, or not needed from, another scope (the tool seam, a token without it);
- stale advice read as current (a decision 3 h old comes back as a reason, not "spray");
- (a token's advice must not depend on whether the farm holds withheld weather: owned by
  tests/apps/share/test_recipient_identity.py::test_shared_advice_and_mcp_apply_the_same_weather_gate)
- a farm-level reason or a missing station is dropped for tokens;
- raw weather in the answer (`mm_since_spray`), another farm's advice, an unaudited read;
- the web assistant (owner repository) and MCP (token repository) tell different stories: both
  call the same tool function, so the owner and the token are compared on one stored row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from farmcore.apps.advisor import tools
from farmcore.apps.advisor.advice import Advice
from farmcore.core import mcp as farm_mcp
from farmcore.core import registry
from farmcore.core.access import (
    ScopedRepository,
    authenticate_token,
    ensure_owner_grant,
    issue_token,
)
from farmcore.core.clock import clock_row
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import AccessLog, Asset, DataGrant, Farm, Parcel
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.tools import ToolContext
from farmcore.core.weather import store
from farmcore.core.weather.contract import Fetched, Licence, ObservationValue, Request, Station

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture(scope="module")
def manifests():
    return registry.discover()


@pytest.fixture(autouse=True)
def _live_clock(session, settings):
    clock_row(session, settings).mode = "live"  # tests default to a 2024 replay clock
    session.flush()


def _farm(session, name: str, *, station: bool = True, props: dict | None = None):
    farm = Farm(name=name, props=props or {})
    session.add(farm)
    session.flush()
    parcel = Parcel(
        farm_id=farm.id, name="Bel-Air", alias="P1", area_ha=Decimal("1.25"), geom=SQUARE
    )
    session.add(parcel)
    if station:
        session.add(Asset(farm_id=farm.id, kind="station", name="Roof station"))
    session.flush()
    return farm, parcel


def _advice(session, farm, parcel, *, age_h: float = 0.2, decision: str = "protected", mm=4.2):
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=datetime.now(UTC) - timedelta(hours=age_h),
            decision=decision,
            reasons=[{"rule": "downy_3_10", "inputs": {"event_r48_mm": mm}}],
            protected_until=datetime.now(UTC) + timedelta(days=3),
            props={"assessment_status": "evaluable", "headline": "Protected", "mm_since_spray": mm},
        )
    )
    session.flush()


def _token(session, farm, scopes: list[str]) -> tuple[int, str]:
    now = datetime.now(UTC)
    grant = DataGrant(
        farm_id=farm.id,
        grantee="agent",
        scopes=scopes,
        purpose="my agent",
        valid_from=now,
        valid_to=now + timedelta(days=30),
    )
    owner = LocalIdentity(login=f"owner-{farm.id}", password_hash=hash_password("x" * 16))
    session.add_all([grant, owner])
    session.flush()
    session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
    session.flush()
    token, raw = issue_token(session, grant, kind="personal", created_by=owner.id)
    return token.id, raw


def _ctx(repo: ScopedRepository) -> ToolContext:
    return ToolContext(repo=repo, now=datetime.now(UTC), tz=PARIS)


def _as_token(session, raw: str) -> ToolContext:
    return _ctx(authenticate_token(session, raw))


def _as_owner(session, farm) -> ToolContext:
    repo = ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), "chat:1", "assistant"
    )
    return _ctx(repo)


def test_a_token_with_advice_read_reads_what_the_owner_reads_minus_raw_weather(
    session, session_factory, settings, manifests
):
    farm, parcel = _farm(session, "A")
    _advice(session, farm, parcel)
    other, other_parcel = _farm(session, "B")
    _advice(session, other, other_parcel, decision="spray")
    token_id, raw = _token(session, farm, ["advice:read", "fields:read"])

    via_mcp = farm_mcp.FarmTools(session_factory, settings, manifests).call(
        raw, "advisor_parcel_status", {"parcel": "P1"}
    )
    web = tools.parcel_status(_as_owner(session, farm), "P1")
    today = farm_mcp.FarmTools(session_factory, settings, manifests).call(raw, "advisor_today", {})

    assert via_mcp.facts["decision"] == web.facts["decision"] == "protected"
    assert web.facts["mm_since_spray"] == 4.2
    assert via_mcp.facts["mm_since_spray"] is None
    assert [p["decision"] for p in today.facts["parcels"]] == ["protected"]
    session.expire_all()
    read = session.scalars(
        select(AccessLog.resource).where(AccessLog.actor == f"token:{token_id}")
    ).all()
    assert {"advice", "mcp:advisor_parcel_status"} <= set(read)


def test_a_token_without_advice_read_gets_assessment_limited(session):
    farm, parcel = _farm(session, "A")
    _advice(session, farm, parcel)
    _, raw = _token(session, farm, ["fields:read", "stations:read", "logs:read"])

    result = tools.parcel_status(_as_token(session, raw), "P1")  # as the web assistant would

    assert set(result.facts) == {"unavailable"}
    assert result.text.startswith("Assessment limited")


def test_stale_advice_never_reads_as_current(session):
    farm, parcel = _farm(session, "A")
    _advice(session, farm, parcel, age_h=3, decision="spray")
    _, raw = _token(session, farm, ["advice:read", "fields:read"])

    result = tools.parcel_status(_as_token(session, raw), "P1")

    assert result.facts["decision"] == "none"
    assert "out of date" in result.text


def _withheld_value(session, farm) -> None:
    """One weather value on the farm, through a receipt that bars redistribution."""
    fetched = Fetched(
        request=Request(dataset="hourly"),
        fetched_at=datetime.now(UTC),
        payload=b"withheld",
        values=[],
    )
    receipt = store.record_receipt(
        session,
        provider="p",
        licence=Licence("CC-BY-NC-4.0", "Synthetic NC network", False),
        fetched=fetched,
        farm_id=farm.id,
    )
    station = store.upsert_station(session, "p", Station("S1", "Station 1", 44.9, -0.15))
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    store.store_observations(
        session,
        receipt,
        station,
        [ObservationValue("air_temperature", "degC", Decimal("12.5"), hour, hour)],
    )
    session.flush()


@pytest.mark.parametrize(
    ("kwargs", "withheld", "reason"),
    [
        (
            {"props": {"advisor_unavailable_reason": "Archive data only."}},
            False,
            "Archive data only.",
        ),
        ({"station": False}, False, "no weather station"),
        # Withheld weather reads as none (owner ruling 2026-11-22), not as a licence reason.
        ({"station": False}, True, "no weather station"),
    ],
    ids=["farm-level-reason", "no-station", "no-station-only-withheld-weather"],
)
def test_the_farm_level_caveats_still_withhold_advice_from_a_token(
    session, kwargs, withheld, reason
):
    farm, parcel = _farm(session, "A", **kwargs)
    if withheld:
        _withheld_value(session, farm)
    _advice(session, farm, parcel)
    _, raw = _token(session, farm, ["advice:read", "fields:read"])

    result = tools.today(_as_token(session, raw))

    assert set(result.facts) == {"unavailable"}
    assert reason in result.text
