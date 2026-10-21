"""`farmcore upgrade`: upgrade an installation with a way back.

Run with the new release, as the database owner, while the app and bot are stopped:

1. Take the installation's migration lock, or exit at once if another upgrade or migrate holds
   it (nothing is done then).
2. Check compatibility: a database whose schema this release does not know (a newer release
   migrated it) is refused before anything is written.
3. Take a pre-upgrade backup: one `farmcore backup` bundle, encrypted to the owner's key, in
   `--backup-dir`. If it cannot be written, nothing is migrated.
4. Run `migrate`'s steps once, on the locked connection. A failed or stopped migration rolls
   back and leaves the installation marked failed (`/health` says so) until a migrate or
   upgrade succeeds; the way back is restoring the pre-upgrade bundle.
5. Report the release, Alembic heads and state, as `/health` will.

Reverting the image never reverts the schema. `--check` only reports steps 2 and 5.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import create_engine

from farmcore import __version__
from farmcore.core.commands.backup import BundleError, create_bundle
from farmcore.core.commands.migrate import (
    MigrateError,
    ServerBusy,
    Stopped,
    check_compatible,
    check_owner,
    migrate_locked,
    migration_lock,
    stop_on_signals,
    stopped_message,
)
from farmcore.core.startup import SchemaState, read_heads, read_schema_state
from farmcore.settings import Settings, get_settings

RECOVERY = 'docs/self-hosting.md, "Recovering from a failed upgrade"'


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def describe(state: SchemaState) -> str:
    heads = ", ".join(state.heads) or "none"
    return f"release {__version__}, alembic heads {heads}, state {state.state}"


def check_upgrade(settings: Settings) -> SchemaState:
    """Steps 2 and 5 without the lock: what an upgrade would find. Changes nothing."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            check_owner(conn)
            return check_compatible(conn)
    finally:
        engine.dispose()


def upgrade_installation(
    settings: Settings, backup_dir: Path, recipients: list[str], echo
) -> tuple[SchemaState, SchemaState, str]:
    """Steps 1-5; (state before, state after, installation id). `echo` reports progress."""
    with migration_lock(settings, wait=False) as conn:
        before = check_compatible(conn)
        if not before.heads:
            raise MigrateError(
                "this database was never migrated: set up a new installation with "
                "`farmcore migrate`; nothing was done"
            )
        bundle = backup_dir / f"pre-upgrade-{_stamp()}.age"
        umask = os.umask(0o077)  # owner-only, like the daily bundles
        try:
            create_bundle(settings.database_url, recipients, bundle)
        except BundleError as exc:
            reason = str(exc).rstrip(".")
            raise MigrateError(
                f"pre-upgrade backup failed: {reason}; nothing was migrated"
            ) from None
        finally:
            os.umask(umask)
        echo(f"upgrade: pre-upgrade backup {bundle}")
        try:
            installation_id = migrate_locked(conn, settings)
        except ServerBusy as exc:
            raise MigrateError(
                f"{exc}, after migrating to {', '.join(read_heads(conn)) or 'no revision'}. Run "
                "`farmcore migrate` again to finish setting up the runtime role; the "
                "installation says failed until then"
            ) from None
        except MigrateError as exc:
            raise MigrateError(
                f"{exc}. The installation is marked failed; to go back, restore {bundle} "
                f"({RECOVERY})"
            ) from None
        after = read_schema_state(conn)
        conn.rollback()
    return before, after, installation_id


def register(cli: typer.Typer) -> None:
    @cli.command()
    def upgrade(
        backup_dir: Path | None = typer.Option(
            None, "--backup-dir", help="Directory for the pre-upgrade bundle (kept, never pruned)."
        ),
        recipient: list[str] = typer.Option(
            [],
            "--recipient",
            "-r",
            envvar="BACKUP_RECIPIENT",
            help="The owner's age public key (age1...), as for `farmcore backup`.",
        ),
        check: bool = typer.Option(
            False, "--check", help="Only check compatibility and report; change nothing."
        ),
    ) -> None:
        """Back up, then migrate this installation (DATABASE_URL, as the owner) to this release.

        Stop the app and bot first. One upgrade at a time; a database from a newer release is
        refused before anything is written."""
        settings = get_settings()
        try:
            if check:
                state = check_upgrade(settings)
                typer.echo(describe(state))
                typer.echo(
                    f"upgrade: compatible ({state.reason}); this release expects "
                    f"{', '.join(state.expected)}"
                )
                return
            if backup_dir is None:
                raise MigrateError("give --backup-dir for the pre-upgrade backup; nothing was done")
            with stop_on_signals():
                before, after, installation_id = upgrade_installation(
                    settings, backup_dir, recipient, typer.echo
                )
        except MigrateError as exc:
            typer.echo(f"upgrade: {exc}", err=True)
            raise typer.Exit(1) from None
        except Stopped as exc:
            typer.echo(f"upgrade: {stopped_message(exc)}", err=True)
            raise typer.Exit(1) from None
        if before.heads == after.heads:
            typer.echo(f"upgrade: already at {', '.join(after.heads)}")
        else:
            typer.echo(f"upgrade: migrated {', '.join(before.heads)} -> {', '.join(after.heads)}")
        typer.echo(describe(after))
        typer.echo(f"installation {installation_id}")
