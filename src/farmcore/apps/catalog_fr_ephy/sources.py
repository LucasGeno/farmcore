"""E-Phy source data: download from data.gouv.fr, decode either encoding, parse the CSV files.

The Anses publishes two ZIP archives each week — one UTF-8, one Windows-1252 — with identical
data. The files are semicolon-delimited CSV with no byte-order mark, so `decode` tries UTF-8
first and falls back to Windows-1252 only when the bytes are not valid UTF-8. Parsing is strict:
a row whose fields do not line up with the header, a non-empty value that does not parse, or an
unknown authorisation/use state is reported and skipped, never imported half-way.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

# The data.gouv.fr dataset that republishes the Anses E-Phy catalogue weekly.
DATASET_URL = "https://www.data.gouv.fr/api/1/datasets/575e9fac88ee38072a640390/"
SOURCE = "ephy-fr"
# data.gouv.fr reports `license: fr-lo` — Licence Ouverte / Open Licence v1.0, not 2.0.
LICENCE = "Licence Ouverte / Open Licence"
LICENCE_ID = "fr-lo"
ATTRIBUTION = "Données E-Phy - Anses"

# The archive's exact file names. `produits_*.csv` is not enough: the UTF-8 archive also ships
# `produits_classe_et_mention_danger_utf8.csv`, which sorts before `produits_utf8.csv`.
_PRODUCT_FILES = ("produits_utf8.csv", "produits_Windows-1252.csv")
_USE_FILES = ("produits_usages_utf8.csv", "produits_usages_Windows-1252.csv")

# Column names exactly as the source publishes them (products.CSV / produits_usages.CSV).
_PRODUCT_COLUMNS = {
    "amm": "numero AMM",
    "name": "nom produit",
    "trade_names": "seconds noms commerciaux",
    "etat": "Etat d’autorisation",
    "holder": "titulaire",
    "gamme": "gamme usage",
    "fonctions": "fonctions",
    "formulation": "formulations",
    "substances": "Substances actives",
    "withdrawal_date": "Date de retrait du produit",
}
_USE_COLUMNS = {
    "amm": "numero AMM",
    "identifiant": "identifiant usage",
    "etat": "etat usage",
    "dose": "dose retenue",
    "dose_unit": "dose retenue unite",
    "phi": "delai avant recolte jour",
    "applications": "nombre max d'application",
    "distribution_end": "date fin distribution",
    "use_end": "date fin utilisation",
    "interval": "intervalle minimum entre applications (jour)",
    "znt_aquatic": "ZNT aquatique (en m)",
    "znt_arthropods": "ZNT arthropodes non cibles (en m)",
    "znt_plants": "ZNT plantes non cibles (en m)",
}

_PRODUCT_ETAT = {"AUTORISE": False, "RETIRE": True}
_USE_ETAT = {
    "Autorisé": "authorised",
    "Retrait": "withdrawn",
    "Autorisé (Provisoire)": "provisional",
}


class SourceError(RuntimeError):
    """The source cannot be downloaded or read."""


class InvalidRow(ValueError):
    """A non-empty value the importer cannot type; the caller reports and skips the row."""


@dataclass(frozen=True)
class SourceMeta:
    """Provenance of a downloaded archive, recorded on `catalog_import` (C5: source and date)."""

    licence: str = LICENCE
    licence_id: str = LICENCE_ID
    attribution: str = ATTRIBUTION
    resource_url: str = ""
    archive_name: str = ""
    archive_date: str = ""


@dataclass(frozen=True)
class ProductRow:
    amm: str
    name: str
    withdrawn: bool
    holder: str
    gamme: str
    fonctions: tuple[str, ...]
    formulation: str
    substances: tuple[dict[str, str], ...]
    trade_names: tuple[str, ...]
    withdrawal_date: str  # ISO YYYY-MM-DD


@dataclass(frozen=True)
class UseRow:
    amm: str
    identifiant: str
    crop: str
    target: str | None
    max_dose: Decimal | None
    max_dose_unit: str | None
    interval_days: Decimal | None
    max_applications: int | None
    phi_days: int | None
    distribution_end: date | None
    use_end: date | None
    znt_aquatic: Decimal | None
    znt_arthropods: Decimal | None
    znt_plants: Decimal | None
    state: str


@dataclass(frozen=True)
class Parsed:
    rows: tuple[ProductRow | UseRow, ...]
    skipped: int
    reasons: tuple[str, ...]


def decode(raw: bytes) -> str:
    """The CSV text: UTF-8 first (stripping a byte-order mark), Windows-1252 only on failure."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def content_hash(products_raw: bytes, uses_raw: bytes) -> str:
    return hashlib.sha256(products_raw + b"\n" + uses_raw).hexdigest()


