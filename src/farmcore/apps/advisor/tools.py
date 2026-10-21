"""Advisor assistant tools (read-only): `advisor_today` and `advisor_parcel_status(parcel)`.

Both read `service.latest_advice` (advice evaluated after the clock's now stays hidden) and quote
the stored decision and headline: a tool never decides. The headline is English, so a French
answer renders its second line from the same stored fields instead (`_fr_detail`).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from farmcore.apps.advisor import service
from farmcore.apps.fields import tools as fields_tools
from farmcore.core.models.canonical import Advice
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

SCOPE = service.SCOPE
MAP_PATH = "/fields/"
MAX_CARD_BUTTONS = 4  # more parcels to spray than this: one "Open map" button instead
TODAY_FACTS = frozenset(
    {"parcels", "parcel", "decision", "headline", "advised", "assessment_status", "config_version"}
)
STATUS_FACTS = frozenset(
    {
        "parcel",
        "decision",
        "headline",
        "protected_until",
        "mm_since_spray",
        "next_window",
        "products",
        "flags",
        "assessment_status",
        "config_version",
    }
)

TOOLS = (
    ToolSpec(
        name="advisor_today",
        description=(
            "Today's spray advice for every parcel: the advisor's decision (spray, wait, "
            "protected, none) and its one-line headline."
        ),
        params={},
        func="farmcore.apps.advisor.tools:today",
        scope=SCOPE,
        keywords=(
            "spray today",
            "what do i spray",
            "what should i spray",
            "which parcels should i spray",
            "should i spray",
            "to spray",
            "need a spray",
            "traiter aujourd hui",
            "traitements aujourd hui",
            "que dois je traiter",
            "quoi traiter",
            "que traiter",
            "quelles parcelles traiter",
            "faut il traiter",
            "a traiter",
            "need to spray",
            # Farm-wide protection and rain since the spray, when no parcel is named (a parcel
            # named goes to advisor_parcel_status, which has the same phrases). Longer than the
            # fields tool's "my parcels" and the weather tool's "rain" on purpose.
            "parcels protected",
            "everything protected",
            "all protected",
            "parcelles protegees",
            "sont elles protegees",
            "tout est protege",
            "rain since",
            "since the spray",
            "since the last spray",
            "pluie depuis",
            "depuis le traitement",
            "depuis le dernier traitement",
        ),
    ),
    ToolSpec(
        name="advisor_parcel_status",
        description=(
            "One parcel's latest advice: decision, headline, protected until, rain since the "
            "last spray, next spray window, recommended products and flags."
        ),
        params={"parcel": "STRING"},
        func="farmcore.apps.advisor.tools:parcel_status",
        scope=SCOPE,
        keywords=(
            "protected",
            "protection",
            "covered",
            "washed off",
            "status",
            "advice for",
            "should i spray",
            "need a spray",
            "protege",
            "protegee",
            "lessive",
            "faut il traiter",
            "dois je traiter",
            "need to spray",
            "to spray",
            "rain since",
            "since the spray",
            "since the last spray",
            "since i last sprayed",
            "pluie depuis",
            "depuis le traitement",
            "depuis le dernier traitement",
        ),
    ),
)

PLAIN = {
    "en": {
        "spray": "not protected, spray needed",
        "wait": "wait",
        "protected": "protected",
        "none": "no action needed",
    },
    "fr": {
        "spray": "non protégée, à traiter",
        "wait": "attendre",
        "protected": "protégée",
        "none": "rien à faire",
    },
}
OTHERS = {
    "en": {
        "protected": "protected",
        "wait": "wait",
        "none": "no action",
        "missing": "awaiting advice",
    },
    "fr": {
        "protected": "protégées",
        "wait": "en attente",
        "none": "sans action",
        "missing": "sans conseil",
    },
}
MISSING = "missing"  # a parcel without advice up to now (never evaluated, or only after now)


MOIS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.")
MOIS += ("nov.", "déc.")


def _fr_when(t: datetime, ctx: ToolContext) -> str:
    local = t.astimezone(ctx.tz)
    return f"{local.day} {MOIS[local.month - 1]} {local:%H:%M}"


def _fr_detail(row: Advice, ctx: ToolContext) -> str | None:
    """The French second line, built from the advice fields: the stored headline is English,
    so a French answer does not quote it (it stays in the facts). None when there is nothing to
    add to the plain first line."""
    props = row.props or {}
    if row.decision == "protected":
        line = "Protégée"
        if row.protected_until is not None:
            line += f" jusqu'au {_fr_when(row.protected_until, ctx)}"
        mm = props.get("mm_since_spray")
        if mm is not None:
            mm_s = f"{float(mm):.1f}".replace(".", ",")
            line += f" · {mm_s} mm de pluie depuis le traitement"
        return line + "."
    if row.decision in ("spray", "wait"):
        names = [p.get("name") for p in row.products or [] if p.get("name")]
        with_ = " avec " + " + ".join(names) if names else ""
        if row.next_window_start is None:
            return "Pas de fenêtre de traitement dans les prévisions."
        lead = "À traiter" if row.decision == "spray" else "Prochaine fenêtre"
        line = f"{lead}{with_} dès le {_fr_when(row.next_window_start, ctx)}"
        if "washed_off" in (props.get("flags") or []):
            line += " (le dernier traitement a été lessivé par la pluie)"
        return line + "."
    return None


def _local(t: datetime | None, ctx: ToolContext) -> str | None:
    return t.astimezone(ctx.tz).isoformat() if t is not None else None


def _card(parcel_id: int) -> str:
    return service.CARD_PATH.format(id=parcel_id)


def _version(row) -> dict:
    """Which parameter version produced the advice, and its review status; nothing else."""
    version = service.advice_json(row)["config_version"]
    return {"id": version.get("id"), "status": version.get("status")}


def today(ctx: ToolContext) -> ToolResult:
    """The farm's advice in at most three lines. A parcel without advice up to now is
    `advised: false` and "awaiting advice", never counted as nothing to spray: "Nothing to
    spray today" only when every parcel is evaluated and none is at risk."""
    parcels = fields_tools.parcels(ctx.repo)
    advice = service.latest_advice(ctx.repo, ctx.now, tz=ctx.tz.key)
    rows: list[tuple[Any, Advice | None]] = [(p, advice.get(p.id)) for p in parcels]
    facts = {
        "parcels": [
            {
                "parcel": fields_tools.label(p),
                "decision": a.decision if a else service.NO_ADVICE,
                "advised": a is not None,
                "assessment_status": (a.props or {}).get("assessment_status", "legacy-unknown")
                if a
                else "unavailable",
                "config_version": _version(a) if a else None,
                "headline": (a.props or {}).get("headline") if a else None,
            }
            for p, a in rows
        ]
    }
    fr = ctx.lang == "fr"
    evaluated = [(p, a) for p, a in rows if a is not None]
    lead = next((d for d in ("spray", "wait") if any(a.decision == d for _, a in evaluated)), None)
    acting = [(p, a) for p, a in evaluated if a.decision == lead]
    lines: list[str] = []
    if acting:
        names = ", ".join(fields_tools.label(p) if len(acting) <= 3 else p.alias for p, _ in acting)
        n = len(acting)
        s_ = "s" if n > 1 else ""
        if lead == "spray":
            lines.append(
                f"{n} parcelle{s_} à traiter : {names}."
                if fr
                else f"{n} parcel{s_} to spray: {names}."
            )
        else:
            lines.append(
                f"{n} parcelle{s_} à risque, en attente d'une fenêtre de traitement : {names}."
                if fr
                else f"{n} parcel{s_} at risk, waiting for a spray window: {names}."
            )
        if fr:
            details = Counter(_fr_detail(a, ctx) for _, a in acting)
        else:
            details = Counter((a.props or {}).get("headline") for _, a in acting)
        [(detail, _)] = details.most_common(1)
        if detail:
            lines.append(detail)
    elif any(
        (a.props or {}).get("assessment_status") in ("limited", "unavailable") for _, a in evaluated
    ):
        lines.append(
            "Évaluation limitée : données requises manquantes; risque inconnu."
            if fr
            else "Assessment limited or unavailable: required inputs missing; "
            "infection risk unknown."
        )
    elif not evaluated:
        n = len(rows)
        lines.append(
            f"Pas encore de conseil : aucune des {n} parcelles n'a été évaluée à cette heure."
            if fr
            else f"No advice yet: the advisor has not evaluated any of the {n} parcels up to now."
        )
    elif len(evaluated) < len(rows):
        n = len(evaluated)
        if fr:
            lines.append(
                f"Aucun traitement conseillé sur {'les ' if n > 1 else 'la '}{n} "
                f"parcelle{'s' if n > 1 else ''} évaluée{'s' if n > 1 else ''}."
            )
        else:
            lines.append(f"No spray advised on the {n} evaluated parcel{'s' if n > 1 else ''}.")
    else:
        lines.append("Rien à traiter aujourd'hui." if fr else "Nothing to spray today.")
    counts = Counter(a.decision if a else MISSING for _, a in rows)
    words = OTHERS["fr" if fr else "en"]
    others = [d for d in ("protected", "wait", MISSING) if d != lead and counts[d]]
    if others and evaluated:
        parts = [f"{counts[d]} {words[d]}" for d in others]
        lines.append(("Autres : " if fr else "Others: ") + ", ".join(parts) + ".")
    if len(acting) > MAX_CARD_BUTTONS:
        links: tuple[tuple[str, str], ...] = (("Open map", MAP_PATH),)
    else:
        links = tuple((f"Open card {p.alias}", _card(p.id)) for p, _ in acting)
    return ToolResult(facts=facts, text="\n".join(lines), links=links)


