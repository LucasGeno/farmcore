"""Shared app fixtures: the planning fixtures are reused by coverage-focused app tests."""

from __future__ import annotations

import pytest

from tests.apps.planning import conftest as _planning


@pytest.fixture
def now():
    return _planning.now.__wrapped__()


@pytest.fixture
def farm(session, settings):
    return _planning.farm.__wrapped__(session, settings)


@pytest.fixture
def parcels(session, farm):
    return _planning.parcels.__wrapped__(session, farm)


@pytest.fixture
def products(session):
    return _planning.products.__wrapped__(session)


@pytest.fixture
def advice(session, farm, parcels):
    return _planning.advice.__wrapped__(session, farm, parcels)
