"""Season gate: before the dated shoots-≥-10-cm assumption, nothing is evaluated."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.types import RuleResult

RULE = "season_gate"


def local_midnight_utc(day: date, tz: str) -> datetime:
    """Start of a local calendar day, as UTC."""
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def gate_start(config: AdvisorConfig) -> datetime:
    return local_midnight_utc(config.season_gate.value, config.local_tz)


def season_gate(config: AdvisorConfig, now: datetime) -> tuple[bool, RuleResult]:
    """(season started, explained reason)."""
    start = gate_start(config)
    started = now >= start
    result = config.explain(
        RULE,
        ("season_gate",),
        {"now": now, "gate_start_utc": start},
        "season started" if started else "season not started",
        f"shoots ≥ 10 cm assumed from {config.season_gate.value.isoformat()}",
    )
    return started, result
