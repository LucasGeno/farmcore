"""Weekly E-Phy import job. Downloads only in live clock mode; replay never touches the network."""

from __future__ import annotations

from farmcore.apps.catalog_fr_ephy import service, sources
from farmcore.core.clock import LiveClock, get_clock
from farmcore.core.manifest import JobContext

JOB_NAME = "catalog_fr_ephy.import"


def import_ephy(ctx: JobContext) -> None:
    if not isinstance(get_clock(ctx.session, ctx.settings), LiveClock):
        return
    products, uses, meta = sources.download(ctx.settings.data_dir / "ephy-fr")
    service.import_ephy(ctx.session, products_path=products, uses_path=uses, meta=meta)
