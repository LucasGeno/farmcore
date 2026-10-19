"""Cookie names scoped by installation, so installations on one host never share a cookie.

Browsers scope cookies by host, not by port: two installations on `localhost:8001` and
`localhost:8002` (or behind one proxy host) would otherwise read each other's session, CSRF and
admin cookies. This outermost ASGI middleware renames every cookie the app sets to
`fc-<installation>-<name>` and, on the way in, passes the app only this installation's cookies
with the prefix removed. Code inside the app keeps using the plain names.

Cookies a browser script writes itself (`CLIENT_COOKIES`, non-secret view preferences) cannot
carry the prefix yet and pass through unchanged.
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Written by browser scripts under their plain name (notifications/static/bell.js).
CLIENT_COOKIES = re.compile(r"farmcore_notifications_read_\d+")


def cookie_prefix(installation_id: str) -> str:
    return f"fc-{installation_id.replace('-', '')[:12]}-"


def _scope_request_cookies(header: str, prefix: str) -> str:
    kept = []
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if name.startswith(prefix) and len(name) > len(prefix):
            kept.append(f"{name[len(prefix) :]}{sep}{value}")
        elif CLIENT_COOKIES.fullmatch(name):
            kept.append(f"{name}{sep}{value}")
    return "; ".join(kept)


class InstallationCookies:
    def __init__(self, app: ASGIApp, installation_id: str) -> None:
        self.app = app
        self.prefix = cookie_prefix(installation_id)
        self._prefix_bytes = self.prefix.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = []
        for key, value in scope["headers"]:
            if key == b"cookie":
                value = _scope_request_cookies(value.decode("latin-1"), self.prefix).encode(
                    "latin-1"
                )
                if not value:
                    continue
            headers.append((key, value))
        scope = {**scope, "headers": headers}

        async def send_scoped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (k, self._prefix_bytes + v if k == b"set-cookie" else v)
                        for k, v in message.get("headers", [])
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_scoped)
