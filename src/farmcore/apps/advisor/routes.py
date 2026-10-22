"""Advisor pages, mounted at `/advisor`.

- `GET /advisor/`: the Crop protection home: the Today hero's summary
  (`presenter.present_farm_advice`), every parcel grouped by decision, the full table in a
  closed "All advice (expert)" section.
- `GET /advisor/parcels/{id}`: the advice card, plain words first: decision chip, a lead
  (what, by when, why), the 3-10 checklist (`checklist.py`), the protection line and wash-off bar,
  the plan (window, products, incubation, flags), then Details (`#why`: every rule, threshold and
  source, internal refs dropped) and the timeline. Rule codes, raw inputs, the stored source
  text and the JSON link render only in debug mode (`debug_enabled`: admins only).
- `GET /advisor/parcels/{id}.json`: the latest advice row as JSON (tests, the bot).

Every read goes through the request's `Repo` (advice under `advice:read`); advice later than
`clock.now()` is ignored, so stepping the replay clock back works. Unknown parcel: 404.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from farmcore.apps.advisor import service
from farmcore.apps.advisor.checklist import assumed_keys, checklist, day_text
from farmcore.apps.advisor.engine.window import REPLAY_NOTE
from farmcore.apps.advisor.navigation import parcel_link
from farmcore.apps.fields.seed import alias_number
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.debug import debug_enabled
from farmcore.core.models.canonical import Parcel
from farmcore.core.navigation import reading_destination, return_destination, workspace_parent
from farmcore.core.registry import visible_manifests
from farmcore.web import LOCAL_FORMAT, render

router = APIRouter()

PARCEL_SCOPE = "fields:read"
NO_ADVICE_HEADLINE = "No advice yet"
REPLAY_FLAG = "replay_forecast"  # a demo/replay detail: shown in debug mode only
NBSP = "\u00a0"  # number and unit stay on one line
FLAG_LABELS = {
    "washed_off": "Washed off: rain since the spray passed the wash-off threshold",
    "expired": "Expired: the protection period of the latest spray has ended",
    REPLAY_FLAG: "Replay: observed weather used as forecast",
    "data_gap": "Data gap: missing station hours in the lookback",
    "spray_without_product": "Spray without a known product ignored: it gives no cover",
}
RULE_LABELS = {
    "season_gate": "Season gate",
    "downy_3_10": "Downy infection (3-10 rule)",
    "protection_downy": "Downy cover",
    "protection_powdery": "Powdery cover",
    "spray_window": "Spray window",
    "downy_incubation": "Downy incubation",
    "powdery_gubler_thomas": "Powdery pressure (Gubler–Thomas)",
    "powdery_pass": "Sulphur-only pass",
    "tankmix": "Tank-mix",
}
# Plain words for the Details table (raw keys show in debug mode only). The unit comes from the
# key's suffix, the value from the reason's `threshold` dict.
THRESHOLD_LABELS = {
    "season_gate": "Shoots ≥ 10 cm from",
    "rapid_growth_start": "Rapid growth starts",
    "rapid_growth_end": "Rapid growth ends",
    "powdery_start": "Powdery period starts (bloom)",
    "powdery_end": "Powdery period ends",
    "downy_window_h": "Rain and temperature window",
    "downy_rain_min_mm": "Rain at least",
    "downy_temp_min_c": "Mean temperature at least",
    "downy_event_max_days": "An uncovered infection stays a risk for",
    "incub_tmin": "No incubation at or below",
    "incub_humid_rh_hourly": "Humid hour from relative humidity",
    "incub_alert_pct": "Alert at incubation",
    "incub_symptoms_pct": "Symptoms expected at incubation",
    "gt_band_min_c": "Risk band from",
    "gt_band_max_c": "Risk band up to",
    "gt_min_hours": "Hours in the band for a risk day",
    "gt_hot_c": "Hot hour from",
    "gt_onset": "Epidemic onset at index",
    "gt_step_pre_onset": "Index step per day before onset",
    "gt_delta_qualify": "Index up on a risk day",
    "gt_delta_qualify_hot": "Index up on a risk day with a hot hour",
    "gt_delta_fail": "Index down on a non-risk day",
    "gt_delta_fail_hot": "Index down on a non-risk day with a hot hour",
    "gt_index_min": "Index lowest",
    "gt_index_max": "Index highest",
    "gt_low_max": "Low risk up to index",
    "gt_moderate_max": "Moderate risk up to index",
    "gt_interval_low_days": "Sulphur interval at low risk",
    "gt_interval_moderate_days": "Sulphur interval at moderate risk",
    "gt_interval_high_days": "Sulphur interval at high risk",
    "tankmix_cover_ending_days": "Tank-mix when powdery cover ends within",
    "protection_contact_days": "Contact product protects",
    "protection_penetrant_days": "Penetrant product protects",
    "protection_rapid_growth_days": "Protection in rapid growth at most",
    "washoff_copper_mm": "Copper washed off after",
    "washoff_sulphur_mm": "Sulphur washed off after",
    "window_wind_max_kmh": "Wind at most",
    "window_rr1_max_mm_h": "Rain at most",
    "window_rain_lookahead_h": "Dry hours needed after spraying",
    "window_rain_next_max_mm": "Rain in those hours below",
    "window_sulphur_tmax_c": "Sulphur: temperature at most",
    "window_min_open_hours": "Window at least",
    "window_horizon_h": "Look ahead",
}
_UNITS = (("_mm_h", "mm/h"), ("_kmh", "km/h"), ("_mm", "mm"), ("_c", "°C"), ("_pct", "%"))
_UNITS += (("_rh_hourly", "%"), ("_days", "d"), ("_h", "h"))
# Internal document references in source strings and notes are dropped for farmers; debug
# mode shows the stored text unchanged.
_REF = r"(?:decision R\d+|[\w-]+-\d{4}-\d{2}-\d{2}|research/[\w.-]+(?:\s*§\s*\w+)?|log\.md)"
_INTERNAL = (
    (re.compile(r"\s*\(\s*" + _REF + r"[^()]*\)"), ""),  # a parenthetical that is a document ref
    (re.compile(r"(?:,\s*|\s+via\s+|\s+in\s+)" + _REF), ""),  # "…, review-2026-10-16"
    (re.compile(_REF + r"\s*:?\s*"), ""),  # "research/agronomy.md §2e: …"
    (re.compile(r"\s*\([a-z]+(?:_[a-z]+)+\)"), ""),  # "(powdery_resistance_days)": a config key
    (re.compile(r"\(\s*\)"), ""),
    (re.compile(r"\s{2,}"), " "),
)
_URL_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s;,)]*[^\s;,):.])")
_ASSUMED_RE = re.compile(r"\(assumed: ([^)]*)\)")
_REPLAY_RE = re.compile(r"(?:;\s*)?" + re.escape(REPLAY_NOTE))
_ISO_DAY_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
MAX_VALUE_CHARS = 160


def _plain(text: str) -> str:
    for pattern, repl in _INTERNAL:
        text = pattern.sub(repl, text)
    return text.strip(" ,;:")


def _top_level_parts(text: str) -> list[str]:
    """`a; b (c; d); e` -> [`a`, `b (c; d)`, `e`]: split on `;` outside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == ";" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [*parts, cur]


