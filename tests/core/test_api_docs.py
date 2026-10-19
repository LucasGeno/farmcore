"""The interactive API docs list the admin endpoints; a public site turns them off."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.main import create_app

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _client(settings, session_factory, *, public: bool) -> TestClient:
    token = SecretStr("test-only-api-docs-credential-000000")
    update = {"farmcore_public": public, "admin_token": token}
    app = create_app(
        settings.model_copy(update=update), manifests=[], session_factory=session_factory
    )
    return TestClient(app, headers={"X-Admin-Token": token.get_secret_value()})


@pytest.mark.parametrize("path", DOC_PATHS)
def test_public_site_serves_no_api_docs(settings, session_factory, path) -> None:
    with _client(settings, session_factory, public=True) as c:
        assert c.get(path, headers={"Accept": "application/json"}).status_code == 404


@pytest.mark.parametrize("path", DOC_PATHS)
def test_local_development_keeps_the_api_docs(settings, session_factory, path) -> None:
    with _client(settings, session_factory, public=False) as c:
        assert c.get(path).status_code == 200
