"""Register checks: label and copper rules on each confirmed spray, as plain-word lines.

Pure: the caller (`register.register_rows(..., checks=True)`) turns confirmed spray logs into
`Application`s (one per log × parcel × product with a known product) and gets back, per log, a
list of `Check(key, status, label, detail)`; `status` is `ok`, `warn` or `info`.

Rules, per parcel, then one line per spray across its parcels (the worst status wins, then the
highest value; the detail names that parcel when the parcels differ):

- `copper`: kg Cu/ha this calendar year on the parcel, this spray included, from every product
  with a `cu_fraction` (dose kg/ha × fraction). Ok up to `CU_YEARLY_KG_HA`, else warn: Reg. (EU)
  2018/1981 caps copper at 28 kg Cu/ha over 7 years, so one year may go above 4 (amber, not red).
  Only on sprays with a copper product.
- `applications:<product id>`: sprays of this product on the parcel this season (calendar year),
  this one included, against the label's maximum.
- `interval:<product id>`: days since the previous spray of this product on the parcel, against
  the label's minimum interval (only when there is a previous spray), including prior years.
- `dose:<product id>`: the dose against the label's maximum dose (kg/ha doses only).
- `phi`: "Harvest not before <date>": the spray day plus the longest pre-harvest interval of its
  products (info).

Label values come from `Product.props` (`Label.from_props`; the operations seed writes them from
each product's E-Phy page). A missing value skips its check, never guesses.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

CU_YEARLY_KG_HA = Decimal("4")  # 28 kg Cu/ha over 7 years, on average
CU_SEVEN_YEARS_KG_HA = Decimal("28")
CU_REGULATION = "Reg. (EU) 2018/1981"
CU_REGULATION_URL = "https://eur-lex.europa.eu/eli/reg_impl/2018/1981/oj"

OK, WARN, INFO = "ok", "warn", "info"
WORDS = {OK: "OK", WARN: "Warning", INFO: "Note"}  # colour is never the only signal
_RANK = {INFO: 0, OK: 1, WARN: 2}
_KG_PER = {"kg/ha": Decimal(1), "g/ha": Decimal("0.001")}


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def fmt(value: Decimal) -> str:
    """At most 2 decimals, no trailing zeros: 0.4, 4, 2.45."""
    text = f"{value.quantize(Decimal('0.01')):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def dose_kg_ha(value: Decimal, unit: str) -> Decimal | None:
    """The dose in kg/ha, or None when the unit is not a mass per hectare."""
    factor = _KG_PER.get(unit.strip().lower())
    return None if factor is None else value * factor


@dataclass(frozen=True, slots=True)
class Label:
    """A product's label limits on the crop (E-Phy usage); None: not on the label / unknown."""

    max_dose_kg_ha: Decimal | None = None
    max_applications: int | None = None
    phi_days: int | None = None
    min_interval_days: int | None = None
    cu_fraction: Decimal | None = None  # copper metal per kg of product

    @classmethod
    def from_props(cls, props: Mapping[str, Any] | None) -> Label:
        p = props or {}
        return cls(
            max_dose_kg_ha=_dec(p.get("label_max_dose_kg_ha")),
            max_applications=_int(p.get("label_max_applications")),
            phi_days=_int(p.get("phi_days")),
            min_interval_days=_int(p.get("min_interval_days")),
            cu_fraction=_dec(p.get("cu_fraction")),
        )


@dataclass(frozen=True, slots=True)
class Application:
    """One product on one parcel in one confirmed spray log."""

    log_id: int
    ts: datetime  # orders sprays; `day` is its local date
    day: date
    parcel: str  # the parcel alias
    product_id: int
    product_name: str
    substance: str
    dose_kg_ha: Decimal | None
    label: Label

    @property
    def order(self) -> tuple[datetime, int]:
        return self.ts, self.log_id


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    status: str
    label: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "status": self.status, "label": self.label, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class _ParcelCheck:
    """One check on one parcel; `value` ranks parcels (highest = tightest) and `shown` is the
    value as the detail names it for the other parcels."""

    check: Check
    parcel: str
    value: Decimal
    shown: str


# --- per-parcel rules -------------------------------------------------------------------------


