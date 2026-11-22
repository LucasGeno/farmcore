"""On-farm sensors push readings over HTTP; they join the merge layer as the on-farm tier.

Failure model (one owning test each):
- a request without a usable device token stores readings: no token, a forged one, an agent
  token (read scopes), a revoked device (refusals table);
- a device token reads farm data through the agent paths (it may only push);
- a payload the schema cannot hold honestly is stored, in part or whole: an unknown unit or
  variable, a reading from the future, soil moisture without its depth, an hour that is not an
  hour, a value outside its physical range, an oversized body, an unknown format (refusals
  table; one bad reading refuses the whole payload);
- a unit is converted wrongly, or the reading lands below a national or model source in the merge
  (JSON test); the same reading pushed twice, or as CSV and JSON, is stored twice (CSV test);
- one device floods the endpoint (rate limit, per device).
Synthetic values only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.share import service as share
from farmcore.apps.weather_push import routes
from farmcore.apps.weather_push.devices import register_device
from farmcore.core.access import (
    ScopedRepository,
    TokenRejected,
    authenticate_token,
    ensure_owner_grant,
    repo_for_job,
)
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import AccessLog, DataGrant
from farmcore.core.models.identity import AccessToken, FarmMembership, LocalIdentity
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt, WeatherStation
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather import merge, store
from farmcore.core.weather.contract import (
    Fetched,
    Licence,
    ObservationValue,
    Request,
    Station,
    Tier,
)
from farmcore.main import create_app

PATH = "/weather_push/observations"
PASSWORD = "synthetic-sensor-owner-password"  # gauntlet:allow-secret synthetic credential
SOIL = "volume_fraction_of_condensed_water_in_soil"
HOUR = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _stream(name, symbol, result, at=HOUR, depth=None):
    stream = {
        "ObservedProperty": {"name": name},
        "unitOfMeasurement": {"symbol": symbol},
        "Observations": [
            {"phenomenonTime": at if isinstance(at, str) else _iso(at), "result": result}
        ],
    }
    if depth is not None:
        stream["properties"] = {"depth_m": depth}
    return stream


def _thing(*streams):
    return {"name": "North block station", "Datastreams": list(streams)}


GOOD = _stream("air_temperature", "degC", 12.0)


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    session.commit()
    return farm


@pytest.fixture
def owner(session, farm):
    identity = LocalIdentity(login="sensor-owner", password_hash=hash_password(PASSWORD))
    session.add(identity)
    session.flush()
    session.add(FarmMembership(identity_id=identity.id, farm_id=farm.id, role="owner"))
    session.commit()
    return identity


def _device(session, farm, owner, name="North block station"):
    grant, raw = register_device(
        session, farm.id, created_by=owner.id, name=name, latitude=44.9, longitude=-0.15
    )
    session.commit()
    return grant, raw


@pytest.fixture
def device(session, farm, owner):
    return _device(session, farm, owner)


@pytest.fixture
def http(settings, session_factory):
    # No operator header and no cookie: a device is neither.
    with TestClient(create_app(settings, session_factory=session_factory)) as c:
        yield c


def _push(http, raw, body, content_type="application/json"):
    headers = {"Content-Type": content_type}
    if raw is not None:
        headers["Authorization"] = f"Bearer {raw}"
    if isinstance(body, list):  # chunks, sent without a Content-Length
        data = iter(body)
    else:
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return http.post(PATH, content=data, headers=headers)


def _observations(session):
    session.expire_all()
    return session.scalars(select(WeatherObservation).order_by(WeatherObservation.id)).all()


def test_json_readings_convert_units_and_lead_the_merge_as_on_farm(
    http, session, farm, device
) -> None:
    national = store.upsert_station(
        session, "meteofrance", Station("31069001", "Toulouse", 44.8, -0.1)
    )
    receipt = store.record_receipt(
        session,
        provider="meteofrance",
        licence=Licence("etalab-2.0", "Météo-France", True),
        fetched=Fetched(Request("hourly"), HOUR, b"synthetic national", []),
        farm_id=farm.id,
        tier=Tier.NATIONAL,
    )
    store.store_observations(
        session,
        receipt,
        national,
        [ObservationValue("air_temperature", "degC", Decimal("9"), HOUR, HOUR)],
    )
    session.commit()

    pushed = _push(
        http,
        device[1],
        _thing(
            _stream("air_temperature", "K", 285.15),
            {
                **_stream("relative_humidity", "%", 81),
                "ObservedProperty": {
                    "definition": "http://vocab.nerc.ac.uk/standard_name/relative_humidity/"
                },
            },
            _stream(SOIL, "m3 m-3", 0.315, depth=0.3),
        ),
    )

    assert pushed.status_code == 201, pushed.text
    assert pushed.json()["readings"] == 3
    merged = merge.farm_hourly(
        repo_for_job(session, farm.id, "test"), HOUR, HOUR, None, datetime.now(UTC)
    )
    [hour] = merged.hours
    assert (hour.values["t_c"], hour.tiers["t_c"]) == (12.0, Tier.ON_FARM)
    assert (hour.values["rh_pct"], hour.tiers["rh_pct"]) == (81.0, Tier.ON_FARM)
    soil = session.scalars(
        select(WeatherObservation).where(WeatherObservation.variable == SOIL)
    ).one()
    assert (soil.unit, soil.value, soil.provisional) == ("%", Decimal("31.5"), False)
    probe = session.get(WeatherStation, soil.station_id)
    assert probe.provider_station_id.endswith("@0.3m") and probe.owner_farm_id == farm.id
    pushed_receipt = session.get(WeatherReceipt, soil.receipt_id)
    assert pushed_receipt.request["tier"] == "on-farm" and pushed_receipt.redistribution
    grant, raw = device
    token = session.scalars(select(AccessToken).where(AccessToken.grant_id == grant.id)).one()
    [audit] = session.scalars(select(AccessLog).where(AccessLog.actor.like("token:%"))).all()
    assert (audit.actor, audit.farm_id, audit.grant_id, audit.resource, audit.scope) == (
        f"token:{token.id}",
        farm.id,
        grant.id,
        "weather_observation",
        "weather:push",
    )
    assert audit.purpose == grant.purpose and raw not in repr(audit.__dict__)


def test_the_same_reading_as_csv_or_json_or_twice_is_stored_once(http, session, device) -> None:
    csv = (
        "observed_property,unit,phenomenon_time,result,depth_m\n"
        f"air_temperature,degC,{_iso(HOUR)},12.0,\n"
        f"{SOIL},%,{_iso(HOUR)},31.5,0.30\n"
    )

    first = _push(http, device[1], csv, "text/csv")
    again = _push(http, device[1], csv, "text/csv")
    as_json = _push(http, device[1], _thing(GOOD, _stream(SOIL, "%", 31.5, depth=0.3)))

    assert [r.status_code for r in (first, again, as_json)] == [201, 201, 201]
    rows = _observations(session)
    assert sorted((r.variable, r.value) for r in rows) == [
        ("air_temperature", Decimal("12.0")),
        (SOIL, Decimal("31.5")),
    ]


FUTURE = HOUR + timedelta(hours=4)
NEXT = HOUR + timedelta(hours=3)  # the top of the coming hour


@pytest.mark.parametrize(
    ("token", "body", "content_type", "status"),
    [
        ("none", _thing(GOOD), "application/json", 401),
        ("forged", _thing(GOOD), "application/json", 401),
        ("agent", _thing(GOOD), "application/json", 401),
        ("revoked", _thing(GOOD), "application/json", 401),
        ("device", b"{" + b" " * routes.MAX_BYTES + b"}", "application/json", 413),
        ("device", [b"{", b" " * routes.MAX_BYTES, b"}"], "application/json", 413),
        ("device", _thing(GOOD), "application/xml", 415),
        ("device", _thing(GOOD, _stream("air_temperature", "furlong", 3)), "application/json", 422),
        (
            "device",
            _thing(GOOD, _stream("sea_surface_temperature", "degC", 3)),
            "application/json",
            422,
        ),
        (
            "device",
            _thing(GOOD, _stream("air_temperature", "degC", 3, FUTURE)),
            "application/json",
            422,
        ),
        (
            "device-2-min-early",
            _thing(GOOD, _stream("air_temperature", "degC", 3, NEXT)),
            "application/json",
            422,
        ),
        ("device", _thing(GOOD, _stream(SOIL, "%", 30)), "application/json", 422),
        (
            "device",
            _thing(GOOD, _stream("air_temperature", "degC", 3, HOUR + timedelta(minutes=7))),
            "application/json",
            422,
        ),
        ("device", _thing(GOOD, _stream("relative_humidity", "%", 140)), "application/json", 422),
        (
            "device",
            _thing(GOOD, _stream("lwe_thickness_of_precipitation_amount", "mm", 1)),
            "application/json",
            422,
        ),
        (
            "device",
            "observed_property,unit,phenomenon_time,result,depth_m\nair_temperature,degC,yesterday,1,\n",
            "text/csv",
            422,
        ),
        ("device", _thing(GOOD, _stream("air_temperature", "degC", 13)), "application/json", 422),
        ("device", json.dumps(_thing(GOOD)).replace("12.0", "NaN"), "application/json", 422),
    ],
    ids=[
        "no-token",
        "forged-token",
        "agent-token",
        "revoked-device",
        "oversized",
        "oversized-unannounced",
        "unknown-format",
        "unknown-unit",
        "unknown-variable",
        "future",
        "hour-not-yet-ended",
        "soil-without-depth",
        "not-on-the-hour",
        "out-of-range",
        "sum-over-an-instant",
        "csv-bad-time",
        "same-reading-twice",
        "not-a-number",
    ],
)
def test_refused_pushes_store_nothing(
    http, session, farm, owner, device, token, body, content_type, status, monkeypatch
) -> None:
    raw = {"device": device[1], "none": None, "forged": "fc_pat_" + "A" * 43}.get(token)
    if token == "device-2-min-early":  # received at 23:58 for a reading of 00:00
        raw = device[1]
        monkeypatch.setattr(routes, "_now", lambda: NEXT - timedelta(minutes=2))
    if token == "agent":
        repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "t")
        _grant, raw = share.create_token(
            repo,
            creator_id=owner.id,
            name="Agent",
            scopes=["stations:read"],
            purpose="read",
            valid_to=datetime.now(UTC) + timedelta(days=3),
            tz="UTC",
        )
        session.commit()
    if token == "revoked":
        grant, raw = device
        session.get(DataGrant, grant.id).revoked_at = datetime.now(UTC)
        session.commit()

    refused = _push(http, raw, body, content_type)

    assert refused.status_code == status, refused.text
    if status == 401:
        assert refused.headers["WWW-Authenticate"].startswith("Bearer")
    assert _observations(session) == []
    assert session.scalar(select(func.count()).select_from(WeatherReceipt)) == 0
    assert session.scalar(select(func.count()).select_from(AccessLog)) == 0


def test_a_device_token_only_pushes(session, device) -> None:
    with pytest.raises(TokenRejected):
        authenticate_token(session, device[1])


def test_each_device_is_rate_limited_on_its_own(
    http, session, farm, owner, device, monkeypatch
) -> None:
    monkeypatch.setattr(routes, "RATE_PER_MINUTE", 2)
    _other_grant, other = _device(session, farm, owner, name="South block station")

    statuses = [_push(http, device[1], _thing(GOOD)).status_code for _ in range(3)]
    refused = _push(http, device[1], _thing(GOOD))

    assert statuses == [201, 201, 429]
    assert int(refused.headers["Retry-After"]) >= 1
    assert _push(http, other, _thing(GOOD)).status_code == 201


def test_forged_tokens_are_limited_per_address_before_any_lookup(http, monkeypatch) -> None:
    monkeypatch.setattr(routes, "RATE_PER_ADDRESS", 2)
    looked_up = []
    monkeypatch.setattr(
        routes,
        "authenticate_device",
        lambda *a: looked_up.append(1) or (_ for _ in ()).throw(TokenRejected()),
    )
    forged = "fc_pat_" + "B" * 43

    statuses = [_push(http, forged, _thing(GOOD)).status_code for _ in range(3)]

    assert statuses == [401, 401, 429]
    assert len(looked_up) == 2
