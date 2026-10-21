"""Restore a backup as a new, independent installation, through the `farmcore` CLI.

Seams: the CLI (`backup`, `restore`, `effects`, `migrate`, `replay`, `bot`), HTTP sign-in, real
`farmcore restore` processes for races, and external tools on PATH (pg_restore stand-ins).
Tests that only read installation A or restore its bundle share one A per module.

Failure model (what these tests are for):
- data loss, a shared identity or carried sessions in the copy;
- two copies both pushing advice, or both polling one bot token;
- a refused bundle that reaches the load, or a refusal that touches the target, the source,
  any bundle or the temp dir; a restore that replaces an installation holding data;
- a failed load that leaves a half-loaded copy the app can open;
- two restores racing on one target, or sweeping each other's staging copies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.instances.support import (
    HEAD,
    OUTCOMES,
    SEMANTIC,
    Marker,
    Untouched,
    backup,
    decrypt,
    effects,
    encrypt,
    forge_dump,
    forged_bundle,
    installation_id,
    make_key,
    member_status,
    members,
    owner_env,
    refused,
    repack,
    restore,
    run_cli,
    sign_in,
    signed_in_member,
    table_rows,
    with_manifest,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


@pytest.fixture(scope="module")
def bundles(shared_source, owner_key, tmp_path_factory) -> dict[str, Path]:
    """A's bundle, and an older one beside it (a refusal must leave both alone)."""
    _, recipient = owner_key
    out = tmp_path_factory.mktemp("bundles")
    return {
        "older": backup(shared_source, recipient, out / "older.age"),
        "good": backup(shared_source, recipient, out / "a.age"),
    }


# --- E: the copy, effects and identity ----------------------------------------------------------

# Tables a restore changes on purpose: sessions are dropped; sign-in throttling starts afresh.
RESET_TABLES = {"local_session", "login_bucket"}


def test_restore_makes_an_independent_copy_with_every_record_and_its_provenance(
    shared_source, module_dbs, owner_key, tmp_path
):
    """A shares its module: the member and session this adds are in no other test's facts."""
    key, recipient = owner_key
    login, password, a_cookies = signed_in_member(shared_source)
    bundle = backup(shared_source, recipient, tmp_path / "a.age")
    target = module_dbs.create()
    c_env = module_dbs.installation(target)

    result = restore(bundle, key, c_env)

    assert result.exit_code == 0, result.output
    a_id, c_id = (
        installation_id(module_dbs, shared_source["name"]),
        installation_id(module_dbs, target),
    )
    assert c_id is not None and uuid.UUID(c_id).version == 4 and c_id != a_id
    assert c_id in result.output
    # Every row of every FarmCore table is the same, except the reset tables.
    a_rows, c_rows = table_rows(module_dbs, shared_source["name"]), table_rows(module_dbs, target)
    assert set(a_rows) == set(c_rows)
    for table in sorted(set(a_rows) - RESET_TABLES):
        assert c_rows[table] == a_rows[table], table
    # ...and by meaning: records, sources, provenance and module evidence.
    for label, sql in SEMANTIC.items():
        assert module_dbs.query(target, sql) == module_dbs.query(shared_source["name"], sql), label
    assert len(module_dbs.query(target, SEMANTIC["records"])) == 10
    assert len(module_dbs.query(target, SEMANTIC["sources"])) == 4
    assert len(module_dbs.query(target, SEMANTIC["evidence"])) == 10
    # No session of A is valid on C; the member signs in again with the same password.
    assert c_rows["local_session"] == []
    c_runtime = module_dbs.runtime_url(c_env)
    from farmcore.core.cookie_scope import cookie_prefix

    a_prefix, c_prefix = cookie_prefix(a_id), cookie_prefix(c_id)
    replayed = {c_prefix + k.removeprefix(a_prefix): v for k, v in a_cookies.items()}
    assert member_status(c_runtime, replayed) in (401, 403)
    assert member_status(c_runtime, sign_in(c_runtime, login, password)) == 200
    # A is untouched: its member is still signed in.
    assert member_status(shared_source["DATABASE_URL"], a_cookies) == 200
    assert bundle.exists()


