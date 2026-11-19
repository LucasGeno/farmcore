"""Weekly EU active-substance import job. Downloads only in live clock mode; replay never touches
the network."""

from __future__ import annotations

from farmcore.apps.catalog_eu_pesticides import service, sources
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext

JOB_NAME = "catalog_eu_pesticides.import"


def import_substances(ctx: JobContext) -> None:
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    path, meta = sources.download(ctx.settings.data_dir / "eu-pesticides")
    service.import_substances(ctx.session, path=path, meta=meta)
