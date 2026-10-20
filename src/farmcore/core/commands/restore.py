"""`farmcore restore BUNDLE --identity KEY`: restore a backup as a new, independent installation.

Order matters; the target database is touched only after step 3:

1. Decrypt the whole bundle with the owner's age identity into a private temporary directory.
   age authenticates every chunk, so a wrong key, a modified byte or a truncated file fails
   here.
2. Read exactly the two expected members, check the manifest (format, fields, dump size and
   SHA-256), check that the dump's own schema revision is the one the manifest names, and that
   this release knows that revision (else the bundle comes from an incompatible release).
3. Check the target: run as the server's superuser, and the database holds no data of its own
   (only extension objects, as a new installation's database does). Restore never replaces an
   installation.
4. Recreate the target database empty, closed to everyone but the owner, with external
   effects paused, then `pg_restore --no-owner --no-acl --single-transaction` into it as the
   owner: every entry except the rows of member sessions and sign-in throttling, so no session
   of the original is ever valid in the copy, even if the restore stops halfway.
5. Run the `migrate` steps: upgrade an older schema, give the copy a new installation id
   (database settings are never part of a dump, so the original's id does not travel) and
   grant the runtime role, the copy's own, with this installation's password. Only then can
   the app connect.

The source installation and the bundle are only ever read. Temporary plaintext is removed on
success and failure alike.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

import typer
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from farmcore.core.commands.backup import (
    DUMP_NAME,
    FORMAT,
    MANIFEST_NAME,
    BundleError,
    _last_line,
    database_error,
    libpq_env,
    tool,
)
from farmcore.core.commands.effects import set_external_effects
from farmcore.core.commands.migrate import (
    MigrateError,
    alembic_config,
    check_owner,
    migrate_installation,
)
from farmcore.core.startup import EFFECTS_PAUSED
from farmcore.settings import Settings, get_settings

MANIFEST_MAX_BYTES = 1 << 20
_REQUIRED = {
    "format": str,
    "installation_id": str,
    "release": str,
    "modules": dict,
    "alembic_heads": list,
    "created_at": str,
    "dump": dict,
}
# Relations outside the system schemas that no extension owns: the target's own data.
_OWN_RELATIONS = text(
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
    "AND n.nspname NOT LIKE 'pg_temp%' AND n.nspname NOT LIKE 'pg_toast_temp%' "
    "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND NOT EXISTS (SELECT 1 FROM pg_depend d "
    "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
)


def decrypt(bundle: Path, identity: Path, out: Path) -> None:
    if not bundle.is_file():
        raise BundleError(f"no bundle at {bundle}")
    done = subprocess.run(
        [tool("age"), "-d", "-i", str(identity), "-o", str(out), str(bundle)], capture_output=True
    )
    if done.returncode != 0:
        raise BundleError(
            "cannot decrypt the bundle (wrong key, or the file is modified or truncated): "
            + _last_line(done.stderr)
        )


def unpack(plain: Path, into: Path) -> tuple[dict, Path]:
    """The manifest and the dump's path; exactly the two expected regular members."""
    try:
        with tarfile.open(plain, mode="r:") as tar:
            found = tar.getmembers()
            names = [m.name for m in found]
            if names != [MANIFEST_NAME, DUMP_NAME] or not all(m.isreg() for m in found):
                raise BundleError("not a FarmCore backup: unexpected contents")
            if found[0].size > MANIFEST_MAX_BYTES:
                raise BundleError("not a FarmCore backup: manifest too large")
            raw = tar.extractfile(found[0]).read()
            dump = into / DUMP_NAME
            with tar.extractfile(found[1]) as src, dump.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
    except tarfile.TarError:
        raise BundleError("not a FarmCore backup: unreadable archive") from None
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise BundleError("not a FarmCore backup: unreadable manifest") from None
    return manifest, dump


def check_manifest(manifest: object, dump: Path) -> dict:
    if not isinstance(manifest, dict):
        raise BundleError("not a FarmCore backup: unreadable manifest")
    if manifest.get("format") != FORMAT:
        raise BundleError(f"unsupported bundle format {manifest.get('format')!r}")
    for key, kind in _REQUIRED.items():
        if not isinstance(manifest.get(key), kind):
            raise BundleError(f"manifest field {key!r} is missing or invalid")
    meta = manifest["dump"]
    digest = hashlib.sha256()
    with dump.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if (
        meta.get("name") != DUMP_NAME
        or meta.get("bytes") != dump.stat().st_size
        or meta.get("sha256") != digest.hexdigest()
    ):
        raise BundleError("the dump does not match the manifest (size or SHA-256)")
    heads = manifest["alembic_heads"]
    if not heads or not all(isinstance(h, str) for h in heads):
        raise BundleError("manifest field 'alembic_heads' is missing or invalid")
    return manifest


