import pytest
from sqlalchemy import select

from farmcore.core import registry
from farmcore.core.commands.seed import run_seeds
from farmcore.core.models.canonical import Farm


@pytest.fixture
def spanish(session, data_dir):
    run_seeds(session, registry.discover(packages=("farmcore.apps",)), scene="B", data_dir=data_dir)
    session.commit()
    return session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
