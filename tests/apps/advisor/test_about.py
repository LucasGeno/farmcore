"""`/advisor/about`: what the advice is built from, in plain words, from stored data and config."""

import html
import re

import pytest
from sqlalchemy import func, select

from farmcore.core.models.canonical import Farm, Observation
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

VALIDATION = (
    "replayed on the 2024 and 2025 seasons of one farm against a calendar programme; "
    "not yet checked against observed disease records or other regions"
)


@pytest.mark.data
def test_about_names_region_data_models_sources_and_validation(client, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    page = html.unescape(client.get("/advisor/about").text)
    for needle in (
        "Saint-Émilion",
        "Bordeaux",
        "33394002",
        "Météo-France",
        "ECMWF",
        "Open-Meteo",
        "as issued",
        "RPG",
        "2024",
        "3-10 rule",
        "Baldacci",
        "Goidanich",
        "Gubler",
        "Thomas",
        VALIDATION,
    ):
        assert needle in page, needle


@pytest.mark.data
def test_about_data_dates_come_from_the_database(client, session, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    first, last = session.execute(select(func.min(Observation.ts), func.max(Observation.ts))).one()
    page = html.unescape(client.get("/advisor/about").text)
    section = page.split('data-about="data"', 1)[1].split('data-about="models"', 1)[0]
    assert f"{first.astimezone().year}" in section
    assert re.search(r"\d{1,2} \w{3} 2024", section)
    assert "forecast runs" in section.lower()


@pytest.mark.data
def test_about_states_assumptions_honestly(client, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    page = html.unescape(client.get("/advisor/about").text)
    assert "assumed" in page and "pending" in page.lower()


@pytest.mark.spanish_data
@pytest.mark.data
def test_about_withholds_advice_for_the_spanish_farm_and_says_why(client, session, seeded):
    seeded("B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    page = html.unescape(client.get("/advisor/about", params={"farm": es.id}).text)
    assert "withheld" in page.lower() and "InfoRiego" in page
    assert "Saint-Émilion" not in page
    assert VALIDATION in page


@pytest.mark.data
def test_about_is_readable_by_a_tour_and_has_no_text_under_12px(client, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    client.cookies.set("farmcore_tour", "1")
    response = client.get("/advisor/about")
    assert response.status_code == 200
    main = response.text.split('<main id="main"', 1)[1].split("</main>", 1)[0]
    assert not re.findall(r"(?<![:\w-])text-\[(?:[0-9]|1[01])px\]", main)


@pytest.mark.data
def test_about_is_linked_from_crop_protection_home_and_the_parcel_card(
    client, session, seeded, step
):
    from farmcore.core.models.canonical import Parcel

    seeded()
    step("2024-04-10 06:00")
    parcel = session.scalar(select(Parcel).order_by(Parcel.id))
    for path in ("/advisor/", "/today/", f"/advisor/parcels/{parcel.id}"):
        page = client.get(path).text
        assert 'href="/advisor/about"' in page, path


@pytest.mark.spanish_data
@pytest.mark.data
def test_parcel_source_is_farm_aware_sigpac_for_spain_rpg_for_france(client, session, seeded, step):
    seeded("B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    spain = html.unescape(client.get("/advisor/about", params={"farm": es.id}).text)
    parcels = spain.split("Parcels", 1)[1].split("</dl>", 1)[0]
    assert "SIGPAC 2024" in parcels and "FEGA/MAPA" in parcels
    assert "Junta de Castilla y León" in parcels
    assert "RPG" not in parcels and "IGN" not in parcels and "Synthetic" not in parcels
    step("2024-04-10 06:00")
    france = html.unescape(client.get("/advisor/about").text)
    parcels = france.split("Parcels", 1)[1].split("</dl>", 1)[0]
    assert "RPG 2024 (IGN)" in parcels and "SIGPAC" not in parcels


@pytest.mark.spanish_data
@pytest.mark.data
def test_spanish_archive_dates_go_through_the_date_formatter(client, session, seeded):
    seeded("B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    page = html.unescape(client.get("/advisor/about", params={"farm": es.id}).text)
    data = page.split('data-about="data"', 1)[1].split('data-about="models"', 1)[0]
    assert "1 Mar 2024 to 1 Nov 2024" in data
    assert "00:30" not in data and "2024-03-01" not in data


@pytest.mark.spanish_data
@pytest.mark.data
def test_station_network_label_comes_from_the_station(client, session, seeded, step):
    seeded("B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    spain = html.unescape(client.get("/advisor/about", params={"farm": es.id}).text)
    data = spain.split('data-about="data"', 1)[1].split('data-about="models"', 1)[0]
    assert "InfoRiego" in data and "Météo-France" not in data
    step("2024-04-10 06:00")
    france = html.unescape(client.get("/advisor/about").text)
    data = france.split('data-about="data"', 1)[1].split('data-about="models"', 1)[0]
    assert "Météo-France" in data and "33394002" in data and "InfoRiego" not in data


def test_equal_dates_collapse_to_one_date():
    from farmcore.apps.advisor.about import date_range

    assert date_range("7 Apr 2024", "7 Apr 2024") == "7 Apr 2024"
    assert date_range("1 Mar 2024", "8 Apr 2024") == "1 Mar 2024 to 8 Apr 2024"
    assert date_range(None, None) is None


@pytest.mark.data
def test_forecast_line_with_one_issue_date_has_no_x_to_x(client, seeded, step):
    seeded()
    step("2024-04-08 06:00")
    page = html.unescape(client.get("/advisor/about").text)
    forecast = page.split("ECMWF forecast as issued", 1)[1].split("Weather only", 1)[0]
    assert " to " not in forecast and "7 Apr 2024" in forecast