def _known_copper(a: Application) -> bool:
    return a.label.cu_fraction is not None or a.substance.strip().casefold() == "copper"


def _copper(this: list[Application], year: list[Application], parcel: str) -> _ParcelCheck | None:
    if not any(_known_copper(a) for a in this):
        return None
    total = sum(
        (
            a.dose_kg_ha * a.label.cu_fraction
            for a in year
            if a.label.cu_fraction is not None and a.dose_kg_ha is not None
        ),
        Decimal(0),
    )
    cap = f"{fmt(CU_SEVEN_YEARS_KG_HA)} kg Cu/ha over 7 years is the legal cap ({CU_REGULATION})"
    if total > CU_YEARLY_KG_HA:
        status = WARN
        detail = f"Above the {fmt(CU_YEARLY_KG_HA)} kg Cu/ha yearly average; {cap}."
    else:
        status = OK
        detail = f"Copper metal from every copper spray on the parcel this year; {cap}."
    label = f"Copper {fmt(total)} of {fmt(CU_YEARLY_KG_HA)} kg Cu/ha this year"
    incomplete = any(
        _known_copper(a) and (a.label.cu_fraction is None or a.dose_kg_ha is None) for a in year
    )
    if incomplete:
        status = WARN
        label = f"Copper at least {fmt(total)} kg Cu/ha this year (incomplete)"
        detail = (
            "A copper dose or composition is unknown, or its rate cannot be converted to kg/ha; "
            f"the known total is a lower bound. {cap}."
        )
    return _ParcelCheck(Check("copper", status, label, detail), parcel, total, fmt(total))


def _applications(a: Application, season: list[Application]) -> _ParcelCheck | None:
    cap = a.label.max_applications
    if cap is None:
        return None
    n = len({s.log_id for s in season if s.product_id == a.product_id})
    if n > cap:
        status, label = WARN, f"{n} {a.substance} applications this season, label max {cap}"
    else:
        status, label = OK, f"{n} of {cap} {a.substance} applications this season"
    detail = f"{a.product_name}: at most {cap} applications a season on the label (E-Phy)."
    return _ParcelCheck(
        Check(f"applications:{a.product_id}", status, label, detail), a.parcel, Decimal(n), str(n)
    )


def _interval(a: Application, season: list[Application]) -> _ParcelCheck | None:
    gap = a.label.min_interval_days
    earlier = [s for s in season if s.product_id == a.product_id and s.log_id != a.log_id]
    if gap is None or not earlier:
        return None
    days = (a.day - max(earlier, key=lambda s: s.order).day).days
    status = WARN if days < gap else OK
    label = f"{days} days since the last {a.substance} spray, label min {gap}"
    detail = f"{a.product_name}: at least {gap} days between applications on the label (E-Phy)."
    # Fewer days is tighter: rank by the negated gap.
    return _ParcelCheck(
        Check(f"interval:{a.product_id}", status, label, detail),
        a.parcel,
        Decimal(-days),
        f"{days} days",
    )


def _dose(apps: list[Application]) -> _ParcelCheck | None:
    a = apps[0]
    cap = a.label.max_dose_kg_ha
    known = [s.dose_kg_ha for s in apps if s.dose_kg_ha is not None]
    if cap is None or not known:
        return None
    name = a.substance.capitalize()
    total = sum(known, Decimal(0))
    if total > cap:
        status, label = (
            WARN,
            f"{name} dose {fmt(total)} kg/ha, above the label max {fmt(cap)}",
        )
    else:
        status, label = OK, f"{name} dose {fmt(total)} kg/ha, label max {fmt(cap)}"
    detail = f"{a.product_name}: at most {fmt(cap)} kg/ha per application on the label (E-Phy)."
    if len(known) != len(apps):
        status = WARN
        label = f"{name} dose incomplete (at least {fmt(total)} kg/ha), label max {fmt(cap)}"
        detail = f"An unknown or unconvertible rate prevents checking the full dose. {detail}"
    check = Check(f"dose:{a.product_id}", status, label, detail)
    return _ParcelCheck(check, a.parcel, total, fmt(total))


