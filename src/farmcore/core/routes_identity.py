"""Local member login; installation operator sign-in remains separate."""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, text

from farmcore.core.identity import (
    DUMMY_HASH,
    canonical_login,
    password_matches,
    reserve_login_attempt,
    token_hash,
    wall_now,
)
from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession
from farmcore.core.routes_demo import same_origin
from farmcore.core.routes_farms import safe_next
from farmcore.web import is_sunlight, templates

router = APIRouter()
LOGIN_CSRF_COOKIE = "farmcore_login_csrf"
NEXT_MAX = 2048


def return_path(value: str | None) -> str:
    """`next` when it is a path on this site (never another origin, never back to sign-in),
    else ""."""
    if not value or len(value) > NEXT_MAX or not value.isprintable() or safe_next(value) != value:
        return ""
    if value.split("?")[0] in {"/login", "/logout"}:
        return ""
    return value


def login_page(request: Request, *, error: str = "", status: int = 200, next: str = ""):
    nonce = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "core/login.html",
        {
            "csrf_token": nonce,
            "error": error,
            "next": return_path(next),
            "sunlight": is_sunlight(request),
        },
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        LOGIN_CSRF_COOKIE, nonce, secure=True, httponly=True, samesite="strict", max_age=600
    )
    return response


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = ""):
    return login_page(request, next=next)


@router.post("/login")
def sign_in(
    request: Request,
    login: str = Form(max_length=128),
    password: str = Form(max_length=256),
    csrf_token: str = Form(default="", max_length=128),
    next: str = Form(default="", max_length=NEXT_MAX),
):
    same_origin(request)
    expected = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not expected or not secrets.compare_digest(expected.encode(), csrf_token.encode()):
        raise HTTPException(403, "Invalid request token.")
    peer = request.client.host if request.client else "unknown"
    if not reserve_login_attempt(request.app.state.session_factory, login, peer):
        response = login_page(
            request, error="Too many attempts. Try again later.", status=429, next=next
        )
        response.headers["Retry-After"] = "600"
        return response
    try:
        normalized = canonical_login(login)
    except ValueError:
        normalized = ""
    with request.app.state.session_factory.begin() as session:
        # Serialize expensive hashes across processes: at most one 128MiB credential check.
        session.execute(text("SELECT pg_advisory_xact_lock(752004102)"))
        identity = session.scalar(
            select(LocalIdentity).where(LocalIdentity.login == normalized).with_for_update()
        )
        valid = password_matches(password, identity.password_hash if identity else DUMMY_HASH)
        if not valid or identity is None or identity.disabled_at is not None:
            return login_page(request, error="Sign-in failed.", status=401, next=next)
        has_membership = (
            session.scalar(
                select(FarmMembership.id)
                .where(
                    FarmMembership.identity_id == identity.id, FarmMembership.revoked_at.is_(None)
                )
                .limit(1)
            )
            is not None
        )
        nonce, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = wall_now()
        session.add(
            LocalSession(
                token_hash=token_hash(nonce),
                csrf_hash=token_hash(csrf),
                identity_id=identity.id,
                created_at=now,
                expires_at=now + timedelta(hours=12),
            )
        )
    landing = "/" if has_membership else "/share/received"
    response = RedirectResponse(return_path(next) or landing, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        "farmcore_session", nonce, max_age=43200, secure=True, httponly=True, samesite="strict"
    )
    response.set_cookie(
        "farmcore_csrf", csrf, max_age=43200, secure=True, httponly=True, samesite="strict"
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, secure=True, httponly=True, samesite="strict")
    response.delete_cookie("farmcore_farm", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request):
    from farmcore.core.identity import principal_for

    principal = principal_for(request)
    if principal is None:
        raise HTTPException(403, "Authentication required.")
    with request.app.state.session_factory.begin() as session:
        saved = session.get(LocalSession, principal.session_hash)
        if saved is not None:
            saved.revoked_at = wall_now()
    response = RedirectResponse("/login", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    for name in ("farmcore_session", "farmcore_csrf", "farmcore_farm"):
        response.delete_cookie(name, secure=True, httponly=True, samesite="strict")
    return response
