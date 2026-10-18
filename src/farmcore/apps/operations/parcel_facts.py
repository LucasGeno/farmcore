"""Per-parcel spray facts for the map panel: last spray, season count, copper this year.

Pure: the caller (`map_sections`) passes the confirmed spray logs linked to the parcel, up to the
clock, as `SprayLog`s. The F1 rule comes first, on logs, never on register rows: a log is this
parcel's own (*attributable*) only when it links this parcel alone. A log shared with other
parcels keeps one global set of quantities (register rows copy it to every linked parcel), so
it gives this parcel a date and a count, never a product list, a dose or a copper amount.

Copper is numeric evidence, not the register check verdict (`checks._copper` keeps its own
policy). Known contributions (dose kg/ha × `cu_fraction`, from this parcel's own logs only) are
summed. The total is incomplete when any log of the year is shared with other parcels (checked
before its products: the core merge replaces a shared log's global quantities, so even a
sulphur-only shared log proves nothing about this parcel), when an own log has a copper product
with an unknown dose or fraction, or an unidentified product, or no product at all. Incomplete
with a known total above 0: "at least …, incomplete"; otherwise "copper unknown", never "at
least 0". Only unidentified or missing products (no copper seen, nothing shared): no copper
claim (None). "no copper recorded" only when every log is this parcel's own and every product
is identified and not copper. The season is the calendar year, as in the checks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from farmcore.apps.operations.checks import fmt

COPPER = "copper"  # a copper identity (`checks._known_copper`)
NOT_COPPER = "not copper"  # a known product without copper
UNIDENTIFIED = "unidentified"  # no known product: no copper claim either way


@dataclass(frozen=True)
class ProductLine:
    """One product of a spray log, as the register lists it."""

    name: str
    dose: str = ""  # blank: unknown
    unit: str = ""
    identity: str = UNIDENTIFIED
    cu_kg_ha: Decimal | None = None  # copper metal per ha, when dose and fraction are known

    @property
    def text(self) -> str:
        name = self.name or "product not recorded"
        if not self.dose:
            return f"{name} (dose unknown)"
        return f"{name} {self.dose} {self.unit}".rstrip()


@dataclass(frozen=True)
class SprayLog:
    """A confirmed spray log linked to the parcel; `parcels` are every linked alias."""

    log_id: int
    ts: datetime
    day: date  # local date of `ts`
    parcels: tuple[str, ...]
    products: tuple[ProductLine, ...] = ()

    def own(self, alias: str) -> bool:
        """Attributable to `alias`: the log links this parcel alone."""
        return self.parcels == (alias,)


@dataclass(frozen=True)
class Copper:
    """Copper this calendar year: `exact`, `at_least` (incomplete), `unknown` or `none`."""

    state: str
    kg_ha: Decimal | None = None

    @property
    def text(self) -> str:
        if self.state == "none":
            return "no copper recorded"
        if self.state == "unknown" or self.kg_ha is None:
            return "copper unknown"
        if self.state == "at_least":
            return f"at least {fmt(self.kg_ha)} kg Cu/ha, incomplete"
        return f"{fmt(self.kg_ha)} kg Cu/ha"


def copper_this_year(alias: str, logs: Iterable[SprayLog], today: date) -> Copper | None:
    """Copper on `alias` over the logs of `today`'s calendar year (the caller bounds the clock).
    None: no copper claim (only unidentified or missing products, nothing shared)."""
    year = [lg for lg in logs if alias in lg.parcels and lg.day.year == today.year]
    known: list[Decimal] = []
    shared = False  # a shared log: its global products are never this parcel's evidence
    copper_gap = False  # an own copper product with an unknown dose or fraction
    unidentified = False  # an own product of unknown identity, or an own log without products
    for lg in year:
        if not lg.own(alias):
            shared = True
            continue
        if not lg.products:
            unidentified = True
        for p in lg.products:
            if p.identity == UNIDENTIFIED:
                unidentified = True
            elif p.identity == COPPER:
                if p.cu_kg_ha is None:
                    copper_gap = True
                else:
                    known.append(p.cu_kg_ha)
    total = sum(known, Decimal(0))
    if not (shared or copper_gap or unidentified):
        return Copper("exact", total) if known else Copper("none")
    if total > 0:
        return Copper("at_least", total)
    if shared or copper_gap or known:
        return Copper("unknown")
    return None  # only unidentified or missing products: no copper claim either way


@dataclass(frozen=True)
class ParcelFacts:
    alias: str
    last: SprayLog | None
    season_sprays: int
    copper: Copper | None

    @property
    def attributable(self) -> bool:
        return self.last is not None and self.last.own(self.alias)

    @property
    def last_products(self) -> tuple[ProductLine, ...]:
        """The last spray's products, only when that log is this parcel's own."""
        return self.last.products if self.last is not None and self.attributable else ()

    @property
    def shared_with(self) -> tuple[str, ...]:
        if self.last is None or self.attributable:
            return ()
        return tuple(a for a in self.last.parcels if a != self.alias)

    @property
    def dose_text(self) -> str:
        """The dose line for a shared last spray ("" when the log is this parcel's own)."""
        if not self.shared_with:
            return ""
        return f"dose for this parcel unknown (logged with {', '.join(self.shared_with)})"


def parcel_facts(alias: str, logs: Iterable[SprayLog], now: datetime, today: date) -> ParcelFacts:
    """The facts of `alias` from its linked confirmed spray logs with `ts <= now`; `today` is
    now's local date (the season year)."""
    mine = sorted(
        (lg for lg in logs if alias in lg.parcels and lg.ts <= now),
        key=lambda lg: (lg.ts, lg.log_id),
    )
    return ParcelFacts(
        alias=alias,
        last=mine[-1] if mine else None,
        season_sprays=sum(lg.day.year == today.year for lg in mine),
        copper=copper_this_year(alias, mine, today),
    )
