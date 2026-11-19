"""Beacon's callables. `ping` (the durable task) writes `began-<attempt>`, waits for a `release`
file (bounded), tries to publish and writes `decided-<attempt>` = published | fenced."""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import select

from farmcore.core import tasks
from farmcore.core.models.canonical import Farm

router = APIRouter()
SWEEPS: list[str] = []


def _dir() -> Path:
    return Path(os.environ["LIFECYCLE_DIR"])


@router.get("/ping")
def page() -> dict[str, str]:
    return {"app": "beacon"}


def plan(ctx) -> None:
    farms = list(ctx.session.scalars(select(Farm.id)))
    tasks.enqueue_due(ctx.session, "beacon.ping", timedelta(0), farms)


def ping(ctx) -> None:
    n = ctx.attempt.number
    (_dir() / f"began-{n}").touch()
    deadline = time.monotonic() + 15
    while not (_dir() / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    with ctx.session_factory.begin() as session:
        published = tasks.publish_if_current(session, ctx.attempt)
    (_dir() / f"decided-{n}").write_text("published" if published else "fenced")


def sweep(ctx) -> None:
    SWEEPS.append("beacon")


def status(ctx):
    from farmcore.core.tools import ToolResult

    return ToolResult(facts={}, text="beacon is on")
