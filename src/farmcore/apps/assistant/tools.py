"""A web Plan tool: project the existing forecast grades, never schedule a treatment."""

from datetime import date

from farmcore.apps.forecast.planning import plan_days
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

PLAN = ToolSpec(
    name="assistant_plan",
    description="The next seven days of stored forecast weather, with this browser's availability.",
    params={},
    func="farmcore.apps.assistant.tools:plan",
    scope="stations:read",
    keywords=(
        "which days",
        "good days",
        "good weather",
        "this week",
        "spray window",
        "quels jours",
    ),
)


def plan(ctx: ToolContext, busy_days: tuple[str, ...] = ()) -> ToolResult:
    view = plan_days(ctx.repo, ctx.now, str(ctx.tz), {date.fromisoformat(d) for d in busy_days})
    links = (("Open Plan", "/forecast/plan"),)
    days = [{k: d[k] for k in ("day", "grade", "reason")} for d in view["days"][:7]]
    lines = ["Weather only · next seven days; not treatment clearance. Browser busy days included."]
    if view["unavailable"]:
        lines.append(view["unavailable"])
    if view["run"]:
        as_issued = view["run"].get("as_issued")
        provenance = (
            "As-issued forecast"
            if as_issued is True
            else "Not as-issued forecast"
            if as_issued is False
            else "Forecast provenance unknown"
        )
        if view["run"].get("receipt"):
            lines.append(
                f"{provenance} · fetched {view['run']['fetched']} · provider issue time unknown."
            )
        else:
            lines.append(f"{provenance} · issued {view['run']['issued']}.")
    lines.extend(f"{d['day']}: {d['word']} — {d['reason']}" for d in view["days"][:7])
    return ToolResult(
        {"days": days, "run": view["run"], "assessment_unavailable": view["unavailable"]},
        "\n".join(lines),
        links,
    )
