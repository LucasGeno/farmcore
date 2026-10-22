"""Small display registry over stored advice; no new evaluation or disease decisions."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from farmcore.core.models.canonical import Advice


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    rule_ids: tuple[str, ...]  # status rule first, then supporting evidence
    enabled: bool = True


MODELS = (
    ModelSpec(
        "downy",
        "Downy mildew",
        (
            "downy_3_10",
            "protection_downy",
            "downy_incubation",
            "season_gate",
            "spray_window",
            "tankmix",
        ),
    ),
    ModelSpec(
        "powdery",
        "Powdery mildew",
        (
            "powdery_gubler_thomas",
            "protection_powdery",
            "powdery_pass",
            "season_gate",
            "spray_window",
            "tankmix",
        ),
    ),
    ModelSpec("insects", "Insects (not modelled yet)", (), enabled=False),
)


@dataclass(frozen=True)
class ModelView:
    model: ModelSpec
    status: str
    action: str
    reasons: tuple[dict[str, Any], ...]
    cover: str = "Not recorded"
    cover_until: datetime | None = None
    actionable: bool = False


def project(model: ModelSpec, row: Advice | None) -> ModelView:
    reasons = tuple(r for r in (row.reasons or []) if r["rule"] in model.rule_ids) if row else ()
    if row and (row.props or {}).get("assessment_status") in ("limited", "unavailable"):
        return ModelView(
            model,
            "Not evaluable",
            (row.props or {}).get("headline", "Required inputs missing"),
            reasons,
        )
    by_rule = {r["rule"]: r for r in reasons}
    risk = by_rule.get(model.rule_ids[0]) if model.enabled else None
    status = risk["verdict"] if risk else "Not evaluated"
    action = "Not evaluated"
    gate = by_rule.get("season_gate", {})
    if not risk and gate.get("verdict") == "season not started":
        status = action = gate["verdict"]
    products = []
    if risk:
        products = [p["name"] for p in (row.products or []) if model.key in p.get("targets", [])]
        if model.key == "powdery" and risk.get("inputs", {}).get("in_period") is False:
            action = "No powdery pass due"
        elif products and row.decision in ("spray", "wait"):
            action = f"{row.decision.capitalize()} {' + '.join(products)}"
        elif model.key == "powdery":
            action = by_rule.get("powdery_pass", {}).get("verdict", "Not evaluated")
        elif row.decision in ("none", "protected") or status in ("covered", "no risk"):
            action = "No downy pass due"
        else:
            action = "See combined advice"
    cover = by_rule.get(f"protection_{model.key}", {})
    until = cover.get("inputs", {}).get("until")
    return ModelView(
        model,
        status,
        action,
        reasons,
        cover=cover.get("verdict", "Not recorded"),
        cover_until=datetime.fromisoformat(until) if until else None,
        actionable=bool(products and row.decision in ("spray", "wait")),
    )
