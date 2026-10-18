"""Browser-scoped tour restriction, enforced before routing or body parsing.

This limits HTTP actions; it is not authentication or a pause for background jobs.
Normal read auditing is retained. Removing the cookie leaves the existing admin policy.
"""

from fastapi import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "farmcore_tour"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_tour(request: Request) -> bool:
    return bool(getattr(request.state, "tour", False)) or COOKIE in request.cookies


class TourMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            if is_tour(request) and request.method not in SAFE_METHODS:
                response = JSONResponse({"detail": "Tour mode is read-only."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