def _dump_heads(dump: Path) -> list[str]:
    """The schema revisions recorded inside the dump itself (its `alembic_version` rows)."""
    data = subprocess.run(
        [tool("pg_restore"), "--data-only", "--table=alembic_version", "-f", "-", str(dump)],
        capture_output=True,
    )
    if data.returncode != 0:
        raise BundleError(f"the dump is unreadable: {_last_line(data.stderr)}")
    sql = data.stdout.decode(errors="replace")
    rows = re.search(r"^COPY public\.alembic_version .*?;\n(.*?)^\\\.$", sql, re.M | re.S)
    return sorted(rows.group(1).split()) if rows else []


def check_compatible(manifest: dict, dump: Path) -> None:
    heads = sorted(manifest["alembic_heads"])
    if _dump_heads(dump) != heads:
        raise BundleError("the dump's schema revision does not match the manifest")
    script = ScriptDirectory.from_config(alembic_config())
    for head in heads:
        try:
            known = script.get_revision(head)
        except CommandError:
            known = None
        if known is None:
            raise BundleError(
                f"incompatible release: the bundle comes from FarmCore {manifest['release']} "
                f"(schema {', '.join(heads)}), which this release does not know; restore it "
                "with that release or a later one"
            )


def check_target(settings: Settings) -> str:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            me = check_owner(conn)
            own = conn.execute(_OWN_RELATIONS).scalar_one()
    finally:
        engine.dispose()
    if own:
        database = make_url(settings.database_url).database
        raise BundleError(
            f"database {database} already holds data ({own} relations); restore only into a new "
            "installation's empty database (start only its `db` service)"
        )
    return me


def recreate(settings: Settings, owner: str) -> None:
    url = make_url(settings.database_url)
    maintenance = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as conn:
            q = conn.dialect.identifier_preparer.quote
            db = q(url.database)
            conn.exec_driver_sql(f"DROP DATABASE {db} WITH (FORCE)")
            conn.exec_driver_sql(f"CREATE DATABASE {db} OWNER {q(owner)} TEMPLATE template0")
            conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC")
    finally:
        maintenance.dispose()
    # Paused before any data arrives: a copy is never able to send, even if restore stops here.
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            set_external_effects(conn, EFFECTS_PAUSED)
    finally:
        engine.dispose()


# Rows that never travel into a copy: member sessions (the copy starts with none) and
# sign-in throttling. Their tables are restored empty.
_LEFT_BEHIND = re.compile(r"^\d+; \d+ \d+ TABLE DATA public (local_session|login_bucket) ")


def pg_restore(settings: Settings, dump: Path) -> None:
    """Restore every entry of the dump except the rows in `_LEFT_BEHIND`, in one transaction."""
    pg = tool("pg_restore")
    listed = subprocess.run([pg, "--list", str(dump)], capture_output=True, text=True)
    if listed.returncode != 0:
        raise BundleError(f"the dump is unreadable: {_last_line(listed.stderr.encode())}")
    entries = dump.with_name("restore.list")
    entries.write_text(
        "".join(ln + "\n" for ln in listed.stdout.splitlines() if not _LEFT_BEHIND.match(ln))
    )
    done = subprocess.run(
        [pg, "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction",
         "--use-list", str(entries), "-d", make_url(settings.database_url).database, str(dump)],
        env=libpq_env(settings.database_url), capture_output=True,
    )  # fmt: skip
    if done.returncode != 0:
        raise BundleError(f"pg_restore failed: {_last_line(done.stderr)}")


def restore_bundle(settings: Settings, bundle: Path, identity: Path) -> tuple[dict, str]:
    """Verify `bundle` completely, then restore it into the empty target; (manifest, new id)."""
    with tempfile.TemporaryDirectory(prefix="farmcore-restore-") as tmp:
        plain = Path(tmp) / "bundle.tar"
        decrypt(bundle, identity, plain)
        manifest, dump = unpack(plain, Path(tmp))
        plain.unlink()
        check_manifest(manifest, dump)
        check_compatible(manifest, dump)
        owner = check_target(settings)
        recreate(settings, owner)
        pg_restore(settings, dump)
    new_id = migrate_installation(settings)
    return manifest, new_id


def register(cli: typer.Typer) -> None:
    @cli.command()
    def restore(
        bundle: Path = typer.Argument(..., help="Bundle written by `farmcore backup`."),
        identity: Path = typer.Option(
            ..., "--identity", "-i", help="The owner's age identity (private key) file."
        ),
    ) -> None:
        """Restore a backup into this new installation's empty database (DATABASE_URL, owner)."""
        settings = get_settings()
        try:
            manifest, new_id = restore_bundle(settings, bundle, identity)
        except (BundleError, MigrateError) as exc:
            typer.echo(f"restore: {exc}", err=True)
            raise typer.Exit(1) from None
        except DBAPIError as exc:
            typer.echo(f"restore: cannot use the database: {database_error(exc)}", err=True)
            raise typer.Exit(1) from None
        typer.echo(
            f"restore: backup of installation {manifest['installation_id']} "
            f"({manifest['created_at']}) restored"
        )
        typer.echo(f"installation {new_id}")
