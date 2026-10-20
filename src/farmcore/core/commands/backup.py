"""`farmcore backup`: write the installation as one file encrypted to the owner's age key.

A bundle is an `age` file (https://age-encryption.org) whose plaintext is a tar archive of
exactly two members, in this order:

- `manifest.json`: format, installation id, release, app module versions and states, Alembic
  heads, creation time (UTC), and the dump's name, size and SHA-256;
- `farmcore.dump`: `pg_dump` custom format of the installation database, without privileges:
  it names no runtime role (the owner's role name is recorded; restore ignores it).

The manifest and the dump come from one database snapshot. Everything private lives in the
database (no file store), so the dump is the whole installation. It never contains a password,
token or key of the platform or its operator: those are environment variables, not data, and
database roles are not part of a database dump. The plaintext exists only in a private
temporary directory that is removed afterwards; the bundle appears under its name only once
complete. Credentials reach `pg_dump` through its environment, never its command line.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import typer
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from farmcore.core import registry
from farmcore.core.startup import read_installation_id
from farmcore.settings import get_settings

FORMAT = "farmcore-backup/1"
MANIFEST_NAME = "manifest.json"
DUMP_NAME = "farmcore.dump"
APP_PACKAGES: tuple[str, ...] = ("farmcore.apps",)


class BundleError(RuntimeError):
    """A backup or restore refusal, reported in one line (never with a credential)."""


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BundleError(f"`{name}` is not installed (the FarmCore image ships it).")
    return path


def libpq_env(database_url: str) -> dict[str, str]:
    """The process environment for pg_dump/pg_restore: the connection in PG* variables."""
    url = make_url(database_url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for key, value in (
        ("PGHOST", url.host),
        ("PGPORT", url.port),
        ("PGUSER", url.username),
        ("PGPASSWORD", url.password),
        ("PGDATABASE", url.database),
    ):
        if value is not None:
            env[key] = str(value)
    return env


def database_error(exc: DBAPIError) -> str:
    """The server's or libpq's message, never the statement or the URL (libpq never prints a
    password)."""
    diag = getattr(exc.orig, "diag", None)
    primary = getattr(diag, "message_primary", None)
    return primary or _last_line(str(exc.orig).encode())


def _last_line(stderr: bytes) -> str:
    lines = [ln for ln in stderr.decode(errors="replace").splitlines() if ln.strip()]
    return lines[-1] if lines else "no message"


def tool_error(stderr: bytes) -> str:
    """A tool's own error line (`age: error: ...`, `pg_dump: error: ...`), not a footer such as
    age's "report unexpected errors" line; the last line when none says `error:`."""
    lines = [ln for ln in stderr.decode(errors="replace").splitlines() if ln.strip()]
    errors = [ln for ln in lines if "error:" in ln]
    return errors[0] if errors else _last_line(stderr)


def _major(version_text: str) -> int | None:
    match = re.search(r"\(PostgreSQL\) (\d+)", version_text)
    return int(match.group(1)) if match else None


def check_pg_dump(pg_dump: str, conn: Connection) -> None:
    """pg_dump refuses a newer server; say so before trying, with both versions."""
    client = _major(subprocess.run([pg_dump, "--version"], capture_output=True, text=True).stdout)
    server = conn.execute(text("SELECT current_setting('server_version_num')::int")).scalar_one()
    if client is not None and server // 10000 > client:
        raise BundleError(
            f"the database runs PostgreSQL {server // 10000} but this is pg_dump {client}: back "
            f"up with a FarmCore image whose PostgreSQL client is {server // 10000} or later"
        )


def check_output(output: Path, recipients: list[str]) -> None:
    if not recipients:
        raise BundleError("give the owner's age recipient (--recipient or BACKUP_RECIPIENT).")
    if not all(r.startswith("age1") and r.isascii() and " " not in r for r in recipients):
        # Never echo the value: someone may have pasted their private key.
        raise BundleError("recipients must be age public keys (age1...); SSH keys are not used")
    if output.exists():
        raise BundleError(f"{output} already exists; backups never overwrite a file.")
    parent = output.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise BundleError(f"cannot write into {parent}: no such directory, or no permission")


