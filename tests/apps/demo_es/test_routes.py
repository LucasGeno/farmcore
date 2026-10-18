import pytest
from sqlalchemy import select

from farmcore.apps.demo_es.archive import NOTICE
from farmcore.core.models.canonical import Asset, Parcel

pytestmark = [pytest.mark.data, pytest.mark.spanish_data]


def test_unverified_archive_is_explained_on_advice_and_station_surfaces(client, session, spanish):
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == spanish.id))
    station = session.scalar(select(Asset).where(Asset.farm_id == spanish.id))
    paths = [
        "/advisor/today",
        "/advisor/",
        "/advisor/map/farm",
        f"/advisor/map/parcels/{parcel.id}",
        f"/advisor/parcels/{parcel.id}",
        "/stations/today",
        "/stations/",
        f"/stations/{station.id}",
    ]
    for path in paths:
        response = client.get(path, params={"farm": spanish.id})
        assert response.status_code == 200
        assert NOTICE in response.text, path
        assert "No spray needed" not in response.text
    response = client.get("/data-sources/today", params={"farm": spanish.id})
    assert "InfoRiego" in response.text and "Météo-France" not in response.text
    assert "11385" in response.text
    assert "timestamp basis unverified" in response.text
    response = client.get("/forecast/today", params={"farm": spanish.id})
    assert "No forecast loaded" in response.text
    response = client.get("/fields/", params={"farm": spanish.id})
    assert "SIGPAC 2024 · ITACyL" in response.text
    assert "common ownership" in response.text


def test_native_archive_browser_keeps_source_times_units_gaps_and_farm_isolation(
    client, session, spanish
):
    station = session.scalar(select(Asset).where(Asset.farm_id == spanish.id))
    path = f"/demo_es/archive/{station.id}"
    response = client.get(path, params={"farm": spanish.id, "day": "2024-03-01"})
    assert response.status_code == 200
    assert 'data-source-time="0030"' in response.text
    assert "0.39" in response.text and "1.65" in response.text
    assert "m/s" in response.text and "W/m²" in response.text
    assert "UTC" in response.text and "unverified" in response.text
    assert f'name="farm" value="{spanish.id}"' in response.text
    response = client.get(path, params={"farm": spanish.id, "day": "2024-03-28"})
    assert "No source readings for this day" in response.text
    response = client.get(path, params={"farm": spanish.id, "day": "2024-10-31"})
    assert 'data-source-time="2400"' in response.text
    assert client.get(path).status_code == 404  # French default cannot read the Spanish station
    assert client.get(path, params={"farm": spanish.id, "day": "invalid"}).status_code == 422


@pytest.mark.parametrize("station_id", [2**63, -(2**63) - 1, 0])
def test_archive_rejects_station_ids_outside_the_database_key_range(client, spanish, station_id):
    response = client.get(f"/demo_es/archive/{station_id}", params={"farm": spanish.id})
    assert response.status_code == 422


def test_archive_displays_corrected_spanish_licences(client, session, spanish):
    import html
    import re

    station = session.scalar(select(Asset).where(Asset.farm_id == spanish.id))
    response = client.get(f"/demo_es/archive/{station.id}", params={"farm": spanish.id})
    assert response.status_code == 200
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", response.text)).split())
    assert (
        "SIGPAC 2024 · FEGA/MAPA and Junta de Castilla y León (ITACyL). "
        "Licence: CC BY 4.0 (FEGA, national) / IGCYL-NC "
        "(Junta de Castilla y León regional catalogue, non-commercial). "
        "Used for a non-commercial demo with attribution."
    ) in text
    assert (
        "InfoRiego · ITACyL, station VA07. Reuse terms set by ITACyL; "
        "not redistributed here, shown with attribution to ITACyL."
    ) in text
    assert "ODbL" not in text
    assert "free non-commercial" not in text.lower()
