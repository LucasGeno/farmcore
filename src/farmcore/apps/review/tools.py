"""One read-only assistant tool: has an assigned advisor checked this saved assessment?

It quotes the stored review events for the parcel's latest advice row: reviewer, farm clock time,
opinion and note. It never writes, never decides and never turns a review into a treatment.
"""

from __future__ import annotations

from datetime import datetime

from farmcore.apps.advisor import service as advisor
from farmcore.apps.fields import tools as fields_tools
from farmcore.apps.review import recommendations
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

CARD = "/advisor/parcels/{id}"
OPINION_FR = {
    "agree": "d'accord avec cette évaluation",
    "disagree": "en désaccord avec cette évaluation",
    "unsure": "incertain sur cette évaluation",
}
TOOLS = (
    ToolSpec(
        name="review_assessment_status",
        description=(
            "Whether an assigned advisor reviewed a parcel's latest saved advice: reviewer, "
            "time, opinion and note. A human opinion about one saved assessment; the model "
            "output and any newer assessment are unchanged."
        ),
        params={"parcel": "STRING"},
        func="farmcore.apps.review.tools:status",
        scope="advice:read",
        keywords=(
            "reviewed",
            "review",
            "advisor check",
            "advisor reviewed",
            "human review",
            "who reviewed",
            "has an advisor",
            "advisor say",
            "advisor note",
            "advisor opinion",
            "avis",
            "revue",
            "relu",
            "relue",
        ),
    ),
)


def _when(value: datetime, ctx: ToolContext) -> str:
    return value.astimezone(ctx.tz).strftime("%d %b %H:%M")


def status(ctx: ToolContext, parcel: str = "") -> ToolResult:
    found = fields_tools.resolve_parcel(ctx.repo, parcel)
    if found is None:
        return fields_tools.unknown_parcel(ctx)
    label = fields_tools.label(found)
    links = ((f"Open card {found.alias}", CARD.format(id=found.id)),)
    row = advisor.latest_advice(ctx.repo, ctx.now, tz=ctx.tz.key, parcel_id=found.id).get(found.id)
    if row is None:
        text = (
            f"{label} : pas encore de conseil; rien à réviser."
            if ctx.lang == "fr"
            else f"{label}: no advice yet; nothing to review."
        )
        return ToolResult(
            {"parcel": label, "advice_id": None, "requested": False, "reviewed": False}, text, links
        )

    props = row.props or {}
    status = props.get("assessment_status", "legacy-unknown")
    state = recommendations.state(ctx.repo, row.id)
    asked, answered = state.request, state.response
    asked_payload = (asked.payload or {}) if asked else {}
    answer = (answered.payload or {}) if answered else {}
    facts = {
        "parcel": label,
        "advice_id": row.id,
        "evaluated_at": row.evaluated_at.isoformat(),
        "decision": row.decision,
        "assessment_status": status,
        "headline": props.get("headline"),
        "requested": asked is not None,
        "requested_by": asked_payload.get("requested_by"),
        "requested_at": asked.occurred_at.isoformat() if asked else None,
        "reviewed": answered is not None,
        "reviewer": answer.get("reviewer"),
        "reviewed_at": answered.occurred_at.isoformat() if answered else None,
        "opinion": answer.get("opinion"),
        "note": answer.get("note"),
    }
    fr = ctx.lang == "fr"
    lines = []
    if status in ("limited", "unavailable"):
        headline = props.get("headline") or "required inputs missing"
        lines.append(
            f"{label} : évaluation limitée — {headline}."
            if fr
            else f"{label}: Assessment limited — {headline}."
        )
    else:
        lines.append(
            f"{label} : évaluation enregistrée le {_when(row.evaluated_at, ctx)} · {row.decision}."
            if fr
            else f"{label}: saved check {_when(row.evaluated_at, ctx)} · {row.decision}."
        )
    if answered is not None:
        opinion = (
            OPINION_FR.get(answer.get("opinion"), "a donné son avis")
            if fr
            else recommendations.OPINION_WORDS.get(answer.get("opinion"), "Reviewed")
        )
        lines.append(
            (
                f"L'advisor {answer.get('reviewer')} a répondu le "
                f"{_when(answered.occurred_at, ctx)} : {opinion}. {answer.get('note') or ''}"
            ).strip()
            if fr
            else (
                f"Advisor {answer.get('reviewer')} answered "
                f"{_when(answered.occurred_at, ctx)}: {opinion}. {answer.get('note') or ''}"
            ).strip()
        )
        lines.append(
            "Avis humain sur cette évaluation enregistrée ; le résultat du modèle est inchangé."
            if fr
            else "A human opinion about this saved check; the model output is unchanged."
        )
    elif asked is not None:
        lines.append(
            f"Avis d'advisor demandé le {_when(asked.occurred_at, ctx)} par "
            f"{asked_payload.get('requested_by')} ; pas encore de réponse."
            if fr
            else f"Advisor check requested {_when(asked.occurred_at, ctx)} by "
            f"{asked_payload.get('requested_by')}; no answer yet."
        )
        lines.append(
            "Le résultat du modèle et toute évaluation plus récente restent inchangés."
            if fr
            else "The model output and any newer check are unchanged."
        )
    else:
        lines.append(
            f"Aucun avis d'advisor demandé pour l'évaluation enregistrée du "
            f"{_when(row.evaluated_at, ctx)}."
            if fr
            else f"No advisor check requested for the saved check at "
            f"{_when(row.evaluated_at, ctx)}."
        )
    return ToolResult(facts=facts, text="\n".join(lines), links=links)
