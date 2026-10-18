"""Disease views project stored reasons; they never run or duplicate the decision rules."""

from farmcore.apps.advisor.models import MODELS, project
from farmcore.core.models.canonical import Advice


def reason(rule, verdict, **inputs):
    return dict(
        rule=rule, verdict=verdict, inputs=inputs, threshold={}, source="stored citation", note=""
    )


def test_april_copper_risk_is_downy_not_powdery():
    row = Advice(
        decision="spray",
        products=[dict(name="copper", targets=["downy"])],
        reasons=[
            reason("downy_3_10", "risk", active_events=["2024-04-08T22:00:00+00:00"]),
            reason("protection_downy", "no cover"),
            reason("powdery_gubler_thomas", "outside powdery period", in_period=False, index=0),
        ],
    )
    downy, powdery = (project(m, row) for m in MODELS if m.enabled)
    assert downy.status == "risk" and downy.action == "Spray copper"
    assert downy.actionable and not powdery.actionable
    assert powdery.status == "outside powdery period"
    assert powdery.action == "No powdery pass due"
    assert all(r["rule"] != "downy_3_10" for r in powdery.reasons)
    assert downy.reasons[0]["source"] == "stored citation"


def test_powdery_cover_does_not_inherit_the_top_level_downy_date():
    from datetime import UTC, datetime

    downy_until = datetime(2024, 6, 19, tzinfo=UTC)
    powdery_until = "2024-06-12T08:00:00+00:00"
    row = Advice(
        decision="protected",
        protected_until=downy_until,
        reasons=[
            reason("powdery_gubler_thomas", "low pressure", in_period=True),
            reason("protection_powdery", "protected", until=powdery_until),
            reason("powdery_pass", "covered"),
        ],
        products=[],
    )
    powdery = project(MODELS[1], row)
    assert powdery.cover == "protected"
    assert powdery.cover_until == datetime.fromisoformat(powdery_until)
    assert powdery.cover_until != downy_until
    assert project(MODELS[0], row).status == "Not evaluated"


def test_unincluded_sulphur_is_not_reported_as_no_pass_due():
    due = "sulphur due; no sulphur-only pass, tankmix decides (downy pass due)"
    row = Advice(
        decision="spray",
        products=[dict(name="copper", targets=["downy"])],
        reasons=[
            reason("powdery_gubler_thomas", "high pressure", in_period=True, index=100),
            reason("protection_powdery", "no cover"),
            reason("powdery_pass", due),
            reason("tankmix", "tank-mix due"),
        ],
    )
    assert project(MODELS[1], row).action == due


def test_missing_and_season_gated_reasons_are_not_safe_results():
    for row in (None, Advice(decision="none", reasons=[], products=[])):
        assert project(MODELS[0], row).status == "Not evaluated"
        assert project(MODELS[1], row).action == "Not evaluated"
    row = Advice(
        decision="none", products=[], reasons=[reason("season_gate", "season not started")]
    )
    assert project(MODELS[0], row).status == "season not started"
    assert project(MODELS[1], row).action == "season not started"


def test_risk_without_a_stored_product_is_not_a_no_pass_claim():
    row = Advice(
        decision="spray",
        products=[],
        reasons=[reason("downy_3_10", "risk"), reason("protection_downy", "no cover")],
    )
    assert project(MODELS[0], row).action == "See combined advice"