def plain_sources(source: str) -> list[list[tuple[str, str | None]]]:
    """A reason's source string for farmers: one entry per distinct citation, internal document refs
    dropped, bare web addresses as links. Each entry is (text, href | None) segments.

    A qualifier left alone once its document ref is dropped (`research/agronomy.md §2e (70 °F)` ->
    `(70 °F)`) joins the citation before it: `Thomas, Gubler et al. 1994 (70 °F; 85 °F)`."""
    entries: list[tuple[str, list[str]]] = []  # (citation, qualifiers)
    index: dict[str, int] = {}
    last: int | None = None
    for part in _top_level_parts(source):
        text = _plain(part)
        if not text:
            continue
        if text.startswith("(") and text.endswith(")"):
            qualifier = text[1:-1].strip()
            if last is not None and qualifier:
                citation, qualifiers = entries[last]
                if qualifier not in qualifiers and qualifier not in citation:
                    qualifiers.append(qualifier)
            continue
        base = re.sub(r"\s*\([^()]*\)$", "", text)  # "3-10 rule … (mean T ≥ 10 °C)" -> once
        hit = index.get(text, index.get(base))
        if hit is None:
            entries.append((text, []))
            hit = index[text] = index[base] = len(entries) - 1
        last = hit
    out: list[list[tuple[str, str | None]]] = []
    for citation, qualifiers in entries:
        text = f"{citation} ({'; '.join(qualifiers)})" if qualifiers else citation
        segments: list[tuple[str, str | None]] = []
        pos = 0
        for m in _URL_RE.finditer(text):
            segments += [(text[pos : m.start()], None), (m.group(1), f"https://{m.group(1)}")]
            pos = m.end()
        segments.append((text[pos:], None))
        out.append([seg for seg in segments if seg[0]])
    return out


