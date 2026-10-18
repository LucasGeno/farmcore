"""Visible app providers compose farm/parcel provenance without invented facts."""

import pytest

from tests.core.test_activity import farm  # noqa: F401


def test_sources_panel_is_composed_on_today_and_the_map_parcel(client, session, farm):  # noqa: F811
    from farmcore.core.models.canonical import Parcel

    pid = session.query(Parcel.id).filter_by(farm_id=farm.id).first()[0]
    assert "/data-sources/today" not in client.get("/today/").text  # Expert off (N4)
    client.cookies.set("expert", "1")
    today = client.get("/today/").text
    assert "Data sources" in today
    assert "/data-sources/today" in today
    panel = client.get(f"/fields/panel/parcels/{pid}").text
    assert f"/data-sources/parcels/{pid}" in panel
    assert 'data-fragment-label="Data sources"' in panel
    from tests.apps.fields.test_panel import slots

    urls = [slot["urls"][0] for slot in slots(panel)]
    assert urls[0] == f"/advisor/map/parcels/{pid}?farm={farm.id}"
    assert urls[-1] == f"/data-sources/parcels/{pid}?farm={farm.id}"
    assert urls.index(f"/operations/map/parcels/{pid}?farm={farm.id}") < len(urls) - 1
    response = client.get("/data-sources/today")
    assert response.status_code == 200
    assert "Météo-France" in response.text
    assert "ISOXML" in response.text
    assert "Telegram" in response.text
    assert "—" in response.text


def test_station_counts_only_observed_values_of_the_farms_station(client, session, farm):  # noqa: F811
    from datetime import timedelta

    from farmcore.core.models.canonical import Asset, Observation, Parcel
    from tests.core.test_activity import NOW

    station = Asset(farm_id=farm.id, kind="station", name="Météo-France test")
    regional = Asset(farm_id=farm.id, kind="regional", name="Not a station")
    session.add_all([station, regional])
    session.flush()
    for asset, at, variable in [
        (station, NOW - timedelta(hours=1), "t_c"),
        (station, NOW - timedelta(hours=1), "ff_kmh"),
        (station, NOW + timedelta(hours=1), "t_c"),
        (regional, NOW, "t_c"),
    ]:
        session.add(Observation(asset_id=asset.id, ts=at, variable=variable, value=1))
    session.commit()
    pid = session.query(Parcel.id).filter_by(farm_id=farm.id).first()[0]
    for path in ("/data-sources/today", f"/data-sources/parcels/{pid}"):
        page = client.get(path).text
        row = page.split('data-source-summary="stations"', 1)[1].split("</li>", 1)[0]
        assert "data-source-count>2</span>" in row
        assert "10 Apr 2024 06:40" in row
        assert "Farm-wide station" in row


def test_forecast_counts_available_runs_not_future_values(client, session, farm):  # noqa: F811
    from datetime import timedelta

    from farmcore.apps.forecast.models import ForecastRun, ForecastValue
    from tests.core.test_activity import NOW

    for hours in (-24, -1, 1):
        run = ForecastRun(
            farm_id=farm.id,
            source="test-forecast",
            issued_at=NOW + timedelta(hours=hours - 1),
            available_at=NOW + timedelta(hours=hours),
        )
        session.add(run)
        session.flush()
        session.add(
            ForecastValue(
                farm_id=farm.id, run_id=run.id, ts=NOW + timedelta(days=2), variable="t_c", value=1
            )
        )
    session.commit()
    row = client.get("/data-sources/today").text.split('data-source-summary="forecast"')[1]
    assert "data-source-count>2</span>" in row
    assert "10 Apr 2024 06:40" in row