def _phi(apps: list[Application]) -> Check | None:
    with_phi = [a for a in apps if a.label.phi_days is not None]
    if not with_phi:
        return None
    a = max(with_phi, key=lambda x: x.label.phi_days or 0)
    days = a.label.phi_days or 0
    harvest = a.day + timedelta(days=days)
    label = f"Harvest not before {harvest.day} {harvest:%b}"
    detail = (
        f"{a.product_name}: {days}-day pre-harvest interval on the label (E-Phy), "
        f"so harvest from {harvest.isoformat()}."
    )
    return Check("phi", INFO, label, detail)


# --- per spray --------------------------------------------------------------------------------


def _merge(found: list[_ParcelCheck]) -> Check:
    """One line across parcels: the worst status, then the highest value; the detail names the
    parcels with that value and the others' values (a range past 3) when they differ."""
    worst = max(found, key=lambda f: (_RANK[f.check.status], f.value))
    if len({f.value for f in found}) == 1:
        return worst.check
    top = [f for f in found if (f.check.status, f.value) == (worst.check.status, worst.value)]
    rest = sorted((f for f in found if f not in top), key=lambda f: f.value)
    if len(rest) <= 3:
        others = ", ".join(f"{f.parcel} {f.shown}" for f in rest)
    else:
        lo, hi = rest[0].shown, rest[-1].shown
        others = f"{len(rest)} other parcels {lo}" + ("" if lo == hi else f" to {hi}")
    head = "Shortest" if worst.check.key.startswith("interval:") else "Highest"
    where = ", ".join(f.parcel for f in top)
    detail = f"{head} on {where} ({others}). {worst.check.detail}"
    return Check(worst.check.key, worst.check.status, worst.check.label, detail)


def _order(key: str, product_order: dict[int, int]) -> tuple[int, int]:
    kind, _, pid = key.partition(":")
    kinds = ("copper", "applications", "interval", "dose", "phi")
    return kinds.index(kind), product_order.get(int(pid), 0) if pid else 0


def _spray(apps: list[Application], history_of: Callable[[str], list[Application]]) -> list[Check]:
    """Checks for one spray log; `history_of(parcel)` includes prior years for intervals."""
    per_key: dict[str, list[_ParcelCheck]] = defaultdict(list)
    me = apps[0]
    by_parcel: dict[str, list[Application]] = defaultdict(list)
    for a in apps:
        by_parcel[a.parcel].append(a)
    for parcel, mine in by_parcel.items():
        upto = [s for s in history_of(parcel) if s.order <= me.order]
        year = [s for s in upto if s.day.year == me.day.year]
        found = [_copper(mine, year, parcel)]
        by_product: dict[int, list[Application]] = defaultdict(list)
        for a in mine:
            by_product[a.product_id].append(a)
        for product_apps in by_product.values():
            a = product_apps[0]
            found += [_applications(a, year), _interval(a, upto), _dose(product_apps)]
        for f in found:
            if f is not None:
                per_key[f.check.key].append(f)
    out = [_merge(found) for found in per_key.values()]
    phi = _phi(apps)
    if phi is not None:
        out.append(phi)
    product_order = {pid: i for i, pid in enumerate(dict.fromkeys(a.product_id for a in apps))}
    return sorted(out, key=lambda c: _order(c.key, product_order))


def checks_by_log(applications: Iterable[Application]) -> dict[int, list[Check]]:
    """Checks for every spray log among `applications` (all confirmed sprays the caller knows,
    which bounds the history: later or unconfirmed sprays must not be passed)."""
    apps = sorted(applications, key=lambda a: a.order)
    history: dict[str, list[Application]] = defaultdict(list)
    of_log: dict[int, list[Application]] = defaultdict(list)
    for a in apps:
        history[a.parcel].append(a)
        of_log[a.log_id].append(a)
    return {log_id: _spray(mine, lambda parcel: history[parcel]) for log_id, mine in of_log.items()}


def spray_checks(log_id: int, applications: Iterable[Application]) -> list[Check]:
    """Checks for one spray log, from the confirmed `applications` (its own included)."""
    return checks_by_log(applications).get(log_id, [])


def summary(checks: Iterable[Mapping[str, str]]) -> str:
    """One CSV cell: `OK: …; Warning: …; Note: …` in the checks' order."""
    return "; ".join(f"{WORDS[c['status']]}: {c['label']}" for c in checks)