def plain_note(note: str) -> str:
    """A reason note for farmers: no `(assumed: …)` keys (the chips say it), no document refs and no
    replay note (debug only)."""
    text = _plain(_REPLAY_RE.sub("", _ASSUMED_RE.sub("", note)))
    return _ISO_DAY_RE.sub(lambda m: day_text(date.fromisoformat(m.group(0))), text)


def _now(request: Request, repo: Repo) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


def _parcel(repo: Repo, parcel_id: int) -> Parcel:
    if not 1 <= parcel_id <= 2**63 - 1:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    parcel = repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE)
    if parcel is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    return parcel


def _parcel_json(parcel: Parcel) -> dict[str, Any]:
    return {
        "id": parcel.id,
        "alias": parcel.alias,
        "name": parcel.name,
        "area_ha": float(parcel.area_ha),
        "variety": (parcel.props or {}).get("variety"),
    }


def _advice_json(repo: Repo, parcel: Parcel, now: datetime, tz: str) -> dict[str, Any]:
    row = service.latest_advice(repo, now, tz=tz, parcel_id=parcel.id).get(parcel.id)
    if row is None:
        body = service.empty_advice_json(
            parcel.id, service.unavailable_reason(repo) or NO_ADVICE_HEADLINE
        )
    else:
        body = service.advice_json(row)
    return {**body, "parcel": _parcel_json(parcel)}


def _fmt(value: Any, tz: str) -> str:
    """A reason input or threshold for the card: ISO times local, long values cut."""
    if isinstance(value, str) and _ISO_RE.match(value):
        try:
            t = datetime.fromisoformat(value)
        except ValueError:
            return value
        return t.astimezone(ZoneInfo(tz)).strftime(LOCAL_FORMAT) if t.tzinfo else value
    if isinstance(value, float):
        return f"{value:.4g}"
    if value is None:
        return "—"
    if isinstance(value, list | dict):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            text = ", ".join(_fmt(v, tz) for v in value) or "none"
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"
    return str(value)


def _unit(key: str) -> str:
    return next((u for suffix, u in _UNITS if key.endswith(suffix)), "")


