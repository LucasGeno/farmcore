"""Coverage for advisor plain-word and saved-evidence edge branches."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from farmcore.apps.advisor import routes, tools
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Advice, Asset
from farmcore.core.tools import ToolContext
from tests.apps.planning.conftest import NOW


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")


def test_incubation_limited_and_event_history_branches():
    limited = routes._incubation(
        [{"rule": "downy_incubation", "inputs": {"coverage": "limited"}}],
        NOW.isoformat(),
        "Europe/Paris",
    )
    assert "not evaluable" in limited
    history = routes._incubation(
        [
            {
                "rule": "downy_incubation",
                "inputs": {"event_history_coverage": "limited", "pct": 50},
            }
        ],
        NOW.isoformat(),
        "Europe/Paris",
    )
    assert "historical event coverage is limited" in history


def test_saved_assessment_rejects_an_unknown_or_mismatched_id(
    client, session, farm, parcels, advice
):
    other = Advice(
        farm_id=farm.id,
        parcel_id=parcels[1].id,
        evaluated_at=NOW,
        decision="spray",
        reasons=[],
        products=[],
        props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
    )
    session.add(other)
    session.flush()
    url = f"/advisor/parcels/{parcels[0].id}/assessments"
    assert client.get(f"{url}/0.json", params={"farm": farm.id}).status_code == 404
    assert client.get(f"{url}/{other.id}.json", params={"farm": farm.id}).status_code == 404
    assert client.get(f"{url}/{advice.id}.json", params={"farm": farm.id}).status_code == 200


def test_debug_panel_shows_an_empty_advice_for_a_parcel_without_one(client, farm, parcels):
    response = client.get(f"/advisor/debug/parcels/{parcels[0].id}", params={"farm": farm.id})
    assert response.status_code == 200


def test_limited_spray_keeps_its_action_and_says_so_on_the_map(client, session, farm, advice):
    session.add(Asset(farm_id=farm.id, kind="station", name="Synthetic", props={}))
    advice.decision = "spray"
    advice.props = {**advice.props, "assessment_status": "limited"}
    session.flush()
    response = client.get("/advisor/layer.geojson", params={"farm": farm.id})
    assert response.status_code == 200
    labels = [f["properties"]["label"] for f in response.json()["features"]]
    assert any("limited" in label.lower() for label in labels)


def test_status_tool_quotes_a_limited_no_action_advice(session, farm, parcels, advice):
    session.add(Asset(farm_id=farm.id, kind="station", name="Synthetic", props={}))
    advice.decision = "none"
    advice.props = {**advice.props, "assessment_status": "limited", "headline": "Limited"}
    session.flush()
    ctx = ToolContext(
        _repo(session, farm), NOW, ZoneInfo("Europe/Paris"), lang="en", member_role="owner"
    )
    result = tools.parcel_status(ctx, parcel=parcels[0].alias)
    assert result.text == f"{parcels[0].alias} {parcels[0].name}: Limited"
