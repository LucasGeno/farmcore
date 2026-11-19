"""Draft review: `GET /drafts/<id>` shows a proposed write with its kind's review fragment;
`POST /drafts/<id>/confirm` (with the reviewed `revision`) records it through the kind's app,
`POST /drafts/<id>/cancel` discards it (`core/drafts.py`).

Owner-only, like every farm write. Another farm's or principal's draft is a 404. A refused
confirm (expired, changed since opened, cancelled) re-renders the page with the reason and a 409.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from farmcore.core import drafts
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.manifest import collect_draft_kinds
from farmcore.core.models.drafts import Draft
from farmcore.core.registry import visible_manifests
from farmcore.web import context_url, is_admin, render

router = APIRouter(dependencies=[Depends(require_farm_owner)])
SCOPE = "logs:write"


def request_actor(request: Request) -> str:
    """The draft actor of this web request: the operator, or the signed-in member."""
    from farmcore.core.identity import principal_for

    if is_admin(request):
        return "operator"
    principal = principal_for(request)
    if principal is None:
        raise HTTPException(403, "Farm owner required.")
    return f"identity:{principal.identity_id}"


def load(request: Request, repo: Repo, draft_id: int) -> Draft:
    """The draft this request may review, read through the audited repository; else 404."""
    repo.execute(
        select(Draft.id).where(Draft.id == draft_id, Draft.farm_id == repo.farm_id),
        scope=SCOPE,
        resource="draft",
    )
    try:
        return drafts.get(
            repo.session, farm_id=repo.farm_id, draft_id=draft_id, actor=request_actor(request)
        )
    except drafts.DraftNotFound as exc:
        raise HTTPException(404, exc.reason) from exc


def _page(request: Request, repo: Repo, draft: Draft, problem: str = "", status: int = 200):
    kind = collect_draft_kinds(visible_manifests(request)).get(draft.kind)
    refusal = problem or (
        (r.reason if (r := drafts.refusal(draft)) else "") if draft.status == "open" else ""
    )
    response = render(
        request,
        "core/draft.html",
        draft=draft,
        status_label="expired" if drafts.is_expired(draft) else draft.status,
        review_url=context_url(request, f"{kind[1].review}?draft={draft.id}") if kind else "",
        problem=refusal,
        can_finalize=draft.status == "open" and not drafts.is_expired(draft),
        csrf_token=request.cookies.get("farmcore_csrf", ""),
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
def review(draft_id: int, request: Request, repo: Repo):
    return _page(request, repo, load(request, repo, draft_id))


@router.post("/drafts/{draft_id}/confirm")
def confirm(
    draft_id: int,
    request: Request,
    repo: Repo,
    tasks: BackgroundTasks,
    revision: Annotated[int, Form(ge=1)],
):
    draft = load(request, repo, draft_id)
    now = get_clock(repo.session, request.app.state.settings).now()
    try:
        draft, recorded = drafts.confirm(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            draft_id=draft.id,
            actor=request_actor(request),
            revision=revision,
            now=now,
            services={
                "spray_weather_reader": getattr(request.app.state, "spray_weather_reader", None)
            },
        )
    except drafts.DraftError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, exc.reason) from exc
        repo.session.rollback()
        draft = load(request, repo, draft_id)
        return _page(request, repo, draft, exc.reason, exc.status_code)
    if recorded:
        # Function-scoped Repo commits before background work; a rollback never runs it.
        from farmcore.core import jobs

        tasks.add_task(
            jobs.run_now,
            request.app.state.session_factory,
            request.app.state.manifests,
            "advisor.evaluate",
            request.app.state.settings,
            farm_ids=(repo.farm_id,),
        )
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)


@router.post("/drafts/{draft_id}/cancel")
def cancel(draft_id: int, request: Request, repo: Repo):
    draft = load(request, repo, draft_id)
    try:
        drafts.cancel(
            repo.session, farm_id=repo.farm_id, draft_id=draft.id, actor=request_actor(request)
        )
    except drafts.DraftError as exc:
        repo.session.rollback()
        return _page(request, repo, load(request, repo, draft_id), exc.reason, exc.status_code)
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)
