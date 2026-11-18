"""E-Phy source data: download from data.gouv.fr, decode either encoding, parse the CSV files.

The Anses publishes two ZIP archives each week — one UTF-8, one Windows-1252 — with identical
data. The files are semicolon-delimited CSV with no byte-order mark, so `decode` tries UTF-8
first and falls back to Windows-1252 only when the bytes are not valid UTF-8. Parsing is strict
about the column count: a row whose fields do not line up with the header is reported and
skipped, never imported half-way.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

# The data.gouv.fr dataset that republishes the Anses E-Phy catalogue weekly.
DATASET_URL = "https://www.data.gouv.fr/api/1/datasets/575e9fac88ee38072a640390/"
SOURCE = "ephy-fr"
LICENCE = "Licence Ouverte / Open Licence 2.0"

# Column names exactly as the source publishes them (products.CSV / produits_usages.CSV).
_PRODUCT_COLUMNS = {
    "amm": "numero AMM",
    "name": "nom produit",
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
    "applications": "nombre max d'application",
    "interval": "intervalle minimum entre applications (jour)",
}

_PRODUCT_ETAT = {"AUTORISE": False, "RETIRE": True}
_USE_ETAT = {
    "Autorisé": "authorised",
    "Retrait": "withdrawn",
    "Autorisé (Provisoire)": "provisional",
}


class SourceError(RuntimeError):
    """The source cannot be downloaded or read."""


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
    withdrawal_date: str


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
    state: str


@dataclass(frozen=True)
class Parsed:
    rows: tuple[ProductRow | UseRow, ...]
    skipped: int
    reasons: tuple[str, ...] = field(default=())


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
        return None


def _integer(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _substances(text: str) -> tuple[dict[str, str], ...]:
    """`diméthoate (Dimethoate) 400.0 g/L | ...` -> one dict per substance. Best effort: a token
    that does not match the name/ref/concentration shape is kept whole under `name`."""
    result: list[dict[str, str]] = []
    for token in text.split("|"):
        token = token.strip()
        if not token:
            continue
        name, _, rest = token.partition("(")
        name = name.strip()
        if not rest:
            result.append({"name": token})
            continue
        ref, _, tail = rest.partition(")")
        pieces = tail.strip().split()
        if len(pieces) >= 2:
            result.append(
                {"name": name, "ref": ref.strip(), "concentration": pieces[0], "unit": pieces[1]}
            )
        else:
            result.append({"name": name, "ref": ref.strip()})
    return tuple(result)


def _split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split("|") if part.strip())


def parse_products(raw: bytes) -> Parsed:
    header, rows, reasons = _read_rows(raw)
    cols = {key: _index(header, name) for key, name in _PRODUCT_COLUMNS.items()}
    parsed: list[ProductRow] = []
    for line, cells in enumerate(rows, start=2):
        amm = cells[cols["amm"]].strip()
        name = cells[cols["name"]].strip()
        if not amm or not amm.isdigit() or not name or len(name) > 200:
            reasons.append(f"line {line}: missing or invalid AMM/product name")
            continue
        etat = cells[cols["etat"]].strip()
        parsed.append(
            ProductRow(
                amm=amm,
                name=name,
                withdrawn=_PRODUCT_ETAT.get(etat, False),
                holder=cells[cols["holder"]].strip(),
                gamme=cells[cols["gamme"]].strip(),
                fonctions=_split(cells[cols["fonctions"]]),
                formulation=cells[cols["formulation"]].strip(),
                substances=_substances(cells[cols["substances"]]),
                withdrawal_date=cells[cols["withdrawal_date"]].strip(),
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
        if not amm or not amm.isdigit() or not identifiant:
            reasons.append(f"line {line}: missing or invalid AMM/usage identifier")
            continue
        crop, target = _identifiant(identifiant)
        if not crop:
            reasons.append(f"line {line}: unparseable usage identifier")
            continue
        parsed.append(
            UseRow(
                amm=amm,
                identifiant=identifiant,
                crop=crop,
                target=target,
                max_dose=_decimal(cells[cols["dose"]]),
                max_dose_unit=cells[cols["dose_unit"]].strip() or None,
                interval_days=_decimal(cells[cols["interval"]]),
                max_applications=_integer(cells[cols["applications"]]),
                state=_USE_ETAT.get(cells[cols["etat"]].strip(), "withdrawn"),
            )
        )
    return Parsed(tuple(parsed), len(reasons), tuple(reasons))


def _csv_zip_url() -> str:
    """The current UTF-8 CSV archive URL, resolved from the dataset's resources."""
    response = httpx.get(DATASET_URL, timeout=30)
    response.raise_for_status()
    resources = response.json()["resources"]
    for resource in resources:
        if resource.get("format") == "zip" and "utf8" in resource.get("title", "").lower():
            return resource["url"]
    raise SourceError("data.gouv.fr returned no UTF-8 CSV archive for the E-Phy dataset")


def find_files(dest: Path) -> tuple[Path, Path]:
    """The `products` and `products_usages` files in an extracted archive."""
    products = sorted(dest.glob("produits_*.csv"))
    uses = sorted(dest.glob("produits_usages_*.csv"))
    if not products or not uses:
        raise SourceError(f"{dest} did not contain produits/produits_usages CSV files")
    return products[0], uses[0]


def extract(zip_path: Path, dest: Path) -> tuple[Path, Path]:
    """Extract an E-Phy CSV archive and return the two files it must contain."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest)
    return find_files(dest)


def download(dest: Path) -> tuple[Path, Path]:
    """Download the E-Phy CSV archive and extract `products` and `products_usages`. Returns their
    paths. Only the CLI and the weekly job call this; tests read recorded fixtures."""
    url = _csv_zip_url()
    response = httpx.get(url, timeout=120)
    response.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(dest)
    return find_files(dest)


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "LICENCE",
    "SOURCE",
    "Parsed",
    "ProductRow",
    "SourceError",
    "UseRow",
    "content_hash",
    "decode",
    "download",
    "extract",
    "find_files",
    "parse_products",
    "parse_uses",
]
