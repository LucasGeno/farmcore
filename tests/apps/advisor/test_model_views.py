"""Model tabs show scoped, stored disease facts without changing advice or exports."""

import html

import pytest
from sqlalchemy import func, select

from farmcore.core.models.canonical import Advice, Farm
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


@pytest.mark.data
def test_model_switcher_shows_actual_april_driver_and_keeps_farm(client, session, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    farm_id = session.scalar(select(Farm.id).order_by(Farm.id))
    before = session.scalar(select(func.count()).select_from(Advice))
    downy = html.unescape(client.get("/advisor/", params={"model": "downy", "farm": farm_id}).text)
    assert 'aria-label="Crop protection models"' in downy
    assert f"/advisor/?model=powdery&farm={farm_id}" in downy
    assert 'data-model="downy"' in downy and 'data-model="powdery"' not in downy
    assert "Spray copper" in downy and "downy_3_10" in downy
    powdery = client.get("/advisor/?model=powdery").text
    assert "outside powdery period" in powdery and "No powdery pass due" in powdery
    assert "Spray copper" not in powdery
    assert "Gubler" in powdery
    assert "disabled" in powdery and "Insects (not modelled yet)" in powdery
    assert session.scalar(select(func.count()).select_from(Advice)) == before


@pytest.mark.spanish_data
@pytest.mark.parametrize("model", ["all", "downy", "powdery"])
@pytest.mark.data
def test_models_withhold_spain_and_preserve_switcher(client, session, seeded, model):
    seeded("B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    page = html.unescape(client.get("/advisor/", params={"farm": es.id, "model": model}).text)
    assert 'aria-label="Crop protection models"' in page
    assert "Crop protection unavailable" in page and "InfoRiego" in page
    assert "Les Granges" not in page and "Spray copper" not in page
    assert 'data-model="downy"' not in page and 'data-model="powdery"' not in page


def test_unmodelled_insects_cannot_be_selected(client, session, settings):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    assert client.get("/advisor/?model=insects").status_code == 422


@pytest.mark.data
def test_all_risks_keeps_aggregate_and_missing_reasons_are_not_safe(client, seeded):
    seeded()
    client.cookies.set("expert", "1")  # model checklists are Expert only (N4)
    page = client.get("/advisor/").text
    assert 'id="farm-summary"' in page
    assert page.count('data-home-parcel="') == 10
    assert "Not evaluated" in page
    assert "No downy pass due" not in page


@pytest.mark.data
def test_tour_can_read_models_and_app_off_removes_entry(client, seeded, step):
    seeded()
    step("2024-04-10 06:00")
    client.cookies.set("farmcore_tour", "1")
    page = client.get("/advisor/?model=downy").text
    assert "Spray copper" in page and "Log a spray" not in page
    assert "https://t.me/" not in page
    client.cookies.clear()
    for name in ("kpi", "chat", "forecast", "advisor"):
        assert client.post(f"/apps/{name}/uninstall").status_code == 200
    home = client.get("/today/").text
    assert 'hx-get="/advisor/today' not in home


@pytest.mark.data
def test_filtered_model_keeps_its_stored_actions_before_protected_parcels(
    client, session, seeded, step
):
    import re

    from farmcore.apps.advisor.demo import apply_live_loop

    seeded("B")
    farm = session.scalar(select(Farm).order_by(Farm.id))
    apply_live_loop(session, farm, scenes.local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    downy = client.get("/advisor/?model=downy").text
    powdery = client.get("/advisor/?model=powdery").text

    def aliases(page):
        return re.findall(r'data-home-parcel="([^"]+)"', page)

    assert aliases(downy)[:3] == ["P5", "P6", "P7"]
    assert aliases(powdery)[:3] == ["P1", "P2", "P3"]
