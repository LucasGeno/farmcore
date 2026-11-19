"""Turn public registry crop codes into FarmCore crops, then write crop seasons in bulk.

The crosswalk is deliberately small and static: a registry code maps to one FarmCore crop only
when the code names that crop. A code that is missing from the table stays unknown and is
reported with the parcel; it is never guessed from a label, a variety or a neighbouring parcel.

Sources
- FR: IGN RPG 2024 culture codes, `REF_CULTURES_2024.csv`
  (https://data.geopf.fr/annexes/ressources/documentation/REF_CULTURES_2024.csv, Licence
  Ouverte Etalab 2.0).
- ES: SIGPAC use codes, Anexo IV of Real Decreto 1047/2022, published by FEGA
  (https://www.fega.gob.es/es/content/%C2%BFqu%C3%A9-usos-se-distinguen-en-sigpac) and the
  SIGPAC code service (https://sigpac-hubcloud.es/codigossigpac/cod_uso_sigpac.json,
  CC BY 4.0).
- EPPO codes: EPPO Global Database (https://gd.eppo.int). `None` means the code does not
  name a species, so the season keeps an unknown EPPO code rather than inventing one.

Crops become `crop_season` rows through the fields app's season service: a new season is
appended, an overlapping one is left untouched, and earlier seasons are never rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from farmcore.apps.fields import seasons
from farmcore.core.access import ScopedRepository
from farmcore.core.farm_location import farm_country
from farmcore.core.models.canonical import CropSeason, Farm, Parcel

SCOPE = "fields:read"


@dataclass(frozen=True)
class Crop:
    """One FarmCore crop key with its farmer-facing name and EPPO code (when it names a species)."""

    key: str
    name: str
    eppo: str | None


CROPS: dict[str, Crop] = {
    crop.key: crop
    for crop in (
        Crop("vine", "Vine", "VITVI"),
        Crop("olive", "Olive", "OLVEU"),
        Crop("soft-wheat", "Soft wheat", "TRZAX"),
        Crop("durum-wheat", "Durum wheat", "TRZDU"),
        Crop("barley", "Barley", "HORVX"),
        Crop("grain-maize", "Grain maize", "ZEAMX"),
        Crop("rapeseed", "Rapeseed", "BRSNN"),
        Crop("sunflower", "Sunflower", "HELAN"),
        Crop("rice", "Rice", "ORYSA"),
        Crop("potato", "Potato", "SOLTU"),
        Crop("alfalfa", "Alfalfa", "MEDSA"),
        Crop("soybean", "Soybean", "GLXMA"),
        Crop("grassland", "Grassland", None),
        Crop("fruit-trees", "Fruit trees", None),
        Crop("citrus", "Citrus", None),
        Crop("nuts", "Nuts", None),
        Crop("vegetables", "Vegetables", None),
        Crop("walnut", "Walnut", "IUGRE"),
        Crop("hazelnut", "Hazelnut", "CYLAV"),
        Crop("peach", "Peach", "PRNPS"),
        Crop("pear", "Pear", "PYUCO"),
        Crop("cherry", "Cherry", "PRNAV"),
        Crop("plum", "Plum", "PRNDO"),
    )
}

# Which parcel prop carries the registry code, and what the owner calls that code list.
CODE_PROP: dict[str, tuple[str, str]] = {
    "FR": ("code_cultu", "RPG code"),
    "ES": ("uso_sigpac", "SIGPAC use code"),
}

# Registry code -> FarmCore crop key. Only codes that name the crop are here: generic codes
# ("VRG" orchard, SIGPAC "TA" arable land) and associations ("VO" vine-olive, "FV" nuts-vine)
# stay unknown because they do not say which crop the parcel carries.
CROSSWALK: dict[str, dict[str, str]] = {
    "FR": {
        "BTH": "soft-wheat",  # blé tendre d'hiver
        "BTP": "soft-wheat",  # blé tendre de printemps
        "BDH": "durum-wheat",  # blé dur d'hiver
        "BDP": "durum-wheat",  # blé dur de printemps
        "ORH": "barley",  # orge d'hiver
        "ORP": "barley",  # orge de printemps
        "MIS": "grain-maize",  # maïs (hors maïs doux)
        "CZH": "rapeseed",  # colza d'hiver
        "CZP": "rapeseed",  # colza de printemps
        "TRN": "sunflower",  # tournesol
        "VRC": "vine",  # vigne
        "PPH": "grassland",  # prairie permanente
        "PTR": "grassland",  # prairie temporaire
        "OLI": "olive",  # olive
        "RIZ": "rice",  # riz
        "PTC": "potato",  # pomme de terre
        "LUZ": "alfalfa",  # luzerne
        "SOJ": "soybean",  # soja
        "NOX": "walnut",  # noix
        "NOS": "hazelnut",  # noisette
        "PVT": "peach",  # pêche
        "PWT": "pear",  # poire
        "CBT": "cherry",  # cerise
        "PRU": "plum",  # prune
    },
    "ES": {
        "VI": "vine",  # viñedo
        "OV": "olive",  # olivar
        "FY": "fruit-trees",  # frutales
        "CI": "citrus",  # cítricos
        "FS": "nuts",  # frutos secos
        "PA": "grassland",  # pasto con arbolado
        "PR": "grassland",  # pasto arbustivo
        "PS": "grassland",  # pastizal
        "TH": "vegetables",  # huerta
    },
}

# The crops each region's farmers grow most often, used as the "common crops" shortlist.
REGIONAL_CROPS: dict[str, tuple[str, ...]] = {
    "FR": (
        "vine",
        "soft-wheat",
        "grain-maize",
        "barley",
        "rapeseed",
        "sunflower",
        "grassland",
        "potato",
        "alfalfa",
    ),
    "ES": (
        "olive",
        "vine",
        "soft-wheat",
        "barley",
        "sunflower",
        "fruit-trees",
        "citrus",
        "nuts",
        "vegetables",
        "grassland",
    ),
}


class CropError(ValueError):
    """The owner's crop choice is unknown; the route answers 422."""


