"""Weekly Italian product-register import job. Downloads only in live clock mode; replay never
touches the network."""

from __future__ import annotations

from farmcore.apps.catalog_it_salute import service, sources
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext

JOB_NAME = "catalog_it_salute.import"


def import_salute(ctx: JobContext) -> None:
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    path, meta = sources.download(ctx.settings.data_dir / "it-salute")
    service.import_salute(ctx.session, path=path, meta=meta)
