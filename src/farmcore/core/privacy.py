"""Fail-closed HTTP boundary, including routes that do not use a repository."""

import re

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from farmcore.core.access import FarmNotSeeded, selected_farm
from farmcore.core.errors import AUTH_REQUIRED, FARM_DENIED, refusal
from farmcore.core.identity import resolve_principal
from farmcore.web import operator_authenticated

PUBLIC_PATHS = frozenset({"/health", "/admin/sign-in", "/admin/sign-out", "/login"})


class PrivacyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            path = request.url.path
            recipient_path = path == "/share/received" or bool(
                re.fullmatch(
                    r"/share/as/-?[0-9]+/(?:register\.json|ngsi-ld\.json|"
                    r"observations\.json|audit\.json|advice\.json)?",
                    path,
                )
            )
            request.state.principal = resolve_principal(request)
            if request.state.principal is not None:
                restricted = (
                    path.rstrip("/") in {"/apps", "/apps.json", "/clock"}
                    or path.startswith(("/demo", "/debug", "/chat"))
                    or (
                        path.startswith("/assistant/")
                        and path not in {"/assistant/ask", "/assistant/voice"}
                    )
                )
                if restricted and not operator_authenticated(request):
                    await refusal(request, 403, "Installation operator required.")(
                        scope, receive, send
                    )
                    return
                if (
                    not operator_authenticated(request)
                    and path not in PUBLIC_PATHS | {"/logout"}
                    and not path.startswith("/static/")
                    and not recipient_path
                ):
                    try:
                        with request.app.state.session_factory() as session:
                            farm = selected_farm(request, session, request.app.state.settings)
                            request.state.selected_farm_id = farm.id
                    except (HTTPException, FarmNotSeeded):
                        await refusal(request, 403, FARM_DENIED)(scope, receive, send)
                        return
                await self.app(scope, receive, send)
                return
            if (
                path not in PUBLIC_PATHS
                and not path.startswith("/static/")
                and not operator_authenticated(request)
            ):
                if request.cookies.get("farmcore_session"):
                    await refusal(request, 403, AUTH_REQUIRED)(scope, receive, send)
                    return
                allowed = False
                settings = request.app.state.settings
                if settings.demo_mode and request.method in {"GET", "HEAD"}:
                    try:
                        with request.app.state.session_factory() as session:
                            selected_farm(request, session, settings)
                        allowed = True
                    except (HTTPException, FarmNotSeeded):
                        pass
                if allowed:
                    await self.app(scope, receive, send)
                    return
                await refusal(request, 403, AUTH_REQUIRED)(scope, receive, send)
                return
        await self.app(scope, receive, send)