CHAT = 4242
TOKEN = "123:fake-test-token"


@pytest.fixture
def telegram():
    """The Telegram boundary: a fake sender for the advice push."""
    from farmcore.apps.chat import push
    from farmcore.apps.chat.telegram import FakeTelegramSender

    fake = FakeTelegramSender()
    push.set_sender(fake)
    yield fake
    push.set_sender(None)


def test_a_restored_copy_pushes_no_advice_until_the_owner_enables_effects(
    source, dbs, owner_key, tmp_path, telegram
):
    key, recipient = owner_key
    dbs.query(
        source["name"],
        "UPDATE farm SET props = props || jsonb_build_object('chat', "
        "jsonb_build_object('telegram_chat_ids', jsonb_build_array(:chat))) "
        "WHERE id = 1 RETURNING id",
        chat=CHAT,
    )
    target = dbs.create()
    c = dbs.installation(target)
    assert restore(backup(source, recipient, tmp_path / "a.age"), key, c).exit_code == 0
    a = owner_env(source)
    assert effects(a) == "external effects: enabled"
    assert effects(c) == "external effects: paused"

    # C's clock moves on and advice changes, but nothing reaches A's Telegram chat.
    stepped = run_cli("replay", "--to", "2024-04-11 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert telegram.sent == []
    # A (effects enabled) pushes the same pending changes: the gate, not a lack of events.
    stepped = run_cli("replay", "--to", "2024-04-11 06:00", env={**a, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert [s.chat_id for s in telegram.sent] == [CHAT]
    telegram.sent.clear()

    # The runtime role (what the app and bot use) cannot switch effects on, and is named.
    runtime = {"DATABASE_URL": dbs.runtime_url(c)}
    denied = run_cli("effects", "enable", env=runtime)
    assert denied.exit_code == 1 and c["RUNTIME_DATABASE_ROLE"] in denied.output
    assert effects(c) == "external effects: paused"

    # The owner enables them; the next push goes out. Pausing stops them again.
    assert run_cli("effects", "enable", env=c).exit_code == 0
    assert effects(runtime) == "external effects: enabled"
    stepped = run_cli("replay", "--to", "2024-04-12 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert stepped.exit_code == 0, stepped.output
    assert [s.chat_id for s in telegram.sent] == [CHAT]
    assert run_cli("effects", "pause", env=c).exit_code == 0
    telegram.sent.clear()
    run_cli("replay", "--to", "2024-04-13 06:00", env={**c, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert telegram.sent == []


@pytest.fixture
def bot_boundaries(monkeypatch):
    """Telegram polling, the Bot API sender, Whisper and the clock's sleep, recorded."""
    from telegram.ext import Application

    from farmcore.apps.chat import commands
    from farmcore.apps.chat.transcribe import FakeTranscriber

    calls: list[str] = []

    class Sender:
        def __init__(self, token):
            calls.append("sender")

        def close(self):
            calls.append("sender closed")

    def sleep(seconds):
        calls.append("waiting")
        if calls.count("waiting") >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(commands, "HttpxTelegramSender", Sender)
    monkeypatch.setattr(commands, "WhisperTranscriber", lambda settings: FakeTranscriber())
    monkeypatch.setattr(commands, "sleep", sleep)
    monkeypatch.setattr(
        Application, "run_polling", lambda app, *, allowed_updates: calls.append("polling")
    )
    return calls


def test_a_paused_copy_never_polls_its_bot_token_until_effects_are_enabled(
    bundles, dbs, owner_key, bot_boundaries
):
    key, _ = owner_key
    target = dbs.create()
    c = dbs.installation(target)
    assert restore(bundles["good"], key, c).exit_code == 0
    runtime = {"DATABASE_URL": dbs.runtime_url(c), "TELEGRAM_BOT_TOKEN": TOKEN}

    waiting = run_cli("bot", env=runtime)

    assert bot_boundaries == ["waiting"] * 3  # never the sender, never polling
    assert "paused" in waiting.output
    assert run_cli("effects", "enable", env=c).exit_code == 0
    bot_boundaries.clear()
    started = run_cli("bot", env=runtime)
    assert started.exit_code == 0, started.output
    assert bot_boundaries == ["sender", "polling", "sender closed"]


def test_a_bundle_from_a_newer_release_whose_schema_this_release_knows_restores(
    shared_source, bundles, module_dbs, owner_key, tmp_path
):
    """An upgrade's pre-upgrade bundle is written by the new release; the previous one must
    restore it: compatibility is the schema, not the release number."""
    key, recipient = owner_key
    files = with_manifest(members(decrypt(bundles["good"], key)), release="999.0.0")
    bundle = encrypt(repack(files), recipient, tmp_path / "newer-release.age")
    target = module_dbs.create()

    result = restore(bundle, key, module_dbs.installation(target))

    assert result.exit_code == 0, result.output
    for label, sql in SEMANTIC.items():
        assert module_dbs.query(target, sql) == module_dbs.query(shared_source["name"], sql), label


def test_a_bundle_from_an_older_schema_is_restored_and_upgraded_to_this_release(
    dbs, owner_key, tmp_path
):
    """Written before the identity tables existed: its rows load at its own revision, then the
    copy is upgraded to head."""
    from alembic import command

    from farmcore.core.commands.migrate import alembic_config

    key, recipient = owner_key
    old = dbs.create()
    env = dbs.installation(old)
    command.upgrade(alembic_config(env["DATABASE_URL"]), "inventory_0001")
    old_id = str(uuid.uuid4())
    dbs.on_server(f"ALTER DATABASE \"{old}\" SET farmcore.installation_id = '{old_id}'")
    dbs.query(old, "INSERT INTO farm (name) VALUES ('Old farm') RETURNING id")
    dbs.query(
        old,
        "INSERT INTO log (farm_id, type, status, name, ts_start) "
        "SELECT id, 'spray', 'done', 'Copper', now() FROM farm RETURNING id",
    )
    bundle = backup(env, recipient, tmp_path / "old.age")
    target = dbs.create()

    result = restore(bundle, key, dbs.installation(target))

    assert result.exit_code == 0, result.output
    assert dbs.query(target, "SELECT version_num FROM alembic_version") == [(HEAD,)]
    joined = "SELECT f.name, l.name FROM log l JOIN farm f ON f.id = l.farm_id"
    assert dbs.query(target, joined) == [("Old farm", "Copper")]


# --- F6, F7: refused before anything is loaded --------------------------------------------------


def _wrong_key(ctx) -> tuple[Path, Path]:
    other, _ = make_key(ctx.tmp / "other")
    return ctx.good, other


def _cut_in_half(ctx) -> tuple[Path, Path]:
    data = ctx.good.read_bytes()
    (ctx.tmp / "half.age").write_bytes(data[: len(data) // 2])
    return ctx.tmp / "half.age", ctx.key


def _dump_hash_changed(ctx) -> tuple[Path, Path]:
    files = members(decrypt(ctx.good, ctx.key))
    meta = {**json.loads(files["manifest.json"])["dump"], "sha256": "0" * 64}
    forged = encrypt(repack(with_manifest(files, dump=meta)), ctx.recipient, ctx.tmp / "f.age")
    return forged, ctx.key


def _incompatible_release(ctx) -> tuple[Path, Path]:
    """A newer release's database: a schema revision this release has never heard of."""
    name = ctx.dbs.create()
    env = ctx.dbs.installation(name)
    assert run_cli("migrate", env=env).exit_code == 0
    ctx.dbs.query(name, "UPDATE alembic_version SET version_num = 'zz_future_0001' RETURNING 1")
    return backup(env, ctx.recipient, ctx.tmp / "newer.age"), ctx.key


def _materialized_view_running_a_program(ctx) -> tuple[Path, Path]:
    """The independent verifier's bundle: refreshing the view runs a program as superuser."""
    files = members(decrypt(ctx.good, ctx.key))
    dump = forge_dump(ctx.dbs, files, ctx.tmp, [
        ctx.marker.function(),
        "CREATE MATERIALIZED VIEW public.fc_mv AS SELECT public.fc_pwn() AS x",
    ])  # fmt: skip
    ctx.marker.clear()
    return forged_bundle(files, dump, ctx.recipient, ctx.tmp / "forged.age"), ctx.key


class Refusal:
    def __init__(self, **attrs) -> None:
        self.__dict__.update(attrs)


@pytest.mark.parametrize(
    ("make", "outcomes"),
    [
        (_wrong_key, ["cannot decrypt"]),
        (_cut_in_half, ["cannot decrypt"]),
        (_dump_hash_changed, []),
        (_incompatible_release, ["incompatible"]),
        (_materialized_view_running_a_program, ["foreign objects"]),
    ],
    ids=["wrong key", "cut in half", "dump hash changed", "incompatible release",
         "materialized view running a program"],
)  # fmt: skip
def test_a_refused_restore_touches_nothing(
    shared_source, bundles, dbs, owner_key, tmp_path, private_tmp, make, outcomes
):
    key, recipient = owner_key
    marker = Marker(dbs.server)
    ctx = Refusal(dbs=dbs, good=bundles["good"], key=key, recipient=recipient, tmp=tmp_path,
                  marker=marker)  # fmt: skip
    bundle, identity = make(ctx)
    target = dbs.create()
    untouched = Untouched(
        dbs, shared_source["name"], target, [bundles["older"], bundles["good"], bundle], private_tmp
    )

    result = restore(bundle, identity, dbs.installation(target))

    output = refused(result, *outcomes)
    assert output.startswith("restore: ")
    assert "filippo.io" not in output  # age's own error, not its footer
    assert OUTCOMES["load failed"] not in output  # refused before pg_restore loads anything
    assert not marker.exists()
    untouched.check()


@pytest.mark.parametrize("case", ["target holds data", "not the superuser"])
def test_restore_never_replaces_an_installation_or_runs_as_another_role(
    shared_source, bundles, module_dbs, owner_key, case
):
    key, _ = owner_key
    name = shared_source["name"]
    before, a_id = table_rows(module_dbs, name), installation_id(module_dbs, name)
    env = owner_env(shared_source)
    if case == "not the superuser":
        env = {"DATABASE_URL": module_dbs.runtime_url(shared_source)}

    result = restore(bundles["good"], key, env)

    refused(result, "holds data" if case == "target holds data" else "superuser")
    assert table_rows(module_dbs, name) == before
    assert installation_id(module_dbs, name) == a_id


# --- L6: real Compose projects: back up A, restore into a new C, B keeps serving -------------


def _psql(project, sql: str) -> list[str]:
    out = project.compose("exec", "-T", "db", "psql", "-U", "farmcore", "-d", "farmcore",
                          "-v", "ON_ERROR_STOP=1", "-Atc", sql).stdout  # fmt: skip
    return out.splitlines()


@pytest.mark.slow
def test_a_backup_of_project_a_restores_into_a_new_project_c_while_b_keeps_serving(
    new_project, owner_key, tmp_path
):
    import threading

    from tests.instances.conftest import inspect

    key, recipient = owner_key
    a, b = new_project(BACKUP_RECIPIENT=recipient), new_project()
    a.up()
    b.up()
    for args in (("seed", "--scene", "B"), ("replay", "--to", "2024-04-10 06:00")):
        a.compose("exec", "-T", "app", "farmcore", *args)
    a_id, b_id = a.health()["installation_id"], b.health()["installation_id"]

    # The compose `backup` service (its daily run at start, then this one-off) writes bundles
    # into A's `backups` volume; the latest holds the seeded farm.
    a.compose("run", "--rm", "--no-deps", "-e", "BACKUP_ONCE=1", "backup")
    listed = a.compose("run", "--rm", "--no-deps", "--entrypoint", "ls", "backup", "/backups")
    names = sorted(listed.stdout.split())
    assert len(names) == 2, names
    assert all(re.fullmatch(r"farmcore-\d{8}T\d{6}Z\.age", n) for n in names), names
    # Copied off the server as the docs say, then made private as a user would keep them.
    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    a.compose("cp", f"backup:/backups/{names[-1]}", str(restore_dir / "a.age"))
    bundle = restore_dir / "a.age"
    manifest = json.loads(members(decrypt(bundle, key))["manifest.json"])
    assert manifest["installation_id"] == a_id
    shutil.copy(key, restore_dir / "owner.key")
    for path in restore_dir.iterdir():
        path.chmod(0o600)

    # A killed backup's plaintext temp directory is removed when the loop starts again.
    a.compose("exec", "-T", "backup", "mkdir", "/tmp/farmcore-backup-stale")
    a.compose("restart", "backup")
    deadline = time.monotonic() + 60
    while "farmcore-backup-stale" in a.compose("exec", "-T", "backup", "ls", "/tmp").stdout:
        assert time.monotonic() < deadline, "stale backup temp dir never removed"
        time.sleep(1)
    # B has no BACKUP_RECIPIENT: its backup service keeps running and says why, hourly.
    b_backup = inspect(b.container("backup"))
    assert b_backup["State"]["Running"] and b_backup["RestartCount"] == 0
    b_logs = b.compose("logs", "backup")
    assert "BACKUP_RECIPIENT" in b_logs.stdout + b_logs.stderr

    # B is probed every half second while C is created and restored.
    failures: list[str] = []
    done = threading.Event()

    def probe() -> None:
        while not done.is_set():
            try:
                assert b.health()["installation_id"] == b_id  # 503 raises
            except Exception as exc:  # noqa: BLE001 - any failure to serve counts
                failures.append(type(exc).__name__)
            time.sleep(0.5)

    watcher = threading.Thread(target=probe)
    watcher.start()
    try:
        c = new_project()
        c.compose("up", "-d", "--wait", "db")
        # Files the container's user (uid 10001) may not read: the Linux case. A root-owned
        # 0600 volume reproduces it on any host.
        volume = f"{c.name}_restore-probe"
        subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
        try:
            subprocess.run(
                ["docker", "run", "--rm", "--user", "0", "-v", f"{volume}:/r",
                 "-v", f"{restore_dir}:/src:ro", "--entrypoint", "sh", c.env["FARMCORE_IMAGE"],
                 "-c", "cp /src/a.age /src/owner.key /r/ && chmod 600 /r/*"],
                check=True, capture_output=True,
            )  # fmt: skip
            locked = c.compose(
                "run", "--rm", "--no-deps", "-v", f"{volume}:/restore:ro", "migrate",
                "farmcore", "restore", "/restore/a.age", "--identity", "/restore/owner.key",
                check=False,
            )  # fmt: skip
        finally:
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
        assert locked.returncode == 1
        assert "cannot read the bundle" in locked.stderr and "--user" in locked.stderr
        # The documented command: run as the user who owns the files.
        restored = c.compose(
            "run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{restore_dir}:/restore:ro", "migrate",
            "farmcore", "restore", "/restore/a.age", "--identity", "/restore/owner.key",
            check=False,
        )  # fmt: skip
        assert restored.returncode == 0, restored.stdout + restored.stderr
        c.up()
    finally:
        done.set()
        watcher.join()
    assert failures == []

    c_id = c.health()["installation_id"]
    assert len({a_id, b_id, c_id}) == 3
    effects = c.compose("run", "--rm", "--no-deps", "migrate", "farmcore", "effects")
    assert effects.stdout.strip() == "external effects: paused"
    for label, sql in SEMANTIC.items():
        assert _psql(c, sql) == _psql(a, sql), label
    assert len(_psql(c, SEMANTIC["records"])) == 10
    assert _psql(c, "SELECT count(*) FROM local_session") == ["0"]
    # A sign-in left halfway (a client stopped mid-handshake) holds up every DROP DATABASE, a
    # stopped restore's cleanup included, for this long: within `docker compose stop`'s 10 s.
    assert _psql(c, "SHOW authentication_timeout") == ["5s"]
    assert b.health()["installation_id"] == b_id  # 503 raises
    assert a.health()["installation_id"] == a_id  # 503 raises
