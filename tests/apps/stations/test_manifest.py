"""Stations manifest: the seed runs under `farmcore seed`, the job under a replay step."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from farmcore.apps.stations.manifest import MANIFEST
from farmcore.core import jobs
from farmcore.core import seed as core_seed
from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.commands.seed import run_seeds
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import Asset, Observation
from tests.apps.stations.synth import hourly, write_csv

# Fields lands in parallel (09); stand in for it so its seed never runs here.
FIELDS = Manifest(name="fields", version="0.0.0")
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)


def test_manifest_declares_scope_nav_job_and_seed():
    assert MANIFEST.depends == ("core", "fields")
    assert MANIFEST.scopes == ("stations:read",)
    assert MANIFEST.nav == ("Weather", "/stations/")
    (job,) = MANIFEST.jobs
    assert (job.name, job.func, job.interval) == (
        "stations.ingest_meteofrance",
        "farmcore.apps.stations.jobs:ingest_meteofrance",
        timedelta(hours=1),
    )


def test_farmcore_seed_twice_makes_one_station(session, settings, monkeypatch, tmp_path):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)

    for _ in range(2):
        results = run_seeds(session, [CORE, FIELDS, MANIFEST], scene=None, data_dir=tmp_path)

    assert ("stations", {"station": 1}) in results
    count = select(func.count()).select_from(Asset).where(Asset.kind == "station")
    assert session.scalar(count) == 1


def test_replay_step_runs_the_ingest_job(session_factory, settings, monkeypatch, tmp_path):
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    with session_factory() as s:
        run_seeds(s, [CORE, FIELDS, MANIFEST], scene=None, data_dir=tmp_path)
        s.commit()
    write_csv(tmp_path, hourly(NOW - timedelta(hours=2), 100))

    runs = jobs.step_to(
        session_factory,
        [CORE, FIELDS, MANIFEST],
        NOW,
        settings.model_copy(update={"data_dir": tmp_path}),
    )

    assert [(r.name, r.ok) for r in runs] == [("stations.ingest_meteofrance", True)]
    with session_factory() as s:
        latest = s.scalar(select(func.max(Observation.ts)))
    assert latest == NOW + timedelta(hours=73)
