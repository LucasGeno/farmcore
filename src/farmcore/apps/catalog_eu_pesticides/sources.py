"""EU Pesticides Database source data: the DG SANTE active-substances download API.

The Commission exposes the same dataset the EU Pesticides Database web search uses, as one
newline-delimited JSON file per line (`format=json`, API v3.0), updated daily. It needs no API
key. The file repeats a substance once per national authorisation, so the importer collapses exact
duplicates. Parsing is strict: a line that is not valid JSON, a substance with no name, an unknown
approval status, or a malformed date is reported and skipped, never imported half-way.

Source: https://food.ec.europa.eu/plants/pesticides/eu-pesticides-database_en
Licence: Commission re-use (Decision 2011/833/EU), CC BY 4.0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from farmcore.apps.catalog_eu_pesticides.substances import substance_key

API_URL = (
    "https://api.datalake.sante.service.ec.europa.eu/sante/pesticides/active-substances-download"
)
SOURCE = "eu-pesticides"
LICENCE = "Creative Commons Attribution 4.0 International (CC BY 4.0)"
LICENCE_ID = "CC-BY-4.0"
ATTRIBUTION = "European Commission, EU Pesticides Database (DG SANTE)"

_STATUS = {
    "Approved": "approved",
    "Not approved": "not_approved",
    "Pending": "pending",
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
class SubstanceRow:
    key: str
    name: str
    cas_number: str | None
    status: str
    approval_date: date | None
    expiry_date: date | None
    props: dict[str, str | int | None]


@dataclass(frozen=True)
class Parsed:
    rows: tuple[SubstanceRow, ...]
    skipped: int
    reasons: tuple[str, ...]


def resource_url() -> str:
    return f"{API_URL}?format=json&api-version=v3.0"


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        raise InvalidRow(f"malformed date {value!r}") from None


def _later_expiry(candidate: SubstanceRow, previous: SubstanceRow) -> bool:
    if candidate.expiry_date is None:
        return False
    if previous.expiry_date is None:
        return True
    return candidate.expiry_date > previous.expiry_date


def parse(raw: bytes) -> Parsed:
    """Parse the NDJSON download. Lines are deduplicated by key, so the source's repeated rows
    (one per national authorisation) collapse onto one substance."""
    rows: dict[str, SubstanceRow] = {}
    reasons: list[str] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            reasons.append(f"line {line_no}: malformed JSON ({exc.msg})")
            continue
        if not isinstance(record, dict):
            reasons.append(f"line {line_no}: expected an object")
            continue
        name = (record.get("substance_name") or "").strip()
        if not name:
            reasons.append(f"line {line_no}: missing substance name")
            continue
        status = _STATUS.get((record.get("substance_status") or "").strip())
        if status is None:
            reasons.append(f"line {line_no}: unknown status {record.get('substance_status')!r}")
            continue
        try:
            approval = _date(record.get("approval_date"))
            expiry = _date(record.get("expiry_date"))
        except InvalidRow as exc:
            reasons.append(f"line {line_no}: {exc}")
            continue
        key = substance_key(name)
        cas = (record.get("as_cas_number") or "").strip() or None
        props: dict[str, str | int | None] = {
            "substance_id": record.get("substance_id"),
            "category": (record.get("substance_category") or "").strip() or None,
            "rms": (record.get("rms") or "").strip() or None,
            "low_risk": (record.get("low_risk_active_substance") or "").strip() or None,
            "candidate_for_substitution": (record.get("candidate_for_substitution") or "").strip()
            or None,
            "basic_substance": (record.get("basic_substance") or "").strip() or None,
            "micro_organism": (record.get("as_micro_org") or "").strip() or None,
            "classification": (record.get("classification_reg_1272") or "").strip() or None,
        }
        candidate = SubstanceRow(
            key=key,
            name=name,
            cas_number=cas,
            status=status,
            approval_date=approval,
            expiry_date=expiry,
            props=props,
        )
        # The source repeats a substance once per national authorisation; collapse them, keeping
        # the latest expiry date when the repeats disagree (a few substances do).
        previous = rows.get(key)
        if previous is None or _later_expiry(candidate, previous):
            rows[key] = candidate
    if not rows or len(reasons) > len(rows):
        raise SourceError(
            f"the active-substances download yielded {len(rows)} rows and {len(reasons)} "
            "skipped: not a usable EU Pesticides Database file"
        )
    return Parsed(tuple(rows.values()), len(reasons), tuple(reasons))


def download(dest: Path) -> tuple[Path, SourceMeta]:
    """Download the active-substances file and return its path and provenance. Only the CLI and
    the weekly job call this; tests read recorded fixtures."""
    url = resource_url()
    response = httpx.get(url, timeout=120)
    response.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "active-substances.ndjson"
    path.write_bytes(response.content)
    meta = SourceMeta(
        resource_url=url, archive_name=path.name, archive_date=now().date().isoformat()
    )
    return path, meta


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "API_URL",
    "ATTRIBUTION",
    "LICENCE",
    "LICENCE_ID",
    "SOURCE",
    "InvalidRow",
    "Parsed",
    "SourceError",
    "SourceMeta",
    "SubstanceRow",
    "content_hash",
    "download",
    "parse",
    "resource_url",
]
