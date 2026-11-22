"""`POST /weather_push/observations`: an on-farm device pushes its readings.

Each request passes, in order, before anything is stored:

1. A device token in `Authorization: Bearer` (`devices.authenticate_device`): 401 otherwise,
   one answer for every cause. The path is open to requests without a session
   (`core.privacy.PUBLIC_PATHS`); the token is the only credential, never a cookie or a URL.
2. That device's rate (429 with `Retry-After`): `RATE_PER_MINUTE`, a token bucket per device
   in this process.
3. The format: `application/json` (SensorThings deep insert) or `text/csv` (415 otherwise).
4. The body size: `MAX_BYTES` (413), read no further than that.
5. Every reading (`parse`): one the schema cannot hold refuses the whole payload (422).

Then one receipt (`store.record_receipt`, the on-farm tier, the farm's own data) and the
readings under the device's stations (`store.store_observations`), in one transaction; the token
is checked again inside it, so a device revoked meanwhile stores nothing. The same payload again
returns the receipt already held and stores nothing new; a reading already held keeps its first
value (append-only evidence). Responses carry no farm data, only counts and the receipt id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from itertools import groupby

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from farmcore.apps.weather_push import parse
from farmcore.apps.weather_push.devices import authenticate_device, station_for
from farmcore.apps.weather_push.provider import LICENCE, NAME
from farmcore.core.access import TokenRejected
from farmcore.core.mcp_http import RateLimit, bearer
from farmcore.core.weather import store
from farmcore.core.weather.contract import Fetched, Tier
from farmcore.core.weather.contract import Request as WeatherRequest
from farmcore.db import session_scope

logger = logging.getLogger(__name__)
router = APIRouter()

PATH = "/observations"
MAX_BYTES = 256 * 1024  # assumed: MAX_READINGS JSON readings fit with room
RATE_PER_MINUTE = 60  # assumed: one push a minute on average, bursts of a minute's worth
FORMATS = {"application/json": "json", "text/csv": "csv"}


def _refusal(status: int, message: str, **headers: str) -> Response:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _unauthorized() -> Response:
    return _refusal(
        401,
        "A device token is required.",
        **{"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _limiter(request: Request) -> RateLimit:
    state = request.app.state
    limiter = getattr(state, "weather_push_rate", None)
    if limiter is None:
        limiter = state.weather_push_rate = RateLimit(RATE_PER_MINUTE)
    return limiter


async def _body(request: Request) -> bytes | None:
    """The body, or None once it is larger than `MAX_BYTES` (the rest is never read)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BYTES:
            return None
    return bytes(body)


@router.post(PATH)
async def push(request: Request) -> Response:
    factory = request.app.state.session_factory
    raw = bearer(request.headers)

    def device() -> int | None:
        with session_scope(factory) as session:
            try:
                return authenticate_device(session, raw)[0].id
            except TokenRejected:
                return None

    device_id = await anyio.to_thread.run_sync(device) if raw else None
    if device_id is None:
        logger.info("weather push refused: 401")
        return _unauthorized()
    if wait := _limiter(request).wait(str(device_id)):
        return _refusal(429, "Too many requests.", **{"Retry-After": str(wait)})
    kind = FORMATS.get(request.headers.get("content-type", "").split(";", 1)[0].strip().lower())
    if kind is None:
        return _refusal(415, "Send application/json or text/csv.")
    body = await _body(request)
    if body is None:
        return _refusal(413, f"A push is at most {MAX_BYTES} bytes.")

    def receive() -> Response:
        with session_scope(factory) as session:
            try:
                token, grant = authenticate_device(session, raw)
            except TokenRejected:
                return _unauthorized()
            now = datetime.now(UTC)
            try:
                readings = (parse.parse_json if kind == "json" else parse.parse_csv)(body, now)
            except parse.Refused as exc:
                return _refusal(422, str(exc))
            receipt = store.record_receipt(
                session,
                provider=NAME,
                licence=LICENCE,
                fetched=Fetched(
                    WeatherRequest(
                        dataset=f"sensorthings-{kind}",
                        params={"device": token.id},
                        endpoint_kind="push",
                    ),
                    fetched_at=now,
                    payload=body,
                    values=[r.value for r in readings],
                ),
                farm_id=grant.farm_id,
                tier=Tier.ON_FARM,
            )
            by_depth = sorted(readings, key=lambda r: (r.depth_m is not None, r.depth_m or 0))
            try:
                for depth, group in groupby(by_depth, key=lambda r: r.depth_m):
                    store.store_observations(
                        session,
                        receipt,
                        station_for(session, token, depth),
                        [r.value for r in group],
                    )
            except store.WeatherValueError as exc:
                session.rollback()
                return _refusal(422, str(exc))
            return JSONResponse(
                {"readings": len(readings), "receipt_id": receipt.id}, status_code=201
            )

    return await anyio.to_thread.run_sync(receive)
