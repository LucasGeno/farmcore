"""Runtime settings from the environment and `.env` (unprefixed names, e.g. DATABASE_URL).

Secrets are `SecretStr` and URLs carrying passwords are hidden from `repr`, so logging a
`Settings` object never prints a credential.
"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

DEFAULT_DATABASE_URL = "postgresql+psycopg://farmcore:farmcore@localhost:55442/farmcore"


def _default_vertex_auth() -> str:
    # google-auth reads GOOGLE_APPLICATION_CREDENTIALS from the process env, never from `.env`.
    return "adc" if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") else "gcloud"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    database_url: str = Field(DEFAULT_DATABASE_URL, repr=False)
    # Admin URL used to create throwaway test databases; default: same server, `/postgres`.
    test_database_url: str = Field("", repr=False)
    data_dir: Path = Path("sample-data")
    display_tz: str = "Europe/Paris"
    # Total live forecast HTTP budget per farm, including headers and streamed body.
    forecast_fetch_timeout_seconds: float = Field(10.0, ge=0.05, le=30.0)
    clock_mode: Literal["replay", "live"] = "replay"
    # Local time (display_tz), naive.
    replay_start: datetime = datetime(2024, 4, 1, 6, 0)
    public_base_url: str = "http://localhost:8000"
    admin_token: SecretStr | None = None
    # Set on any network-reachable deployment: the app then refuses to start without a strong
    # ADMIN_TOKEN and turns off the interactive API docs.
    farmcore_public: bool = False
    demo_mode: bool = False
    demo_farm_ids: list[int] = Field(default_factory=list)
    default_farm_key: str = "chateau"
    telegram_bot_token: SecretStr | None = None
    # Public @username of the bot (no "@"): the web "Log a spray" button opens t.me/<username>.
    telegram_bot_username: str | None = None
    # Comma-separated Telegram user ids allowed to use the bot. Empty = nobody (fail closed).
    telegram_allowed_user_ids: str = ""
    # AI Studio key, reserved for a non-EU fallback (not built). Vertex never uses it: the
    # regional host rejects API keys.
    gemini_api_key: SecretStr | None = None
    # Google Cloud project for Vertex AI. Empty: the vertex extractor falls back to regex.
    vertex_project: str = ""
    vertex_location: str = "europe-west4"
    # Default `{vertex_location}-aiplatform.googleapis.com`; `aiplatform.eu.rep.googleapis.com`
    # with location `eu` selects the EU multi-region.
    vertex_host: str | None = None
    vertex_model: str = "gemini-2.5-flash"
    # `adc` service-account file (containers), `gcloud` user token (laptop), `token` fixed
    # VERTEX_ACCESS_TOKEN (short-lived; debugging only). Default: `adc` when
    # GOOGLE_APPLICATION_CREDENTIALS is set, else `gcloud`.
    vertex_auth: Literal["adc", "gcloud", "token"] = Field(default_factory=_default_vertex_auth)
    vertex_access_token: SecretStr | None = None
    extractor: Literal["vertex", "regex", "fake"] = "vertex"
    whisper_model: str = "small"

    @field_validator("database_url", mode="before")
    @classmethod
    def _default_when_empty(cls, v: object) -> object:
        return v or DEFAULT_DATABASE_URL

    @model_validator(mode="after")
    def _derive_test_database_url(self) -> Settings:
        if not self.test_database_url:
            url = make_url(self.database_url).set(database="postgres")
            self.test_database_url = url.render_as_string(hide_password=False)
        return self

    def allowed_telegram_users(self) -> frozenset[int]:
        parts = (p.strip() for p in self.telegram_allowed_user_ids.split(","))
        return frozenset(int(p) for p in parts if p)


@lru_cache
def get_settings() -> Settings:
    return Settings()
