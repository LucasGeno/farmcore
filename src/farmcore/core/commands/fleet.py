"""`farmcore fleet plan`: report what a private fleet inventory declares, changing nothing."""

from __future__ import annotations

import json
from pathlib import Path

import typer


def register(cli: typer.Typer) -> None:
    fleet = typer.Typer(help="Operator tooling for a declared fleet of installations.")
    cli.add_typer(fleet, name="fleet")

    @fleet.callback()
    def _fleet() -> None:
        """Operator tooling for a declared fleet of installations."""

    @fleet.command("plan")
    def plan(
        inventory: Path = typer.Argument(..., help="Folder of <slug>.yaml declarations."),
        allow_domain_suffix: list[str] = typer.Option(
            [], "--allow-domain-suffix", help="Accept subdomains of this; repeatable."
        ),
        test_domains: bool = typer.Option(
            False, "--test-domains", help="Disposable fixtures: accept only <name>.example.test."
        ),
    ) -> None:
        """Print the declared fleet as JSON. Reads only the inventory folder."""
        # Here, not at module level: other commands and the web app never load hosting code.
        from farmcore.hosting.inventory import DomainPolicy, InventoryError
        from farmcore.hosting.plan import build_plan

        try:
            policy = DomainPolicy.chosen(test_domains=test_domains, suffixes=allow_domain_suffix)
            result = build_plan(inventory, policy)
        except InventoryError as exc:
            for problem in exc.problems:
                typer.echo(f"fleet plan: {problem}", err=True)
            raise typer.Exit(1) from None
        typer.echo(json.dumps(result, indent=2))
