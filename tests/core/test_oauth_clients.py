"""MCP clients register here, by metadata document or by registration, and the owner consents.

Failure model (written before the code; one owning test each):
- SSRF through the metadata fetch: a client ID URL that is not https or carries credentials, a
  fragment, a query, dot segments or only the root path (CIMD §3); a host resolving to a
  loopback, private, link-local, shared, multicast, unspecified, IPv4-mapped or NAT64 address,
  or to a mix of public and internal ones (CIMD §10.7, RFC 6890); DNS rebinding between the
  check and the connection (the fetch connects to the address it checked, resolving once); a
  redirect (CIMD §6: never followed); a non-200 answer, a body over 5 KB with or without a
  Content-Length (CIMD §10.11), a body dribbled past the deadline, a type other than JSON,
  a non-ASCII host (a homograph), whitespace or port 0 in the URL (fetch table);
- a slow server holding threads: headers dribbled one byte at a time under every per-read
  timeout, or more fetches at once than the cap (real TLS test); the same URL failing again and
  again, or one address (an IPv6 host by its /64) asking for fetch after fetch (repeat test);
- spoofed metadata: a document whose `client_id` is not the URL it came from byte for byte (no
  normalisation, CIMD §3 and §4), that holds a secret or a shared-secret method (§4.1), lacks a
  name or redirect URIs, or offers a wildcard, fragment, custom-scheme, plain-http
  non-loopback or non-ASCII-host redirect URI, or no code flow (§6), or a name of only
  invisible or bidi control characters (document table);
- a redirect URI matched loosely: a loopback one may change only its port (RFC 8252 §7.3,
  RFC 9700 §4.1.3); another host spelling, path, query or scheme, or a port change off loopback,
  is refused (redirect table);
- cache poisoning: a failed or invalid document is never cached (CIMD §6.2); a cached one is
  used only while fresh, within bounds whatever its Cache-Control or its stored row says; one
  that turns invalid leaves the cache, a transient failure keeps the last good one (§6.1); a
  client ID on this server's own host, trailing dot or not, is never fetched (§10.1) (cache
  test);
- open-registration abuse: a public client gets no secret, a shared-secret method or a bad
  redirect URI is refused (RFC 7591 §3.2.2), a software statement is not trusted (§3.1.1: its
  claims never override), registrations are capped per address (an IPv6 host by its /64) and
  while unused, an issued ID
  never looks like a metadata URL (CIMD §9.1), a replayed registration is just another
  unapproved client (registration test);
- consent phishing: a registered or metadata client's name is shown as self-reported with the
  metadata host and the redirect host, a loopback redirect warns (MCP authorization security
  considerations), scopes are said in plain words, no logo is shown; unreachable metadata is an
  error page that redirects nowhere (CIMD §6.1); a denial creates no code and no grant (journey).
- a downgrade that would orphan connected agents: refused while `oauth_client` has rows
  (test_schema round trip).
- a connected agent outliving its client: once the client's document turns invalid or names
  another client, every connection of that client ends on every farm, and only that client's
  (CIMD §6.1) (turned-invalid test); a code approved before then, spent after, connects nothing
  (race test).
Not committed: real-client runs (recorded by hand in the real-clients finding); TLS to a live
host (smoke-tested by hand).
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from farmcore.core.access import TokenRejected, authenticate_token, ensure_owner_grant
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import DataGrant, Event, Farm
from farmcore.core.models.identity import (
    FarmMembership,
    LocalIdentity,
    OAuthCode,
    OAuthToken,
    RegisteredClient,
)
from farmcore.core.oauth import clients, fetch, service
from farmcore.main import create_app

PASSWORD = "synthetic-oauth-owner-password"  # gauntlet:allow-secret synthetic credential
ISSUER = "https://testserver"
RESOURCE = "https://farm.example/mcp"
CLIENT_URL = "https://agent.example/oauth/client.json"
PUBLIC_IP = "93.184.215.14"
LOOPBACK_REDIRECT = "http://127.0.0.1/callback"
# RFC 7636 Appendix B: the worked example's verifier and its S256 challenge.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _document(**over) -> dict:
    """A client ID metadata document as an MCP client publishes it (CIMD §4, MCP spec)."""
    return {
        "client_id": CLIENT_URL,
        "client_name": "Claude",
        "redirect_uris": [LOOPBACK_REDIRECT, "https://agent.example/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **over,
    }


class World:
    """The internet as the fetch sees it: DNS answers and one HTTP handler, no real network."""

    def __init__(self, document: dict | None = None) -> None:
        self.dns: dict[str, list[list[str]]] = {"agent.example": [[PUBLIC_IP], ["127.0.0.1"]]}
        self.requests: list[httpx.Request] = []
        self.lookups: list[str] = []
        self.answer = lambda request: httpx.Response(
            200, json=document or _document(), headers={"Cache-Control": "max-age=600"}
        )
        self.now = 0.0

    def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        answers = self.dns.get(host, [[]])
        # A rebinding name answers differently the second time.
        return answers[min(len(self.lookups) - 1, len(answers) - 1)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def clock(self) -> float:
        return self.now

    def fetcher(self) -> fetch.MetadataFetcher:
        return fetch.MetadataFetcher(
            resolve=self.resolve, transport=httpx.MockTransport(self.handler), clock=self.clock
        )


def test_a_metadata_fetch_connects_only_to_the_public_address_it_checked() -> None:
    """CIMD §10.7 and DNS rebinding: the name is resolved once and the connection goes to that
    checked address, with the name only as Host and TLS server name; CIMD §11: no cookies."""
    world = World()

    fetched = world.fetcher()(CLIENT_URL)

    [sent] = world.requests
    assert world.lookups == ["agent.example"]  # the rebinding answer was never asked for
    assert sent.url.host == PUBLIC_IP and sent.url.path == "/oauth/client.json"
    assert sent.headers["host"] == "agent.example"
    assert sent.extensions["sni_hostname"] == "agent.example"
    assert sent.headers["accept"] == "application/json"
    assert "cookie" not in sent.headers and "authorization" not in sent.headers
    assert json.loads(fetched.body)["client_id"] == CLIENT_URL
    assert fetched.max_age == 600


def _slow(world: World, request: httpx.Request) -> httpx.Response:
    def dribble():
        for _ in range(10):
            world.now += 1.0  # each byte arrives a second after the last
            yield b" "

    return httpx.Response(200, headers={"Content-Type": "application/json"}, content=dribble())


def _unbounded(world: World, request: httpx.Request) -> httpx.Response:
    def stream():
        yield b'{"client_id": "' + b"a" * 4000
        yield b"a" * 4000 + b'"}'

    return httpx.Response(200, headers={"Content-Type": "application/json"}, content=stream())


def _unreachable(world: World, request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    ("name", "url", "dns", "answer", "transient"),
    [
        ("plain-http", "http://agent.example/c.json", None, None, False),
        ("credentials", "https://user:pw@agent.example/c.json", None, None, False),
        ("fragment", "https://agent.example/c.json#x", None, None, False),
        ("query", "https://agent.example/c.json?next=x", None, None, False),
        ("dot-segments", "https://agent.example/a/../c.json", None, None, False),
        ("encoded-dots", "https://agent.example/a/%2e%2e/c.json", None, None, False),
        ("root-path", "https://agent.example/", None, None, False),
        ("homograph-host", "https://\u0430gent.example/c.json", None, None, False),
        ("trailing-space", "https://agent.example/c.json ", None, None, False),
        ("tab-in-path", "https://agent.example/c\t.json", None, None, False),
        ("port-zero", "https://agent.example:0/c.json", None, None, False),
        ("loopback", CLIENT_URL, ["127.0.0.1"], None, False),
        ("private", CLIENT_URL, ["10.0.0.5"], None, False),
        ("link-local-metadata-service", CLIENT_URL, ["169.254.169.254"], None, False),
        ("shared-cgnat", CLIENT_URL, ["100.64.0.1"], None, False),
        ("unspecified", CLIENT_URL, ["0.0.0.0"], None, False),
        ("multicast", CLIENT_URL, ["224.0.0.1"], None, False),
        ("ipv6-unique-local", CLIENT_URL, ["fd00::1"], None, False),
        ("ipv4-mapped-loopback", CLIENT_URL, ["::ffff:127.0.0.1"], None, False),
        ("nat64-to-private", CLIENT_URL, ["64:ff9b::a00:1"], None, False),
        ("ipv4-compatible-loopback", CLIENT_URL, ["::7f00:1"], None, False),
        ("ipv4-translated-loopback", CLIENT_URL, ["::ffff:0:7f00:1"], None, False),
        ("public-and-private", CLIENT_URL, [PUBLIC_IP, "10.0.0.5"], None, False),
        ("ip-literal-loopback", "https://127.0.0.1/c.json", None, None, False),
        ("no-address", CLIENT_URL, [], None, True),
        (
            "redirect-inside",
            CLIENT_URL,
            None,
            lambda w, r: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}),
            False,
        ),
        ("not-found", CLIENT_URL, None, lambda w, r: httpx.Response(404), False),
        ("server-down", CLIENT_URL, None, lambda w, r: httpx.Response(503), True),
        (
            "html-not-json",
            CLIENT_URL,
            None,
            lambda w, r: httpx.Response(
                200, headers={"Content-Type": "text/html"}, content=json.dumps(_document())
            ),
            False,
        ),
        (
            "declared-too-big",
            CLIENT_URL,
            None,
            lambda w, r: httpx.Response(200, json={"client_id": CLIENT_URL, "pad": "a" * 6000}),
            False,
        ),
        ("streamed-too-big", CLIENT_URL, None, _unbounded, False),
        ("dribbled-past-deadline", CLIENT_URL, None, _slow, True),
        ("connect-fails", CLIENT_URL, None, _unreachable, True),
    ],
)
def test_a_metadata_fetch_that_could_reach_inside_or_hang_is_refused(
    name, url, dns, answer, transient
) -> None:
    """CIMD §3 (the URL; ASCII only, so no homograph host), §6 (no redirects, 200 only), §10.7
    (no special-use address, RFC 6890), §10.11 (5 KB); a body dribbled past the deadline (slow
    headers need a real socket: the TLS test); JSON only. Refused before any connection when
    the URL or an address is wrong; transient only when retrying may help."""
    world = World()
    if dns is not None:
        world.dns["agent.example"] = [dns]
    if answer is not None:
        world.answer = lambda request: answer(world, request)

    with pytest.raises(fetch.FetchFailed) as failed:
        world.fetcher()(url)

    assert failed.value.transient is transient
    if answer is None:
        assert world.requests == []  # nothing left this server


def _tls_contexts(host: str, folder: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server context with a fresh self-signed certificate for `host`, and a client context
    that trusts only it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (folder / "cert.pem").write_bytes(pem)
    (folder / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    return server, ssl.create_default_context(cadata=pem.decode())


def test_a_server_dribbling_its_headers_cannot_hold_a_fetch_or_every_thread(tmp_path) -> None:
    """Real TLS on a local socket (a mock transport cannot dribble): headers sent one line
    every 0.1 s pass every per-read timeout, yet the fetch ends at its deadline; meanwhile a
    fetch past the concurrency cap is refused at once, as transient, so slow servers cannot
    take the threads every request shares."""
    server_tls, client_tls = _tls_contexts("agent.example", tmp_path)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    asked, stop = threading.Event(), threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        with server_tls.wrap_socket(conn, server_side=True) as tls:
            request = b""
            while b"\r\n\r\n" not in request:
                request += tls.recv(1024)
            asked.set()
            tls.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.wait(0.1):
                try:
                    tls.sendall(b"X-Slow: 1\r\n")
                except OSError:
                    return

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    fetcher = fetch.MetadataFetcher(
        resolve=lambda host, port: ["127.0.0.1"],
        allow=lambda address: True,  # the one public-address check this test steps around
        transport=httpx.HTTPTransport(verify=client_tls),
        deadline=0.8,
        concurrency=1,
    )
    url = f"https://agent.example:{port}/oauth/client.json"
    outcome: list[BaseException | None] = []

    def first() -> None:
        try:
            fetcher(url)
            outcome.append(None)
        except fetch.FetchFailed as exc:
            outcome.append(exc)

    started = time.monotonic()
    slow = threading.Thread(target=first)
    slow.start()
    try:
        assert asked.wait(2.0)
        second = time.monotonic()
        with pytest.raises(fetch.FetchFailed) as busy:
            fetcher("https://other.example/oauth/client.json")
        waited = time.monotonic() - second
        slow.join(2.5)
        took = time.monotonic() - started
    finally:
        stop.set()
        server.join(1.0)
        listener.close()

    assert busy.value.transient and waited < 0.3  # refused without waiting its turn
    [failed] = outcome
    assert isinstance(failed, fetch.FetchFailed) and failed.transient
    assert took < 2.0  # deadline 0.8 s; the server would have dribbled on for ever


def test_a_client_id_url_cannot_make_this_server_fetch_again_and_again() -> None:
    """Anyone can name a client ID URL before signing in (CIMD §10.7), so each is a fetch
    someone else chose: a URL that failed is not fetched again for a minute, and one address
    (an IPv6 host by its /64, which it can rotate through) gets a bounded number a minute."""
    world = World()
    world.dns = {"agent.example": [[PUBLIC_IP]], "down.example": [[PUBLIC_IP]]}
    world.answer = lambda request: httpx.Response(
        503 if request.headers["host"] == "down.example" else 200, json=_document()
    )
    fetcher = world.fetcher()
    down = "https://down.example/oauth/client.json"

    for _ in range(2):
        with pytest.raises(fetch.FetchFailed) as failed:
            fetcher(down, peer="198.51.100.1")
        assert failed.value.transient
    assert len(world.requests) == 1  # the second failure came from memory
    world.now += 61
    with pytest.raises(fetch.FetchFailed):
        fetcher(down, peer="198.51.100.2")
    assert len(world.requests) == 2

    for n in range(fetch.FETCHES_PER_PEER):
        fetcher(f"https://agent.example/c{n}.json", peer=f"2001:db8:5:6::{n + 1:x}")
    with pytest.raises(fetch.FetchFailed) as limited:
        fetcher(CLIENT_URL, peer="2001:db8:5:6::ffff")
    assert limited.value.transient
    assert len(world.requests) == 2 + fetch.FETCHES_PER_PEER
    fetcher(CLIENT_URL, peer="2001:db8:5:7::1")  # another network is not held back
    world.now += 61
    fetcher(CLIENT_URL, peer="2001:db8:5:6::ffff")  # nor this one, a minute later


@pytest.mark.parametrize(
    ("name", "url", "document"),
    [
        ("not-an-object", CLIENT_URL, ["client_id"]),
        ("other-client-id", CLIENT_URL, _document(client_id="https://claude.ai/client.json")),
        ("trailing-slash", CLIENT_URL, _document(client_id=CLIENT_URL + "/")),
        (
            "explicit-port",
            CLIENT_URL,
            _document(client_id="https://agent.example:443/oauth/client.json"),
        ),
        ("host-case", CLIENT_URL, _document(client_id="https://AGENT.example/oauth/client.json")),
        ("client-secret", CLIENT_URL, _document(client_secret="s3cret")),
        ("secret-method", CLIENT_URL, _document(token_endpoint_auth_method="client_secret_basic")),
        (
            "secret-methods",
            CLIENT_URL,
            _document(token_endpoint_auth_methods_supported=["none", "client_secret_post"]),
        ),
        ("no-name", CLIENT_URL, {k: v for k, v in _document().items() if k != "client_name"}),
        ("no-redirects", CLIENT_URL, _document(redirect_uris=[])),
        ("wildcard-redirect", CLIENT_URL, _document(redirect_uris=["https://*.agent.example/cb"])),
        ("fragment-redirect", CLIENT_URL, _document(redirect_uris=["https://agent.example/cb#x"])),
        ("http-redirect", CLIENT_URL, _document(redirect_uris=["http://agent.example/cb"])),
        ("custom-scheme", CLIENT_URL, _document(redirect_uris=["cursor://anysphere/cb"])),
        ("userinfo-redirect", CLIENT_URL, _document(redirect_uris=["https://a@evil.example/cb"])),
        ("homograph-redirect", CLIENT_URL, _document(redirect_uris=["https://claude.\u0430i/cb"])),
        ("invisible-name", CLIENT_URL, _document(client_name="\u202e\u200b\u2066")),
        ("no-code-flow", CLIENT_URL, _document(response_types=["token"])),
    ],
)
def test_a_metadata_document_that_spoofs_or_overreaches_is_refused(name, url, document) -> None:
    """CIMD §4 (`client_id` equals the URL, compared as strings, §3: never normalised), §4.1 (no
    secret, no shared-secret method), §4.2 and the MCP spec (name and redirect URIs required,
    https or loopback, RFC 6749 §3.1.2 no fragment), §6 (a code flow to offer), §10.6 (the
    consent page's hosts are ASCII, and a name cannot reorder or hide what follows it)."""
    with pytest.raises(clients.Unacceptable):
        clients.from_document(url, json.dumps(document).encode())

    accepted = clients.from_document(
        CLIENT_URL, json.dumps(_document(client_name="Cl\u202eau\u200bde\n")).encode()
    )
    assert (accepted.client_name, accepted.self_reported) == ("Claude", True)


@pytest.mark.parametrize(
    ("registered", "presented", "matches"),
    [
        ("http://127.0.0.1/callback", "http://127.0.0.1:53682/callback", True),
        ("http://127.0.0.1:8080/callback", "http://127.0.0.1:9090/callback", True),
        ("http://[::1]/callback", "http://[::1]:41234/callback", True),
        ("http://localhost:3000/callback", "http://localhost:6274/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:53682/other", False),
        ("http://127.0.0.1/callback", "http://localhost:53682/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.2:53682/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:53682/callback?x=1", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:53682/callback#x", False),
        ("http://127.0.0.1/callback", "http://u@127.0.0.1:53682/callback", False),
        ("http://127.0.0.1/callback", "http://127.0.0.1:99999/callback", False),
        ("http://127.0.0.1/callback", "https://127.0.0.1:53682/callback", False),
        ("https://agent.example/callback", "https://agent.example:8443/callback", False),
        ("https://agent.example/callback", "https://agent.example/callback", True),
    ],
)
def test_a_loopback_redirect_may_change_only_its_port(registered, presented, matches) -> None:
    """RFC 8252 §7.3 (any port on a loopback IP redirect, chosen at request time) and RFC 9700
    §4.1.3 (localhost too); everything else is an exact match (OAuth 2.1 §4.1.1)."""
    assert clients.redirect_matches(registered, presented) is matches


def test_a_metadata_document_is_cached_only_when_valid_and_only_while_fresh(
    settings, session
) -> None:
    """CIMD §6.2: errors and invalid documents are never cached, Cache-Control is honoured
    within a minimum and a maximum (also when reading a row back); §6.1: a transient failure
    keeps the last good document, a document that turns invalid is dropped; §10.1: never a
    client ID on this server's own host."""
    calls: list[str] = []
    answers: list[object] = []

    def fetcher(url: str) -> fetch.Fetched:
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def good(max_age: int | None) -> fetch.Fetched:
        return fetch.Fetched(json.dumps(_document()).encode(), max_age)

    def look() -> clients.Client | None:
        return clients.resolve(session, settings, fetcher, CLIENT_URL)

    def age(seconds: float) -> None:
        session.execute(
            update(RegisteredClient).values(
                fresh_until=RegisteredClient.fresh_until - timedelta(seconds=seconds)
            )
        )

    answers += [fetch.FetchFailed("down", transient=True)]
    assert look() is None  # nothing cached to fall back on
    answers += [fetch.Fetched(json.dumps(_document(client_id="https://x.example/c")).encode(), 60)]
    assert look() is None
    assert session.scalar(select(func.count()).select_from(RegisteredClient)) == 0

    answers += [good(0)]  # "max-age=0" still caches for the minimum
    assert look().client_name == "Claude"
    assert look().client_name == "Claude"
    assert len(calls) == 3
    age(25 * 3600)
    answers += [good(10**9)]  # a decades-long max-age is cut to the maximum
    assert look().client_name == "Claude"
    age(25 * 3600)
    answers += [fetch.FetchFailed("timeout", transient=True)]
    assert look().client_name == "Claude"  # the last good document, still trusted
    age(25 * 3600)
    answers += [fetch.Fetched(json.dumps(_document(client_secret="x")).encode(), 60)]
    assert look() is None  # it turned invalid: no longer trusted, stale or not
    assert session.scalar(select(func.count()).select_from(RegisteredClient)) == 0
    answers += [good(None)]
    assert look().client_name == "Claude"
    session.execute(  # a row fresh for a year (a hand edit, a forged bundle) is not believed
        update(RegisteredClient).values(fresh_until=datetime.now(UTC) + timedelta(days=365))
    )
    answers += [good(None)]
    assert look().client_name == "Claude"
    age(25 * 3600)
    answers += [fetch.FetchFailed("gone", transient=False)]
    assert look() is None
    assert session.scalar(select(func.count()).select_from(RegisteredClient)) == 0
    assert len(calls) == 9
    issuer_host = urlsplit(settings.oauth_issuer).hostname
    for own in (f"https://{issuer_host}/c.json", f"https://{issuer_host}./c.json"):
        assert clients.resolve(session, settings, fetcher, own) is None
    assert len(calls) == 9  # this server never fetches from itself


# --- over HTTP: registration and consent ---------------------------------------------------


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def app(settings, session_factory, session, world):
    farm = Farm(name="OAuth farm")
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    owner = LocalIdentity(login="oauth-owner", password_hash=hash_password(PASSWORD))
    session.add(owner)
    session.flush()
    session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
    session.commit()
    oauth = settings.model_copy(
        update={
            "public_base_url": ISSUER,
            "mcp_http_enabled": True,
            "mcp_oauth_enabled": True,
            "mcp_http_public_url": RESOURCE,
            "mcp_oauth_clients": [],
        }
    )
    app = create_app(oauth, session_factory=session_factory)
    app.state.oauth_fetch = world.fetcher()
    app.state.farm_id = farm.id
    return app


@pytest.fixture
def agent(app):
    with TestClient(app, base_url=ISSUER) as client:
        yield client


def _register(agent, peer: str = "203.0.113.7", **body):
    with TestClient(agent.app, base_url=ISSUER, client=(peer, 50000)) as other:
        return other.post(
            "/oauth/register",
            json={
                "client_name": "Codex",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "token_endpoint_auth_method": "none",
                **body,
            },
        )


def test_open_registration_issues_a_public_client_within_limits(agent, monkeypatch) -> None:
    """RFC 7591 §3.2.1 (201, `client_id`, the registered metadata, no secret for `none`),
    §3.2.2 (`invalid_redirect_uri`, `invalid_client_metadata`), §3.1.1 (a software statement
    may be ignored: its claims are not trusted), §3 (rate limits), CIMD §9.1 (issued IDs never
    start with https://). Discovery: RFC 8414 `registration_endpoint`, CIMD §8."""
    server = agent.get("/.well-known/oauth-authorization-server").json()
    statement = "eyJhbGciOiJub25lIn0.eyJjbGllbnRfbmFtZSI6IkZhcm1Db3JlIn0."
    registered = _register(agent, software_statement=statement)
    again = _register(agent)
    secret = _register(agent, token_endpoint_auth_method="client_secret_basic")
    remote_http = _register(agent, redirect_uris=["http://agent.example/cb"])
    implicit = _register(agent, response_types=["token"])
    monkeypatch.setattr(clients, "REGISTRATIONS_PER_PEER", 2)
    monkeypatch.setattr(clients, "UNUSED_REGISTRATIONS", 5)
    peer_capped = _register(agent)
    # One IPv6 host can take any address in its /64: it is one peer.
    rotating = [_register(agent, peer=f"2001:db8:7:9::{n}") for n in (1, 2, 3)]
    elsewhere = _register(agent, peer="198.51.100.9")
    all_capped = _register(agent, peer="192.0.2.44")

    assert server["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert server["client_id_metadata_document_supported"] is True
    assert registered.status_code == 201
    body = registered.json()
    assert not body["client_id"].startswith("https://")
    assert body["client_id"] != again.json()["client_id"]  # a replay is just another client
    assert "client_secret" not in body and "software_statement" not in body
    assert body["client_name"] == "Codex"  # the statement's "FarmCore" is not trusted
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == ["http://127.0.0.1/callback"]
    assert (secret.status_code, secret.json()["error"]) == (400, "invalid_client_metadata")
    assert (remote_http.status_code, remote_http.json()["error"]) == (400, "invalid_redirect_uri")
    assert (implicit.status_code, implicit.json()["error"]) == (400, "invalid_client_metadata")
    assert peer_capped.status_code == 429
    assert [r.status_code for r in rotating] == [201, 201, 429]
    assert elsewhere.status_code == 201
    assert all_capped.status_code == 429
    page = agent.get(
        "/oauth/authorize",
        params=_request(client_id=body["client_id"], redirect_uri="http://127.0.0.1:5173/callback"),
        follow_redirects=False,
    )
    assert page.status_code == 303 and page.headers["location"].startswith("/login?")


def _request(**over: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": CLIENT_URL,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "s1",
        "scope": "fields:read drafts:propose",
        "resource": RESOURCE,
        **over,
    }


def _signed_in(app) -> TestClient:
    browser = TestClient(app, base_url=ISSUER)
    page = browser.get("/login")
    nonce = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    signed = browser.post(
        "/login",
        data={"login": "oauth-owner", "password": PASSWORD, "csrf_token": nonce},
        headers={"Origin": ISSUER},
        follow_redirects=False,
    )
    assert signed.status_code == 303
    return browser


def _answer(owner, app, decision: str, farm: int | None = None, **over: str) -> dict[str, str]:
    answered = owner.post(
        "/oauth/authorize",
        data={
            **_request(**over),
            "farm": str(farm or app.state.farm_id),
            "name": "Claude on my laptop",
            "allow": ["fields:read", "drafts:propose"],
            "decision": decision,
            "csrf_token": owner.cookies.get("farmcore_csrf", ""),
        },
        headers={"Origin": ISSUER},
        follow_redirects=False,
    )
    assert answered.status_code == 303, answered.text
    location = answered.headers["location"]
    assert location.startswith("http://127.0.0.1:53682/callback?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def test_an_owner_sees_who_is_asking_and_a_denial_grants_nothing(app, agent, world, session):
    """CIMD §10.6 and the MCP spec's localhost risk (the client ID's host and the redirect
    host shown, a loopback redirect warned about), RFC 7591 §5 and CIMD §10.10 (name and logo
    are self-asserted: the name is marked so, no logo is shown), CIMD §6.1 (unreachable
    metadata: an error page, no redirect), RFC 6749 §4.1.2.1 (`access_denied`); approval then
    works through the token endpoint with a loopback port the client chose (RFC 8252 §7.3)."""
    with _signed_in(app) as owner:
        page = owner.get("/oauth/authorize", params=_request())
        denied = _answer(owner, app, "deny")
        grants_after_denial = session.scalar(
            select(func.count()).select_from(DataGrant).where(DataGrant.grantee != "owner")
        )
        codes_after_denial = session.scalar(select(func.count()).select_from(OAuthCode))
        approved = _answer(owner, app, "approve")
        unreachable = owner.get(
            "/oauth/authorize",
            params=_request(client_id="https://gone.example/client.json"),
            follow_redirects=False,
        )
    issued = agent.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": approved["code"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "client_id": CLIENT_URL,
            "code_verifier": VERIFIER,
        },
    )

    assert page.status_code == 200
    text = page.text
    assert "self-reported" in text
    assert "agent.example" in text and "127.0.0.1" in text
    assert "this computer" in text  # the loopback warning's key phrase
    assert "See your fields" in text and "drafts you confirm" in text
    assert "<img" not in text
    assert (denied["error"], denied["iss"]) == ("access_denied", ISSUER)
    assert (grants_after_denial, codes_after_denial) == (0, 0)
    assert unreachable.status_code == 400 and "location" not in unreachable.headers
    assert issued.status_code == 200, issued.text
    assert sorted(issued.json()["scope"].split()) == ["drafts:propose", "fields:read"]
    assert len(world.requests) == 1  # one fetch served the page, both answers and the token


OTHER_URL = "https://agent.example/oauth/other.json"


def _asked_for(request: httpx.Request) -> str:
    """The client ID URL a fetch asked for (the fetch connects to the checked IP, by Host)."""
    return f"https://{request.headers['host']}{request.url.path}"


def _connect(app, agent, client_id: str = CLIENT_URL, farm: int | None = None) -> dict:
    with _signed_in(app) as owner:
        approved = _answer(owner, app, "approve", farm=farm, client_id=client_id)
    issued = agent.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": approved["code"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert issued.status_code == 200, issued.text
    return issued.json()


def _turn_invalid(world: World, session) -> None:
    """CLIENT_URL's host now serves a document naming another client; its cache row is stale."""
    world.answer = lambda request: httpx.Response(
        200,
        json=_document(client_id="https://elsewhere.example/client.json")
        if _asked_for(request) == CLIENT_URL
        else _document(client_id=_asked_for(request)),
    )
    session.execute(
        update(RegisteredClient)
        .where(RegisteredClient.client_id == CLIENT_URL)
        .values(fresh_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.commit()


def test_a_client_whose_document_turns_invalid_loses_its_connections(app, agent, world, session):
    """CIMD §6.1 SHOULD: when the document at a client ID URL no longer names that client, every
    connection of that client ends at once, on every farm, with an event on Sharing, and its
    access token is refused; another client's connection is untouched."""
    world.dns["agent.example"] = [[PUBLIC_IP]]
    world.answer = lambda request: httpx.Response(
        200, json=_document(client_id=_asked_for(request))
    )
    second = Farm(name="Second farm")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    owner_id = session.scalar(select(LocalIdentity.id).where(LocalIdentity.login == "oauth-owner"))
    session.add(FarmMembership(identity_id=owner_id, farm_id=second.id, role="owner"))
    session.commit()
    first = _connect(app, agent)
    _connect(app, agent, farm=second.id)
    other = _connect(app, agent, client_id=OTHER_URL)
    _turn_invalid(world, session)

    refreshed = agent.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": CLIENT_URL,
        },
    )

    assert refreshed.status_code in (400, 401)
    session.expire_all()
    grants = session.scalars(select(DataGrant).where(DataGrant.grantee != "owner")).all()
    ended = {g.farm_id for g in grants if g.revoked_at is not None}
    assert ended == {app.state.farm_id, second.id}  # every farm this client reached
    assert [g.purpose for g in grants if g.revoked_at is None] == [
        f"MCP agent over OAuth ({OTHER_URL})"
    ]
    events = session.scalars(select(Event).where(Event.kind == "share.revoked")).all()
    assert {e.farm_id for e in events} == {app.state.farm_id, second.id}
    with pytest.raises(TokenRejected):  # dies now, not within the hour
        authenticate_token(session, first["access_token"], audience=RESOURCE)
    assert authenticate_token(session, other["access_token"], audience=RESOURCE) is not None


def test_a_code_approved_before_the_document_turned_invalid_cannot_connect(
    app, agent, world, session, monkeypatch
):
    """The race: the token endpoint resolves the client while it is still valid, another request
    ends that client before the code is spent. The code must not open a connection afterwards."""
    world.dns["agent.example"] = [[PUBLIC_IP]]
    with _signed_in(app) as owner:
        approved = _answer(owner, app, "approve")
    spend = service.exchange_code

    def invalidated_meanwhile(session_, raw, client_id):
        _turn_invalid(world, session)
        with app.state.session_factory() as other, other.begin():
            assert (
                clients.resolve(other, app.state.settings, app.state.oauth_fetch, CLIENT_URL)
                is None
            )
        return spend(session_, raw, client_id)

    monkeypatch.setattr(service, "exchange_code", invalidated_meanwhile)
    issued = agent.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": approved["code"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "client_id": CLIENT_URL,
            "code_verifier": VERIFIER,
        },
    )

    assert issued.status_code == 400 and issued.json()["error"] == "invalid_grant"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(OAuthToken)) == 0
    live = (
        select(func.count())
        .select_from(DataGrant)
        .where(DataGrant.grantee != "owner", DataGrant.revoked_at.is_(None))
    )
    assert session.scalar(live) == 0
