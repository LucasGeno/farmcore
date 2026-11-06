"""Explicit parameter version/review actions bound to a currently assigned farm member."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from farmcore.apps.review import recommendations, service
from farmcore.apps.review.models import ParameterActivation, ParameterReview, ParameterVersion
from farmcore.apps.review.parameters import catalog
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.identity import principal_for
from farmcore.core.models.canonical import Advice, Parcel
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.tour import is_tour
from farmcore.web import LOCAL_FORMAT, is_admin

router = APIRouter()


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    overrides: dict = Field(min_length=1, max_length=80)
    evidence: str = Field(min_length=1, max_length=10000)
    parent_version_id: int | None = Field(default=None, gt=0, le=2**63 - 1)


class Opinion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verdict: str
    note: str = Field(min_length=1, max_length=10000)
    evidence: str = Field(min_length=1, max_length=10000)


def authorized_review(request, repo):
    """Review authority is narrower than arbitrary owner operation writes.

    get_repo already holds advisory and farm row locks on mutation. Recheck membership
    in that transaction so revocation/disable takes effect on the next write.
    """
    if is_admin(request):
        return
    principal = principal_for(request)
    if is_tour(request) or principal is None:
        raise HTTPException(403, "Assigned owner or advisor required")
    member = repo.session.scalar(
        select(FarmMembership.id)
        .join(LocalIdentity)
        .where(
            FarmMembership.farm_id == repo.farm_id,
            FarmMembership.identity_id == principal.identity_id,
            FarmMembership.role.in_(("owner", "advisor")),
            FarmMembership.revoked_at.is_(None),
            LocalIdentity.disabled_at.is_(None),
        )
    )
    if member is None:
        raise HTTPException(403, "Assigned owner or advisor required")


def current(request, repo, **kwargs):
    settings = request.app.state.settings
    return service.resolve(
        repo, get_clock(repo.session, settings).now(), settings.display_tz, **kwargs
    )


def selected_version(repo, version_id):
    if not 0 < version_id < 2**63:
        raise HTTPException(404, "Parameter version unavailable")
    version = service.version_for(repo, version_id)
    if version is None:
        raise HTTPException(404, "Parameter version unavailable")
    return version


@router.get("/parameters.json")
def parameters(request: Request, repo: Repo):
    authorized_review(request, repo)
    effective = current(request, repo)
    return {
        "status": effective.status,
        "scope": effective.scope,
        "catalog": catalog(effective.config),
        "version": effective.identity(),
        "accuracy_claim": False,
    }


@router.post("/versions", status_code=201)
def propose(request: Request, repo: Repo, body: Proposal):
    authorized_review(request, repo)
    if not body.evidence.strip():
        raise HTTPException(422, "Evidence is required")
    settings = request.app.state.settings
    try:
        version = service.propose(
            repo,
            get_clock(repo.session, settings).now(),
            settings.display_tz,
            body.overrides,
            body.evidence.strip(),
            body.parent_version_id,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    return service.version_json(repo, version)


@router.get("/versions/{version_id}.json")
def version_detail(request: Request, repo: Repo, version_id: int):
    authorized_review(request, repo)
    version = selected_version(repo, version_id)
    reviews = repo.list(
        ParameterReview,
        ParameterReview.version_id == version.id,
        scope=service.SCOPE,
        order_by=ParameterReview.id,
    )
    activations = repo.list(
        ParameterActivation,
        ParameterActivation.version_id == version.id,
        scope=service.SCOPE,
        order_by=ParameterActivation.id,
    )
    return {
        **service.version_json(repo, version),
        "reviews": [review_json(r) for r in reviews],
        "activations": [
            {
                "id": a.id,
                "review_id": a.review_id,
                "actor": a.actor,
                "created_at": a.created_at.isoformat(),
            }
            for a in activations
        ],
    }


def review_json(review):
    return {
        "id": review.id,
        "version_id": review.version_id,
        "reviewer": review.reviewer,
        "created_at": review.created_at.isoformat(),
        "verdict": review.verdict,
        "note": review.note,
        "evidence": review.evidence,
        "accuracy_claim": False,
    }


@router.post("/versions/{version_id}/reviews", status_code=201)
def review(request: Request, repo: Repo, version_id: int, body: Opinion):
    authorized_review(request, repo)
    version = selected_version(repo, version_id)
    if (
        body.verdict not in ("approved", "rejected", "unsure")
        or not body.note.strip()
        or not body.evidence.strip()
    ):
        raise HTTPException(422, "Use approved, rejected or unsure with note and evidence")
    scope = {key: getattr(version, key) for key in service.SCOPE_KEYS}
    opinion = ParameterReview(
        **scope,
        version_id=version.id,
        reviewer=repo.actor,
        created_at=get_clock(repo.session, request.app.state.settings).now(),
        verdict=body.verdict,
        note=body.note.strip(),
        evidence=body.evidence.strip(),
    )
    repo.session.add(opinion)
    repo.session.flush()
    return review_json(opinion)


@router.post("/versions/{version_id}/activate", status_code=201)
def activate(request: Request, repo: Repo, version_id: int):
    authorized_review(request, repo)
    version = selected_version(repo, version_id)
    effective = current(request, repo)
    if any(getattr(version, key) != value for key, value in effective.scope.items()):
        raise HTTPException(409, "Version is outside the current farm/crop/region/year scope")
    opinion = service.latest_review(repo, version.id)
    if opinion is None or opinion.verdict != "approved":
        raise HTTPException(409, "Latest review must approve the version before activation")
    event = ParameterActivation(
        **effective.scope,
        version_id=version.id,
        review_id=opinion.id,
        actor=repo.actor,
        created_at=get_clock(repo.session, request.app.state.settings).now(),
    )
    repo.session.add(event)
    repo.session.flush()
    return {
        "id": event.id,
        "version_id": version.id,
        "review_id": opinion.id,
        "actor": event.actor,
        "created_at": event.created_at.isoformat(),
        "accuracy_claim": False,
    }


@router.get("/")
def page(request: Request, repo: Repo, version: int | None = None, error: str | None = None):
    from farmcore.web import render

    authorized_review(request, repo)
    effective = current(request, repo)
    versions = repo.list(
        ParameterVersion,
        *service.match(ParameterVersion, effective.scope),
        scope=service.SCOPE,
        order_by=ParameterVersion.id.desc(),
        limit=100,
    )
    selected = selected_version(repo, version) if version is not None else effective.version
    detail = version_detail(request, repo, selected.id) if selected else None
    return render(
        request,
        "review/index.html",
        effective=effective.identity(),
        catalog=catalog(effective.config),
        versions=versions,
        selected=detail,
        can_review=True,
        error=error,
    )


def form_redirect(repo, version_id, error=None):
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    values = {"farm": repo.farm_id}
    if version_id is not None:
        values["version"] = version_id
    if error:
        values["error"] = error
    return RedirectResponse("/review/?" + urlencode(values), status_code=303)


@router.post("/propose-form")
async def propose_form(request: Request, repo: Repo):
    authorized_review(request, repo)
    form = await request.form()
    key, raw = str(form.get("parameter", "")), str(form.get("value", ""))
    effective = current(request, repo)
    spec = catalog(effective.config).get(key)
    if spec is None:
        raise HTTPException(422, "Unsupported parameter")
    try:
        value = (
            int(raw)
            if spec["type"] == "integer"
            else float(raw)
            if spec["type"] == "number"
            else raw
        )
        parent = int(form["parent_version_id"]) if form.get("parent_version_id") else None
        result = propose(
            request,
            repo,
            Proposal(
                overrides={key: value},
                evidence=str(form.get("evidence", "")),
                parent_version_id=parent,
            ),
        )
    except (ValueError, HTTPException) as exc:
        error = (
            str(exc.detail)
            if isinstance(exc, HTTPException)
            else "Enter a value and evidence within the parameter bounds"
        )
        return form_redirect(repo, None, error)
    return form_redirect(repo, result["id"])


@router.post("/versions/{version_id}/review-form")
async def review_form(request: Request, repo: Repo, version_id: int):
    authorized_review(request, repo)
    form = await request.form()
    try:
        review(
            request,
            repo,
            version_id,
            Opinion(
                verdict=str(form.get("verdict", "")),
                note=str(form.get("note", "")),
                evidence=str(form.get("evidence", "")),
            ),
        )
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException) and exc.status_code == 404:
            raise
        return form_redirect(repo, version_id, "Choose an opinion and provide a note and evidence")
    return form_redirect(repo, version_id)


@router.post("/versions/{version_id}/activate-form")
def activate_form(request: Request, repo: Repo, version_id: int):
    try:
        activate(request, repo, version_id)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return form_redirect(repo, version_id, str(exc.detail))
    return form_redirect(repo, version_id)


# --- recommendation review: one exact saved assessment, one human opinion -------------------


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    note: str = Field(default="", max_length=2000)


class CheckAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    opinion: str
    note: str = Field(min_length=1, max_length=10000)


def farm_now(request: Request, repo: Repo):
    return get_clock(repo.session, request.app.state.settings).now()


def _tz(request: Request) -> str:
    return request.app.state.settings.display_tz


def require_assigned_advisor(request: Request, repo: Repo) -> None:
    """Only an advisor currently assigned to this farm answers a check; owners ask."""
    if is_admin(request):
        return
    principal = principal_for(request)
    if is_tour(request) or principal is None:
        raise HTTPException(403, "Assigned advisor required")
    member = repo.session.scalar(
        select(FarmMembership.id)
        .join(LocalIdentity)
        .where(
            FarmMembership.farm_id == repo.farm_id,
            FarmMembership.identity_id == principal.identity_id,
            FarmMembership.role == "advisor",
            FarmMembership.revoked_at.is_(None),
            LocalIdentity.disabled_at.is_(None),
        )
    )
    if member is None:
        raise HTTPException(403, "Assigned advisor required")


def actor_name(request: Request) -> str:
    if is_admin(request):
        return "operator"
    principal = principal_for(request)
    return principal.login if principal else "operator"


def role_on_farm(request: Request, repo: Repo) -> str | None:
    principal = principal_for(request)
    return principal.memberships.get(repo.farm_id) if principal else None


def can_request(request: Request, repo: Repo) -> bool:
    return is_admin(request) or role_on_farm(request, repo) == "owner"


def can_respond(request: Request, repo: Repo) -> bool:
    return is_admin(request) or role_on_farm(request, repo) == "advisor"


def assessment(repo: Repo, advice_id: int, now):
    """One stored assessment on this farm, never a future row (the replay clock hides those)."""
    if not 0 < advice_id < 2**63:
        raise HTTPException(404, "Assessment unavailable")
    row = repo.get(Advice, advice_id, scope="advice:read")
    if row is None or row.evaluated_at > now:
        raise HTTPException(404, "Assessment unavailable")
    parcel = repo.get(Parcel, row.parcel_id, scope="fields:read")
    if parcel is None:
        raise HTTPException(404, "Assessment unavailable")
    return row, parcel


def _label(value: str, tz: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz)).strftime(LOCAL_FORMAT)


def recommendation_json(advice, parcel, state, tz: str, *, current: bool):
    def request_payload(event):
        payload = event.payload or {}
        at = event.occurred_at.isoformat()
        return {
            "id": event.id,
            "requested_by": payload.get("requested_by"),
            "requested_at": at,
            "requested_label": _label(at, tz),
            "note": payload.get("note") or "",
        }

    def response_payload(event):
        payload = event.payload or {}
        at = event.occurred_at.isoformat()
        return {
            "id": event.id,
            "request_id": payload.get("request_id"),
            "reviewer": payload.get("reviewer"),
            "reviewed_at": at,
            "reviewed_label": _label(at, tz),
            "opinion": payload.get("opinion"),
            "note": payload.get("note") or "",
        }

    evaluated = advice.evaluated_at.isoformat()
    return {
        "advice_id": advice.id,
        "parcel": {"id": parcel.id, "alias": parcel.alias, "name": parcel.name},
        "evaluated_at": evaluated,
        "evaluated_label": _label(evaluated, tz),
        "decision": advice.decision,
        "assessment_status": (advice.props or {}).get("assessment_status", "legacy-unknown"),
        "headline": (advice.props or {}).get("headline"),
        "request": request_payload(state.request) if state.request else None,
        "response": response_payload(state.response) if state.response else None,
        "open": state.open,
        "current": current,
        "accuracy_claim": False,
        "model_output_unchanged": True,
    }


def latest_advice_ids(repo: Repo, now):
    """parcel id -> the latest stored assessment up to the clock, in one farm-scoped read."""
    stmt = (
        select(Advice)
        .where(Advice.farm_id == repo.farm_id, Advice.evaluated_at <= now)
        .order_by(Advice.parcel_id, Advice.evaluated_at.desc(), Advice.id.desc())
        .ext(distinct_on(Advice.parcel_id))
    )
    return {
        row.parcel_id: row.id
        for row in repo.execute(stmt, scope="advice:read", resource="advice").scalars()
    }


def farm_reviews(request: Request, repo: Repo):
    """Resolve each request/answer event to its stored assessment, newest request first."""
    now = farm_now(request, repo)
    latest = latest_advice_ids(repo, now)
    out = []
    for state in recommendations.states(repo):
        row = repo.get(Advice, state.advice_id, scope="advice:read")
        if row is None or row.evaluated_at > now:
            continue
        parcel = repo.get(Parcel, row.parcel_id, scope="fields:read")
        if parcel is None:
            continue
        out.append(
            recommendation_json(
                row, parcel, state, _tz(request), current=latest.get(row.parcel_id) == row.id
            )
        )
    return out


@router.get("/recommendations.json")
def recommendations_json(request: Request, repo: Repo):
    authorized_review(request, repo)
    return {"reviews": farm_reviews(request, repo), "accuracy_claim": False}


@router.get("/assessments/{advice_id}.json")
def assessment_state(request: Request, repo: Repo, advice_id: int):
    authorized_review(request, repo)
    now = farm_now(request, repo)
    row, parcel = assessment(repo, advice_id, now)
    latest = latest_advice_ids(repo, now)
    return recommendation_json(
        row,
        parcel,
        recommendations.state(repo, row.id),
        _tz(request),
        current=latest.get(row.parcel_id) == row.id,
    )


@router.post("/assessments/{advice_id}/request", status_code=201)
def request_check(request: Request, repo: Repo, advice_id: int, body: CheckRequest):
    require_farm_owner(request, repo)
    now = farm_now(request, repo)
    row, parcel = assessment(repo, advice_id, now)
    try:
        state = recommendations.request_check(
            repo,
            now,
            row,
            parcel,
            note=body.note.strip(),
            requested_by=actor_name(request),
        )
    except recommendations.ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from None
    latest = latest_advice_ids(repo, now)
    return recommendation_json(
        row, parcel, state, _tz(request), current=latest.get(row.parcel_id) == row.id
    )


@router.post("/requests/{request_id}/response", status_code=201)
def respond_to_request(request: Request, repo: Repo, request_id: int, body: CheckAnswer):
    require_assigned_advisor(request, repo)
    if body.opinion not in recommendations.OPINIONS:
        raise HTTPException(422, "Use agree, disagree or unsure with a note")
    if not body.note.strip():
        raise HTTPException(422, "A review note is required")
    now = farm_now(request, repo)
    try:
        asked = recommendations.request(repo, request_id)
    except recommendations.ReviewNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    row, parcel = assessment(repo, asked.res_id, now)
    try:
        state = recommendations.respond(
            repo,
            now,
            row,
            parcel,
            asked.id,
            opinion=body.opinion,
            note=body.note.strip(),
            reviewer=actor_name(request),
        )
    except recommendations.ReviewNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except recommendations.ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from None
    latest = latest_advice_ids(repo, now)
    return recommendation_json(
        row, parcel, state, _tz(request), current=latest.get(row.parcel_id) == row.id
    )


@router.get("/assessments/{advice_id}")
def assessment_page(request: Request, repo: Repo, advice_id: int):
    """The same check block as a full page, for a browser without the card fragment."""
    from farmcore.web import render

    authorized_review(request, repo)
    now = farm_now(request, repo)
    row, parcel = assessment(repo, advice_id, now)
    latest = latest_advice_ids(repo, now)
    return render(
        request,
        "review/assessment.html",
        review=recommendation_json(
            row,
            parcel,
            recommendations.state(repo, row.id),
            _tz(request),
            current=latest.get(row.parcel_id) == row.id,
        ),
        can_request=can_request(request, repo),
        can_respond=can_respond(request, repo),
        opinions=recommendations.OPINIONS,
        opinion_words=recommendations.OPINION_WORDS,
    )


@router.get("/assessments/{advice_id}/card")
def assessment_card(request: Request, repo: Repo, advice_id: int):
    from farmcore.web import render

    authorized_review(request, repo)
    now = farm_now(request, repo)
    row, parcel = assessment(repo, advice_id, now)
    latest = latest_advice_ids(repo, now)
    return render(
        request,
        "review/_check.html",
        review=recommendation_json(
            row,
            parcel,
            recommendations.state(repo, row.id),
            _tz(request),
            current=latest.get(row.parcel_id) == row.id,
        ),
        can_request=can_request(request, repo),
        can_respond=can_respond(request, repo),
        opinions=recommendations.OPINIONS,
        opinion_words=recommendations.OPINION_WORDS,
    )


def recommendations_redirect(repo, *, error=None, requested=None):
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    values = {"farm": repo.farm_id}
    if error:
        values["error"] = error
    if requested:
        values["requested"] = requested
    return RedirectResponse("/review/recommendations/?" + urlencode(values), status_code=303)


@router.post("/assessments/{advice_id}/request-form")
async def request_check_form(request: Request, repo: Repo, advice_id: int):
    require_farm_owner(request, repo)
    form = await request.form()
    note = str(form.get("note", ""))
    if len(note) > 2000:
        return recommendations_redirect(repo, error="Keep the request under 2000 characters")
    now = farm_now(request, repo)
    try:
        row, parcel = assessment(repo, advice_id, now)
        recommendations.request_check(
            repo,
            now,
            row,
            parcel,
            note=note.strip(),
            requested_by=actor_name(request),
        )
    except recommendations.ReviewConflict as exc:
        return recommendations_redirect(repo, error=str(exc))
    return recommendations_redirect(repo, requested=advice_id)


@router.post("/requests/{request_id}/response-form")
async def respond_to_request_form(request: Request, repo: Repo, request_id: int):
    require_assigned_advisor(request, repo)
    form = await request.form()
    try:
        respond_to_request(
            request,
            repo,
            request_id,
            CheckAnswer(
                opinion=str(form.get("opinion", "")),
                note=str(form.get("note", "")),
            ),
        )
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            if exc.status_code in (403, 404):
                raise
            error = str(exc.detail)
        else:
            error = "Choose an opinion and provide a note"
        return recommendations_redirect(repo, error=error)
    return recommendations_redirect(repo)


@router.get("/recommendations/")
def recommendations_page(request: Request, repo: Repo, error: str | None = None):
    from farmcore.web import render

    authorized_review(request, repo)
    return render(
        request,
        "review/recommendations.html",
        reviews=farm_reviews(request, repo),
        can_respond=can_respond(request, repo),
        error=error,
    )
