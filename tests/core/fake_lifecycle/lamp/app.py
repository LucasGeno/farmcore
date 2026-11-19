"""Lamp's callables: a live job that records itself, and an outbox provider that writes
`sent-<intent id>` under `$LIFECYCLE_DIR` for each delivery."""

from __future__ import annotations

import os
from pathlib import Path

from tests.core.fake_lifecycle.beacon.app import SWEEPS


def sweep(ctx) -> None:
    SWEEPS.append("lamp")


class Notes:
    def deliver(self, intent) -> dict:
        (Path(os.environ["LIFECYCLE_DIR"]) / f"sent-{intent.id}").touch()
        return {"ok": True}


def provider(settings) -> Notes:
    return Notes()
