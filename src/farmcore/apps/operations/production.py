"""Farm production mode (N8): `conventional` (the default and the demo farm) or `organic`, kept
in `farm.props["production"]`.

- Products: the farm's shortlist (`catalogue.products_for_farm`). Conventional offers all of it.
  Organic offers only products whose `props.organic` names a source (the seed's Reg. (EU)
  2021/1165 Annex I entries); any other product is withheld with the reason that no source in our
  product data shows its active substances are allowed in organic production.
- Copper tracker: each parcel's copper this season from its logged sprays up to the clock
  (`parcel_facts.copper_this_year` over `map_sections._spray_logs`: dose kg/ha × the product's
  stored `cu_fraction`, incomplete or unknown when a dose or composition is missing), shown
  against Reg. (EU) 2018/1981's 28 kg Cu/ha over 7 years (approval extended to 30 June 2029 by
  Reg. (EU) 2025/1489). Only this season is summed: earlier
  seasons are not in these records, so the screen never claims a seven-year total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from farmcore.apps.fields.seed import alias_number
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.apps.operations.map_sections import _spray_logs
from farmcore.apps.operations.parcel_facts import Copper, copper_this_year
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Farm, Parcel, Product

CONVENTIONAL = "conventional"
ORGANIC = "organic"
MODES = (CONVENTIONAL, ORGANIC)
SCOPE = "logs:read"
NO_SOURCE = (
    "Not offered in organic mode: no source in our product data shows its active substances "
    "are allowed in organic production."
)
# Reg. (EU) 2018/1981, Annex I (copper compounds, specific provisions): "Only uses resulting in a
# total application of maximum 28 kg of copper per hectare over a period of 7 years shall be
# authorised." OJ L 317, 14.12.2018, p. 16, read 17 Oct 2026.
CU_LIMIT = "28 kg Cu/ha over 7 years"
CU_SOURCE = "Reg. (EU) 2018/1981 Annex I"
CU_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32018R1981"
# 2018/1981 set the approval to expire on 31 Dec 2025. Implementing Regulation (EU) 2025/1489
# (OJ L, 25.7.2025; CELEX 32025R1489), Annex point 3: "In Part E of the Annex to Implementing
# Regulation (EU) No 540/2011, in the sixth column, expiration of approval, of row 10, Copper
# compounds, the date is replaced by '30 June 2029'." It changes only the date, not the 28 kg
# condition. Read 17 Oct 2026 (Publications Office copy of the OJ text).
CU_APPROVAL = "approval extended to 30 June 2029 by Reg. (EU) 2025/1489"
CU_APPROVAL_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32025R1489"


def mode(farm: Farm | None) -> str:
    value = (farm.props or {}).get("production") if farm else None
    return value if value in MODES else CONVENTIONAL


@dataclass(frozen=True)
class Offer:
    product: Product
    source: str | None  # the organic source, if any
    url: str | None


@dataclass(frozen=True)
class CopperRow:
    alias: str
    name: str
    copper: Copper | None  # None: no copper claim either way (unidentified products only)


def offers(products: list[Product], current: str) -> tuple[list[Offer], list[Offer]]:
    """(offered, withheld) for the mode: organic withholds every product without a source."""
    rows = []
    for p in products:
        organic = (p.props or {}).get("organic") or {}
        rows.append(Offer(p, organic.get("source") or None, organic.get("url") or None))
    if current != ORGANIC:
        return rows, []
    return [o for o in rows if o.source], [o for o in rows if not o.source]


def copper_rows(repo: ScopedRepository, now: datetime, tz: str) -> list[CopperRow]:
    today = now.astimezone(ZoneInfo(tz)).date()
    parcels = sorted(
        repo.list(Parcel, scope="fields:read"), key=lambda p: (alias_number(p.alias), p.alias)
    )
    return [
        CopperRow(
            p.alias, p.name, copper_this_year(p.alias, _spray_logs(repo, p.id, now, tz), today)
        )
        for p in parcels
    ]


def view(repo: ScopedRepository, now: datetime, tz: str) -> dict:
    farm = repo.get(Farm, repo.farm_id, scope=SCOPE)
    current = mode(farm)
    offered, withheld = offers(products_for_farm(repo, scope=SCOPE), current)
    return {
        "mode": current,
        "offered": offered,
        "withheld": withheld,
        "copper": copper_rows(repo, now, tz),
        "season": now.astimezone(ZoneInfo(tz)).year,
    }
