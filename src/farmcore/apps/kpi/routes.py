"""`GET /kpi/` (page) and `GET /kpi/kpis.json`: the season KPIs of the selected farm.

Reads through the request's `Repo`: the farm's station (`stations.service.station_for_farm`,
`stations:read`; no station or not station 33394002 → `config.NO_STATION`, no numbers), the farm
area (sum of parcel `area_ha`, `fields:read`) and the voice operations (done spray logs with a
`voice` log source that is not a typed chat message, and `ts_start ≤ clock now`, `logs:read`,
counted per local year). The seasons
come from `season.season_results` (cached; the whole file season, labelled retrospective).

Assumptions: query parameters named like the `Assumptions` fields override the defaults for this
request only (never persisted); a non-numeric or out-of-bounds value is a 422. A missing station
file is a plain message naming the file (200).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, select

from farmcore.apps.kpi import backtest, config, season, service
from farmcore.apps.kpi.config import Assumptions
from farmcore.apps.stations import connector
from farmcore.apps.stations import service as stations
from farmcore.core.access import Repo, ScopedRepository
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import ExtId, Log, LogSource, Parcel
from farmcore.web import is_expert, render

router = APIRouter()

STATION_SCOPE = stations.SCOPE  # "stations:read"
FIELDS_SCOPE = "fields:read"
LOGS_SCOPE = "logs:read"
TYPED_INPUT = "text"  # chat.handlers' raw_payload["input"] for a typed (not spoken) message


def _assumptions(request: Request) -> Assumptions:
    """Defaults overridden by the query parameters named like the fields; 422 when invalid."""
    values: dict[str, float] = {}
    errors: list[str] = []
    for name in Assumptions.names():
        raw = request.query_params.get(name)
        if raw is None or raw.strip() == "":
            continue
        try:
            values[name] = float(raw)
        except ValueError:
            errors.append(f"{name}: {raw!r} is not a number")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    try:
        return Assumptions().with_values(**values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _station_key(repo: ScopedRepository) -> str | None:
    """The ext key (`meteofrance`:<key>) of the farm's station, or None (no station)."""
    station = stations.station_for_farm(repo)
    if station is None:
        return None
    stmt = select(ExtId.key).where(
        ExtId.source == connector.EXT_SOURCE, ExtId.model == "asset", ExtId.res_id == station.id
    )
    return repo.execute(stmt, scope=STATION_SCOPE, resource="ext_id").scalar() or ""


def _area_ha(repo: ScopedRepository) -> float:
    return float(sum(p.area_ha for p in repo.list(Parcel, scope=FIELDS_SCOPE)))


def _voice_ops(repo: ScopedRepository, now: datetime, tz: str) -> Counter[int]:
    """Done spray logs with a voice source started on or before `now`, per local year.

    A typed chat message is stored as a `voice` source marked `raw_payload.input == "text"`
    (`chat.handlers`; log_source.source_type has no `text` value): it is not counted."""
    spoken = and_(
        LogSource.source_type == "voice",
        ~LogSource.raw_payload.contains({"input": TYPED_INPUT}),
    )
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        Log.sources.any(spoken),
        scope=LOGS_SCOPE,
    )
    return Counter(log.ts_start.astimezone(ZoneInfo(tz)).year for log in logs)


def _pairs(a: Assumptions) -> list[tuple[config.Assumption, config.Assumption]]:
    defaults = Assumptions()
    return [(getattr(a, n), getattr(defaults, n)) for n in Assumptions.names()]


def _assumption_rows(a: Assumptions) -> dict[str, dict[str, Any]]:
    defaults = Assumptions()
    out: dict[str, dict[str, Any]] = {}
    for name in Assumptions.names():
        field, default = getattr(a, name), getattr(defaults, name)
        out[name] = {
            **field.model_dump(),
            "default": default.value,
            "edited": field.value != default.value,
        }
    return out


def _lead(r: season.SeasonYear | None, a: Assumptions) -> dict[str, Any] | None:
    """The lead figure for `config.LEAD_YEAR`: the plain fixed-interval calendar first ("at least"),
    then the season's own calendar (rapid-growth intervals)."""
    if r is None:
        return None
    calendars = [
        service.copper_lead(
            r.model,
            r.plain_calendar,
            a,
            copper=r.copper,
            key="plain_10d",
            name=config.plain_calendar_name(r.plain_interval_days),
            label=config.plain_calendar_label(r.plain_interval_days),
            at_least=True,
            calendar_assumed=r.calendar_assumed,
        ),
        service.copper_lead(
            r.model,
            r.calendar,
            a,
            copper=r.copper,
            key="rapid_growth",
            name=config.RAPID_CALENDAR_NAME,
            label=config.CALENDAR_LABEL,
            calendar_assumed=r.calendar_assumed,
        ),
    ]
    return {
        "year": r.year,
        "label": config.LEAD_LABEL,
        "cu_kg_ha_treatment": a.cu_kg_ha_treatment.value,
        "calendars": [asdict(c) for c in calendars],
    }


