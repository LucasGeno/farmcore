"""Who an OAuth client is: listed by the operator, registered by itself, or a metadata URL.

Lookup order (`resolve`): the operator's `MCP_OAUTH_CLIENTS`, then an https client ID as a
Client ID Metadata Document URL (draft-ietf-oauth-client-id-metadata-document-03, "CIMD"; the
MCP authorization spec's preferred registration), then a client that registered itself through
Dynamic Client Registration (RFC 7591, kept for clients without CIMD).

- A metadata document is fetched by `fetch.MetadataFetcher`, accepted only when its `client_id`
  is the URL it came from byte for byte (CIMD §4) and it asks for nothing a public client may
  not hold (§4.1), then cached (`oauth_client`, §6.2) between `CACHE_MIN` and `CACHE_MAX`. A
  failed or invalid fetch is never cached; a transient failure keeps the last good document,
  an invalid one ends it and every connection of that client (§6.1).
- Open registration issues public clients only (`none`, no secret), never an https client ID
  (CIMD §9.1), and is capped per address slot and while unused (RFC 7591 §3). A software
  statement is ignored (RFC 7591 §3.1.1), so nothing in one is trusted.
- Every name and redirect URI from a client is self-reported (RFC 7591 §5, CIMD §10.10): the
  consent page says so and shows the hosts. A redirect URI is https, or http to a loopback
  host, whose port may then change per request (RFC 8252 §7.3, RFC 9700 §4.1.3).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from farmcore.core.models.identity import AccessToken, OAuthCode, RegisteredClient
from farmcore.core.oauth.fetch import Fetched, FetchFailed, peer_bucket, plain_host, url_problem
from farmcore.settings import Settings

OPERATOR, DYNAMIC, METADATA = "operator", "dynamic", "metadata"
NAME_MAX = 80
UNNAMED = "Unnamed agent"
REDIRECT_URIS_MAX = 10  # assumed: enough for any real client, bounds what is stored
REDIRECT_URI_MAX = 2048  # assumed
# CIMD §6.2: honour Cache-Control within a minimum and a maximum (both assumed).
CACHE_MIN = timedelta(minutes=5)
CACHE_DEFAULT = timedelta(hours=1)
CACHE_MAX = timedelta(hours=24)
# RFC 7591 §3: open registration "MAY be rate-limited"; these bounds are assumed.
REGISTRATIONS_PER_PEER = 10  # per address slot, per hour
UNUSED_REGISTRATIONS = 100  # registered and never connected, installation-wide
UNUSED_TTL = timedelta(days=1)  # an unused registration is forgotten after this
DYNAMIC_PREFIX = "fc_dcr_"
_REGISTER_LOCK = 0x6663_6463_7200  # pg_advisory_xact_lock key: one registration at a time
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "[::1]", "localhost"})
_NETLOC = re.compile(r"(\[[^\]]*\]|[^:\[\]@]*)(?::([0-9]{1,5}))?")
_UNSAFE = re.compile(r"[^\x21-\x7e]|[*#]")  # whitespace, controls, non-ASCII, wildcards
# Characters a name could use to hide or reorder what the consent page shows after it: controls
# and format characters (bidi overrides and isolates, zero-width), line and paragraph breaks.
_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})


@dataclass(frozen=True)
class Client:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    registration: str  # OPERATOR, DYNAMIC or METADATA

    @property
    def self_reported(self) -> bool:
        """Its name and redirect URIs are its own claim, not the operator's."""
        return self.registration != OPERATOR

    @property
    def metadata_host(self) -> str | None:
        """The host that published its metadata document (CIMD §10.6: shown on consent)."""
        return urlsplit(self.client_id).hostname if self.registration == METADATA else None

    def accepts(self, redirect_uri: str) -> bool:
        return any(redirect_matches(r, redirect_uri) for r in self.redirect_uris)


class Unacceptable(Exception):
    """Client metadata refused, with the RFC 7591 §3.2.2 error code."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _loopback(uri: str) -> tuple[str, str, str] | None:
    """(host, path, query) of a loopback http redirect URI with a valid port or none."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    found = _NETLOC.fullmatch(parts.netloc)
    if parts.scheme != "http" or "#" in uri or found is None:
        return None
    host, port = found.group(1), found.group(2)
    if host not in LOOPBACK_HOSTS or (port is not None and int(port) > 65535):
        return None
    return host, parts.path, parts.query


def loopback_redirect(uri: str) -> bool:
    """`uri` returns to a program on the owner's own computer (any of them could listen)."""
    return _loopback(uri) is not None


