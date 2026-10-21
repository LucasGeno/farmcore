"""Explicit parameter version/review actions bound to a currently assigned farm member."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from farmcore.apps.review import service
from farmcore.apps.review.models import ParameterActivation, ParameterReview, ParameterVersion
from farmcore.apps.review.parameters import catalog
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.identity import principal_for
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.tour import is_tour
from farmcore.web import is_admin

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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