def _plain_value(key: str, value: Any) -> str:
    """A threshold for farmers: `10 mm`, `5 Apr 2024`, `72 h`."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return day_text(date.fromisoformat(value))
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{float(value):g} {_unit(key)}".strip()
    return str(value)


def _reason_view(reason: dict[str, Any], tz: str) -> dict[str, Any]:
    note = reason.get("note", "")
    assumed = assumed_keys(reason)
    threshold = reason.get("threshold", {})
    return {
        "rule": reason["rule"],
        "label": RULE_LABELS.get(reason["rule"], reason["rule"]),
        "verdict": reason["verdict"],
        "source": reason["source"],
        "sources": plain_sources(reason["source"]),
        "note": note,
        "plain_note": plain_note(note),
        # Stored JSONB re-sorts keys; list the infection-event values (what the verdict rests
        # on) before the current-hour ones.
        "inputs": [
            (k, _fmt(v, tz))
            for k, v in sorted(
                reason.get("inputs", {}).items(), key=lambda kv: not kv[0].startswith("event_")
            )
        ],
        "threshold": [(k, _fmt(v, tz), k in assumed) for k, v in threshold.items()],
        "plain_threshold": [
            (
                THRESHOLD_LABELS.get(k, k.replace("_", " ").capitalize()),
                _plain_value(k, v),
                k in assumed,
            )
            for k, v in threshold.items()
        ],
    }


def _incubation(reasons: list[dict[str, Any]], evaluated_at: str | None, tz: str) -> str | None:
    """Incubation of the latest infection in plain words. A projected symptom date beyond the
    spray-window forecast is labelled indicative ."""
    by_rule = {r["rule"]: r for r in reasons}
    reason = by_rule.get("downy_incubation")
    if reason is None or evaluated_at is None:
        return None
    inputs, threshold = reason.get("inputs", {}), reason.get("threshold", {})
    pct, event = inputs.get("pct"), inputs.get("event")
    if inputs.get("coverage") == "limited":
        return (
            "Incubation not evaluable: observed-hours accumulation only; "
            "required temperature/humidity history is incomplete"
        )
    if not event and inputs.get("event_history_coverage") == "limited":
        return "No observed infection event; historical event coverage is limited"
    if pct is None or not event:
        return "No infection event to incubate"
    zone = ZoneInfo(tz)
    at = datetime.fromisoformat(event).astimezone(zone)
    alert, symptoms = threshold.get("incub_alert_pct"), threshold.get("incub_symptoms_pct")
    text = f"{float(pct):.0f} % for the {at.day} {at:%b %H:%M} infection"
    if alert is not None and symptoms is not None:
        text += f" (alert at {float(alert):g} %, symptoms expected at {float(symptoms):g} %)"
    expected = inputs.get("symptoms_at") or inputs.get("expected_symptoms")
    if expected:
        when = datetime.fromisoformat(expected).astimezone(zone)
        horizon = _horizon_h(by_rule) or 72
        evaluated = datetime.fromisoformat(evaluated_at)
        beyond = when > evaluated + timedelta(hours=int(horizon))
        if inputs.get("symptoms_at"):  # the incubation crossed 100 %: an actual date
            since = "expected since" if when <= evaluated else "due"
            text += f". Symptoms {since} {when.day} {when:%b}"
        elif beyond:
            text += ". Symptoms: beyond the forecast, no date yet (indicative)"
        else:
            text += f". Symptoms expected around {when.day} {when:%b} (projected)"
    return text


def _tankmix_outcome(reasons: list[dict[str, Any]]) -> str | None:
    """08 puts the tank-mix outcome on the sulphur `spray_window` reason's note."""
    for r in reasons:
        if r["rule"] == "spray_window" and r.get("inputs", {}).get("sulphur"):
            note = r.get("note", "")
            if note.startswith(("copper + sulphur", "copper alone")):
                return note.split(";", 1)[0]
    return None


def _chip_label(has_advice: bool, decision: str, status: str | None = None) -> str | None:
    """The label override for `core/_ui.html` `decision_chip(decision, label)`: None keeps the
    decision word. The core word for `none` is "No advice yet", true only without an advice row;
    an evaluated parcel with nothing to spray says "No action", as its headline does. A limited
    or unavailable assessment says so, as the map panel does, never "No action"."""
    if not has_advice:
        return NO_ADVICE_HEADLINE
    if status in service.UNCERTAIN:
        return f"Assessment {status}"
    return "No action" if decision == service.NO_ADVICE else None