def redirect_matches(registered: str, presented: str) -> bool:
    """Exact, except that a loopback redirect URI may name any port (RFC 8252 §7.3): native
    clients listen on whatever port the system gives them."""
    if registered == presented:
        return True
    ours = _loopback(registered)
    return ours is not None and ours == _loopback(presented)


def valid_redirect_uri(uri: object) -> bool:
    """https with a plain ASCII host, or http to a loopback host (MCP authorization spec, RFC
    8252 §7.3); no fragment (RFC 6749 §3.1.2), wildcard, credentials, whitespace or non-ASCII
    character (the consent page shows the host: no homograph)."""
    if not isinstance(uri, str) or len(uri) > REDIRECT_URI_MAX or _UNSAFE.search(uri):
        return False
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    if parts.scheme == "https":
        return plain_host(parts.hostname)
    return _loopback(uri) is not None


def check_metadata(meta: Mapping[str, Any], *, name_required: bool) -> tuple[str, tuple[str, ...]]:
    """The name and redirect URIs of client metadata (RFC 7591 §2) a public client may hold:
    no secret and no shared-secret method (CIMD §4.1, §5: absent means `none`), the code flow
    (CIMD §6), valid redirect URIs. `Unacceptable` otherwise."""
    uris = meta.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= REDIRECT_URIS_MAX
        or not all(valid_redirect_uri(u) for u in uris)
    ):
        raise Unacceptable(
            "invalid_redirect_uri",
            "Redirect URIs must be https, or http to 127.0.0.1, [::1] or localhost, "
            "without fragments or wildcards.",
        )
    name = meta.get("client_name", None if name_required else UNNAMED)
    if isinstance(name, str):
        name = "".join(c for c in name if unicodedata.category(c) not in _INVISIBLE).strip()
    if not isinstance(name, str) or not name:
        raise Unacceptable("invalid_client_metadata", "A client_name is required.")
    if "client_secret" in meta or "client_secret_expires_at" in meta:
        raise Unacceptable("invalid_client_metadata", "Clients here hold no secret.")
    methods: list[object] = []
    if "token_endpoint_auth_method" in meta:
        methods.append(meta["token_endpoint_auth_method"])
    plural = meta.get("token_endpoint_auth_methods_supported", [])
    methods += plural if isinstance(plural, list) else [plural]
    shared_secret = any(not isinstance(m, str) or m.startswith("client_secret") for m in methods)
    if methods and ("none" not in methods or shared_secret):
        raise Unacceptable(
            "invalid_client_metadata", "Only public clients (token_endpoint_auth_method none)."
        )
    for field, needed in (("grant_types", "authorization_code"), ("response_types", "code")):
        offered = meta.get(field, [needed])
        if not isinstance(offered, list) or needed not in offered:
            raise Unacceptable("invalid_client_metadata", f"{field} must include {needed}.")
    return name[:NAME_MAX], tuple(uris)


def from_document(url: str, body: bytes) -> Client:
    """The client a fetched metadata document describes (CIMD §4), or `Unacceptable`."""
    try:
        document = json.loads(body)
    except ValueError:
        raise Unacceptable("invalid_client_metadata", "Not a JSON document.") from None
    if not isinstance(document, dict) or document.get("client_id") != url:
        raise Unacceptable("invalid_client_metadata", "Its client_id is not its URL.")
    name, uris = check_metadata(document, name_required=True)
    return Client(url, name, uris, METADATA)


def _client(row: RegisteredClient) -> Client:
    return Client(row.client_id, row.client_name, tuple(row.redirect_uris), row.registration)


def _cache_for(fetched: Fetched) -> timedelta:
    if fetched.max_age is None:
        return CACHE_DEFAULT
    return max(CACHE_MIN, min(CACHE_MAX, timedelta(seconds=min(fetched.max_age, 10**9))))


