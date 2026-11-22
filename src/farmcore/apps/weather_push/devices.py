"""Devices: an on-farm sensor's token and station.

A device token is a personal access token (`access.issue_token`, the byo-agent token store: no
store of its own) on a grant whose only scope is `weather:push`. That scope is outside
`access.TOKEN_SCOPES`, so every read path (`access.authenticate_token`: MCP, agents) refuses
the token, and `authenticate_device` refuses every token holding anything else. The grant shows
on Sharing and is revoked there like any grant; the next push is then refused.

Each device has its own station, owned by the farm and never shared (`device<token id>`), made
when it is registered at the location given then. A soil probe's readings at one depth go to a
station of their own at the same place (`device<token id>@<depth>m`): the observation key has
no depth, so each depth is one series.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from farmcore.apps.weather_push.provider import NAME
from farmcore.core.access import (
    LAST_USED_EVERY,
    OWNER,
    TOKEN_MAX_LEN,
    TOKEN_PREFIXES,
    TokenRejected,
    is_farm_owner,
    issue_token,
)
from farmcore.core.identity import token_hash
from farmcore.core.models.canonical import DataGrant
from farmcore.core.models.identity import AccessToken
from farmcore.core.models.weather import WeatherStation
from farmcore.core.weather import store
from farmcore.core.weather.contract import Station

SCOPE = "weather:push"
PURPOSE = "on-farm sensor readings"
VALID_DAYS = 366  # assumed: a device token is renewed yearly, like any credential
NAME_MAX_LEN = 100  # assumed


def register_device(
    session: Session,
    farm_id: int,
    *,
    created_by: int,
    name: str,
    latitude: float,
    longitude: float,
    valid_days: int = VALID_DAYS,
) -> tuple[DataGrant, str]:
    """A device of `farm_id`, issued by `created_by` (a current owner): its grant and token, and
    its station at (`latitude`, `longitude`). Returns (grant, token); the token exists only in
    the return value, so show it once. `PermissionError` for a non-owner, `ValueError` on a
    bad name, location or validity; nothing is written then."""
    if not is_farm_owner(session, created_by, farm_id):
        raise PermissionError("Only a farm owner registers a device.")
    label = name.strip()
    if not label or len(label) > NAME_MAX_LEN or label.casefold() == OWNER:
        raise ValueError(f"Name the device (1 to {NAME_MAX_LEN} characters, not 'owner').")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("The device's location is not a latitude and longitude.")
    if not 1 <= valid_days <= VALID_DAYS:
        raise ValueError(f"A device token lasts 1 to {VALID_DAYS} days; renew it then.")
    now = datetime.now(UTC)
    grant = DataGrant(
        farm_id=farm_id,
        grantee=label,
        scopes=[SCOPE],
        purpose=PURPOSE,
        valid_from=now,
        valid_to=now + timedelta(days=valid_days),
    )
    session.add(grant)
    session.flush()
    token, raw = issue_token(session, grant, kind="personal", created_by=created_by)
    store.upsert_station(
        session, NAME, Station(_station_id(token.id), label, latitude, longitude), farm_id
    )
    return grant, raw


def authenticate_device(session: Session, raw: str | None) -> tuple[AccessToken, DataGrant]:
    """The device a token belongs to: a personal token whose grant holds `weather:push` only,
    not revoked, inside its validity, its issuer still an owner of the farm. Checked afresh on
    every push. `TokenRejected` otherwise (one message for every cause)."""
    prefix = TOKEN_PREFIXES["personal"]
    if not isinstance(raw, str) or len(raw) > TOKEN_MAX_LEN or not raw.startswith(prefix):
        raise TokenRejected
    digest = token_hash(raw)
    token = session.scalar(
        select(AccessToken).where(AccessToken.token_hash == digest, AccessToken.kind == "personal")
    )
    if token is None or not hmac.compare_digest(token.token_hash or "", digest):
        raise TokenRejected
    grant = session.get(DataGrant, token.grant_id, populate_existing=True)
    now = datetime.now(UTC)
    if (
        grant is None
        or grant.revoked_at is not None
        or (grant.valid_from is not None and grant.valid_from > now)
        or (grant.valid_to is not None and grant.valid_to <= now)
        or list(grant.scopes or ()) != [SCOPE]
        or not is_farm_owner(session, token.created_by_identity_id, grant.farm_id)
    ):
        raise TokenRejected
    if token.last_used_at is None or now - token.last_used_at >= LAST_USED_EVERY:
        token.last_used_at = now
        session.flush()
    return token, grant


def station_for(session: Session, token: AccessToken, depth_m: Decimal | None) -> WeatherStation:
    """The device's station, or its probe station at `depth_m` (made on first use, at the
    device's place). `TokenRejected` when the device has no station (it was never registered
    here)."""
    base = session.execute(
        select(
            WeatherStation,
            func.ST_Y(WeatherStation.geom).label("latitude"),
            func.ST_X(WeatherStation.geom).label("longitude"),
        ).where(
            WeatherStation.provider == NAME,
            WeatherStation.provider_station_id == _station_id(token.id),
        )
    ).first()
    if base is None:
        raise TokenRejected
    if depth_m is None:
        return base.WeatherStation
    depth = format(depth_m.normalize(), "f")
    return store.upsert_station(
        session,
        NAME,
        Station(
            f"{_station_id(token.id)}@{depth}m",
            f"{base.WeatherStation.name} ({depth} m deep)",
            base.latitude,
            base.longitude,
        ),
        base.WeatherStation.owner_farm_id,
    )


def _station_id(token_id: int) -> str:
    return f"device{token_id}"
