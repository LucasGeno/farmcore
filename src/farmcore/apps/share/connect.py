"""Connect your agent: where the MCP endpoint is, whether it answers, and how a client reaches it.

Pure of the web layer so the states and snippets are tested without a browser. Two security rules
from SECURITY.md are enforced here, not left to the template:

- stdio is an operator transport: its snippet runs `farmcore mcp` *inside* the container
  (`docker compose exec`), so the client config holds `FARMCORE_TOKEN` and never `DATABASE_URL`;
- HTTP beyond loopback needs a buffering TLS reverse proxy, and the token travels only in the
  `Authorization` header.

The reachability probe sends one POST with no credentials and expects the 401 challenge the MCP
gateway answers (`core/mcp_http.py`). It never carries a token and is bounded to `PROBE_TIMEOUT`.
"""

from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as DeadlineExceeded
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from farmcore.settings import Settings

PROBE_DEADLINE = 2.0  # seconds in all, DNS included: the page must not hang on a dead URL
TOKEN_PLACEHOLDER = "<your-token>"  # the page swaps in the once-shown token, client-side
MCP_PATH = "/mcp"

State = Literal["off", "up", "down", "unexpected", "host"]


@dataclass(frozen=True)
class Reachability:
    state: State
    public: bool  # False: only this machine can reach the URL (loopback)


@dataclass(frozen=True)
class Snippet:
    client: str
    key: str
    where: str  # the file to edit
    text: str
    operator_only: bool = False


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def mcp_url(settings: Settings) -> str:
    """The URL a remote agent calls: the public site's `/mcp` when the site has a public address
    (a reverse proxy routes it to the MCP process), else the local listener."""
    base = settings.public_base_url.rstrip("/")
    if not is_loopback(base):
        return base + MCP_PATH
    host = settings.mcp_http_host
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # noqa: S104 (a client address)
    host = f"[{host}]" if ":" in host else host
    return f"http://{host}:{settings.mcp_http_port}{MCP_PATH}"


def _post(url: str, transport: httpx.BaseTransport | None) -> httpx.Response:
    timeout = httpx.Timeout(1.0, pool=0.2)  # per step; `probe` bounds the total
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        return client.post(url, json={})


def probe(url: str, transport: httpx.BaseTransport | None = None) -> Reachability:
    """Ask the MCP URL for nothing, without a token: "up" only for the 401 + `WWW-Authenticate`
    challenge of FarmCore's gateway; 421 is that gateway refusing the Host header."""
    public = not is_loopback(url)
    # httpx's timeouts are per step and skip DNS, so a worker carries the real deadline. It is
    # abandoned on expiry: its own per-step timeouts end it shortly after.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        response = pool.submit(_post, url, transport).result(timeout=PROBE_DEADLINE)
    except (httpx.HTTPError, DeadlineExceeded):
        return Reachability("down", public)
    finally:
        pool.shutdown(wait=False)
    if response.status_code == 421:
        return Reachability("host", public)
    ok = response.status_code == 401 and "www-authenticate" in response.headers
    return Reachability("up" if ok else "unexpected", public)


def reachability(settings: Settings, transport: httpx.BaseTransport | None = None) -> Reachability:
    url = mcp_url(settings)
    if not settings.mcp_http_enabled:
        return Reachability("off", not is_loopback(url))
    return probe(url, transport)


def snippets(url: str) -> tuple[Snippet, Snippet, Snippet]:
    desktop = (
        '{\n  "mcpServers": {\n    "farmcore": {\n      "command": "docker",\n'
        '      "args": ["compose", "-f", "/path/to/farmcore/docker-compose.yml",\n'
        '               "exec", "-T", "-e", "FARMCORE_TOKEN", "app", "farmcore", "mcp"],\n'
        f'      "env": {{ "FARMCORE_TOKEN": "{TOKEN_PLACEHOLDER}" }}\n'
        "    }\n  }\n}"
    )
    codex_env = f'export FARMCORE_TOKEN="{TOKEN_PLACEHOLDER}"'
    codex_toml = f'[mcp_servers.farmcore]\nurl = "{url}"\nbearer_token_env_var = "FARMCORE_TOKEN"'
    return (
        Snippet(
            "Claude Desktop",
            "desktop",
            "claude_desktop_config.json",
            desktop,
            operator_only=True,
        ),
        Snippet("Codex", "codex-env", "your shell profile (~/.zshrc or ~/.bashrc)", codex_env),
        Snippet("Codex", "codex-toml", "~/.codex/config.toml (no token in this file)", codex_toml),
    )