def resolve(
    session: Session, settings: Settings, fetch: Callable[[str], Fetched], client_id: object
) -> Client | None:
    """The client `client_id` names, or None (unknown, or its metadata cannot be trusted)."""
    if not isinstance(client_id, str) or not client_id:
        return None
    for listed in settings.mcp_oauth_clients:
        if listed.client_id == client_id:
            return Client(
                listed.client_id, listed.client_name, tuple(listed.redirect_uris), OPERATOR
            )
    row = session.scalar(select(RegisteredClient).where(RegisteredClient.client_id == client_id))
    if not client_id.startswith("https://"):
        return _client(row) if row is not None else None
    now = datetime.now(UTC)
    # Fresh, and never longer than this code would have cached it (a hand edit, a bad bundle).
    if row is not None and row.fresh_until is not None and now < row.fresh_until <= now + CACHE_MAX:
        return _client(row)
    if url_problem(client_id) is not None or _own_host(settings, client_id):
        return None
    try:
        fetched = fetch(client_id)
        found = from_document(client_id, fetched.body)
    except FetchFailed as exc:
        if exc.transient and row is not None:
            return _client(row)
        _forget(session, client_id)
        return None
    except Unacceptable:
        _forget(session, client_id)
        _end_connections(session, client_id)
        return None
    values = {
        "client_name": found.client_name,
        "redirect_uris": list(found.redirect_uris),
        "fresh_until": datetime.now(UTC) + _cache_for(fetched),
    }
    session.execute(
        insert(RegisteredClient)
        .values(client_id=client_id, registration=METADATA, **values)
        .on_conflict_do_update(index_elements=[RegisteredClient.client_id], set_=values)
    )
    if row is not None:
        session.expire(row)
    return found


def _own_host(settings: Settings, url: str) -> bool:
    """A client ID on this installation's own hosts (CIMD §10.1: never this server itself),
    however spelt: `host.` is the same name as `host`."""

    def host(of: str) -> str:
        return (urlsplit(of).hostname or "").rstrip(".")

    return host(url) in {host(settings.oauth_issuer), host(settings.mcp_resource_url)}


def _forget(session: Session, client_id: str) -> None:
    session.execute(delete(RegisteredClient).where(RegisteredClient.client_id == client_id))


def _end_connections(session: Session, client_id: str) -> None:
    """CIMD §6.1: the document no longer vouches for this client, so its connections end now
    (grant and tokens revoked, an event on Sharing) rather than at the next refresh.

    Its unspent codes expire first. A token request that resolved the client while it was still
    valid spends its code with `UPDATE ... WHERE expires_at > now`: if that runs after this
    commits, it finds no code; if it got there first, this UPDATE waits on its row lock until
    the exchange commits, and the scan below (a later statement, read committed) then sees the
    connection it created and ends it. Either way no connection outlives the client."""
    from farmcore.core.oauth.service import end_connection  # service imports this module

    session.execute(
        update(OAuthCode)
        .where(OAuthCode.client_id == client_id, OAuthCode.used_at.is_(None))
        .values(expires_at=func.now())
    )
    grants = session.scalars(
        select(AccessToken.grant_id).where(
            AccessToken.kind == "oauth", AccessToken.client_id == client_id
        )
    ).all()
    for grant_id in grants:
        end_connection(session, grant_id, "its client's metadata document is no longer valid")


def _peer_slot(peer: str) -> int:
    """A coarse bucket for the address, an IPv6 one by its /64 (the address is never stored)."""
    return int.from_bytes(hashlib.sha256(peer_bucket(peer).encode()).digest()[:4]) % 1024


def register(
    session: Session, peer: str, name: str, redirect_uris: tuple[str, ...]
) -> Client | None:
    """A new public client (RFC 7591 §3.2.1), or None when a registration limit is reached.
    Registrations that never connected are forgotten after `UNUSED_TTL`."""
    session.execute(select(func.pg_advisory_xact_lock(_REGISTER_LOCK)))
    now = datetime.now(UTC)
    connected = select(AccessToken.client_id).where(AccessToken.client_id.is_not(None))
    dynamic = RegisteredClient.registration == DYNAMIC
    unused = RegisteredClient.client_id.not_in(connected)
    session.execute(
        delete(RegisteredClient).where(
            dynamic, unused, RegisteredClient.created_at < now - UNUSED_TTL
        )
    )
    slot = _peer_slot(peer)
    recent = session.scalar(
        select(func.count())
        .select_from(RegisteredClient)
        .where(
            dynamic,
            RegisteredClient.peer_slot == slot,
            RegisteredClient.created_at > now - timedelta(hours=1),
        )
    )
    waiting = session.scalar(
        select(func.count()).select_from(RegisteredClient).where(dynamic, unused)
    )
    if recent >= REGISTRATIONS_PER_PEER or waiting >= UNUSED_REGISTRATIONS:
        return None
    row = RegisteredClient(
        client_id=DYNAMIC_PREFIX + secrets.token_urlsafe(16),
        registration=DYNAMIC,
        client_name=name,
        redirect_uris=list(redirect_uris),
        peer_slot=slot,
    )
    session.add(row)
    session.flush()
    return _client(row)
