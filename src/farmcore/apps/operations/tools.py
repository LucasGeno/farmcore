"""Operations assistant tools (read-only): `operations_last_spray(parcel)` and
`operations_recent_sprays()` (which parcels were sprayed this season, latest sprays first).

The parcel's latest done spray with `ts_start <= now` (a spray after the clock stays hidden),
read through the chat's repository: the log, its rate quantities with their products, its
sources. Each product carries its own dose and unit (a tank mix keeps them apart); rates of one
product in one unit add up, with `parts` listing the stored values; other units and unknown
labels stay separate. The top-level `dose`/`unit` are set only for one product in one unit.
There is no `log_spray` tool, on purpose: a spray is logged only through ✅.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from farmcore.apps.fields import tools as fields_tools
from farmcore.apps.operations import spray_items
from farmcore.apps.operations.spray_items import SprayDose, SprayItem
from farmcore.core import drafts
from farmcore.core.models.canonical import Log, LogParcel, LogSource
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec, object_schema

SCOPE = "logs:read"
DRAFTS_SCOPE = "drafts:propose"
REGISTER_PATH = "/operations/register"
CARD_PATH = "/advisor/parcels/{id}"  # advisor's card URL (a Telegram button already)
LAST_SPRAY_FACTS = frozenset(
    {"parcel", "date", "products", "name", "active_substance", "dose", "unit", "sources", "parts"}
)

TOOLS = (
    ToolSpec(
        name="operations_last_spray",
        description=(
            "The latest spray logged on one parcel up to now: local date and time, products "
            "(name, active substance), dose and unit, and where the record came from."
        ),
        params=object_schema({"parcel": {"type": "string"}}),
        effect="read",
        func="farmcore.apps.operations.tools:last_spray",
        scope=SCOPE,
        keywords=(
            "last spray",
            "last sprayed",
            "last treatment",
            "when did i spray",
            "when did i last",
            "did i spray",
            "have i sprayed",
            "dernier traitement",
            "derniere pulverisation",
            "quand ai je traite",
            "traite quand",
        ),
    ),
    ToolSpec(
        name="operations_recent_sprays",
        description=(
            "Which parcels were sprayed this season up to now, with the latest sprays: local "
            "date and time, parcels, products and doses."
        ),
        params=object_schema(),
        effect="read",
        func="farmcore.apps.operations.tools:recent_sprays",
        scope=SCOPE,
        keywords=(
            "which parcels did i spray",
            "which parcels have i sprayed",
            "what did i spray",
            "what have i sprayed",
            "recent sprays",
            "sprays this season",
            "did i spray",
            "have i sprayed",
            "quelles parcelles ai je traitees",
            "derniers traitements",
        ),
    ),
    ToolSpec(
        name="propose_record",
        description=(
            "Propose a record as a draft for the farmer to confirm: spray, irrigation or "
            "observation, with its parcels, date and amounts. It never records on its own."
        ),
        params=object_schema(
            {
                "type": {"type": "string", "enum": ["spray", "irrigation", "observation"]},
                "parcel_ids": {"type": "array", "items": {"type": "integer"}},
                "ts_start": {"type": "string"},
                "ts_end": {"type": "string"},
                "doses": {"type": "array", "items": {"type": "object"}},
                "fact": {"type": "object"},
            },
            required=("type", "parcel_ids", "ts_start"),
        ),
        effect="propose",
        func="farmcore.apps.operations.tools:propose_record",
        scope=DRAFTS_SCOPE,
    ),
    ToolSpec(
        name="get_draft",
        description="Read one of your own proposed drafts by id, with its status and review link.",
        params=object_schema({"draft_id": {"type": "integer"}}, required=("draft_id",)),
        effect="read",
        func="farmcore.apps.operations.tools:get_draft",
        scope=DRAFTS_SCOPE,
    ),
)
RECENT_FACTS = frozenset({"season", "parcels", "sprays", "date", "products"})
RECENT_SHOWN = 2  # Telegram replies keep three lines


def _dose(value: float) -> str:
    return f"{value:g}"


def last_spray(ctx: ToolContext, parcel: str = "") -> ToolResult:
    repo = ctx.repo
    found = fields_tools.resolve_parcel(repo, parcel)
    if found is None:
        return fields_tools.unknown_parcel(ctx)
    label = fields_tools.label(found)
    links = (
        (f"Open card {found.alias}", CARD_PATH.format(id=found.id)),
        ("Open register", REGISTER_PATH),
    )
    [log, *_] = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= ctx.now,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == found.id)),
        scope=SCOPE,
        order_by=(Log.ts_start.desc(), Log.id.desc()),
        limit=1,
    ) or [None]
    if log is None:
        text = (
            f"Aucun traitement enregistré sur {label}."
            if ctx.lang == "fr"
            else f"No spray is logged on {label} yet."
        )
        return ToolResult(facts={"parcel": label, "date": None}, text=text, links=links)

    rates = spray_items.doses_for_logs(repo, [log.id], scope=SCOPE, rates_only=True)[log.id]
    products = spray_items.items_by_id(repo, [q.item_id for q in rates], scope=SCOPE)
    sources = repo.list(LogSource, LogSource.log_id == log.id, scope=SCOPE, order_by=LogSource.id)

    # One item per (product, unit), each with its own dose: a tank mix of copper 1.94 + sulphur
    # 2.06 kg/ha never reads as both at 1.94. Rates of one product in one unit add up (`parts`
    # keeps what was stored); other units and unknown labels stay separate, never converted.
    named: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for q in rates:
        p = products.get(q.item_id) if q.item_id is not None else None
        name = p.name if p else (q.label or "?")
        substance = p.active_substance if p is not None else None
        key = (name, substance, q.unit)
        item = by_key.get(key)
        if item is None:
            item = {"name": name, "active_substance": substance, "dose": float(q.value)}
            item["unit"] = q.unit
            by_key[key] = item
            named.append(item)
        else:
            item.setdefault("parts", [item["dose"]]).append(float(q.value))
            item["dose"] = round(sum(item["parts"]), 6)
    when = log.ts_start.astimezone(ctx.tz)
    facts: dict[str, Any] = {
        "parcel": label,
        "date": when.isoformat(),
        "products": named,
        "sources": list(dict.fromkeys(s.source_type for s in sources)),
    }

    def amount(p: dict[str, Any]) -> str:
        return f"{_dose(p['dose'])} {p['unit']}"

    if len(named) == 1:  # one product in one unit: its dose is the spray's dose
        [only] = named
        facts["dose"], facts["unit"] = only["dose"], only["unit"]
        what = f"{only['active_substance'] or only['name']}, {amount(only)}"
        if "parts" in only:
            logged = " + ".join(_dose(x) for x in only["parts"])
            what += f" ({logged} {only['unit']} {'enregistrés' if ctx.lang == 'fr' else 'logged'})"
    elif named:
        what = " + ".join(f"{p['active_substance'] or p['name']} {amount(p)}" for p in named)
    else:
        what = "?, no dose"
    if ctx.lang == "fr":
        text = f"Dernier traitement de {label} : {when:%d/%m %H:%M}, {what}."
    else:
        text = f"{label} was last sprayed on {when:%d %b %H:%M}: {what}."
    return ToolResult(facts=facts, text=text, links=links)


def recent_sprays(ctx: ToolContext) -> ToolResult:
    """This season's done sprays up to now (calendar year of the clock), newest first."""
    repo = ctx.repo
    start = ctx.now.astimezone(ctx.tz).replace(month=1, day=1, hour=0, minute=0, second=0)
    done = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= ctx.now,
        Log.ts_start >= start,
        scope=SCOPE,
        order_by=(Log.ts_start.desc(), Log.id.desc()),
    )
    links = (("Open register", REGISTER_PATH),)
    season = start.year
    if not done:
        text = (
            f"Aucun traitement enregistré en {season} pour l'instant."
            if ctx.lang == "fr"
            else f"No spray is logged in {season} yet."
        )
        return ToolResult({"season": season, "parcels": [], "sprays": []}, text, links)
    ids = [log.id for log in done]
    links_of: dict[int, list[Any]] = {}
    for link in repo.list(LogParcel, LogParcel.log_id.in_(ids), scope=SCOPE):
        links_of.setdefault(link.log_id, []).append(link.parcel_id)
    farm_parcels = fields_tools.parcels(repo)
    by_id = {p.id: p for p in farm_parcels}
    order = {p.id: n for n, p in enumerate(farm_parcels)}
    rates_of = spray_items.doses_for_logs(repo, ids, scope=SCOPE, rates_only=True)
    products = spray_items.items_by_id(
        repo, [q.item_id for qs in rates_of.values() for q in qs], scope=SCOPE
    )
    sprays = []
    for log in done:
        ids_here = sorted((i for i in links_of.get(log.id, []) if i in by_id), key=order.get)
        aliases = [by_id[i] for i in ids_here]
        names = [
            f"{_product_name(products, q)} {_dose(float(q.value))} {q.unit}"
            for q in rates_of.get(log.id, [])
        ]
        sprays.append(
            {
                "date": log.ts_start.astimezone(ctx.tz).isoformat(),
                "parcels": [p.alias for p in aliases],
                "products": names,
            }
        )
    sprayed = sorted(
        {i for log in done for i in links_of.get(log.id, []) if i in by_id}, key=order.get
    )
    labels = [fields_tools.label(by_id[i]) for i in sprayed]
    if ctx.lang == "fr":
        lines = [f"Traité en {season} : {', '.join(labels)}."]
    else:
        lines = [f"Sprayed in {season} so far: {', '.join(labels)}."]
    for spray in sprays[:RECENT_SHOWN]:
        when = datetime.fromisoformat(spray["date"])
        lines.append(
            f"{when:%d %b %H:%M} · {', '.join(spray['parcels'])} · "
            f"{' + '.join(spray['products']) or '?'}"
        )
    facts = {"season": season, "parcels": labels, "sprays": sprays[:RECENT_SHOWN]}
    return ToolResult(facts, "\n".join(lines), links)


