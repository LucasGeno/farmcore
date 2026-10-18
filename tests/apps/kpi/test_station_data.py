"""`/kpi/kpis.json` on the open St-Émilion station file (Météo-France, Licence Ouverte).

Expected values are recomputed in the test from `simulate` + `calendar_passes` over
`season_span` (no pinned constants), so a config change moves both sides.
"""

from __future__ import annotations

import pytest

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import calendar_passes, season_span, simulate
from farmcore.apps.advisor.engine.types import HourlyObs
from farmcore.apps.stations.connector import FILE, read_hours
from tests.apps.kpi.synth import seed_farm

pytestmark = pytest.mark.data


def test_kpis_json_matches_simulate_and_calendar_on_the_station_file(
    client, settings, session, data_dir
):
    path = data_dir / FILE
    if not path.is_file():
        pytest.skip(f"{FILE} is absent")
    seed_farm(session, settings)
    series = [HourlyObs(ts=h.ts, **h.values) for h in read_hours(path)]

    body = client.get("/kpi/kpis.json").json()

    assert [y["year"] for y in body["years"]] == [2024, 2025]
    for y in body["years"]:
        cfg = AdvisorConfig.for_region_year("FRI12", y["year"])
        model = simulate(series, cfg, *season_span(cfg))
        cal = calendar_passes(cfg, y["year"])
        assert (y["start"], y["end"]) == tuple(d.isoformat() for d in season_span(cfg))
        assert (y["model"]["passes"], y["model"]["treatments"]) == (model.passes, model.treatments)
        calendar = (y["calendar"]["passes"], y["calendar"]["treatments"])
        assert calendar == (cal.passes, cal.treatments)
        assert y["model"]["treatments"] < 20.1
        kpi = {k["key"]: k["value"] for k in y["kpis"]}
        assert kpi["treatments_avoided"] == pytest.approx(20.1 - model.treatments)
        assert kpi["passes_avoided"] == cal.passes - model.passes


def test_2024_copper_lead_on_the_station_file(client, settings, session, data_dir):
    """Vs the plain 10-day copper calendar (the "at least" figure) 3 avoided, −30 %,
    0.99 kg Cu/ha; vs the 12b rapid-growth calendar model 7 vs 13, 6 avoided, −46 %, 1.98."""
    if not (data_dir / FILE).is_file():
        pytest.skip(f"{FILE} is absent")
    seed_farm(session, settings)

    plain, rapid = client.get("/kpi/kpis.json").json()["lead"]["calendars"]

    assert (plain["model_copper"], plain["calendar_copper"], plain["avoided"]) == (7, 10, 3)
    assert round(plain["pct"]) == -30
    assert plain["kg_cu_ha"] == pytest.approx(0.99)
    assert (rapid["model_copper"], rapid["calendar_copper"], rapid["avoided"]) == (7, 13, 6)
    assert round(rapid["pct"]) == -46
    assert rapid["kg_cu_ha"] == pytest.approx(1.98)
