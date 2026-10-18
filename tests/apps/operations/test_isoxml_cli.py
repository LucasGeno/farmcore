"""`farmcore operations isoxml-demo --out <dir>` and `farmcore operations import-isoxml <path>`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from farmcore.cli import cli
from farmcore.core import clock
from farmcore.core.models.canonical import Log
from farmcore.settings import get_settings
from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm, voice_spray


@pytest.fixture
def env(committed_session_factory, settings, tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no .env: the command reads the env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _farm(committed_session_factory, settings, *, at: str = CLOCK, voice: bool = True) -> int:
    with committed_session_factory() as s:
        farm = make_farm(s, settings)
        log_id = voice_spray(s, farm).id if voice else 0
        clock.set_replay(s, local(at), settings)
        s.commit()
    return log_id


def test_demo_then_import_prints_one_line_per_task(
    env, committed_session_factory, settings, tmp_path
):
    log_id = _farm(committed_session_factory, settings)
    runner = CliRunner()

    demo = runner.invoke(cli, ["operations", "isoxml-demo", "--out", str(tmp_path / "td")])
    assert demo.exit_code == 0, demo.output
    path = tmp_path / "td" / "TASKDATA" / "TASKDATA.XML"
    assert path.is_file()

    first = runner.invoke(cli, ["operations", "import-isoxml", str(path)])
    second = runner.invoke(cli, ["operations", "import-isoxml", str(path)])

    assert first.exit_code == 0, first.output
    assert first.output.splitlines() == [
        f"TSK1 Les Granges: log {log_id} merged, time and dose replaced",
        f"TSK2 Peyrot: log {log_id} merged, not replaced",
    ]
    assert second.exit_code == 0, second.output
    assert second.output.splitlines() == [
        f"TSK1 Les Granges: log {log_id} merged, already imported",
        f"TSK2 Peyrot: log {log_id} merged, already imported",
    ]


def test_reimport_without_a_voice_log_says_already_imported_not_new_log(
    env, committed_session_factory, settings, tmp_path
):
    _farm(committed_session_factory, settings, voice=False)
    runner = CliRunner()
    runner.invoke(cli, ["operations", "isoxml-demo", "--out", str(tmp_path)])
    path = str(tmp_path / "TASKDATA" / "TASKDATA.XML")

    first = runner.invoke(cli, ["operations", "import-isoxml", path])
    second = runner.invoke(cli, ["operations", "import-isoxml", path])

    assert first.exit_code == 0, first.output
    assert [line.endswith("new log") for line in first.output.splitlines()] == [True, True]
    assert second.exit_code == 0, second.output
    lines = second.output.splitlines()
    assert len(lines) == 2
    assert all(line.endswith("new log, already imported") for line in lines), lines
    with committed_session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Log)) == 2


@pytest.mark.parametrize(
    ("at", "unknown", "message"),
    [
        ("2024-04-10 07:00", False, "step the clock past 08:03"),
        (CLOCK, True, "unknown partfield(s) Le Clos"),
    ],
)
def test_a_late_task_or_unknown_partfield_exits_1_and_writes_nothing(
    env, committed_session_factory, settings, tmp_path, at, unknown, message
):
    _farm(committed_session_factory, settings, at=at)
    runner = CliRunner()
    runner.invoke(cli, ["operations", "isoxml-demo", "--out", str(tmp_path)])
    path = tmp_path / "TASKDATA" / "TASKDATA.XML"
    if unknown:
        path.write_bytes(path.read_bytes().replace(b'C="Peyrot"', b'C="Le Clos"'))

    result = runner.invoke(cli, ["operations", "import-isoxml", str(path)])

    assert result.exit_code == 1
    assert message in result.output
    with committed_session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Log)) == 1


def test_import_of_a_missing_file_exits_1(env, tmp_path):
    result = CliRunner().invoke(cli, ["operations", "import-isoxml", str(tmp_path / "nope.xml")])

    assert result.exit_code == 1
    assert "nope.xml" in result.output
