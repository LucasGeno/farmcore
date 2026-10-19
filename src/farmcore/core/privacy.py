"""Fail-closed HTTP boundary, including routes that do not use a repository."""

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from farmcore.core.access import FarmNotSeeded, selected_farm
from farmcore.web import operator_authenticated

PUBLIC_PATHS = frozenset({"/health", "/admin/sign-in", "/admin/sign-out", "/login"})


class PrivacyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            path = request.url.path
            if (
                path not in PUBLIC_PATHS
                and not path.startswith("/static/")
                and not operator_authenticated(request)
            ):
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
                response = JSONResponse({"detail": "Authentication required."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