def _read_rows(raw: bytes) -> tuple[list[str], list[list[str]], list[str]]:
    reader = csv.reader(io.StringIO(decode(raw)), delimiter=";")
    header = next(reader)
    width = len(header)
    rows: list[list[str]] = []
    reasons: list[str] = []
    for line, row in enumerate(reader, start=2):
        if len(row) != width:
            reasons.append(f"line {line}: {len(row)} columns, expected {width}")
            continue
        rows.append(row)
    return header, rows, reasons


def _index(header: list[str], name: str) -> int:
    try:
        return header.index(name)
    except ValueError:
        raise SourceError(f"E-Phy file is missing column {name!r}; refusing to guess") from None


def _decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise InvalidRow(f"malformed decimal {value!r}") from None


def _integer(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise InvalidRow(f"malformed integer {value!r}") from None


def _date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        raise InvalidRow(f"malformed date {value!r}") from None


def _split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split("|") if part.strip())


def _substances(text: str) -> tuple[dict[str, str], ...]:
    """`diméthoate (Dimethoate) 400.0 g/L | ...` -> one dict per substance. The reference is the
    last top-level parenthesised group (nested parens, as in
    `… (Cydia pomonella Granulovirus (CpGV))`, count as one); concentration and unit are the two
    trailing tokens. Best effort: a token with no concentration/unit keeps name and reference."""
    result: list[dict[str, str]] = []
    for token in text.split("|"):
        token = token.strip()
        if not token:
            continue
        ref = ""
        body = token
        groups = _top_level_parens(token)
        if groups:
            start, end = groups[-1]
            ref = token[start + 1 : end].strip()
            body = (token[:start] + token[end + 1 :]).strip()
        parts = body.split()
        if len(parts) >= 3 and _looks_decimal(parts[-2]):
            name = " ".join(parts[:-2]).strip()
            result.append({"name": name, "ref": ref, "concentration": parts[-2], "unit": parts[-1]})
        elif ref:
            result.append({"name": body, "ref": ref})
        else:
            result.append({"name": body or token})
    return tuple(result)


def _top_level_parens(token: str) -> list[tuple[int, int]]:
    """The `(start, end)` spans of each parenthesised group that is not nested inside another."""
    groups: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    for index, char in enumerate(token):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append((start, index))
                start = None
    return groups