def parcel_status(ctx: ToolContext, parcel: str = "") -> ToolResult:
    found = fields_tools.resolve_parcel(ctx.repo, parcel)
    if found is None:
        return fields_tools.unknown_parcel(ctx)
    label = fields_tools.label(found)
    fr = ctx.lang == "fr"
    links = ((f"Open card {found.alias}", _card(found.id)),)
    row = service.latest_advice(ctx.repo, ctx.now, tz=ctx.tz.key, parcel_id=found.id).get(found.id)
    if row is None:
        text = f"{label} : pas encore de conseil." if fr else f"{label}: no advice yet."
        facts = {"parcel": label, "decision": service.NO_ADVICE, "headline": None}
        return ToolResult(facts=facts, text=text, links=links)
    props = row.props or {}
    facts = {
        "parcel": label,
        "assessment_status": props.get("assessment_status", "legacy-unknown"),
        "config_version": _version(row),
        "decision": row.decision,
        "headline": props.get("headline"),
        "protected_until": _local(row.protected_until, ctx),
        "mm_since_spray": props.get("mm_since_spray"),
        "next_window": (
            [_local(row.next_window_start, ctx), _local(row.next_window_end, ctx)]
            if row.next_window_start
            else None
        ),
        "products": [p.get("name") for p in row.products or []],
        "flags": list(props.get("flags") or []),
    }
    if props.get("assessment_status") in ("limited", "unavailable") and row.decision in (
        "none",
        "protected",
    ):
        return ToolResult(facts=facts, text=f"{label}: {props.get('headline')}", links=links)
    plain = PLAIN["fr" if fr else "en"].get(row.decision, row.decision)
    lines = [f"{label} : {plain}." if fr else f"{label}: {plain}."]
    detail = _fr_detail(row, ctx) if fr else facts["headline"]
    if detail:
        lines.append(detail)
    return ToolResult(facts=facts, text="\n".join(lines), links=links)
