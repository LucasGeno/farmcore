"""Connect your agent: the MCP URL, reachability and copy-paste setup for token clients.

Failure model (one owning test each):
- a snippet hands a farmer `DATABASE_URL` or a token in a URL (stdio is operator-only);
- the probe sends a token, hangs the page, or calls the network under test;
- the page says "reachable" for a URL only this machine can reach;
- the issued token is rendered twice server-side.
"""

from __future__ import annotations

import re

import httpx
import pytest

from farmcore.apps.share import connect
from farmcore.apps.share.routes import get_probe_transport
from tests.apps.share.test_tokens import (  # noqa: F401
    SHOWN,
    _form,
    _post,
    credential_hash,
    owner,
    owner_browser,
)

LOCAL = "http://127.0.0.1:8001/mcp"
PUBLIC = "https://farm.example.org/mcp"
CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


@pytest.mark.parametrize(
    ("url", "answer", "state", "public"),
    [
        (LOCAL, httpx.Response(401, headers=CHALLENGE), "up", False),
        (PUBLIC, httpx.Response(401, headers=CHALLENGE), "up", True),
        (PUBLIC, httpx.Response(200), "unexpected", True),  # something else answers there
        (PUBLIC, httpx.Response(401), "unexpected", True),  # no challenge: not our MCP server
        (PUBLIC, httpx.ConnectError("refused"), "down", True),
        (LOCAL, httpx.ReadTimeout("slow"), "down", False),
    ],
)
def test_the_probe_reads_a_tokenless_request_and_calls_loopback_local(url, answer, state, public):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    result = connect.probe(url, transport=httpx.MockTransport(handler))

    assert (result.state, result.public) == (state, public)
    [request] = seen
    assert "authorization" not in request.headers and not request.url.query
    assert connect.PROBE_TIMEOUT <= 2


def test_setup_snippets_never_leak_the_database_url_or_put_a_token_in_a_url():
    snippets = connect.snippets(PUBLIC)

    for snippet in snippets:
        assert "DATABASE_URL" not in snippet.text and "postgres" not in snippet.text
        assert "?" not in snippet.text.replace("--", "")  # no query string anywhere
    desktop, codex = snippets
    assert desktop.operator_only and not codex.operator_only
    assert "exec -T" in desktop.text or '"exec"' in desktop.text
    assert PUBLIC in codex.text and connect.TOKEN_PLACEHOLDER in codex.text


def test_the_page_explains_local_versus_public_and_probes_only_in_expert_view(
    owner_browser,  # noqa: F811
    demo,
    settings,
    monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(settings, "mcp_http_enabled", True)
    probed: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        return httpx.Response(401, headers=CHALLENGE)

    owner_browser.app.dependency_overrides[get_probe_transport] = lambda: httpx.MockTransport(
        transport
    )
    plain = owner_browser.get(f"/share/connect?farm={demo.farm.id}")
    assert plain.status_code == 200 and not probed
    assert "tunnel" in plain.text.lower()  # loopback-only default: needs a tunnel or hosting
    assert "coming" in plain.text.lower()  # OAuth clients wait for ticket 11

    owner_browser.cookies.set("expert", "1")
    expert = owner_browser.get(f"/share/connect?farm={demo.farm.id}")
    assert probed == [connect.mcp_url(settings)]
    assert 'data-reachability="up"' in expert.text


def test_a_token_made_here_is_rendered_once_and_inserted_into_snippets_client_side(
    owner_browser,  # noqa: F811
    demo,  # noqa: F811
):
    created = _post(
        owner_browser,
        f"/share/tokens?farm={demo.farm.id}",
        _form(origin="connect"),
    )

    assert created.status_code == 201 and created.headers["Cache-Control"] == "no-store"
    assert "data-connect-page" in created.text
    assert len(re.findall(SHOWN, created.text)) == 1
