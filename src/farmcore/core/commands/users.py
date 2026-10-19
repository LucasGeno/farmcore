"""Operator-local identity provisioning; passwords arrive only by prompt or stdin."""

import sys

import typer
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from farmcore.core.identity import canonical_login, hash_password, wall_now
from farmcore.core.models.canonical import Farm
from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def _password(stdin: bool) -> str:
    if stdin:
        password = sys.stdin.readline(1026).rstrip("\r\n")
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    return hash_password(password)


def register(cli: typer.Typer) -> None:
    users = typer.Typer(help="Local operator identity provisioning. No public signup.")
    cli.add_typer(users, name="users")

    @users.command("create")
    def create(login: str, farm_id: int | None = None, password_stdin: bool = False):
        """Create an identity, optionally bootstrap owner of the explicitly named farm."""
        engine = make_engine(get_settings())
        try:
            normalized = canonical_login(login)
            credential = _password(password_stdin)
            with session_scope(make_session_factory(engine)) as session:
                if farm_id is not None and session.get(Farm, farm_id) is None:
                    raise ValueError("No such farm.")
                identity = LocalIdentity(login=normalized, password_hash=credential)
                session.add(identity)
                session.flush()
                if farm_id is not None:
                    session.add(
                        FarmMembership(identity_id=identity.id, farm_id=farm_id, role="owner")
                    )
            typer.echo("Local identity created.")
        except (ValueError, IntegrityError) as exc:
            typer.echo(
                str(exc) if isinstance(exc, ValueError) else "Login already exists.", err=True
            )
            raise typer.Exit(1) from None
        finally:
            engine.dispose()

    @users.command("disable")
    def disable(login: str):
        """Disable an identity immediately, including all existing sessions."""
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                identity = session.scalar(
                    select(LocalIdentity).where(LocalIdentity.login == canonical_login(login))
                )
                if identity is None:
                    raise typer.BadParameter("No such local identity.")
                identity.disabled_at = wall_now()
            typer.echo("Identity disabled.")
        finally:
            engine.dispose()

    @users.command("revoke-sessions")
    def revoke_sessions(login: str):
        """Revoke every session without exposing a token."""
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                identity = session.scalar(
                    select(LocalIdentity).where(LocalIdentity.login == canonical_login(login))
                )
                if identity is None:
                    raise typer.BadParameter("No such local identity.")
                session.execute(
                    update(LocalSession)
                    .where(LocalSession.identity_id == identity.id)
                    .values(revoked_at=wall_now())
                )
            typer.echo("Sessions revoked.")
        finally:
            engine.dispose()

    @users.command("set-password")
    def set_password(login: str, password_stdin: bool = False):
        """Replace a password, revoke every session and re-enable the local identity."""
        engine = make_engine(get_settings())
        try:
            credential = _password(password_stdin)
            with session_scope(make_session_factory(engine)) as session:
                identity = session.scalar(
                    select(LocalIdentity)
                    .where(LocalIdentity.login == canonical_login(login))
                    .with_for_update()
                )
                if identity is None:
                    raise typer.BadParameter("No such local identity.")
                identity.password_hash = credential
                identity.disabled_at = None
                session.execute(
                    update(LocalSession)
                    .where(LocalSession.identity_id == identity.id)
                    .values(revoked_at=wall_now())
                )
            typer.echo("Password replaced; sessions revoked.")
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        finally:
            engine.dispose()
