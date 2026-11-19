"""Ministero della Salute "Fitosanitari" source data: the weekly product register.

The dataset page (https://www.dati.salute.gov.it/it/dataset/fitosanitari/) lists the current CSV,
XML and JSON downloads; the JSON filename embeds its week (`PROD_FTS_6_YYYYMMDD.json`), so the
importer discovers the current URL from that page. The file is a JSON array, one object per
product, ~17,700 rows. Parsing is strict: a product with no registration number or name, an
unknown administrative state, or a malformed date is reported and skipped, never imported
half-way. Active substances are a `|`-separated list.

Source: https://www.dati.salute.gov.it/it/dataset/fitosanitari/
Licence: Italian Open Data Licence v2.0 (IODL 2.0) — not CC BY 4.0.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from farmcore.apps.catalog_eu_pesticides.substances import substance_key

DATASET_URL = "https://www.dati.salute.gov.it/it/dataset/fitosanitari/"
RESOURCE_RE = re.compile(r"/sites/default/files/opendata/(PROD_FTS_6_\d{8})\.json")
SOURCE = "it-salute"
LICENCE = "Italian Open Data Licence v2.0 (IODL 2.0)"
LICENCE_ID = "IODL-2.0"
ATTRIBUTION = "Ministero della Salute — Prodotti fitosanitari"

# Normalised product state from the source's `stato_amministrativo`.
_STATE = {
    "Autorizzato": "authorised",
    "Autorizzato con procedura zonale": "authorised",
    "Autorizzato Art. 10 D.P.R. 290/2001": "authorised",
    "Autorizzato in regime di Riconoscimento Reciproco": "authorised",
    "Rinnovato Art. 43 Reg. 1107/2009": "authorised",
    "Autorizzato Art. 34 Reg. 1107/2009": "authorised",
    "Ri-registrato": "authorised",
    "Ri-registrato (Air 1 Fase 1)": "authorised",
    "Autorizzato provvisoriamente": "provisional",
    "Autorizzato in deroga (art. 53 Reg. 1107/2009)": "provisional",
    "Revocato": "withdrawn",
    "Scaduto": "withdrawn",
    "Sospeso": "withdrawn",
}


class SourceError(RuntimeError):
    """The source cannot be downloaded or read."""


class InvalidRow(ValueError):
    """A value the importer cannot type; the caller reports and skips the row."""


@dataclass(frozen=True)
class SourceMeta:
    """Provenance of a downloaded file, recorded on `catalog_import` (C5: source and date)."""

    licence: str = LICENCE
    licence_id: str = LICENCE_ID
    attribution: str = ATTRIBUTION
    resource_url: str = ""
    archive_name: str = ""
    archive_date: str = ""


@dataclass(frozen=True)
class ProductRow:
    number: str
    name: str
    holder: str
    state: str  # normalised: authorised / provisional / withdrawn
    raw_state: str
    expiry: date | None
    substances: tuple[tuple[str, str], ...]  # (substance_key, name)
    props: dict[str, str]


@dataclass(frozen=True)
class Parsed:
    rows: tuple[ProductRow, ...]
    skipped: int
    reasons: tuple[str, ...]


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _date(value: str) -> date | None:
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        raise InvalidRow(f"malformed date {value!r}") from None


def _substances(text: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for name in text.split("|"):
        name = name.strip()
        if not name:
            continue
        result.append((substance_key(name), name))
    return tuple(result)


def parse(raw: bytes) -> Parsed:
    try:
        records = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceError(f"the Fitosanitari download is not valid JSON: {exc}") from None
    rows: list[ProductRow] = []
    reasons: list[str] = []
    for index, record in enumerate(records, start=1):
        number = (record.get("num_registrazione") or "").strip()
        name = (record.get("denominazione_prodotto") or "").strip()
        if not number or not name:
            reasons.append(f"record {index}: missing or invalid registration number/product name")
            continue
        raw_state = (record.get("stato_amministrativo") or "").strip()
        state = _STATE.get(raw_state)
        if state is None:
            reasons.append(f"record {index}: unknown administrative state {raw_state!r}")
            continue
        try:
            expiry = _date(record.get("data_scadenza_autorizzazione") or "")
        except InvalidRow as exc:
            reasons.append(f"record {index}: {exc}")
            continue
        substances = _substances(record.get("sostanze_attive") or "")
        props: dict[str, str] = {
            "hazard": (record.get("indicazioni_di_pericolo") or "").strip(),
            "activity": (record.get("attivita") or "").strip(),
            "formulation": (record.get("descrizione_formulazione") or "").strip(),
            "parallel_import": (record.get("importazione_parallela") or "").strip(),
        }
        rows.append(
            ProductRow(
                number=number,
                name=name,
                holder=(record.get("ragione_sociale") or "").strip(),
                state=state,
                raw_state=raw_state,
                expiry=expiry,
                substances=substances,
                props=props,
            )
        )
    return Parsed(tuple(rows), len(reasons), tuple(reasons))


def resource_url() -> tuple[str, SourceMeta]:
    """The current JSON download URL and its provenance, discovered from the dataset page."""
    response = httpx.get(DATASET_URL, timeout=30)
    response.raise_for_status()
    match = RESOURCE_RE.search(response.text)
    if match is None:
        raise SourceError("dati.salute.gov.it lists no JSON download for the Fitosanitari dataset")
    name = match.group(1)
    archive_date = f"{name[-8:-4]}-{name[-4:-2]}-{name[-2:]}"
    return (
        f"https://www.dati.salute.gov.it/sites/default/files/opendata/{name}.json",
        SourceMeta(
            resource_url=f"https://www.dati.salute.gov.it/sites/default/files/opendata/{name}.json",
            archive_name=f"{name}.json",
            archive_date=archive_date,
        ),
    )


def download(dest: Path) -> tuple[Path, SourceMeta]:
    """Download the current Fitosanitari JSON file and return its path and provenance. Only the
    CLI and the weekly job call this; tests read recorded fixtures."""
    url, meta = resource_url()
    response = httpx.get(url, timeout=120)
    response.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / meta.archive_name
    path.write_bytes(response.content)
    return path, meta


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
    "content_hash",
    "download",
    "parse",
    "resource_url",
]