def _when(d: datetime, ref: datetime) -> str:
    """`today` / `tomorrow` / `Sat 13 Apr`, relative to `ref` (both local)."""
    days = (d.date() - ref.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"{d:%a} {d.day} {d:%b}"


def _window_phrase(
    window: list[str], evaluated_at: datetime, horizon_h: Any, tz: str, *, lead: bool = False
) -> tuple[str, str]:
    """(`today from 06:00`, `before 15:00`) for a spray window. A window still open at the end
    of the forecast has no known end: the plan says `before Sat 13 Apr 06:00 (end of forecast)`,
    the lead (`lead=True`) `open to the forecast end, Sat 13 Apr 06:00`, so neither reads as a
    spray deadline."""
    zone = ZoneInfo(tz)
    start, end = (datetime.fromisoformat(t).astimezone(zone) for t in window)
    now = evaluated_at.astimezone(zone)
    begin = f"{_when(start, now)} from {start:%H:%M}"
    stop = f"{end:%H:%M}" if end.date() == start.date() else f"{_when(end, now)} {end:%H:%M}"
    open_ended = service.runs_to_forecast_edge(end, evaluated_at, horizon_h)
    if not open_ended:
        return begin, f"before {stop}"
    if lead:
        return begin, f"open to the forecast end, {stop}"
    return begin, f"before {stop} (end of forecast)"


def _num(value: Any) -> str:
    return f"{float(value):.1f}"


def _horizon_h(by_rule: dict[str, dict[str, Any]]) -> Any:
    """The spray-window look-ahead (`window_horizon_h`) the advice was computed with."""
    return by_rule.get("spray_window", {}).get("threshold", {}).get("window_horizon_h")


def _no_window(horizon_h: Any) -> str:
    """`No spray window in the next 3 days` (72 h), `… next 36 h`, from the stored threshold."""
    if horizon_h is None:
        return "No spray window in the forecast"
    hours = int(horizon_h)
    span = f"{hours // 24} day{'s' if hours != 24 else ''}" if hours % 24 == 0 else f"{hours} h"
    return f"No spray window in the next {span}"


def _because(by_rule: dict[str, dict[str, Any]], products: list[dict[str, Any]]) -> str:
    """Why a pass is due, in plain words, from the reasons. Same order as `service.headline`:
    an active infection event gives the downy reason, else a recommended product means the
    powdery cover ended. Flags are not a reason: an old wash-off outlives its spray."""
    downy = by_rule.get("downy_3_10", {})
    inputs = downy.get("inputs", {})
    if inputs.get("active_events"):
        rain, temp = inputs.get("event_r48_mm"), inputs.get("event_t48_c")
        if rain is not None and temp is not None:
            return f"{_num(rain)}{NBSP}mm rain at {_num(temp)}{NBSP}°C → downy mildew risk"
        return "an infection event → downy mildew risk"
    if products:
        return "the powdery mildew cover has ended"
    return "no pass is due"


def _lead(advice: dict[str, Any], tz: str) -> str:
    """What, where and by when, in plain words, from the stored advice (no engine call)."""
    if advice.get("assessment_status") in ("limited", "unavailable"):
        return advice["headline"]
    if advice["id"] is None:
        return NO_ADVICE_HEADLINE
    by_rule = {r["rule"]: r for r in advice["reasons"]}
    decision = advice["decision"]
    evaluated_at = datetime.fromisoformat(advice["evaluated_at"])
    horizon = _horizon_h(by_rule)
    window = advice["next_window"]
    if decision == "spray":
        verb = " ".join(["Spray", *[" + ".join(p["name"] for p in advice["products"])]]).strip()
        because = _because(by_rule, advice["products"])
        if not window:
            return f"{verb}, because {because}"
        begin, stop = _window_phrase(window, evaluated_at, horizon, tz, lead=True)
        return f"{verb} {begin}, {stop}, because {because}"
    if decision == "wait":
        if not window:
            return f"Wait. {_no_window(horizon)}"
        begin, stop = _window_phrase(window, evaluated_at, horizon, tz, lead=True)
        return f"Wait. Next window {begin}, {stop}"
    if decision == "protected":
        until = advice["protected_until"]
        mm = advice["mm_since_spray"]
        text = "Protected"
        if until:
            u = datetime.fromisoformat(until).astimezone(ZoneInfo(tz))
            text += f" until {u:%a} {u.day} {u:%b}"
        if mm is not None:
            text += f" · {_num(mm)}{NBSP}mm rain since spray"
        return text
    gate = by_rule.get("season_gate", {})
    if gate.get("verdict") == "season not started":
        return "Nothing to do today: the season has not started"
    return "Nothing to do today"


def _protection(reasons: list[dict[str, Any]], tz: str) -> dict[str, Any] | None:
    """The downy protection line and its wash-off bar, from the `protection_downy` reason.

    The bar drains with the rain since the spray against the copper wash-off threshold
    (contact products only; a penetrant is not washed off by rain, so it gets no bar)."""
    reason = next((r for r in reasons if r["rule"] == "protection_downy"), None)
    if reason is None:
        return None
    inputs, threshold = reason.get("inputs", {}), reason.get("threshold", {})
    verdict = reason["verdict"]
    until = inputs.get("until")
    until_s = ""
    if until:
        u = datetime.fromisoformat(until).astimezone(ZoneInfo(tz))
        until_s = f"{u:%a} {u.day} {u:%b}"
    mm, washoff = inputs.get("mm_since_spray"), threshold.get("washoff_copper_mm")
    spray_ts = inputs.get("spray_ts")
    spray_s = "the last spray"
    if spray_ts:
        s = datetime.fromisoformat(str(spray_ts)).astimezone(ZoneInfo(tz))
        spray_s = f"the {s.day} {s:%b} spray"
    # Once the cover is gone the bar is cut (an empty bar), so the mm go in the line itself:
    # demo click 5 reads "≈ 44 mm since the 9 May copper" here.
    rain_s = f"{_num(mm)} mm rain since {spray_s}" if mm is not None else ""
    washoff_note = f" (wash-off at {float(washoff):g} mm)" if washoff else ""
    if verdict == "washed off" and rain_s:
        line = f"Not protected: washed off by {rain_s}{washoff_note}"
    elif verdict == "washed off":
        line = "Not protected: the last spray was washed off by rain"
    elif verdict == "expired":
        line = f"Not protected: the last spray's cover ended {until_s}"
        if rain_s:
            line += f" · {rain_s}"
    else:
        line = {"protected": f"Protected until {until_s}"}.get(verdict, "Not protected")
    view: dict[str, Any] = {"verdict": verdict, "line": line, "bar": None}
    if mm is not None and washoff and "contact" in (inputs.get("product_types") or []):
        left = 0 if verdict != "protected" else max(0, round(100 * (1 - mm / washoff)))
        washoff_s = f"{float(washoff):g}"
        view["bar"] = {
            "mm": _num(mm),
            "left_pct": left,
            "label": f"Cover left: {left} % · {_num(mm)} mm of the {washoff_s} mm wash-off "
            "rain since the spray",
        }
    return view


def unavailable_view(
    request: Request, repo: Repo, *, full_page=False, parcel=None, availability=None
):
    reason = (
        availability
        or service.current_availability(
            repo, _now(request, repo), request.app.state.settings.display_tz
        )
    )[1]
    if reason:
        history_context = (
            _history_context(request, repo, parcel, request.query_params.get("season"))
            if full_page and parcel is not None
            else {}
        )
        return render(
            request,
            "advisor/unavailable.html" if full_page else "advisor/_unavailable.html",
            reason=reason,
            **{**history_context, "parcel": parcel},
            return_destination=(
                return_destination(request, repo, "/advisor/", "All advice")
                if full_page and parcel is not None
                else None
            ),
        )
    return None


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request, repo: Repo, model: Literal["all", "downy", "powdery"] = "all"
) -> HTMLResponse:
    """The Crop protection home: the Today hero's summary, every parcel by decision, then
    the full table in a closed "All advice (expert)" section."""
    # presenter imports this module's plain-word helpers, so it is imported here, not on top.
    from farmcore.apps.advisor.models import MODELS, project
    from farmcore.apps.advisor.presenter import by_priority, parcel_row, present_farm_advice
    from farmcore.apps.advisor.today import bot_url

    if reason := service.unavailable_reason(repo):
        return render(
            request,
            "advisor/unavailable.html",
            reason=reason,
            models=MODELS,
            selected_model=model,
        )

    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    parcels = sorted(
        repo.list(Parcel, scope=PARCEL_SCOPE), key=lambda p: (alias_number(p.alias), p.alias)
    )
    latest = service.latest_advice(repo, now, tz=tz)
    rows = [parcel_row(p, latest.get(p.id), tz) for p in parcels]
    model_views = {
        p.id: tuple(
            project(m, latest.get(p.id)) for m in MODELS if m.enabled and model in ("all", m.key)
        )
        for p in parcels
    }
    if model != "all":
        # A model tab keeps its own order: that model's actions first (priority is downy-led).
        rows.sort(key=lambda r: not model_views[r.id][0].actionable)
    else:
        rows = by_priority(rows)  # highest priority first, then alias
    return render(
        request,
        "advisor/index.html",
        now=now,
        parcels=rows,
        hero=present_farm_advice(rows, now, tz),
        models=MODELS,
        selected_model=model,
        workspace_links={
            p.id: parcel_link(request, p.id, f"/advisor/?model={model}#advisor-parcel-{p.id}")
            for p in parcels
        },
        model_views=model_views,
        bot_url=bot_url(request),
    )