def _manifest_facts(conn: Connection) -> dict:
    heads = sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    ordered = registry.discover(packages=APP_PACKAGES)
    with Session(bind=conn) as session:
        states = registry.read_states(session, ordered)
    return {
        "installation_id": read_installation_id(conn),
        "release": version("farmcore"),
        "modules": {m.name: {"version": m.version, "state": states[m.name]} for m in ordered},
        "alembic_heads": heads,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add(tar: tarfile.TarFile, name: str, path: Path, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mtime = int(mtime)
    info.mode = 0o600
    with path.open("rb") as f:
        tar.addfile(info, f)


def _encrypt(proc: subprocess.Popen, manifest: Path, dump: Path, mtime: float) -> bytes:
    """Stream the tar into age and wait for it; age's stderr."""
    stdin = proc.stdin
    if stdin is None:  # pragma: no cover - Popen(stdin=PIPE) always sets it
        raise BundleError("age failed: no input pipe")
    # age stops reading when it fails (say, a malformed recipient); its error says why.
    with (
        contextlib.suppress(BrokenPipeError),
        tarfile.open(fileobj=stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar,
    ):
        _add(tar, MANIFEST_NAME, manifest, mtime)
        _add(tar, DUMP_NAME, dump, mtime)
    with contextlib.suppress(BrokenPipeError):
        stdin.close()
    stderr = proc.stderr.read() if proc.stderr else b""
    proc.wait()
    return stderr


def create_bundle(database_url: str, recipients: list[str], output: Path) -> dict:
    """Dump the database and write the encrypted bundle to `output`; return the manifest."""
    check_output(output, recipients)
    age, pg_dump = tool("age"), tool("pg_dump")
    engine = create_engine(database_url, isolation_level="REPEATABLE READ")
    partial = output.with_name(output.name + ".partial")
    try:
        with tempfile.TemporaryDirectory(prefix="farmcore-backup-") as tmp:
            dump = Path(tmp) / DUMP_NAME
            try:
                with engine.connect() as conn:
                    check_pg_dump(pg_dump, conn)
                    # One snapshot for the manifest and the dump.
                    snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                    facts = _manifest_facts(conn)
                    done = subprocess.run(
                        [pg_dump, "-Fc", "--no-acl", f"--snapshot={snapshot}", "-f", str(dump)],
                        env=libpq_env(database_url), capture_output=True,
                    )  # fmt: skip
                    conn.rollback()
            except DBAPIError as exc:
                raise BundleError(f"cannot read the database: {database_error(exc)}") from None
            if done.returncode != 0:
                raise BundleError(f"pg_dump failed: {tool_error(done.stderr)}")
            created = datetime.now(UTC)
            manifest = {
                "format": FORMAT,
                **facts,
                "created_at": created.isoformat(),
                "dump": {"name": DUMP_NAME, "bytes": dump.stat().st_size, "sha256": _sha256(dump)},
            }
            manifest_path = Path(tmp) / MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            args = [age]
            for r in recipients:
                args += ["-r", r]
            proc = subprocess.Popen(
                [*args, "-o", str(partial)], stdin=subprocess.PIPE, stderr=subprocess.PIPE
            )
            try:
                stderr = _encrypt(proc, manifest_path, dump, created.timestamp())
            except BaseException:  # pragma: no cover - Ctrl-C mid-stream: never leave age running
                proc.kill()
                proc.wait()
                raise
            if proc.returncode != 0:
                raise BundleError(f"age failed: {tool_error(stderr)}")
        with partial.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(partial, output)
        return manifest
    finally:
        partial.unlink(missing_ok=True)
        engine.dispose()


def register(cli: typer.Typer) -> None:
    @cli.command()
    def backup(
        output: Path = typer.Option(..., "--output", "-o", help="Bundle file to write (new)."),
        recipient: list[str] = typer.Option(
            [],
            "--recipient",
            "-r",
            envvar="BACKUP_RECIPIENT",
            help="The owner's age public key (age1...); repeat for several (space-separated in "
            "BACKUP_RECIPIENT).",
        ),  # fmt: skip
    ) -> None:
        """Write the installation (DATABASE_URL, as the owner) to one encrypted bundle."""
        settings = get_settings()
        try:
            manifest = create_bundle(settings.database_url, recipient, output)
        except BundleError as exc:
            typer.echo(f"backup: {exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"backup: wrote {output} (installation {manifest['installation_id']})")
