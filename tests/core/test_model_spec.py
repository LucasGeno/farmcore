"""The agronomic model contract (`core/model_spec.py`): every model behind an assessment declares
what it is and how far to trust it, a missing declaration fails the start, and the validation
level shows beside every stored result and in its JSON.

Failure model (one test per named failure, at the layer that catches it):
- F1 a spec omits a declaration (method, codes, stages, validation...) and loads anyway;
- F2 a parameter carries no citation without saying it is unsourced (an invented-looking value);
- F3 a validation level above `unvalidated` is claimed without the fields that back it;
- F4 an assessment kind loads with no model, or names something that is not a spec, so its
  results would show no level; two apps declare different models under one key;
- F5 a stored result claims a level its models did not have when it was computed;
- F6 a surface shows or exports a stored result without its validation level;
- F7 the About view says something other than the spec, or 403s for a share recipient;
- F8 an engine threshold is used by a rule but declared in no spec.
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from farmcore.apps.advisor import tools as advisor_tools
from farmcore.apps.advisor.advice import Advice
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.specs import DOWNY_3_10, SPECS
from farmcore.apps.review import recommendations
from farmcore.apps.review import tools as review_tools
from farmcore.apps.share import service as share
from farmcore.apps.share.service import shared_advice
from farmcore.core import assessments
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.clock import set_replay
from farmcore.core.manifest import AssessmentKind, Manifest
from farmcore.core.model_spec import (
    Method,
    ModelInput,
    ModelSpec,
    ModelSpecInvalid,
    Parameter,
    Stages,
    Validation,
)
from farmcore.core.registry import resolve_order
from farmcore.core.seed import ensure_default_farm
from farmcore.core.timeline import post_event
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.chat.assistant.toolkit import tool_context
from tests.apps.operations.conftest import make_parcel
from tests.apps.share.conftest import (  # noqa: F401  (fixtures)
    grantee_client,
    grantee_identity,
    share_password_hash,
)

NOW = datetime(2024, 5, 10, 4, tzinfo=UTC)
ADVISOR_MODELS = [s.key for s in SPECS]

# A spec that loads. Its unknown-source parameter stays honest: marked unsourced, no citation.
FROST = ModelSpec(
    key="frost.hours",
    title="Frost hours",
    method=Method("Hours below a threshold", "Invented test citation"),
    host="VITVI",
    pests=("PLASVI",),
    stages=Stages(5, 19),
    inputs=(ModelInput("air temperature", "°C", "hourly", "measured"),),
    parameters=(
        Parameter("threshold_c", 0.0, "°C", "Invented test citation"),
        Parameter("min_minutes", 30, "min", status="unsourced"),
    ),
    validation=Validation("unvalidated"),
    provenance="test fixture",
)
OTHER_FROST = replace(FROST, title="Another model under the same key")


class FrostHours(BaseModel):
    hours: int


FROST_KIND = AssessmentKind(
    "frost.hours", 1, f"{__name__}:FrostHours", models=(f"{__name__}:FROST",)
)


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"validation": Validation()}, "missing validation level"),  # F1
        ({"method": Method()}, "missing method"),  # F1
        ({"pests": ()}, "missing pest EPPO codes"),  # F1
        ({"stages": Stages(61, 13)}, "missing BBCH stages"),  # F1: end before start
        ({"parameters": (Parameter("threshold_c", 0.0, "°C"),)}, "has no source"),  # F2
        (
            {"parameters": (Parameter("threshold_c", 0.0, "°C", "x", status="unsourced"),)},
            "unsourced but names a source",
        ),  # F2
        (
            {"validation": Validation("literature-evaluated", "free text", region="FRI12")},
            "needs what backs it: years",
        ),  # F3
        ({"validation": Validation("official-service", "x")}, "needs what backs it: service"),
    ],
)
def test_a_model_missing_a_declaration_fails_with_a_clear_error(change, says):
    with pytest.raises(ModelSpecInvalid, match=rf"model 'frost\.hours': .*{says}"):
        replace(FROST, **change)


@pytest.mark.parametrize(
    ("kinds", "shown", "says"),
    [
        ((replace(FROST_KIND, models=()),), (), "declares no model"),
        (
            (replace(FROST_KIND, models=("farmcore.core.model_spec:Method",)),),
            (),
            "not a ModelSpec",
        ),
        (
            (
                FROST_KIND,
                AssessmentKind(
                    "frost.days", 1, "pydantic:BaseModel", models=(f"{__name__}:OTHER_FROST",)
                ),
            ),
            (),
            "'frost.hours' is declared twice",
        ),
        # A model the app only shows (Manifest.model_specs) is checked the same way.
        ((FROST_KIND,), ("farmcore.core.model_spec:Method",), "not a ModelSpec"),
        ((FROST_KIND,), (f"{__name__}:OTHER_FROST",), "'frost.hours' is declared twice"),
    ],
)
def test_an_assessment_kind_without_valid_models_fails_at_load(kinds, shown, says):  # F4
    app = Manifest(name="frost", version="0.1.0", assessment_kinds=kinds, model_specs=shown)
    with pytest.raises(ModelSpecInvalid, match=says):
        resolve_order([CORE, app])
    assert resolve_order([CORE, replace(app, assessment_kinds=(FROST_KIND,), model_specs=())])


def test_every_engine_threshold_is_declared_in_a_crop_protection_spec():  # F8
    declared = {p.name for spec in SPECS for p in spec.parameters}
    assert set(AdvisorConfig.threshold_names()) <= declared


@pytest.fixture
def scene(session, settings):
    """One parcel with a stored `wait` advice stamped as the shipped models compute it, its
    activity event and an open advisor-check request."""
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    parcel = make_parcel(session, farm, 301, area="1.5", props={"variety": "Merlot"})
    advice = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="wait",
        props={
            "headline": "Synthetic stored current advice",
            "assessment_status": "evaluable",
            "model_validation": {"level": "unvalidated", "models": ADVISOR_MODELS},
        },
        reasons=[],
    )
    session.add(advice)
    session.flush()
    post_event(
        session,
        farm_id=farm.id,
        res_model="parcel",
        res_id=parcel.id,
        kind="advice.changed",
        body=f"{parcel.alias}: none → wait.",
        payload={"parcel_id": parcel.id, "alias": parcel.alias, "old": "none", "new": "wait"},
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=NOW,
    )
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    recommendations.request_check(repo, NOW, advice, parcel, note="", requested_by="owner")
    set_replay(session, NOW, settings)
    session.commit()
    return farm, parcel, advice


def _decisions_without_validation(body) -> list:
    """Every JSON object with a `decision` that lacks a stamped `model_validation`."""
    found = []
    if isinstance(body, dict):
        if "decision" in body and (body.get("model_validation") or {}).get("level") is None:
            found.append(body)
        for value in body.values():
            found += _decisions_without_validation(value)
    elif isinstance(body, list):
        for value in body:
            found += _decisions_without_validation(value)
    return found


PAGES = [
    "/advisor/parcels/{pid}",
    "/advisor/",
    "/advisor/today?view=full",
    "/advisor/today?view=attention",
    "/advisor/map/farm",
    "/advisor/map/parcels/{pid}",
    "/fields/list",
    "/fields/today",  # the map popup's badge template
    "/activity/",
    "/activity/today",
    "/notifications/",
    "/review/assessments/{aid}",
    "/review/recommendations/",
]
JSONS = [
    "/advisor/parcels/{pid}.json",
    "/advisor/parcels/{pid}/assessments/{aid}.json",
    "/advisor/layer.geojson",
    "/fields/parcels.geojson",
    "/review/assessments/{aid}.json",
]


@pytest.mark.parametrize("url", PAGES + JSONS + ["tool:en", "tool:fr"])
def test_every_surface_shows_a_stored_results_validation_level(
    request, session, settings, scene, url
):  # F6
    farm, parcel, advice = scene
    if url.startswith("tool:"):
        lang = url.split(":")[1]
        ctx = tool_context(session, settings, 4242, NOW, lang=lang)
        results = [
            advisor_tools.today(ctx),
            advisor_tools.parcel_status(ctx, parcel.alias),
            review_tools.status(replace(ctx, member_role="owner"), parcel.alias),
        ]
        for result in results:
            assert not _decisions_without_validation(result.facts)
            assert ("non validé" if lang == "fr" else "not validated") in result.text
        return
    path = url.format(pid=parcel.id, aid=advice.id)
    response = request.getfixturevalue("client").get(path, params={"farm": farm.id})
    assert response.status_code == 200, path
    if url in JSONS:
        body = response.json()
        assert "decision" in str(body)
        assert not _decisions_without_validation(body)
    else:
        assert 'data-validation="unvalidated"' in response.text, path


def test_a_result_keeps_the_level_stamped_when_it_was_computed(client, session, scene):  # F5
    farm, parcel, advice = scene
    stamped = assessments.envelope(
        FROST_KIND,
        farm_id=farm.id,
        parcel_id=None,
        evaluated_at=NOW,
        status="evaluable",
        config_version={"id": None},
        input_snapshot={},
        result={"hours": 3},
    )
    assert stamped.config_version["model_validation"] == {
        "level": "unvalidated",
        "models": ["frost.hours"],
    }

    def card_level(props):
        advice.props = props
        session.commit()
        return client.get(f"/advisor/parcels/{parcel.id}.json", params={"farm": farm.id}).json()

    # A stronger level stamped on a result computed under it stays that result's own claim...
    future = {"level": "locally-evaluated", "models": ADVISOR_MODELS}
    assert card_level({**advice.props, "model_validation": future})["model_validation"][
        "level"
    ] == ("locally-evaluated")
    # ... and a result stored before stamps existed claims nothing.
    legacy = {k: v for k, v in advice.props.items() if k != "model_validation"}
    assert card_level(legacy)["model_validation"] == {
        "level": "unvalidated",
        "label": "Unvalidated",
        "models": [],
        "about": "/models?kind=advisor.spray",
    }


def test_about_this_model_is_generated_from_its_spec_and_open_to_a_recipient(
    client, session, scene, request
):  # F7
    recipient = request.getfixturevalue("grantee_identity")
    browser = request.getfixturevalue("grantee_client")
    page = html.unescape(client.get(f"/models/{DOWNY_3_10.key}").text)
    for needle in (
        "Downy mildew primary infection (3-10 rule)",
        "Baldacci 1947",
        "Velasquez-Camacho et al. 2023",
        "https://doi.org/10.3390/microorganisms11010073",
        "Host VITVI · PLASVI",
        "From BBCH 13",
        "Unvalidated",
        "not yet checked against observed disease records",
    ):
        assert needle in page, needle
    rain = re.search(
        r'data-parameter="downy_rain_min_mm" data-status="sourced">(.*?)</li>', page, re.S
    )
    assert rain and "10.0 mm" in rain.group(1) and "Velasquez-Camacho" in rain.group(1)
    assert client.get("/models/frost.hours").status_code == 404

    farm, _, advice = scene
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")
    grant = share.create_grant(
        repo,
        grantee="Co-op adviser",
        recipient_identity_id=recipient.id,
        preset="advice",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
        tz="Europe/Paris",
    )
    session.commit()
    assert shared_advice(advice)["model_validation"]["level"] == "unvalidated"
    shared = browser.get(f"/share/as/{grant.id}/").text
    href = re.search(r'<a href="([^"]+)" data-validation="unvalidated"', shared)
    assert href, "badge on the recipient's view"
    assert browser.get(html.unescape(href.group(1))).status_code == 200
