"""The agronomic model contract (`core/model_spec.py`): every model behind an assessment declares
what it is and how far to trust it, a missing declaration fails the start, and the validation
level shows beside every stored result and in its JSON.

Failure model (one test per named failure, at the layer that catches it):
- F1 a spec omits a declaration (method, codes, stages, validation...) and loads anyway;
- F2 a parameter carries no citation without saying it is unsourced (an invented-looking value);
- F3 a validation level above `unvalidated` is claimed without what backs it;
- F4 an assessment kind loads with no model, or names something that is not a spec, so its
  results would show no level; two apps declare different models under one key;
- F5 a stored result is shown or exported without its validation level;
- F6 the About view says something other than the spec, or shows a model of no loaded app.
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.advisor.specs import DOWNY_3_10
from farmcore.apps.share.service import shared_advice
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
from farmcore.core.models.canonical import Advice
from farmcore.core.registry import resolve_order
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.operations.conftest import make_parcel

NOW = datetime(2024, 5, 10, 4, tzinfo=UTC)

FROST = ModelSpec(
    key="frost.hours",
    title="Frost hours",
    method=Method("Hours below a threshold", "Invented test citation"),
    host="VITVI",
    pests=("PLASVI",),
    stages=Stages(5, 19),
    inputs=(ModelInput("air temperature", "°C", "hourly", "measured"),),
    parameters=(Parameter("threshold_c", 0.0, "°C", "Invented test citation"),),
    validation=Validation("unvalidated"),
    provenance="test fixture",
)
FROST_KIND = AssessmentKind("frost.hours", 1, "pydantic:BaseModel", models=(f"{__name__}:FROST",))


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
        ({"validation": Validation("literature-evaluated")}, "needs what backs it"),  # F3
    ],
)
def test_a_model_missing_a_declaration_fails_with_a_clear_error(change, says):
    with pytest.raises(ModelSpecInvalid, match=rf"model 'frost\.hours': .*{says}"):
        replace(FROST, **change)
    # An unknown source stays honest: marked unsourced, it loads and claims no citation.
    replace(FROST, parameters=(Parameter("threshold_c", 0.0, "°C", status="unsourced"),))


@pytest.mark.parametrize(
    ("kinds", "says"),
    [
        ((replace(FROST_KIND, models=()),), "declares no model"),
        ((replace(FROST_KIND, models=("farmcore.core.model_spec:Method",)),), "not a ModelSpec"),
        (
            (
                FROST_KIND,
                AssessmentKind(
                    "frost.days", 1, "pydantic:BaseModel", models=(f"{__name__}:OTHER_FROST",)
                ),
            ),
            "'frost.hours' is declared twice",
        ),
    ],
)
def test_an_assessment_kind_without_valid_models_fails_at_load(kinds, says):  # F4
    app = Manifest(name="frost", version="0.1.0", assessment_kinds=kinds)
    with pytest.raises(ModelSpecInvalid, match=says):
        resolve_order([CORE, app])
    assert resolve_order([CORE, replace(app, assessment_kinds=(FROST_KIND,))])


OTHER_FROST = replace(FROST, title="Another model under the same key")


@pytest.fixture
def stored(session, settings):
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    parcel = make_parcel(session, farm, 301, area="1.5", props={"variety": "Merlot"})
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="wait",
            props={"headline": "Synthetic stored current advice"},
            reasons=[],
        )
    )
    set_replay(session, NOW, settings)
    session.commit()
    return farm, parcel


def test_a_stored_result_shows_and_exports_its_validation_level(client, session, stored):  # F5
    farm, parcel = stored
    card = client.get(f"/advisor/parcels/{parcel.id}", params={"farm": farm.id}).text
    badge = re.search(
        r'<a href="([^"]+)" data-validation="([^"]+)" data-kind="advisor.spray"', card
    )
    assert badge and badge.group(2) == "unvalidated"

    body = client.get(f"/advisor/parcels/{parcel.id}.json", params={"farm": farm.id}).json()
    row = session.scalar(select(Advice).where(Advice.parcel_id == parcel.id))
    for exported in (body, shared_advice(row)):
        assert exported["model_validation"]["level"] == "unvalidated"
        assert exported["model_validation"]["about"] == html.unescape(badge.group(1))

    about = html.unescape(client.get(html.unescape(badge.group(1))).text)
    shown = re.findall(r'data-model-spec="([^"]+)"', about)
    assert shown == [
        "advisor.downy_3_10",
        "advisor.downy_incubation",
        "advisor.powdery_gubler_thomas",
        "advisor.spray_decision",
    ]


def test_about_this_model_is_generated_from_its_spec(client):  # F6
    page = html.unescape(client.get(f"/models/{DOWNY_3_10.key}").text)
    for needle in (
        "Downy mildew primary infection (3-10 rule)",
        "Baldacci 1947",  # the method's citation, from the engine config
        "Host VITVI · PLASVI",
        "From BBCH 13",
        "Unvalidated",
        "not yet checked against observed disease records",
    ):
        assert needle in page, needle
    rain = re.search(
        r'data-parameter="downy_rain_min_mm" data-status="sourced">(.*?)</li>', page, re.S
    )
    assert rain and "10.0 mm" in rain.group(1) and "Gessi et al. 2022" in rain.group(1)
    assert client.get("/models/frost.hours").status_code == 404
