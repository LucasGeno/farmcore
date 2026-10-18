"""`farmcore` CLI. Commands register themselves: every module in `farmcore.core.commands` and
every `farmcore.apps.<app>.commands` (module or package) exposes `register(cli)`."""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Iterator
from types import ModuleType

import typer

import farmcore.apps
import farmcore.core.commands

cli = typer.Typer(no_args_is_help=True, help="farmcore: farm data kernel and apps.")


@cli.callback()
def _root() -> None:
    """farmcore: farm data kernel and apps."""


def _command_modules() -> Iterator[ModuleType]:
    for info in sorted(pkgutil.iter_modules(farmcore.core.commands.__path__), key=lambda i: i.name):
        yield importlib.import_module(f"farmcore.core.commands.{info.name}")
    for info in sorted(pkgutil.iter_modules(farmcore.apps.__path__), key=lambda i: i.name):
        name = f"farmcore.apps.{info.name}.commands"
        if not info.ispkg or importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        yield module
        if hasattr(module, "__path__"):
            for sub in sorted(pkgutil.iter_modules(module.__path__), key=lambda i: i.name):
                yield importlib.import_module(f"{name}.{sub.name}")


for _module in _command_modules():
    if hasattr(_module, "register"):
        _module.register(cli)


def main() -> None:
    cli()
