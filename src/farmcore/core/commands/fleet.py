"""`farmcore fleet plan`: report what a private fleet inventory declares, changing nothing.
`farmcore fleet apply|status`: provision one declared installation, or read its live state."""

from __future__ import annotations

import json
from collections.abc import Callable
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

    def _report(
        command: str,
        run: Callable[..., dict],
        inventory: Path,
        slug: str,
        allow_domain_suffix: list[str],
        test_domains: bool,
        *arguments: object,
    ) -> None:
        """The declaration of `slug` handed to `run`; its report, or the refusal, and exit code."""
        from farmcore.hosting.apply import READY, Refused
        from farmcore.hosting.inventory import DomainPolicy, InventoryError, read_inventory

        try:
            policy = DomainPolicy.chosen(test_domains=test_domains, suffixes=allow_domain_suffix)
            declared = {item.slug: item for item in read_inventory(inventory, policy)}
            if slug not in declared:
                raise Refused("that slug is not declared in this inventory")
            report = run(declared[slug], *arguments)
        except InventoryError as exc:
            for problem in exc.problems:
                typer.echo(f"fleet {command}: {problem}", err=True)
            raise typer.Exit(1) from None
        except Refused as exc:
            typer.echo(f"fleet {command}: {exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(0 if report["state"] == READY else 3)

    @fleet.command("apply")
    def apply(
        inventory: Path = typer.Argument(..., help="Folder of <slug>.yaml declarations."),
        slug: str = typer.Argument(..., help="The one installation to apply."),
        state_root: Path = typer.Option(..., "--state-root", help="Private operator state."),
        allow_domain_suffix: list[str] = typer.Option(
            [], "--allow-domain-suffix", help="Accept subdomains of this; repeatable."
        ),
        test_domains: bool = typer.Option(
            False, "--test-domains", help="Disposable fixtures: accept only <name>.example.test."
        ),
        readiness_seconds: int = typer.Option(
            180, "--readiness-seconds", min=1, help="How long to wait for readiness."
        ),
    ) -> None:
        """Provision or re-enter one declared installation; print its report as JSON.

        Exit 0 ready, 1 refused (nothing changed), 3 started but not ready."""
        from farmcore.hosting.apply import apply_installation

        _report(
            "apply", apply_installation, inventory, slug, allow_domain_suffix, test_domains,
            state_root, readiness_seconds,
        )  # fmt: skip

    @fleet.command("status")
    def status(
        inventory: Path = typer.Argument(..., help="Folder of <slug>.yaml declarations."),
        slug: str = typer.Argument(..., help="The one installation to read."),
        state_root: Path = typer.Option(..., "--state-root", help="Private operator state."),
        allow_domain_suffix: list[str] = typer.Option(
            [], "--allow-domain-suffix", help="Accept subdomains of this; repeatable."
        ),
        test_domains: bool = typer.Option(
            False, "--test-domains", help="Disposable fixtures: accept only <name>.example.test."
        ),
    ) -> None:
        """Report one installation's live state against its saved identity. Changes nothing.

        Exit 0 ready, 1 refused, 3 not ready."""
        from farmcore.hosting.apply import installation_status

        _report(
            "status", installation_status, inventory, slug, allow_domain_suffix, test_domains,
            state_root,
        )  # fmt: skip
