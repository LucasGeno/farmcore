"""Done sprays as FIWARE `AgriParcelOperation` entities, NGSI-LD key-values (simplified).

Checked against the published smart-data-models schema
https://github.com/smart-data-models/dataModel.Agrifood/blob/1d5694e021b7070fd08b647093186e7ad8828202/AgriParcelOperation/schema.json
(commit 1d5694e021b7070fd08b647093186e7ad8828202, 2024-09-17, `$schemaVersion` 0.0.3) and its
`examples/example.jsonld`. Property names and enum values follow it:

- `required` is `id, type, hasAgriParcel, plannedStartAt, plannedEndAt`. farmcore records what
  was done, not a plan, so `plannedStartAt` / `plannedEndAt` repeat the actual start / end (the
  end is the start when the selected parcel interval has no end).
- `operationType` from `config.OPERATION_TYPES` (`spray` -> `pesticide`, in the enum
  fertiliser, inspection, pesticide, water, other); `status` `finished` (a done log; enum planned,
  ongoing, finished, scheduled, cancelled).
- `startedAt` / `endedAt`: the parcel-specific source interval (`register.parcel_intervals`),
  falling back to the log for legacy sources. These and `dateCreated` / `dateModified` are
  ISO 8601 UTC with `Z`.
- `hasAgriProductType`, `quantity` (a number), `description` as in the schema. `unitCode` is not
  a schema property (the schema sets no `additionalProperties: false`); it is the NGSI-LD core
  unit term, flattened in key-values, and only codes from `config.UNIT_CODES` (UN/CEFACT Rec 20)
  are emitted; any other unit goes in `description`.

One entity per done spray log × parcel × product with `ts_start ≤ now` (replay clock). The
product items follow the register's rule (`register.py`): product quantities, labelled
quantities (enriched from `log.props["products"][label]` when present), then `props.products`
entries without a quantity (a null dose; id suffix `0-<n>`, no quantity); a log with none gives
one entity per parcel (id suffix `0`, no quantity). So an imported log has as many
entities as register rows. One deliberate difference: a labelled quantity with no product row
and no `props.products` entry (an unmatched ISOXML product) is exported under its label, where
the register shows a blank-product row. Read through the `ScopedRepository` under
`register:read`, so a grantee export is logged under the grant.

Portable parcel ids: `export` appends, after the operations, one `AgriParcel` per parcel
they reference (`hasAgriParcel` resolves inside the file). These are deliberately partial entities,
not complete schema-valid `AgriParcel` entities. Property shapes follow `AgriParcel/schema.json`
at the same commit (`$schemaVersion` 1.0.2) and `agrifood-schema.json` (AgriFood-Commons):

- `relatedSource` (AgriFood-Commons, "List of IDs the current entity may have in external
  applications"; `application` + `applicationEntityId`, both terms in the Agrifood context, so
  no custom @context term): the IACS/LPIS id first (application `urn:ngsi-ld:AgriApp:iacs-lpis`;
  left out for a synthetic id, `props.id_label`, or an id matching an `ign-rpg-*` alias),
  then portable `ext_id` aliases, application `urn:ngsi-ld:AgriApp:<source>`, key as is
  (`register.parcel_aliases`). A Varda Global FieldID
  goes here as source `varda` when one is recorded; nothing calls Varda or makes one up.
  Distinct application/key pairs are retained, even when unrelated sources share a key.
  The demo's `iacs_parcel_id` register column carries the open RPG polygon id as a stand-in
  for the IACS reference; it is exported only under its IGN RPG application here.
  Synthetic DG AGRI aliases are omitted under the same `props.id_label` rule as the IACS entry.
- `name` is only the parcel alias (e.g. `P5`), already given to a grantee by the register.
  `area` is in m² (the schema's unit; `area_ha` × 10 000).
- Left out on purpose, though the schema requires them: `location`, because the parcel shape is
  more than a `register:read` grant reads today (portable ids only, no wider access), and
  `hasAgriCrop`, because farmcore has no `AgriCrop` entity to point to (none is invented).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from farmcore.apps.operations.register import parcel_aliases, parcel_intervals, parcel_key
from farmcore.apps.share import config
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Log, LogParcel, Parcel, Product, Quantity

SCOPE = "register:read"
ENTITY_TYPE = "AgriParcelOperation"
PARCEL_TYPE = "AgriParcel"
IACS_APPLICATION = "iacs-lpis"  # the parcel's IACS/LPIS reference (`parcel.iacs_id`)
STATUS_DONE = "finished"
MEDIA_TYPE = "application/ld+json"


def urn(kind: str, *parts: int) -> str:
    return f"urn:ngsi-ld:{kind}:farmcore-" + "-".join(str(p) for p in parts)


def iso_z(value: datetime) -> str:
    """ISO 8601 UTC with `Z` (naive values are taken as UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(q: Quantity) -> int | float:
    value = float(q.value)
    return int(value) if value.is_integer() else value


ProductItem = tuple[Quantity | None, dict[str, Any] | None]


def _product_items(log: Log, quantities: list[Quantity]) -> list[ProductItem]:
    """(quantity, props entry) per product: product or labelled quantities, then
    `props.products` entries no quantity covers; `[(None, None)]` when there are none."""
    entries: dict[str, Any] = (log.props or {}).get("products") or {}
    items: list[ProductItem] = [
        (q, entries.get(q.label) if q.label is not None else None)
        for q in quantities
        if q.product_id is not None or q.label
    ]
    covered = {q.label for q, _ in items if q is not None}
    items += [(None, entries[key]) for key in sorted(entries) if key not in covered]
    return items or [(None, None)]


def _entry_text(entry: dict[str, Any]) -> str | None:
    name = entry.get("name")
    if not name:
        return None
    substance = entry.get("active_substance")
    return f"{name} ({substance})" if substance else str(name)


def _description(
    log: Log, q: Quantity | None, product: Product | None, entry: dict[str, Any] | None
) -> str:
    if product is not None:
        text = (
            f"{product.name} ({product.active_substance}), authorisation {product.authorisation_no}"
        )
    elif entry is not None and (from_entry := _entry_text(entry)):
        text = from_entry
    elif q is not None and q.label:
        text = q.label
    else:
        return log.name
    if q is not None and q.unit not in config.UNIT_CODES:
        text += f"; {q.measure} {_number(q)} {q.unit}"
    return text


def _entity(
    log: Log,
    parcel: Parcel,
    q: Quantity | None,
    product: Product | None,
    entry: dict[str, Any] | None,
    suffix: tuple[int, ...],
    interval: tuple[datetime, datetime | None],
) -> dict[str, Any]:
    start, end = interval
    started = iso_z(start)
    ended = iso_z(end) if end else None
    e: dict[str, Any] = {
        "id": urn(ENTITY_TYPE, log.id, parcel.id, *suffix),
        "type": ENTITY_TYPE,
        "hasAgriParcel": urn("AgriParcel", parcel.id),
        "operationType": config.OPERATION_TYPES[log.type],
        "status": STATUS_DONE,
        "plannedStartAt": started,
        "plannedEndAt": ended or started,
        "startedAt": started,
    }
    if ended:
        e["endedAt"] = ended
    if product is not None:
        e["hasAgriProductType"] = urn("AgriProductType", product.id)
    e["description"] = _description(log, q, product, entry)
    if q is not None:
        e["quantity"] = _number(q)
        if q.unit in config.UNIT_CODES:
            e["unitCode"] = config.UNIT_CODES[q.unit]
    e["dateCreated"] = iso_z(log.created_at)
    e["dateModified"] = iso_z(log.updated_at)
    e["@context"] = [config.NGSI_CONTEXT]
    return e


def operations(
    repo: ScopedRepository,
    now: datetime,
    *,
    since: datetime | None = None,
    before: datetime | None = None,
    parcel_id: int | None = None,
) -> list[dict[str, Any]]:
    """The farm's done exported-type logs with `since ≤ ts_start ≤ now` (and `< before`) as
    entities, ordered by
    time, log, parcel, quantity; with `parcel_id`, that parcel's entities only (D4)."""
    done = repo.list(
        Log,
        Log.type.in_(tuple(config.OPERATION_TYPES)),
        Log.status == "done",
        Log.ts_start <= now,
        *([Log.ts_start >= since] if since is not None else []),
        *([Log.ts_start < before] if before is not None else []),
        scope=SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    if not done:
        return []
    ids = [log.id for log in done]
    links = repo.list(LogParcel, LogParcel.log_id.in_(ids), scope=SCOPE)
    parcels = {
        p.id: p
        for p in repo.list(Parcel, Parcel.id.in_({link.parcel_id for link in links}), scope=SCOPE)
    }
    quantities = repo.list(Quantity, Quantity.log_id.in_(ids), scope=SCOPE, order_by=Quantity.id)
    product_ids = {q.product_id for q in quantities if q.product_id is not None}
    products = (
        {p.id: p for p in repo.list(Product, Product.id.in_(product_ids), scope=SCOPE)}
        if product_ids
        else {}
    )
    parcels_of: dict[int, list[Parcel]] = defaultdict(list)
    for link in links:
        parcels_of[link.log_id].append(parcels[link.parcel_id])
    quantities_of: dict[int, list[Quantity]] = defaultdict(list)
    for q in quantities:
        quantities_of[q.log_id].append(q)

    intervals = parcel_intervals(repo, done, parcels_of)
    out = []
    for log in done:
        items = _product_items(log, quantities_of[log.id])
        # Quantity id, else `0` (no product at all) or `0-<n>` (n-th quantity-less entry).
        suffixes: list[tuple[int, ...]] = []
        n = 0
        for q, entry in items:
            if q is not None:
                suffixes.append((q.id,))
            elif entry is not None:
                n += 1
                suffixes.append((0, n))
            else:
                suffixes.append((0,))
        for parcel in sorted(parcels_of[log.id], key=parcel_key):
            if parcel_id is not None and parcel.id != parcel_id:
                continue
            for (q, entry), suffix in zip(items, suffixes, strict=True):
                product = products.get(q.product_id) if q and q.product_id else None
                out.append(
                    _entity(log, parcel, q, product, entry, suffix, intervals[log.id, parcel.id])
                )
    return out


def _area_m2(area_ha: Any) -> int | float:
    value = float(area_ha) * 10_000
    return int(value) if value.is_integer() else round(value, 2)


def _related(parcel: Parcel, aliases: list[tuple[str, str]]) -> list[dict[str, str]]:
    out = []
    rpg_stand_in = any(
        source.startswith("ign-rpg-") and key == parcel.iacs_id for source, key in aliases
    )
    if parcel.iacs_id and not (parcel.props or {}).get("id_label") and not rpg_stand_in:
        out.append(
            {"application": urn_app(IACS_APPLICATION), "applicationEntityId": parcel.iacs_id}
        )
    seen = {(entry["application"], entry["applicationEntityId"]) for entry in out}
    for source, key in aliases:
        pair = (urn_app(source), key)
        if pair not in seen:
            out.append({"application": pair[0], "applicationEntityId": key})
            seen.add(pair)
    return out


def urn_app(source: str) -> str:
    return f"urn:ngsi-ld:AgriApp:{source}"


def parcels(repo: ScopedRepository, parcel_ids: list[int]) -> list[dict[str, Any]]:
    """Partial `AgriParcel` entities for `parcel_ids` (the farm's only), ordered P1, P2, …"""
    if not parcel_ids:
        return []
    rows = repo.list(Parcel, Parcel.id.in_(parcel_ids), scope=SCOPE)
    aliases = parcel_aliases(repo, rows)
    out = []
    for parcel in sorted(rows, key=parcel_key):
        out.append(
            {
                "id": urn(PARCEL_TYPE, parcel.id),
                "type": PARCEL_TYPE,
                "name": parcel.alias,
                "area": _area_m2(parcel.area_ha),
                "relatedSource": _related(parcel, aliases[parcel.id]),
                "@context": [config.NGSI_CONTEXT],
            }
        )
    return out


def export(
    repo: ScopedRepository,
    now: datetime,
    *,
    since: datetime | None = None,
    before: datetime | None = None,
    parcel_id: int | None = None,
) -> list[dict[str, Any]]:
    """The NGSI-LD export: `operations`, then the `AgriParcel` of every parcel they reference."""
    ops = operations(repo, now, since=since, before=before, parcel_id=parcel_id)
    referenced = list(dict.fromkeys(e["hasAgriParcel"] for e in ops))
    ids = [int(u.removeprefix(urn(PARCEL_TYPE))) for u in referenced]
    return ops + parcels(repo, ids)
