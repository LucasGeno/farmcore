"""App factory: `uvicorn farmcore.main:create_app --factory`."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from farmcore import __version__
from farmcore.composition import compose_spray_weather_reader
from farmcore.core import errors, jobs, registry
from farmcore.core.cookie_scope import InstallationCookies
from farmcore.core.identity import require_csrf
from farmcore.core.manifest import Manifest
from farmcore.core.privacy import PrivacyMiddleware
from farmcore.core.startup import (
    STATE_OK,
    check_public_settings,
    read_installation_id,
    read_schema_state,
)
from farmcore.core.tour import TourMiddleware
from farmcore.db import get_session, make_engine, make_session_factory
from farmcore.settings import Settings, get_settings
from farmcore.web import context_url

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    app_packages: tuple[str, ...] = ("farmcore.apps",),
    manifests: list[Manifest] | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    check_public_settings(settings)
    engine = None
    if session_factory is None:
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)
    with session_factory() as session:
        installation_id = read_installation_id(session)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        jobs.start(app)
        try:
            yield
        finally:
            jobs.shutdown(app)
            if engine is not None:
                engine.dispose()

    # A public site serves no interactive API docs: they list the admin endpoints.
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(
        dependencies=[Depends(require_csrf)],
        title="farmcore",
        version=__version__,
        lifespan=lifespan,
        **(docs if settings.farmcore_public else {}),
    )
    app.add_middleware(PrivacyMiddleware)
    app.add_middleware(TourMiddleware)
    cookie_scope = installation_id
    if not installation_id:
        # Fail closed on a network deployment: a per-process scope (everyone signs in again
        # after a restart) rather than cookies another installation on this host could read.
        if settings.farmcore_public:
            cookie_scope = str(uuid.uuid4())
        log.warning(
            "No installation_id in this database: run `farmcore migrate` as the database owner. "
            "Cookies are %s until then.",
            "scoped to this process" if cookie_scope else "NOT scoped to this installation",
        )
    if cookie_scope:
        # Outermost: every other layer sees this installation's cookies under plain names.
        app.add_middleware(InstallationCookies, installation_id=cookie_scope)
    app.state.installation_id = installation_id
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    errors.register(app)

    if manifests is None:
        manifests = registry.discover(packages=app_packages)
    registry.load(app, session_factory, manifests)
    # Saved spray weather: from the discovered apps; Weather's live state is read at capture.
    app.state.spray_weather_reader = compose_spray_weather_reader(app.state.all_manifests)

    @app.get("/health")
    def health(response: Response, session: Session = Depends(get_session)) -> dict:
        """Public: the release and Alembic heads in use and the schema state, nothing more.
        503 unless the state is `ok` (a migration running or pending, or a failed upgrade)."""
        state = read_schema_state(session)
        if state.state != STATE_OK:
            response.status_code = 503
        return {
            "db": "ok",
            "installation_id": installation_id,
            "release": __version__,
            "alembic_heads": list(state.heads),
            "state": state.state,
        }

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> RedirectResponse:
        if any(m.name == "today" for m in request.app.state.manifests):
            return RedirectResponse(context_url(request, "/today/"))
        for manifest in request.app.state.manifests:
            if manifest.nav is not None:
                return RedirectResponse(context_url(request, manifest.nav[1]))
        return RedirectResponse("/health")

    return app