@router.get("/parcels/{parcel_id}.json")
def card_json(parcel_id: int, request: Request, repo: Repo) -> JSONResponse:
    parcel = _parcel(repo, parcel_id)
    return JSONResponse(
        _advice_json(repo, parcel, _now(request, repo), request.app.state.settings.display_tz)
    )


def _next_window(advice: dict[str, Any], tz: str) -> str:
    """The next-window line: the window, or why none is needed."""
    decision, window = advice["decision"], advice["next_window"]
    if decision == "protected":
        until = advice["protected_until"]
        if until:
            u = datetime.fromisoformat(until).astimezone(ZoneInfo(tz))
            return f"Not needed: protected until {u:%a} {u.day} {u:%b}"
        return "Not needed: protected"
    if decision == "none":
        return "Not needed: nothing to spray"
    by_rule = {r["rule"]: r for r in advice["reasons"]}
    horizon = _horizon_h(by_rule)
    if not window:
        return _no_window(horizon)
    evaluated_at = datetime.fromisoformat(advice["evaluated_at"])
    begin, stop = _window_phrase(window, evaluated_at, horizon, tz)
    return f"{begin[0].upper()}{begin[1:]}, {stop}"


@router.get("/parcels/{parcel_id}", response_class=HTMLResponse)
def card(parcel_id: int, request: Request, repo: Repo, season: str | None = None) -> HTMLResponse:
    _require_on(request)
    parcel = _parcel(repo, parcel_id)
    if (unavailable := unavailable_view(request, repo, full_page=True, parcel=parcel)) is not None:
        return unavailable
    return render(
        request,
        "advisor/card.html",
        **{
            **_history_context(request, repo, parcel, season),
            **_current_context(request, repo, parcel),
        },
        return_destination=return_destination(request, repo, "/advisor/", "All advice"),
    )


