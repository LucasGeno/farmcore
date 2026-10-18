"""App factory: `uvicorn farmcore.main:create_app --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from farmcore import __version__
from farmcore.composition import compose_spray_weather_reader
from farmcore.core import errors, jobs, registry
from farmcore.core.manifest import Manifest
from farmcore.core.startup import check_public_settings
from farmcore.core.tour import TourMiddleware
from farmcore.db import get_session, make_engine, make_session_factory
from farmcore.settings import Settings, get_settings
from farmcore.web import context_url


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
        title="farmcore",
        version=__version__,
        lifespan=lifespan,
        **(docs if settings.farmcore_public else {}),
    )
    app.add_middleware(TourMiddleware)
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
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"db": "ok"}

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> RedirectResponse:
        if any(m.name == "today" for m in request.app.state.manifests):
            return RedirectResponse(context_url(request, "/today/"))
        for manifest in request.app.state.manifests:
            if manifest.nav is not None:
                return RedirectResponse(context_url(request, manifest.nav[1]))
        return RedirectResponse("/health")

    return app
