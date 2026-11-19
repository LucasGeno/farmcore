"""Browser journey: an owner creates an agent token on "Connect your agent" and copies the Codex
snippets: the shell line holds the once-shown token (inserted client-side), the config holds
the MCP URL and no token.

Needs `node` and the `playwright` package (resolved through `PLAYWRIGHT_NODE_PATH`, e.g. a
`node_modules` directory) and a Chromium; skips without them. The reachability states are covered
as pure seams in tests/apps/share/test_connect.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import uvicorn

from farmcore.apps.share import connect
from farmcore.core.identity import hash_password
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app

pytestmark = pytest.mark.slow  # a real browser: ~10 s, over the 5 s budget

SCRIPT = Path(__file__).with_name("connect_agent.cjs")
PASSWORD = "synthetic-browser-owner-password"  # gauntlet:allow-secret synthetic credential


def test_owner_creates_a_token_and_copies_the_two_codex_snippets(
    settings, committed_session_factory, free_tcp_port
):
    node = shutil.which("node")
    modules = os.environ.get("PLAYWRIGHT_NODE_PATH", "")
    if not node or not modules:
        pytest.skip("needs node and PLAYWRIGHT_NODE_PATH (a node_modules with playwright)")
    with committed_session_factory() as session:
        farm = ensure_default_farm(session, settings)
        owner = LocalIdentity(login="browser-owner", password_hash=hash_password(PASSWORD))
        session.add(owner)
        session.flush()
        session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
        session.commit()
    app = create_app(settings, session_factory=committed_session_factory)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=free_tcp_port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        done = subprocess.run(
            [node, str(SCRIPT), f"http://127.0.0.1:{free_tcp_port}", "browser-owner", PASSWORD],
            env={**os.environ, "NODE_PATH": modules},
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        server.should_exit = True
        thread.join(10)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["token"].startswith("fc_pat_")
    # The shell line carries the token; the TOML names the variable and holds no secret.
    assert seen["env"] == f'export FARMCORE_TOKEN="{seen["token"]}"'
    assert connect.mcp_url(settings) in seen["toml"]
    assert 'bearer_token_env_var = "FARMCORE_TOKEN"' in seen["toml"]
    assert seen["token"] not in seen["toml"]