def _current_context(request: Request, repo: Repo, parcel: Parcel) -> dict[str, Any]:
    """One detailed stored presentation for canonical advice and parcel Protection."""
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    advice = _advice_json(repo, parcel, now, tz)
    reasons = advice["reasons"]
    debug = debug_enabled(request)
    flags = [f for f in advice["flags"] if debug or f != REPLAY_FLAG]
    view = {
        **advice,
        "chip_label": _chip_label(
            advice["id"] is not None, advice["decision"], advice.get("assessment_status")
        ),
        "reasons": [_reason_view(r, tz) for r in reasons],
        "flags": [(f, FLAG_LABELS.get(f, f)) for f in flags],
        # The flag code stays debug-only; farmers get the same fact in plain words.
        "replay": REPLAY_FLAG in advice["flags"],
        "tankmix": _tankmix_outcome(reasons),
        "lead": _lead(advice, tz),
        "checklist": checklist(reasons, tz),
        "protection": _protection(reasons, tz),
        "next_window_text": _next_window(advice, tz),
        "incubation": _incubation(reasons, advice["evaluated_at"], tz),
        "incubation_limited": any(
            r.get("rule") == "downy_incubation" and r.get("inputs", {}).get("coverage") == "limited"
            for r in reasons
        ),
        "evaluated_at": advice["evaluated_at"] and _fmt(advice["evaluated_at"], tz),
    }
    return {"parcel": advice["parcel"], "advice": view, "now": now, "debug": debug}


