"""Fetch a client ID metadata document without letting the URL reach inside this network.

The client ID URL comes from whoever starts an authorization request, so the fetch is a
server-side request forgery surface (draft-ietf-oauth-client-id-metadata-document §10.7):

- the URL must be a client identifier URL (§3): https, a path other than `/`, no credentials,
  fragment, query or dot segments;
- the name is resolved once and every address it resolves to must be public (RFC 6890 special
  use addresses are refused, as are IPv4-mapped and NAT64 ones). The connection then goes to
  that checked address, with the name only as Host and TLS server name, so a second answer
  (DNS rebinding) is never asked for. No proxy from the environment, which would resolve again;
- redirects are never followed (§6), only a 200 with a JSON type is read, at most `MAX_BYTES`
  (§10.11), and the whole fetch has one deadline, also against a body dribbled slowly;
- no cookie or credential is sent (§11).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from farmcore import __version__

MAX_BYTES = 5 * 1024  # CIMD §10.11: "The recommended maximum size to read is 5 kilobytes."
DEADLINE = 5.0  # assumed: seconds for the whole fetch, while an owner waits on the consent page
CLIENT_ID_MAX = 255  # assumed: a client ID is stored where the operator's client IDs are
# Special-use ranges `is_global` misses (multicast) or deems public though they reach a v4
# address of the operator's choosing through a translator (NAT64, RFC 6052 and RFC 8215).
_TRANSLATED = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)\"?\s*(?:,|$)", re.IGNORECASE)


class FetchFailed(Exception):
    """No document. `transient`: the client's server may answer later (a timeout, a 5xx), so a
    document fetched before may still be trusted (CIMD §6.1); otherwise the answer is final."""

    def __init__(self, reason: str, *, transient: bool) -> None:
        super().__init__(reason)
        self.transient = transient


@dataclass(frozen=True)
class Fetched:
    body: bytes
    max_age: int | None  # Cache-Control max-age, if the server sent one


def url_problem(url: str) -> str | None:
    """Why `url` is not a client identifier URL (CIMD §3), or None."""
    if len(url) > CLIENT_ID_MAX:
        return "too long"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "malformed"
    if parts.scheme != "https" or not parts.hostname:
        return "not an https URL"
    if "@" in parts.netloc:
        return "credentials in the URL"
    if "#" in url or parts.query or "?" in url:
        return "a fragment or query"
    segments = unquote(parts.path).split("/")
    if parts.path in ("", "/") or any(s in (".", "..") for s in segments):
        return "no path, or dot segments"
    return None


def public(address: str) -> bool:
    """An address anyone on the internet could reach: never this host or its networks."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return public(str(ip.ipv4_mapped))
        if any(ip in net for net in _TRANSLATED):
            return False
    return ip.is_global and not ip.is_multicast


def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (the system resolver)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class MetadataFetcher:
    """`fetcher(url)`: the document's bytes, or `FetchFailed`. `resolve`, `transport` and `clock`
    are the outside world (tests fake them; nothing else here touches the network)."""

    def __init__(
        self,
        *,
        resolve: Callable[[str, int], list[str]] = resolve_host,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        deadline: float = DEADLINE,
    ) -> None:
        self._resolve = resolve
        self._transport = transport
        self._clock = clock
        self._deadline = deadline

    def __call__(self, url: str) -> Fetched:
        problem = url_problem(url)
        if problem is not None:
            raise FetchFailed(f"not a client ID URL: {problem}", transient=False)
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 443
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = self._resolve(host, port)
        if not addresses:
            raise FetchFailed("the host does not resolve", transient=True)
        if not all(public(a) for a in addresses):
            raise FetchFailed("the host resolves to a non-public address", transient=False)
        pinned = addresses[0]
        literal = f"[{pinned}]" if ":" in pinned else pinned
        target = urlunsplit(("https", f"{literal}:{port}", parts.path, "", ""))
        headers = {
            "Host": parts.netloc,
            "Accept": "application/json",
            "Accept-Encoding": "identity",  # the size cap counts the bytes as they arrive
            "User-Agent": f"FarmCore/{__version__} (OAuth client metadata)",
        }
        started = self._clock()
        try:
            with httpx.Client(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                timeout=self._deadline,
            ) as client:
                request = client.build_request(
                    "GET", target, headers=headers, extensions={"sni_hostname": host}
                )
                response = client.send(request, stream=True)
                try:
                    return self._read(response, started)
                finally:
                    response.close()
        except httpx.HTTPError as exc:
            raise FetchFailed(f"the fetch failed: {type(exc).__name__}", transient=True) from None

    def _read(self, response: httpx.Response, started: float) -> Fetched:
        if response.status_code != 200:
            transient = response.status_code >= 500 or response.status_code in (408, 429)
            raise FetchFailed(f"HTTP {response.status_code}", transient=transient)
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json" and not media.endswith("+json"):
            raise FetchFailed("not a JSON document", transient=False)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise FetchFailed("a compressed document", transient=False)
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            raise FetchFailed("the document is too large", transient=False)
        body = bytearray()
        for chunk in response.iter_bytes():  # identity only (checked above): raw bytes
            if self._clock() - started > self._deadline:
                raise FetchFailed("the fetch took too long", transient=True)
            body += chunk
            if len(body) > MAX_BYTES:
                raise FetchFailed("the document is too large", transient=False)
        found = _MAX_AGE.search(response.headers.get("cache-control", ""))
        return Fetched(bytes(body), int(found.group(1)) if found else None)
