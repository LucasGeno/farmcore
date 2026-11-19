"""A live tick stores the farm's Météo-France station hours: the last 24 from DPObs, older
missing hours from the DPClim archive, under the station the replay already uses. The
credentials never reach a receipt, the station's health or a log line."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from farmcore.apps.stations import service
from farmcore.apps.stations.provider import STATION
from farmcore.apps.weather_meteofrance.ingest import JOB_NAME
from farmcore.apps.weather_meteofrance.manifest import MANIFEST
from farmcore.apps.weather_meteofrance.provider import MeteoFranceLive, PortalError
from farmcore.core import clock
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Asset, Farm
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt
from farmcore.core.weather.contract import TimeRange
from farmcore.settings import Settings
from tests.apps.weather_meteofrance.conftest import CLIENT_ID, CLIENT_SECRET
from tests.task_drain import drain


def _tick(session, session_factory, settings) -> Asset:
    clock.set_live(session, settings)
    session.commit()
    run_now(session_factory, [MANIFEST], JOB_NAME, settings)
    drain(session_factory, settings, [MANIFEST])
    session.expire_all()
    return session.scalar(select(Asset).where(Asset.kind == "station"))


def _stored(session, dataset: str) -> set[tuple[str, Decimal, str]]:
    rows = session.execute(
        select(WeatherObservation.variable, WeatherObservation.value, WeatherObservation.qc_flag)
        .join(WeatherReceipt, WeatherReceipt.id == WeatherObservation.receipt_id)
        .where(WeatherReceipt.dataset == dataset)
    )
    return {(v, value.normalize(), qc) for v, value, qc in rows}


def test_a_live_tick_stores_recent_hours_and_backfills_the_archive_under_the_replay_station(
    session, session_factory, credentials, station, portal
) -> None:
    portal()

    asset = _tick(session, session_factory, credentials)

    # DPObs: kelvin and m/s converted; one hour per call over the 24-hour window.
    assert _stored(session, "DPObs/v2/station/horaire") == {
        ("air_temperature", Decimal("10.6"), "unchecked"),
        ("relative_humidity", Decimal("89"), "unchecked"),
        ("wind_speed", Decimal("11.16"), "unchecked"),
        ("lwe_thickness_of_precipitation_amount", Decimal("0.4"), "unchecked"),
    }
    # DPClim: decimal commas, quality codes (1 validated, 0 protected, 2 doubtful), an empty
    # cell stored as nothing.
    assert _stored(session, "DPClim/v1/commande-station/horaire") == {
        ("lwe_thickness_of_precipitation_amount", Decimal("0.2"), "passed"),
        ("wind_speed", Decimal("9.36"), "passed"),
        ("air_temperature", Decimal("9.8"), "passed"),
        ("relative_humidity", Decimal("94"), "passed"),
        ("lwe_thickness_of_precipitation_amount", Decimal("0"), "passed"),
        ("air_temperature", Decimal("10.4"), "suspect"),
        ("relative_humidity", Decimal("92"), "passed"),
    }
    receipts = session.scalars(select(WeatherReceipt)).all()
    assert {(r.provider, r.licence, r.redistribution) for r in receipts} == {
        ("meteofrance-portal", "etalab-2.0", True)
    }
    assert all(CLIENT_SECRET not in json.dumps(r.request) for r in receipts)
    # The stations app reads the hours through the station it already knows: 24 + 2 hours.
    farm = session.get(Farm, asset.farm_id)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    now = datetime.now(UTC)
    assert len(service.load_hourly(repo, asset.id, now - timedelta(days=2), now)) == 26


@pytest.mark.parametrize(
    ("fail", "stored"),
    # A refused token stores nothing; a failed archive still stores the 24 recent hours (the
    # archive gap is ordered again on the next tick).
    [("token", 0), ("archive", 24 * 4)],
    ids=["token-refused", "archive-failed"],
)
def test_a_failed_live_tick_records_why_without_the_credentials(
    session, session_factory, credentials, station, portal, caplog, fail, stored
) -> None:
    portal(fail=fail)

    asset = _tick(session, session_factory, credentials)

    assert session.scalar(select(func.count(WeatherObservation.id))) == stored
    health = asset.props["meteofrance_live"]
    assert health["error"]
    for secret in (CLIENT_ID, CLIENT_SECRET):
        assert secret not in json.dumps(asset.props)
        assert secret not in caplog.text


def test_the_client_secret_is_refused_over_plain_http_without_echoing_it() -> None:
    # Short, plain and last: pydantic prints the ends of its input beside the refusal.
    with pytest.raises(ValidationError) as refused:
        Settings(
            _env_file=None, meteofrance_token_url="http://x", meteofrance_client_secret="s3cr3t"
        )
    assert "s3cr3t" not in str(refused.value)


def _one_window(portal, **kw):
    """The provider alone over the fake portal, for one window."""
    fake = portal(**kw)
    live = MeteoFranceLive(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        base_url="https://public-api.meteofrance.fr/public",
        token_url="https://portail-api.meteofrance.fr/token",
        timeout=0.2,
    )
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return fake, live, STATION, end


def test_a_token_refused_by_a_data_call_is_renewed_once(portal) -> None:
    fake, live, station, end = _one_window(portal, expired=1)

    fetched = live.fetch_observations(station, TimeRange(end - timedelta(hours=1), end))

    assert fake.tokens == 2
    assert len(fetched.values) == 4


def test_an_archive_order_that_is_never_ready_ends_out_of_time(portal) -> None:
    fake, live, station, end = _one_window(portal, pending=10**9)
    old = end - timedelta(hours=30)

    with pytest.raises(PortalError, match="out of time"):
        live.fetch_observations(station, TimeRange(old - timedelta(hours=2), old))
