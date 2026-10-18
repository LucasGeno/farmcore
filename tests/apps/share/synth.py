"""Synthetic constants shared by the share tests."""

from __future__ import annotations

from datetime import UTC, datetime

from farmcore.core import logs

# 2024-04-10 08:00 Europe/Paris (CEST) = 06:00 UTC; the replay clock two days later.
T_SPRAY = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
NOW = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
VOICE = logs.SourceInput("voice", "tg:1:1", {"transcript": "synthetic"}, 0.9, logs.RANK_VOICE)
