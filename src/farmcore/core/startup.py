"""Startup checks run by `create_app` before anything else is built."""

from __future__ import annotations

from farmcore.settings import Settings

MIN_TOKEN_CHARS = 32


def check_public_settings(settings: Settings) -> None:
    """Refuse a public deployment without a strong admin token.

    Without a token `web.is_admin` treats every visitor as admin, so a public site would expose
    the clock step form, uploads and the full chat audit to anyone. The message names the
    variables only, never a value.
    """
    if not settings.farmcore_public:
        return
    token = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if len(token) < MIN_TOKEN_CHARS or any(c.isspace() for c in token):
        raise RuntimeError(
            f"FARMCORE_PUBLIC=1 needs ADMIN_TOKEN: at least {MIN_TOKEN_CHARS} characters, no "
            "whitespace (e.g. openssl rand -hex 24), or unset FARMCORE_PUBLIC for local development"
        )