def _looks_decimal(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except InvalidOperation:
        return False


def parse_products(raw: bytes) -> Parsed:
    header, rows, reasons = _read_rows(raw)
    cols = {key: _index(header, name) for key, name in _PRODUCT_COLUMNS.items()}
    parsed: list[ProductRow] = []
    for line, cells in enumerate(rows, start=2):
        amm = cells[cols["amm"]].strip()
        name = cells[cols["name"]].strip()
        if not amm or not name or len(name) > 200:
            reasons.append(f"line {line}: missing or invalid AMM/product name")
            continue
        etat = cells[cols["etat"]].strip()
        withdrawn = _PRODUCT_ETAT.get(etat)
        if withdrawn is None:
            reasons.append(f"line {line}: unknown authorisation state {etat!r}")
            continue
        try:
            withdrawal_date = _date(cells[cols["withdrawal_date"]])
        except InvalidRow as exc:
            reasons.append(f"line {line}: {exc}")
            continue
        parsed.append(
            ProductRow(
                amm=amm,
                name=name,
                withdrawn=withdrawn,
                holder=cells[cols["holder"]].strip(),
                gamme=cells[cols["gamme"]].strip(),
                fonctions=_split(cells[cols["fonctions"]]),
                formulation=cells[cols["formulation"]].strip(),
                substances=_substances(cells[cols["substances"]]),
                trade_names=_split(cells[cols["trade_names"]]),
                withdrawal_date=withdrawal_date.isoformat() if withdrawal_date else "",
            )
        )
    return Parsed(tuple(parsed), len(reasons), tuple(reasons))


def _identifiant(value: str) -> tuple[str, str | None]:
    parts = [part.strip() for part in value.split("*") if part.strip()]
    crop = parts[0] if parts else ""
    target = parts[2] if len(parts) == 3 else None
    return crop, target


def parse_uses(raw: bytes) -> Parsed:
    header, rows, reasons = _read_rows(raw)
    cols = {key: _index(header, name) for key, name in _USE_COLUMNS.items()}
    parsed: list[UseRow] = []
    for line, cells in enumerate(rows, start=2):
        amm = cells[cols["amm"]].strip()
        identifiant = cells[cols["identifiant"]].strip()
        if not amm or not identifiant:
            reasons.append(f"line {line}: missing or invalid AMM/usage identifier")
            continue
        crop, target = _identifiant(identifiant)
        if not crop:
            reasons.append(f"line {line}: unparseable usage identifier")
            continue
        etat = cells[cols["etat"]].strip()
        state = _USE_ETAT.get(etat)
        if state is None:
            reasons.append(f"line {line}: unknown use state {etat!r}")
            continue
        try:
            dose = _decimal(cells[cols["dose"]])
            interval = _decimal(cells[cols["interval"]])
            applications = _integer(cells[cols["applications"]])
            phi = _integer(cells[cols["phi"]])
            distribution_end = _date(cells[cols["distribution_end"]])
            use_end = _date(cells[cols["use_end"]])
            znt_aquatic = _decimal(cells[cols["znt_aquatic"]])
            znt_arthropods = _decimal(cells[cols["znt_arthropods"]])
            znt_plants = _decimal(cells[cols["znt_plants"]])
        except InvalidRow as exc:
            reasons.append(f"line {line}: {exc}")
            continue
        parsed.append(
            UseRow(
                amm=amm,
                identifiant=identifiant,
                crop=crop,
                target=target,
                max_dose=dose,
                max_dose_unit=cells[cols["dose_unit"]].strip() or None,
                interval_days=interval,
                max_applications=applications,
                phi_days=phi,
                distribution_end=distribution_end,
                use_end=use_end,
                znt_aquatic=znt_aquatic,
                znt_arthropods=znt_arthropods,
                znt_plants=znt_plants,
                state=state,
            )
        )
    return Parsed(tuple(parsed), len(reasons), tuple(reasons))


def find_files(dest: Path) -> tuple[Path, Path]:
    """The `products` and `products_usages` files in an extracted archive, by exact name."""
    products = [dest / name for name in _PRODUCT_FILES if (dest / name).is_file()]
    uses = [dest / name for name in _USE_FILES if (dest / name).is_file()]
    if not products or not uses:
        raise SourceError(f"{dest} did not contain produits/produits_usages CSV files")
    return products[0], uses[0]


def extract(zip_path: Path, dest: Path) -> tuple[Path, Path]:
    """Extract an E-Phy CSV archive and return the two files it must contain."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest)
    return find_files(dest)


def _resource_date(resource: dict) -> str:
    last = resource.get("last_modified") or ""
    try:
        return datetime.fromisoformat(last.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _csv_zip_resource() -> tuple[str, SourceMeta]:
    """The current UTF-8 CSV archive URL and its provenance, from the dataset's resources."""
    response = httpx.get(DATASET_URL, timeout=30)
    response.raise_for_status()
    dataset = response.json()
    licence_id = dataset.get("license") or LICENCE_ID
    for resource in dataset["resources"]:
        if resource.get("format") == "zip" and "utf8" in resource.get("title", "").lower():
            name = resource.get("title", "")
            return resource["url"], SourceMeta(
                licence_id=licence_id,
                resource_url=resource["url"],
                archive_name=name,
                archive_date=_resource_date(resource),
            )
    raise SourceError("data.gouv.fr returned no UTF-8 CSV archive for the E-Phy dataset")


def download(dest: Path) -> tuple[Path, Path, SourceMeta]:
    """Download the E-Phy CSV archive and extract `products` and `products_usages`. Returns their
    paths and the archive's provenance. Only the CLI and the weekly job call this; tests read
    recorded fixtures."""
    url, meta = _csv_zip_resource()
    response = httpx.get(url, timeout=120)
    response.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(dest)
    products, uses = find_files(dest)
    return products, uses, meta


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ATTRIBUTION",
    "LICENCE",
    "LICENCE_ID",
    "SOURCE",
    "InvalidRow",
    "Parsed",
    "ProductRow",
    "SourceError",
    "SourceMeta",
    "UseRow",
    "content_hash",
    "decode",
    "download",
    "extract",
    "find_files",
    "parse_products",
    "parse_uses",
]