def test_receipt_counts_use_original_parcels_and_replay_availability(client, session, farm):  # noqa: F811
    from datetime import timedelta

    from farmcore.apps.operations.spray_weather import NAMESPACE
    from farmcore.core.models.canonical import LogSource, Parcel
    from tests.core.test_activity import NOW, _spray

    log = _spray(session, farm, ["P1", "P2"], NOW - timedelta(hours=2))
    pids = {p.alias: p.id for p in session.query(Parcel).filter_by(farm_id=farm.id)}

    def metadata(at, parcels):
        return {
            NAMESPACE: {
                "v": 1,
                "status": "unavailable",
                "reason": "weather_unavailable",
                "values": {},
                "data_clock": at.isoformat(),
                "parcel_ids": parcels,
            },
            "transcript": "private-source-content",
        }

    log.sources[0].raw_payload = metadata(NOW - timedelta(hours=1), [pids["P1"], pids["P2"]])
    for key, at, parcels in [
        ("a", NOW, [pids["P1"]]),
        ("b", NOW, [pids["P2"]]),
        ("future", NOW + timedelta(hours=1), [pids["P1"]]),
    ]:
        session.add(
            LogSource(
                log_id=log.id,
                source_type="isoxml",
                external_key=key,
                raw_payload=metadata(at, parcels),
                precision_rank=40,
            )
        )
    session.commit()
    for path, count in [
        ("/data-sources/today", 2),
        (f"/data-sources/parcels/{pids['P1']}", 1),
        (f"/data-sources/parcels/{pids['P2']}", 1),
    ]:
        page = client.get(path).text
        row = page.split('data-source-summary="operations"')[1].split("</li>")[0]
        assert f"data-source-count>{count}</span>" in row
        assert "10 Apr 2024 07:40" in row
        voice = page.split('data-source-summary="chat"')[1].split("</li>")[0]
        assert "data-source-count>1</span>" in voice
        assert "10 Apr 2024 06:40" in voice
        assert "private-source-content" not in page
    row = client.get(f"/data-sources/parcels/{pids['P3']}").text
    row = row.split('data-source-summary="operations"')[1].split("</li>")[0]
    assert "data-source-count>—</span>" in row


def test_hidden_or_absent_apps_never_resolve_their_providers(client, session, farm):  # noqa: F811
    from dataclasses import replace

    from farmcore.core.models.kernel import AppModule

    client.app.state.manifests = [
        replace(m, data_sources="not_installed:never_import") if m.name == "chat" else m
        for m in client.app.state.manifests
        if m.name != "forecast"
    ]
    session.get(AppModule, "chat").state = "uninstalled"
    session.commit()
    page = client.get("/data-sources/today")
    assert page.status_code == 200
    assert "Telegram" not in page.text
    assert 'data-source-summary="forecast"' not in page.text
    assert client.get("/data-sources/parcels/999999").status_code == 404


def test_legacy_receipt_is_unknown_not_a_fabricated_replay_sync(client, session, farm):  # noqa: F811
    from tests.core.test_activity import NOW, _spray

    _spray(session, farm, ["P1"], NOW)
    session.commit()
    row = client.get("/data-sources/today").text.split('data-source-summary="chat"')[1]
    assert "data-source-count>—</span>" in row
    assert "data-source-time>—</span>" in row
    assert "totals unknown" in row


def test_foreign_farm_and_pending_receipts_cannot_inflate_counts(client, session, farm):  # noqa: F811
    from farmcore.apps.operations.spray_weather import NAMESPACE
    from farmcore.core.models.canonical import Farm, Log, LogSource
    from tests.core.test_activity import NOW

    foreign = Farm(name="Other farm")
    session.add(foreign)
    session.flush()
    for farm_id, status in [(farm.id, "done"), (foreign.id, "done"), (farm.id, "pending")]:
        log = Log(
            farm_id=farm_id, name="Synthetic source test", type="spray", status=status, ts_start=NOW
        )
        session.add(log)
        session.flush()
        session.add(
            LogSource(
                log_id=log.id,
                source_type="isoxml",
                external_key=str(log.id),
                precision_rank=40,
                raw_payload={
                    NAMESPACE: {
                        "v": 1,
                        "status": "unavailable",
                        "reason": "weather_unavailable",
                        "values": {},
                        "data_clock": NOW.isoformat(),
                        "parcel_ids": [],
                    }
                },
            )
        )
    session.commit()
    row = client.get("/data-sources/today").text.split('data-source-summary="operations"')[1]
    assert "data-source-count>1</span>" in row


@pytest.mark.parametrize(
    "saved",
    [
        {"v": 1, "data_clock": "not-a-date", "parcel_ids": [1]},
        {"v": 99, "data_clock": "2024-04-10T04:00:00+00:00", "parcel_ids": [1]},
        {"v": 1, "data_clock": "2024-04-10T04:00:00+00:00"},
    ],
)
def test_unsupported_receipts_show_unknown_instead_of_failing(client, session, farm, saved):  # noqa: F811
    from farmcore.apps.operations.spray_weather import NAMESPACE
    from farmcore.core.models.canonical import Parcel
    from tests.core.test_activity import NOW, _spray

    log = _spray(session, farm, ["P1"], NOW)
    log.sources[0].raw_payload = {NAMESPACE: saved}
    session.commit()
    pid = session.query(Parcel.id).filter_by(farm_id=farm.id, alias="P1").scalar()
    for path in ["/data-sources/today", f"/data-sources/parcels/{pid}"]:
        response = client.get(path)
        assert response.status_code == 200
        row = response.text.split('data-source-summary="chat"')[1]
        assert "data-source-count>—</span>" in row
        assert "data-source-time>—</span>" in row
        assert "totals unknown" in row
