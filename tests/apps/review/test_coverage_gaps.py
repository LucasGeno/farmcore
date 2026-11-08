"""Coverage for review helper validation, recommendations and the read-only tool."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.review import parameters, recommendations, tools
from farmcore.apps.review.models import ParameterVersion
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Advice
from farmcore.core.tools import ToolContext
from tests.apps.planning.conftest import NOW


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")


def _advice(session, farm, parcel):
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="spray",
        reasons=[],
        products=[],
        props={"assessment_status": "evaluable", "config_version": {"id": None, "status": "x"}},
    )
    session.add(row)
    session.flush()
    return row


def test_parameter_overrides_reject_empty_and_non_date_values():
    config = AdvisorConfig.for_region_year("FRI12", 2024)
    with pytest.raises(ValueError, match="at least one"):
        parameters.apply_overrides(config, {})
    with pytest.raises(ValueError, match="expected ISO date"):
        parameters.apply_overrides(config, {"season_gate": 20240405})


def test_recommendation_request_lookup_and_mismatched_answer(session, farm, parcels, advice):
    repo = _repo(session, farm)
    other = _advice(session, farm, parcels[1])
    with pytest.raises(recommendations.ReviewNotFound):
        recommendations.request(repo, NOW, 999999)
    state = recommendations.request_check(
        repo, NOW, advice, parcels[0], note="Check this", requested_by="owner"
    )
    with pytest.raises(recommendations.ReviewNotFound):
        recommendations.respond(
            repo,
            NOW,
            other,
            parcels[1],
            state.request.id,
            opinion="agree",
            note="Wrong row",
            reviewer="expert",
        )


def test_review_tool_unknown_parcel_and_parcel_without_advice(session, farm, parcels):
    from tests.apps.share.conftest import make_parcel

    repo = _repo(session, farm)
    ctx = ToolContext(repo, NOW, ZoneInfo("Europe/Paris"), lang="en", member_role="owner")
    unknown = tools.status(ctx, parcel="NOPE")
    assert "don't know that parcel" in unknown.text
    third = make_parcel(session, farm, 3)
    session.flush()
    empty = tools.status(ctx, parcel=third.alias)
    assert empty.facts["advice_id"] is None
    assert "no advice" in empty.text


def test_saved_config_unverifiable_branches(session, farm, parcels, advice):
    from farmcore.apps.advisor import service

    repo = _repo(session, farm)
    advice.props = {
        **advice.props,
        "config_version": {"id": 999999, "hash": "deadbeef"},
    }
    session.flush()
    assert service.saved_config(repo, advice) == (None, "unverifiable")

    version = ParameterVersion(
        farm_id=farm.id,
        crop="vine",
        region="FRI12",
        evaluation_year=2024,
        parent_version_id=None,
        created_by="test",
        evidence="Synthetic",
        overrides={},
        parameter_catalog={},
        effective_config={},
        config_hash="b" * 64,
        baseline=False,
    )
    session.add(version)
    session.flush()
    advice.props = {
        **advice.props,
        "config_version": {"id": version.id, "hash": "deadbeef"},
    }
    session.flush()
    assert service.saved_config(repo, advice) == (None, "unverifiable")