@router.get("/parcels/{parcel_id}/workspace", response_class=HTMLResponse)
def protection_workspace(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    _require_on(request)
    parcel = _parcel(repo, parcel_id)
    if (unavailable := unavailable_view(request, repo)) is not None:
        return unavailable
    return render(
        request, "advisor/_workspace_protection.html", **_current_context(request, repo, parcel)
    )


def _require_on(request: Request) -> None:
    if not any(m.name == "advisor" for m in visible_manifests(request)):
        raise HTTPException(status_code=404, detail="Crop protection is not enabled.")


@router.get("/parcels/{parcel_id}/history", response_class=HTMLResponse)
def history_fragment(
    parcel_id: int, request: Request, repo: Repo, season: str | None = None
) -> HTMLResponse:
    """Stored parcel history, with an ordinary record-year selector independent of replay."""
    _require_on(request)
    parcel = _parcel(repo, parcel_id)
    return render(
        request, "advisor/_history.html", **_history_context(request, repo, parcel, season)
    )


def _history_context(request: Request, repo: Repo, parcel: Parcel, season: str | None) -> dict:
    from farmcore.apps.advisor import history

    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    choices = history.seasons(repo, parcel.id, now, tz)
    parsed = int(season) if season and re.fullmatch(r"[0-9]{4}", season) else None
    supported = parsed in choices and len(request.query_params.getlist("season")) <= 1
    chosen = parsed if supported else choices[0]
    parent = workspace_parent(request, repo, parcel.id, "history")
    controls = {"farm": str(repo.farm_id)}
    action = f"/advisor/parcels/{parcel.id}#history-heading"
    if parent:
        action = f"/fields/parcels/{parcel.id}"
        controls.update(
            {
                key: value
                for key, value in parse_qsl(urlsplit(parent).query)
                if key in {"view", "return_to"}
            }
        )
        controls["view"] = "history"
    elif len(request.query_params.getlist("return_to")) == 1:
        origin = reading_destination(request, repo, request.query_params["return_to"])
        if origin:
            controls["return_to"] = origin.url
    zone = ZoneInfo(tz)

    def when(value: datetime | None) -> str:
        return value.astimezone(zone).strftime("%a %-d %b %H:%M") if value else ""

    return {
        "parcel": parcel,
        "history": history.season_history(repo, parcel.id, now, tz, chosen),
        "history_action": action,
        "history_controls": controls,
        "history_notice": f"Showing history season {chosen}; the requested year is unavailable."
        if season is not None and not supported
        else None,
        "when": when,
        "now": now,
    }


@router.get("/parcels/{parcel_id}/assessments/{advice_id}.json")
def saved_assessment(parcel_id: int, advice_id: int, request: Request, repo: Repo):
    """Exact saved assessment evidence for assigned members; never current-view projection."""
    from farmcore.apps.review.routes import authorized_review
    from farmcore.core.models.canonical import Advice

    authorized_review(request, repo)
    parcel = _parcel(repo, parcel_id)
    if not 0 < advice_id < 2**63:
        raise HTTPException(404, "Assessment unavailable")
    row = repo.get(Advice, advice_id, scope=service.SCOPE)
    if row is None or row.parcel_id != parcel.id or row.evaluated_at > _now(request, repo):
        raise HTTPException(404, "Assessment unavailable")
    config, verification = service.saved_config(repo, row)
    body = service.advice_json(row)
    if verification == "unverifiable":
        # The named version does not match the digest saved with the advice: never present
        # it as a reproducible assessment.
        body["assessment_status"] = "limited"
        body["headline"] = "Assessment unverifiable: its parameter version does not match"
    return {
        **body,
        "config_verification": verification,
        "effective_config": config,
        "input_snapshot": (row.props or {}).get("input_snapshot"),
    }
