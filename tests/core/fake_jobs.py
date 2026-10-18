"""Fake job callables for clock/jobs tests, referenced by dotted path
(`JobSpec(func="tests.core.fake_jobs:record")`). Not a test module.

Each call appends `(label, ctx.now, clock now seen through ctx.session)` to `CALLS`; tests
clear it before use.
"""

from __future__ import annotations

import threading
from datetime import datetime

from farmcore.core.clock import get_clock
from farmcore.core.manifest import JobContext

CALLS: list[tuple[str, datetime, datetime]] = []


def _record(label: str, ctx: JobContext) -> None:
    CALLS.append((label, ctx.now, get_clock(ctx.session, ctx.settings).now()))


def record(ctx: JobContext) -> None:
    _record("record", ctx)


def record_b(ctx: JobContext) -> None:
    _record("record_b", ctx)


def fail(ctx: JobContext) -> None:
    _record("fail", ctx)
    raise RuntimeError("fake job failure")


# `slow` sets STARTED, then blocks until RELEASE (5 s cap), so a test can overlap two steps.
STARTED = threading.Event()
RELEASE = threading.Event()


def slow(ctx: JobContext) -> None:
    _record("slow", ctx)
    STARTED.set()
    RELEASE.wait(timeout=5)
