from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.advisor.job import evaluate
from farmcore.apps.chat.service import load_context
from farmcore.core.access import repo_for_job
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice

pytestmark = [pytest.mark.data, pytest.mark.spanish_data]


def test_advisor_job_withholds_spain_instead_of_evaluating_an_empty_series(
    session, settings, spanish
):
    evaluate(JobContext(session, datetime(2024, 4, 8, 4, tzinfo=UTC), settings))
    session.flush()
    assert not session.scalar(select(Advice.id).where(Advice.farm_id == spanish.id))
    assert session.scalar(select(Advice.id).where(Advice.farm_id != spanish.id))


def test_spanish_chat_has_no_french_shortlist_but_french_context_keeps_it(
    session, settings, spanish
):
    from farmcore.core.access import default_farm

    now = datetime(2024, 4, 8, 4, tzinfo=UTC)
    es = load_context(repo_for_job(session, spanish.id, "test"), now, settings.display_tz)
    fr = load_context(
        repo_for_job(session, default_farm(session, settings).id, "test"), now, settings.display_tz
    )
    assert es.products == []
    assert len(fr.products) == 3
