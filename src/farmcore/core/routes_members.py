"""Farm owners explicitly assign existing local accounts; names never imply ownership."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.identity import canonical_login, wall_now
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.urls import with_farm
from farmcore.web import render

router = APIRouter(dependencies=[Depends(require_farm_owner)])


def members_view(request: Request, repo: Repo, error="", status=200):
    rows = repo.session.execute(
        select(FarmMembership, LocalIdentity.login)
        .join(LocalIdentity)
        .where(FarmMembership.farm_id == repo.farm_id)
        .order_by(LocalIdentity.login)
    ).all()
    response = render(
        request,
        "core/members.html",
        members=[
            {"id": row.id, "login": login, "role": row.role, "revoked": row.revoked_at is not None}
            for row, login in rows
        ],
        error=error,
        selected_farm=repo.farm_id,
        csrf_token=request.cookies.get("farmcore_csrf", ""),
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def removes_last_owner(repo, membership):
    if membership.role != "owner" or membership.revoked_at is not None:
        return False
    identity = repo.session.get(LocalIdentity, membership.identity_id)
    if identity.disabled_at is not None:
        return False  # This membership is not counted among enabled owners.
    # require_farm_owner holds the farm row lock, serializing removals/demotions.
    count = repo.session.scalar(
        select(func.count())
        .select_from(FarmMembership)
        .join(LocalIdentity)
        .where(
            FarmMembership.farm_id == repo.farm_id,
            FarmMembership.role == "owner",
            FarmMembership.revoked_at.is_(None),
            LocalIdentity.disabled_at.is_(None),
        )
    )
    return count <= 1


@router.get("/farms/members")
def members(request: Request, repo: Repo):
    return members_view(request, repo)


@router.post("/farms/members")
def assign(
    request: Request, repo: Repo, login: str = Form(max_length=128), role: str = Form(max_length=16)
):
    try:
        normalized = canonical_login(login)
    except ValueError:
        normalized = ""
    target = repo.session.scalar(
        select(LocalIdentity).where(
            LocalIdentity.login == normalized, LocalIdentity.disabled_at.is_(None)
        )
    )
    if target is None or role not in {"owner", "advisor"}:
        return members_view(
            request, repo, "Choose an existing enabled login and a valid role.", 422
        )
    membership = repo.session.scalar(
        select(FarmMembership).where(
            FarmMembership.identity_id == target.id, FarmMembership.farm_id == repo.farm_id
        )
    )
    if membership is None:
        repo.session.add(FarmMembership(identity_id=target.id, farm_id=repo.farm_id, role=role))
    else:
        if role != "owner" and removes_last_owner(repo, membership):
            return members_view(request, repo, "Keep at least one enabled owner on this farm.", 409)
        membership.role, membership.revoked_at = role, None
    return RedirectResponse(with_farm("/farms/members", repo.farm_id), status_code=303)


@router.post("/farms/members/{membership_id}/revoke")
def revoke(request: Request, repo: Repo, membership_id: int):
    if not 1 <= membership_id < 2**63:
        raise HTTPException(404, "Membership not found.")
    membership = repo.session.scalar(
        select(FarmMembership).where(
            FarmMembership.id == membership_id, FarmMembership.farm_id == repo.farm_id
        )
    )
    if membership is None:
        raise HTTPException(404, "Membership not found.")
    if removes_last_owner(repo, membership):
        return members_view(request, repo, "Keep at least one enabled owner on this farm.", 409)
    membership.revoked_at = wall_now()
    return RedirectResponse(with_farm("/farms/members", repo.farm_id), status_code=303)
