"""Coverage for grant form validation through the share service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.share import config, service
from farmcore.core.access import ScopedRepository, ensure_owner_grant


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")


def test_grant_requires_a_purpose_and_a_timezone(session, farm):
    repo = _repo(session, farm)
    valid_to = datetime.now(UTC) + timedelta(days=2)
    base = {
        "grantee": "Co-op",
        "preset": config.DEFAULT_PRESET,
        "purpose": "advice",
        "valid_to": valid_to,
    }
    with pytest.raises(service.InvalidGrant, match="purpose"):
        service.create_grant(repo, **{**base, "purpose": "   "})
    with pytest.raises(service.InvalidGrant, match="longer than"):
        service.create_grant(repo, **{**base, "purpose": "x" * (config.PURPOSE_MAX_LEN + 1)})
    with pytest.raises(service.InvalidGrant, match="timezone"):
        service.create_grant(repo, **{**base, "valid_to": datetime(2030, 1, 1)})