def _backtest() -> dict[str, Any]:
    years = [
        {
            **asdict(r),
            "bsv_quote": backtest.display(r.bsv.quote),
            "bsv_source": r.bsv.source,
            "bsv_url": r.bsv.url,
        }
        for r in backtest.run_all()
    ]
    return {
        "label": backtest.LABEL,
        "consistency_label": backtest.CONSISTENCY_LABEL,
        "assumption_note": backtest.ASSUMPTION_NOTE,
        "disclaimer": backtest.DISCLAIMER,
        "bsv_provenance": backtest.BSV_PROVENANCE,
        "source_line": backtest.SOURCE_LINE,
        "years": years,
    }


def _tested(station_file) -> dict[str, Any]:
    rows = backtest.tested_seasons(station_file)
    return {
        "rows": rows,
        "count": len(rows),
        "footnotes": backtest.TESTED_FOOTNOTES,
        "season_notes": [r.note for r in rows if r.note],
    }


def _context(request: Request, repo: ScopedRepository) -> dict[str, Any]:
    a = _assumptions(request)
    settings = request.app.state.settings
    ctx: dict[str, Any] = {
        "banner": config.BANNER,
        "retrospective_label": config.RETROSPECTIVE_LABEL,
        "calendar_label": config.CALENDAR_LABEL,
        "cu_s_label": config.CU_S_LABEL,
        "voice_label": config.VOICE_LABEL,
        "agreste_source": config.AGRESTE_TREATMENTS.source,
        "copper_treatments_label": config.COPPER_TREATMENTS_LABEL,
        "copper_label_cap": {
            "product": config.COPPER_LABEL_PRODUCT,
            "authorisation_no": config.COPPER_LABEL_AMM,
            "max_applications": config.COPPER_LABEL_MAX_APPLICATIONS,
            "source": config.COPPER_LABEL_SOURCE,
            "note": config.COPPER_CAP_NOTE,
        },
        "rapid_calendar_name": config.RAPID_CALENDAR_NAME,
        "pending_label": config.PENDING,
        "assumptions": _assumption_rows(a),
        "assumptions_edited": any(f.value != d.value for f, d in _pairs(a)),
        "station": None,
        "message": None,
        "area_ha": None,
        "lead": None,
        "years": [],
    }
    key = _station_key(repo)
    if key != connector.STATION_KEY:
        ctx["message"] = config.NO_STATION
        return ctx
    ctx["station"] = key
    if is_expert(request):
        ctx["backtest"] = _backtest()
    path = season.station_file(settings.data_dir)
    if not path.is_file():
        ctx["message"] = f"Station file not found: {path.name} (expected at {season.FILE})"
        return ctx
    ctx["tested"] = _tested(path)
    now = get_clock(repo.session, settings).now()
    area = _area_ha(repo)
    voice = _voice_ops(repo, now, settings.display_tz)
    years = []
    results = season.season_results(path)
    ctx["lead"] = _lead(results.get(config.LEAD_YEAR), a)
    for year, r in results.items():
        k = service.kpis(
            r.model,
            r.calendar,
            voice[year],
            area,
            a,
            copper=r.copper,
            sulphur=r.sulphur,
            calendar_assumed=r.calendar_assumed,
        )
        years.append(
            {
                "year": year,
                "start": r.model.start.isoformat(),
                "end": r.model.end.isoformat(),
                "model": {
                    "passes": k.model_passes,
                    "treatments": k.model_treatments,
                    "copper": k.model_copper,
                    "sulphur": k.model_sulphur,
                },
                "calendar": {
                    "passes": k.calendar_passes,
                    "treatments": k.calendar_treatments,
                    "copper": k.calendar_copper,
                    "sulphur": k.calendar_sulphur,
                },
                "agreste_treatments": k.agreste_treatments,
                "voice_ops": k.voice_ops,
                "calendar_assumed": r.calendar_assumed,
                "kpis": [asdict(x) for x in k.kpis],
            }
        )
    ctx.update(area_ha=area, years=years)
    return ctx


@router.get("/", response_class=HTMLResponse)
def kpi_page(request: Request, repo: Repo) -> HTMLResponse:
    return render(request, "kpi/index.html", **_context(request, repo))


@router.get("/kpis.json")
def kpis_json(request: Request, repo: Repo) -> JSONResponse:
    ctx = _context(request, repo)
    ctx.pop("backtest", None)  # an Expert page section, not part of the KPI JSON contract
    ctx.pop("tested", None)  # a page table, not part of the KPI JSON contract
    return JSONResponse(ctx)
