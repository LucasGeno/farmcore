"""Runtime settings from the environment and `.env` (unprefixed names, e.g. DATABASE_URL).

Secrets are `SecretStr` and URLs carrying passwords are hidden from `repr`, so logging a
`Settings` object never prints a credential.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

DEFAULT_DATABASE_URL = "postgresql+psycopg://farmcore:farmcore@localhost:55442/farmcore"
DEFAULT_OPEN_METEO_BASE_URL = "https://api.open-meteo.com"
DEFAULT_OPEN_METEO_SINGLE_RUNS_BASE_URL = "https://single-runs-api.open-meteo.com"
DEFAULT_METEOFRANCE_API_BASE_URL = "https://public-api.meteofrance.fr/public"
DEFAULT_METEOFRANCE_TOKEN_URL = "https://portail-api.meteofrance.fr/token"


def _check_http_url(v: str) -> str:
    """An http(s) URL with a host, a numeric valid port, no credentials, query or fragment."""
    url = urlsplit(v)
    try:
        port = url.port
    except ValueError:
        raise ValueError("must use a numeric port") from None
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("must be an http(s) URL")
    if url.username is not None or url.password is not None:
        raise ValueError("must not contain credentials")
    if url.query or url.fragment:
        raise ValueError("must not contain a query or fragment")
    if port is not None and not 0 < port < 65536:
        raise ValueError("must use a valid port")
    return v


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_vertex_auth() -> str:
    # google-auth reads GOOGLE_APPLICATION_CREDENTIALS from the process env, never from `.env`.
    return "adc" if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") else "gcloud"


class Settings(BaseSettings):
    # No validation error echoes its input: a refused setting may sit beside a credential.
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", hide_input_in_errors=True
    )

    database_url: str = Field(DEFAULT_DATABASE_URL, repr=False)
    # Admin URL used to create throwaway test databases; default: same server, `/postgres`.
    test_database_url: str = Field("", repr=False)
    data_dir: Path = Path("sample-data")
    display_tz: str = "Europe/Paris"
    # Total live forecast HTTP budget per farm, including headers and streamed body.
    forecast_fetch_timeout_seconds: float = Field(10.0, ge=0.05, le=30.0)
    # Open-Meteo hosts (scheme + authority, no /v1/forecast path): the free public API by
    # default. Point at customer-api.open-meteo.com (with a key) or a self-hosted server.
    open_meteo_base_url: str = DEFAULT_OPEN_METEO_BASE_URL
    open_meteo_single_runs_base_url: str = DEFAULT_OPEN_METEO_SINGLE_RUNS_BASE_URL
    # Customer API key, sent as `apikey`; never logged, stored or rendered.
    open_meteo_api_key: SecretStr | None = None
    # Météo-France portal application (live station observations, `weather_meteofrance`): the
    # OAuth client credentials, exchanged for a bearer token; never logged, stored or rendered.
    # No client: the live ingest stays off.
    meteofrance_client_id: SecretStr | None = None
    meteofrance_client_secret: SecretStr | None = None
    meteofrance_api_base_url: str = DEFAULT_METEOFRANCE_API_BASE_URL
    meteofrance_token_url: str = DEFAULT_METEOFRANCE_TOKEN_URL
    # Optional address search on the location step: a Nominatim or Photon base URL. Empty (the
    # default) keeps the geocoder off; map, browser location and coordinates still work.
    geocoder_base_url: str = ""
    # The location step's world base map: empty uses OpenStreetMap. Set a self-hosted or
    # licensed {z}/{x}/{y} template to keep the base requests off a public host. The IGN
    # orthophoto is layered over France and the DOM by the page itself.
    location_map_tiles_url: str = ""
    location_map_attribution: str = "© OpenStreetMap contributors"
    clock_mode: Literal["replay", "live"] = "replay"
    # Local time (display_tz), naive.
    replay_start: datetime = datetime(2024, 4, 1, 6, 0)
    public_base_url: str = "http://localhost:8000"
    # `farmcore migrate` only (run as the database owner): the DML-only role the app and bot
    # connect as. It is created or updated, with this password, on every migrate. No password:
    # no runtime role is provisioned (local development against the owner URL).
    runtime_database_role: str = Field("farmcore_app", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    runtime_database_password: SecretStr | None = None
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
    # Which model the web assistant's tool chooser calls: Vertex (above), any OpenAI-compatible
    # server (OpenAI, OpenRouter, Mistral, Ollama, vLLM), or none (the keyword router answers).
    assistant_provider: Literal["vertex", "openai-compatible", "none"] = "vertex"
    # Base URL including the version path (`https://api.openai.com/v1`, `http://localhost:11434/v1`);
    # `/chat/completions` is appended. The key is sent as a bearer token, never logged or audited.
    openai_compatible_base_url: str = ""
    openai_compatible_api_key: SecretStr | None = None
    openai_compatible_model: str = ""
    whisper_model: str = "small"

    @field_validator("database_url", mode="before")
    @classmethod
    def _default_when_empty(cls, v: object) -> object:
        return v or DEFAULT_DATABASE_URL

    @field_validator(
        "open_meteo_base_url",
        "open_meteo_single_runs_base_url",
        "meteofrance_api_base_url",
        "meteofrance_token_url",
    )
    @classmethod
    def _http_url_without_credentials(cls, v: str) -> str:
        return _check_http_url(v)

    @field_validator("openai_compatible_base_url")
    @classmethod
    def _openai_base_url(cls, v: str) -> str:
        return _check_http_url(v) if v else v

    @field_validator("geocoder_base_url")
    @classmethod
    def _geocoder_base_url(cls, v: str) -> str:
        return _check_http_url(v) if v else v

    @model_validator(mode="after")
    def _key_requires_https(self) -> Settings:
        if self.open_meteo_api_key is not None and self.open_meteo_api_key.get_secret_value():
            for url in (self.open_meteo_base_url, self.open_meteo_single_runs_base_url):
                if urlsplit(url).scheme != "https":
                    raise ValueError("OPEN_METEO_API_KEY requires https Open-Meteo base URLs")
        return self

    @model_validator(mode="after")
    def _meteofrance_credentials_require_https(self) -> Settings:
        if self.meteofrance_client_secret is not None:
            for url in (self.meteofrance_api_base_url, self.meteofrance_token_url):
                if urlsplit(url).scheme != "https":
                    raise ValueError("METEOFRANCE_CLIENT_SECRET requires https Météo-France URLs")
        return self

    @model_validator(mode="after")
    def _openai_compatible_complete(self) -> Settings:
        incomplete = not (self.openai_compatible_base_url and self.openai_compatible_model)
        if self.assistant_provider == "openai-compatible" and incomplete:
            raise ValueError(
                "ASSISTANT_PROVIDER=openai-compatible needs OPENAI_COMPATIBLE_BASE_URL "
                "and OPENAI_COMPATIBLE_MODEL"
            )
        key = self.openai_compatible_api_key
        if key is not None and key.get_secret_value() and self.openai_compatible_base_url:
            url = urlsplit(self.openai_compatible_base_url)
            if url.scheme != "https" and not _is_loopback(url.hostname or ""):
                raise ValueError(
                    "OPENAI_COMPATIBLE_API_KEY requires an https base URL (http only to loopback)"
                )
        return self

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
