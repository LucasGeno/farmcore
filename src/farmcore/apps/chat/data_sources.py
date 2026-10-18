"""Telegram provenance. Typed messages also use the canonical voice source type."""

from farmcore.apps.operations.data_sources import receipt_summary


def summary(repo, now, parcel_id=None):
    return receipt_summary(
        repo,
        now,
        parcel_id,
        source_type="voice",
        key="chat",
        label="Telegram notes",
        unit="notes",
        note="Voice and typed Telegram notes; time is confirmation availability.",
    )