def _product_name(products: dict[int, SprayItem], q: SprayDose) -> str:
    product = products.get(q.item_id) if q.item_id is not None else None
    return product.name if product is not None else (q.label or "?")


def propose_record(
    ctx: ToolContext,
    type: str,
    parcel_ids: list[int],
    ts_start: str,
    ts_end: str | None = None,
    doses: list[dict[str, Any]] | None = None,
    fact: dict[str, Any] | None = None,
) -> ToolResult:
    """Propose a spray, irrigation or observation as a draft (`operations.drafts` validates)."""
    payload: dict[str, Any] = {"parcel_ids": parcel_ids, "ts_start": ts_start}
    if ts_end:
        payload["ts_end"] = ts_end
    if doses:
        payload["doses"] = doses
    if fact is not None:
        payload["fact"] = fact
    draft, _ = drafts.propose(
        ctx.repo.session,
        ctx.manifests,
        farm_id=ctx.repo.farm_id,
        actor=ctx.repo.actor,
        kind=type,
        payload=payload,
        provenance={"source_type": "agent"},
        request_key=uuid4().hex,
    )
    text = f"Proposed a {type} record; the farmer reviews and confirms it."
    return ToolResult(
        facts={"draft_id": draft.id, "type": type},
        text=text,
        links=(("Review record", f"/drafts/{draft.id}"),),
    )


def get_draft(ctx: ToolContext, draft_id: int) -> ToolResult:
    """The agent's own draft (its token's farm and actor); another farm's id is not found."""
    draft = drafts.get(
        ctx.repo.session, farm_id=ctx.repo.farm_id, draft_id=draft_id, actor=ctx.repo.actor
    )
    facts = {
        "draft_id": draft.id,
        "kind": draft.kind,
        "status": draft.status,
        "revision": draft.revision,
    }
    text = f"Draft {draft.id} ({draft.kind}) is {draft.status}."
    return ToolResult(facts=facts, text=text, links=(("Review draft", f"/drafts/{draft.id}"),))
