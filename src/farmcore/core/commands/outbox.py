"""`farmcore outbox`: list the deliveries whose outcome is unknown; `farmcore outbox resolve ID
sent|resend|drop` settles one after the operator checked the recipient's chat.

An ambiguous delivery may or may not have arrived (the worker died mid-send, Telegram gave no
answer, or a restore inherited it from the source). FarmCore never resends one on its own.
"""

from __future__ import annotations

import typer

from farmcore.core import outbox
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    group = typer.Typer(help="Deliveries outside the installation whose outcome is unknown.")
    cli.add_typer(group, name="outbox")

    @group.callback(invoke_without_command=True)
    def listing(ctx: typer.Context) -> None:
        """List ambiguous deliveries, oldest first (never their text)."""
        if ctx.invoked_subcommand is not None:
            return
        engine = make_engine(get_settings())
        try:
            with make_session_factory(engine)() as session:
                rows = outbox.ambiguous(session)
        finally:
            engine.dispose()
        typer.echo(f"{len(rows)} ambiguous deliveries")
        for row in rows:
            typer.echo(
                f"{row.id}  farm {row.farm_id}  {row.kind}  created {row.created_at.isoformat()}"
                f"  attempts {row.attempts}  {row.error or ''}".rstrip()
            )

    @group.command("resolve")
    def resolve(
        intent_id: int,
        resolution: str = typer.Argument(
            ..., help="sent (it arrived), resend (it did not) or drop (never send it)."
        ),
    ) -> None:
        """Settle one ambiguous delivery."""
        if resolution not in outbox.RESOLUTIONS:
            raise typer.BadParameter(f"one of {', '.join(outbox.RESOLUTIONS)}")
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                state = outbox.resolve(session, intent_id, resolution)
        finally:
            engine.dispose()
        if state is None:
            typer.echo(f"outbox: delivery {intent_id} is not ambiguous", err=True)
            raise typer.Exit(1)
        typer.echo(f"delivery {intent_id}: {state}")
