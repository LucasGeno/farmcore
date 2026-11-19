"""Fetch a client ID metadata document without letting the URL reach inside this network.

The client ID URL comes from whoever starts an authorization request, so the fetch is a
server-side request forgery surface (draft-ietf-oauth-client-id-metadata-document §10.7):

- the URL must be a client identifier URL (§3): https, a path other than `/`, no credentials,
  fragment, query or dot segments, printable ASCII only (so no homograph host on the consent
  page), a real port;
- the name is resolved once and every address it resolves to must be public (RFC 6890 special
  use addresses are refused, as are IPv4-mapped and NAT64 ones). The connection then goes to
  that checked address, with the name only as Host and TLS server name, so a second answer
  (DNS rebinding) is never asked for. No proxy from the environment, which would resolve again;
- redirects are never followed (§6), only a 200 with a JSON type is read, at most `MAX_BYTES`
  (§10.11), and the whole fetch has one deadline: connection, TLS, headers and body. httpx's
  timeouts bound each read, so a server sending a byte at a time would pass them; a watchdog
  shuts the socket down at the deadline instead;
- no cookie or credential is sent (§11);
- anyone may name a URL before signing in, so fetches are bounded: a few at once (one more is
  refused at once, not queued, so slow servers never take the threads every request shares),
  `FETCHES_PER_PEER` a minute per address, and a URL that failed is not fetched again for
  `FAILED_FOR`. These live in memory, per process.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from farmcore import __version__

MAX_BYTES = 5 * 1024  # CIMD §10.11: "The recommended maximum size to read is 5 kilobytes."
DEADLINE = 5.0  # assumed: seconds for the whole fetch, while an owner waits on the consent page
CLIENT_ID_MAX = 255  # assumed: a client ID is stored where the operator's client IDs are
FETCHES_AT_ONCE = 4  # assumed: well under the 40 threads sync routes and to_thread share
FETCHES_PER_PEER = 10  # assumed: a client's document is cached, so a real one needs one
PEER_WINDOW = 60.0  # seconds that `FETCHES_PER_PEER` counts over
FAILED_FOR = 60.0  # assumed: seconds a failed URL is answered from memory
_REMEMBERED = 4096  # assumed: URLs and peers held in memory at most
# Special-use ranges `is_global` misses (multicast) or deems public though they reach a v4
# address of the operator's choosing through a translator (NAT64, RFC 6052 and RFC 8215), or
# embed one (IPv4-compatible ::/96, RFC 4291 §2.5.5.1; IPv4-translated, RFC 2765 §2.1).
_TRANSLATED = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "::/96", "::ffff:0:0:0/96")
)
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")
_HOST = re.compile(r"[a-z0-9.-]+")
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


def plain_host(hostname: str | None) -> bool:
    """An ASCII DNS name or an IP literal (`urlsplit().hostname`): what the consent page shows
    is what the browser and this server connect to, never a homograph or an encoded name."""
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return _HOST.fullmatch(hostname) is not None


def url_problem(url: str) -> str | None:
    """Why `url` is not a client identifier URL (CIMD §3), or None."""
    if len(url) > CLIENT_ID_MAX:
        return "too long"
    if not _PRINTABLE_ASCII.fullmatch(url):
        return "whitespace, control or non-ASCII characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed"
    if parts.scheme != "https" or not plain_host(parts.hostname):
        return "not an https URL with a plain host name"
    if port == 0:
        return "port 0"
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


def peer_bucket(address: str) -> str:
    """The unit a per-address limit counts: an IPv4 address, or an IPv6 host's /64 (one host
    can take any address in it, RFC 4291 §2.5.1)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (the system resolver)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class _Watchdog:
    """Shuts the fetch's connection down at the deadline, whatever it waits for (the TLS
    handshake, the headers, the body): the blocked read then fails at once. It shuts a
    duplicate of the socket it holds itself, closed only after the fetch, so it can never
    reach a descriptor the system has since given to someone else."""

    def __init__(self, seconds: float) -> None:
        self.fired = False
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._done = False
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def trace(self, event: str, info: dict) -> None:
        """httpx's `trace` request extension: told of each connection as it is made."""
        if event != "connection.connect_tcp.complete":
            return
        raw = info["return_value"].get_extra_info("socket")
        with self._lock:
            if raw is None or self._done or self._socket is not None:
                return
            self._socket = socket.socket(fileno=os.dup(raw.fileno()))
            if self.fired:
                self._shut()

    def _fire(self) -> None:
        with self._lock:
            if not self._done:
                self.fired = True
                self._shut()

    def _shut(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.shutdown(socket.SHUT_RDWR)

    def stop(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._done = True
            if self._socket is not None:
                self._socket.close()


class MetadataFetcher:
    """`fetcher(url, peer=...)`: the document's bytes, or `FetchFailed`. `resolve`, `transport`
    and `clock` are the outside world (tests fake them; nothing else here touches the network);
    `allow` says which addresses may be connected to (`public`; a test's local server aside).
    `peer` is the address that asked, for the per-address limit."""

    def __init__(
        self,
        *,
        resolve: Callable[[str, int], list[str]] = resolve_host,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        deadline: float = DEADLINE,
        allow: Callable[[str], bool] = public,
        concurrency: int = FETCHES_AT_ONCE,
    ) -> None:
        self._resolve = resolve
        self._transport = transport
        self._clock = clock
        self._deadline = deadline
        self._allow = allow
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._failed: dict[str, tuple[float, FetchFailed]] = {}  # url: (until, why)
        self._asked: dict[str, deque[float]] = {}  # peer bucket: when it had fetches made

    def __call__(self, url: str, peer: str = "") -> Fetched:
        problem = url_problem(url)
        if problem is not None:
            raise FetchFailed(f"not a client ID URL: {problem}", transient=False)
        self._admit(url, peer_bucket(peer))
        if not self._slots.acquire(blocking=False):
            raise FetchFailed("too many fetches at once", transient=True)
        try:
            return self._fetch(url)
        except FetchFailed as exc:
            with self._lock:
                _bounded(self._failed)[url] = (self._clock() + FAILED_FOR, exc)
            raise
        finally:
            self._slots.release()

    def _admit(self, url: str, bucket: str) -> None:
        """Refuse a URL that failed moments ago, or an address past its fetches a minute."""
        now = self._clock()
        with self._lock:
            failed = self._failed.get(url)
            if failed is not None and failed[0] > now:
                raise FetchFailed(f"failed moments ago: {failed[1]}", transient=failed[1].transient)
            self._failed.pop(url, None)
            asked = self._asked.pop(bucket, deque())
            while asked and asked[0] <= now - PEER_WINDOW:
                asked.popleft()
            _bounded(self._asked)[bucket] = asked  # most recent last, so pruned last
            if len(asked) >= FETCHES_PER_PEER:
                raise FetchFailed("too many fetches from this address", transient=True)
            asked.append(now)

    def _fetch(self, url: str) -> Fetched:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 443
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = self._resolve(host, port)
        if not addresses:
            raise FetchFailed("the host does not resolve", transient=True)
        if not all(self._allow(a) for a in addresses):
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
        watchdog = _Watchdog(self._deadline)
        try:
            with httpx.Client(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                timeout=self._deadline,
            ) as client:
                request = client.build_request(
                    "GET",
                    target,
                    headers=headers,
                    extensions={"sni_hostname": host, "trace": watchdog.trace},
                )
                try:
                    response = client.send(request, stream=True)
                    try:
                        return self._read(response, started)
                    finally:
                        response.close()
                finally:
                    watchdog.stop()
        except httpx.HTTPError as exc:
            if watchdog.fired:
                raise FetchFailed("the fetch took too long", transient=True) from None
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


def _bounded[V](remembered: dict[str, V]) -> dict[str, V]:
    """`remembered`, its oldest entries dropped to make room for one more."""
    while len(remembered) >= _REMEMBERED:
        remembered.pop(next(iter(remembered)))
    return remembered
