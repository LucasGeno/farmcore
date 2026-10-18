"""A public deployment (FARMCORE_PUBLIC=1) refuses to start without a strong admin token.

Without a token, `web.is_admin` treats every visitor as admin, so the app-level assert is the
defence in depth behind the compose `${ADMIN_TOKEN:?}` guard.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.main import create_app
from farmcore.settings import Settings

MARKER = "test-only-startup-credential-marker-7f3a"


def _public(settings: Settings, token: str | None) -> Settings:
    return settings.model_copy(
        update={
            "farmcore_public": True,
            "admin_token": None if token is None else SecretStr(token),
        }
    )


def test_public_without_a_token_refuses_to_start(settings, session_factory) -> None:
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(_public(settings, None), manifests=[], session_factory=session_factory)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_public_with_a_blank_token_refuses_to_start(settings, session_factory, blank) -> None:
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(_public(settings, blank), manifests=[], session_factory=session_factory)


@pytest.mark.parametrize("weak", ["short", "x" * 31, "x" * 32 + "\n", "x" * 16 + " " + "x" * 16])
def test_public_with_a_weak_token_refuses_to_start(settings, session_factory, weak) -> None:
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(_public(settings, weak), manifests=[], session_factory=session_factory)


def test_public_with_a_token_starts_and_is_healthy(settings, session_factory) -> None:
    app = create_app(_public(settings, MARKER), manifests=[], session_factory=session_factory)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_not_public_without_a_token_starts(settings, session_factory) -> None:
    assert settings.farmcore_public is False
    app = create_app(settings, manifests=[], session_factory=session_factory)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_refusal_message_never_contains_a_secret(settings, session_factory) -> None:
    # Every other secret carries the marker: the refusal names the variable, never a value.
    bad = _public(settings, " ").model_copy(
        update={
            "database_url": f"postgresql+psycopg://u:{MARKER}@db:5432/x",
            "telegram_bot_token": SecretStr(MARKER),
        }
    )
    with pytest.raises(RuntimeError) as exc:
        create_app(bad, manifests=[], session_factory=session_factory)
    assert MARKER not in str(exc.value)
    assert MARKER not in repr(exc.value)


def test_farmcore_public_reads_the_env(monkeypatch) -> None:
    monkeypatch.setenv("FARMCORE_PUBLIC", "1")
    assert Settings(_env_file=None).farmcore_public is True
    monkeypatch.delenv("FARMCORE_PUBLIC")
    assert Settings(_env_file=None).farmcore_public is False
