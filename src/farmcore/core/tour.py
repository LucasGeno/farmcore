"""Browser-scoped tour restriction, enforced before routing or body parsing.

This limits HTTP actions; it is not authentication or a pause for background jobs.
Normal read auditing is retained. Removing the cookie leaves the existing admin policy.

The only unsafe requests a tour browser may send are the ones that leave the tour: `EXIT`
(drop the cookie) and the two sign-outs, which also drop it. None writes farm data, and each
keeps its usual Origin/CSRF checks.
"""

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "farmcore_tour"
EXIT = "/tour/exit"
LEAVES_TOUR = frozenset({EXIT, "/logout", "/admin/sign-out"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_tour(request: Request) -> bool:
    return bool(getattr(request.state, "tour", False)) or COOKIE in request.cookies


def end_tour(request: Request, response) -> None:
    """Drop the tour cookie, with the attributes `/tour` set it with."""
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )


class TourMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            leaving = request.method == "POST" and request.url.path in LEAVES_TOUR
            if is_tour(request) and request.method not in SAFE_METHODS and not leaving:
                from farmcore.core.errors import refusal  # errors -> web -> tour
                from farmcore.core.identity import resolve_principal

                # The refusal page carries the tour banner; its exit form needs the member's
                # CSRF nonce, so resolve the member here (privacy runs after this layer).
                request.state.principal = resolve_principal(request)
                await refusal(request, 403, "Tour mode is read-only.")(scope, receive, send)
                return
        await self.app(scope, receive, send)
