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

from sqlalchemy import select

from farmcore.apps.fields import tools as fields_tools
from farmcore.core.models.canonical import Log, LogParcel, LogSource, Product, Quantity
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

SCOPE = "logs:read"
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
        params={"parcel": "STRING"},
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
        params={},
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

    rates = repo.list(
        Quantity,
        Quantity.log_id == log.id,
        Quantity.measure == "rate",
        scope=SCOPE,
        order_by=Quantity.id,
    )
    ids = [q.product_id for q in rates if q.product_id is not None]
    products = {
        p.id: p for p in (repo.list(Product, Product.id.in_(ids), scope=SCOPE) if ids else [])
    }
    sources = repo.list(LogSource, LogSource.log_id == log.id, scope=SCOPE, order_by=LogSource.id)

    # One item per (product, unit), each with its own dose: a tank mix of copper 1.94 + sulphur
    # 2.06 kg/ha never reads as both at 1.94. Rates of one product in one unit add up (`parts`
    # keeps what was stored); other units and unknown labels stay separate, never converted.
    named: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for q in rates:
        p = products.get(q.product_id) if q.product_id is not None else None
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
    rates = repo.list(
        Quantity,
        Quantity.log_id.in_(ids),
        Quantity.measure == "rate",
        scope=SCOPE,
        order_by=Quantity.id,
    )
    product_ids = [q.product_id for q in rates if q.product_id is not None]
    products = {
        p.id: p
        for p in (
            repo.list(Product, Product.id.in_(product_ids), scope=SCOPE) if product_ids else []
        )
    }
    sprays = []
    for log in done:
        ids_here = sorted((i for i in links_of.get(log.id, []) if i in by_id), key=order.get)
        aliases = [by_id[i] for i in ids_here]
        names = [
            f"{_product_name(products, q)} {_dose(float(q.value))} {q.unit}"
            for q in rates
            if q.log_id == log.id
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


def _product_name(products: dict[int, Product], q: Quantity) -> str:
    product = products.get(q.product_id) if q.product_id is not None else None
    return product.name if product is not None else (q.label or "?")
