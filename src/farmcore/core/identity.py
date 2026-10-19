"""Local credentials and session primitives. Never derive identity from grant names."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request

SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 192 * 1024 * 1024


def wall_now():
    return datetime.now(UTC)


def canonical_login(value: str) -> str:
    value = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", value) is None:
        raise ValueError("Use a login of 1–128 letters, digits, dots, underscores or hyphens.")
    return value


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 256:
        raise ValueError("Use a password of 15–256 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def reserve_login_attempt(factory, login: str, peer: str) -> bool:
    """Reserve before credentials in an independent committed transaction.

    Fixed slots cap storage at 2049 rows. Collisions conservatively share a budget.
    The global row is locked first, bounding total hash work across processes as well.
    Client forwarding headers are deliberately ignored: only the socket peer is trusted.
    """
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from farmcore.core.models.identity import LoginBucket

    now = wall_now()
    dimensions = (
        ("global", 0, 100),
        ("peer", _slot(peer), 20),
        ("account", _slot(login.strip().lower()), 5),
    )
    with factory.begin() as session:
        allowed = True
        for dimension, slot, limit in dimensions:
            session.execute(
                insert(LoginBucket)
                .values(dimension=dimension, slot=slot, window_start=now, attempts=0)
                .on_conflict_do_nothing()
            )
            row = session.scalar(
                select(LoginBucket)
                .where(LoginBucket.dimension == dimension, LoginBucket.slot == slot)
                .with_for_update()
            )
            if row.window_start <= now - timedelta(minutes=10):
                row.window_start, row.attempts = now, 0
            if row.attempts >= limit:
                allowed = False
            else:
                row.attempts += 1
        return allowed


def _slot(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:4]) % 1024


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_matches(password: str, encoded: str) -> bool:
    import hmac

    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=32,
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${'00' * 16}${'00' * 32}"


@dataclass(frozen=True)
class Principal:
    identity_id: int
    login: str
    session_hash: str
    csrf_hash: str
    memberships: dict[int, str]


def resolve_principal(request):
    """Resolve afresh on every HTTP request; revocation never waits for a cache."""
    from sqlalchemy import select

    from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession

    token = request.cookies.get("farmcore_session", "")
    if not token or len(token) > 128:
        return None
    with request.app.state.session_factory() as session:
        saved = session.get(LocalSession, token_hash(token))
        if saved is None or saved.revoked_at is not None or saved.expires_at <= wall_now():
            return None
        identity = session.get(LocalIdentity, saved.identity_id)
        if identity is None or identity.disabled_at is not None:
            return None
        memberships = dict(
            session.execute(
                select(FarmMembership.farm_id, FarmMembership.role).where(
                    FarmMembership.identity_id == identity.id, FarmMembership.revoked_at.is_(None)
                )
            ).all()
        )
        return Principal(
            identity.id, identity.login, saved.token_hash, saved.csrf_hash, memberships
        )


def principal_for(request):
    return getattr(request.state, "principal", None)


async def require_csrf(request: Request):
    """Every member-cookie mutation proves its origin and server-bound nonce."""
    from fastapi import HTTPException

    from farmcore.core.routes_demo import same_origin
    from farmcore.web import operator_authenticated

    principal = principal_for(request)
    if request.method in {"GET", "HEAD", "OPTIONS"} or request.url.path == "/login":
        return
    if operator_authenticated(request):
        if request.headers.get("X-Admin-Token"):
            return  # Explicit operator header, never an ambient cookie credential.
        same_origin(request)
        return
    if principal is None:
        return
    same_origin(request)
    nonce = request.headers.get("X-CSRF-Token", "")
    if not nonce and request.headers.get("content-type", "").split(";", 1)[0] in {
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }:
        form = await request.form()
        nonce = form.get("csrf_token", "")
    if (
        not isinstance(nonce, str)
        or len(nonce) > 128
        or not secrets.compare_digest(token_hash(nonce), principal.csrf_hash)
    ):
        raise HTTPException(403, "Invalid request token.")