@dataclass(frozen=True)
class AssignResult:
    """What a bulk assignment did: seasons added and parcels whose covering season was kept."""

    assigned: int
    covered: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    """One parcel on the crops step: its registry code, the matching crop and current season."""

    parcel: Parcel
    code: str
    code_label: str
    crop: Crop | None
    season: CropSeason | None

    @property
    def unknown_code(self) -> str:
        return self.code if self.code and self.crop is None else ""

    @property
    def selected(self) -> bool:
        return self.crop is not None and self.season is None

    @property
    def state(self) -> str:
        if self.season is not None:
            return "covered"
        if self.unknown_code:
            return "unknown"
        if self.crop is not None:
            return "suggested"
        return "choose"


def code_for(country: str | None, props: Mapping[str, object] | None) -> str:
    """The parcel's raw registry code for its country, or "" when it has none."""
    if country not in CODE_PROP:
        return ""
    name, _ = CODE_PROP[country]
    value = (props or {}).get(name)
    return str(value).strip().upper() if value is not None else ""


def crosswalk(country: str | None, code: str) -> Crop | None:
    """The FarmCore crop a published registry code names, or None when it stays unknown."""
    key = CROSSWALK.get(country or "", {}).get((code or "").strip().upper())
    return CROPS.get(key) if key else None


def suggested(country: str | None, props: Mapping[str, object] | None) -> Crop | None:
    """The crop pre-filled from a registry code; never a guess from a name or label."""
    return crosswalk(country, code_for(country, props))


def regional(country: str | None) -> tuple[Crop, ...]:
    return tuple(CROPS[key] for key in REGIONAL_CROPS.get(country or "", ()))


def options(country: str | None) -> list[Crop]:
    """Regional common crops first, then every other crop alphabetically."""
    chosen = regional(country)
    seen = {crop.key for crop in chosen}
    return [*chosen, *(CROPS[key] for key in sorted(CROPS) if key not in seen)]


def rows(repo: ScopedRepository, day: date) -> tuple[list[Row], str | None]:
    """Every parcel of the farm with its pre-filled crop for `day`, and the farm's country."""
    farm = repo.session.get(Farm, repo.farm_id)
    if farm is None:  # the repository is bound to a committed farm
        raise CropError("Farm not found.")
    country = farm_country(farm)
    parcels = repo.list(
        Parcel,
        Parcel.farm_id == repo.farm_id,
        scope=SCOPE,
        order_by=(Parcel.alias, Parcel.id),
    )
    covering: dict[int, CropSeason] = {}
    for season in repo.list(
        CropSeason,
        CropSeason.farm_id == repo.farm_id,
        scope=SCOPE,
        order_by=(CropSeason.starts_on, CropSeason.id),
    ):
        if season.starts_on <= day and (season.ends_on is None or season.ends_on >= day):
            covering[season.parcel_id] = season
    code_label = CODE_PROP.get(country or "", ("", "Registry code"))[1]
    return (
        [
            Row(
                parcel=parcel,
                code=code_for(country, parcel.props),
                code_label=code_label,
                crop=suggested(country, parcel.props),
                season=covering.get(parcel.id),
            )
            for parcel in parcels
        ],
        country,
    )


def assign(repo: ScopedRepository, *, starts_on: date, choices: Mapping[int, str]) -> AssignResult:
    """Append one crop season per chosen parcel; leave covered or unset parcels untouched.

    `choices` maps a parcel id to a FarmCore crop key. An unknown key raises `CropError` before
    any write (the route's transaction rolls back). A parcel that already has a season covering
    `starts_on` is reported and kept: history is never rewritten.
    """
    selected: list[tuple[Parcel, Crop]] = []
    for parcel_id, key in choices.items():
        crop = CROPS.get(key)
        if crop is None:
            raise CropError(f"Unknown crop: {key}.")
        parcel = repo.get(Parcel, parcel_id, scope=SCOPE)
        if parcel is not None and parcel.farm_id == repo.farm_id:
            selected.append((parcel, crop))
    assigned = 0
    covered: list[str] = []
    for parcel, crop in selected:
        if seasons.season_on(repo, parcel.id, starts_on) is not None:
            covered.append(parcel.alias)
            continue
        try:
            seasons.create(
                repo,
                parcel,
                {
                    "starts_on": starts_on,
                    "crop_eppo": crop.eppo,
                    "crop_name": crop.name,
                },
            )
        except seasons.SeasonConflict:
            covered.append(parcel.alias)
            continue
        assigned += 1
    return AssignResult(assigned=assigned, covered=tuple(covered))
