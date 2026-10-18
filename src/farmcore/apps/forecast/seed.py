"""Forecast seed: import the saved single runs under `DATA_DIR/open-meteo/ecmwf_ifs/` (written
by `farmcore forecast fetch`, shipped with the other open data) into the demo farm. Offline and
idempotent; no folder, no runs (the window then says "No forecast loaded")."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from farmcore.apps.forecast import sources, store
from farmcore.core.seed import ensure_default_farm


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    folder = data_dir / sources.RUNS_DIR
    if not folder.is_dir():
        return {"forecast_runs": 0}
    farm = ensure_default_farm(session)
    counts = store.import_runs(session, farm.id, sources.SingleRunsDir(folder).runs())
    return {"forecast_runs": counts["runs"]}
