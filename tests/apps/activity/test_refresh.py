"""Both feeds poll fragments so confirmations from the bot process appear without navigation."""

import pytest

from farmcore.core import logs
from tests.core.test_activity import NOW, _spray, farm  # noqa: F401


@pytest.mark.parametrize("path", ["/activity/", "/activity/today"])
def test_feed_polls_a_fragment_and_shows_a_new_confirmation(client, session, farm, path):  # noqa: F811
    initial = client.get(path).text
    assert "data-poll-feed" in initial
    assert "every 12s [!document.hidden]" in initial
    pending = _spray(session, farm, ["P1"], NOW, pending=True)
    logs.confirm(session, pending.id, "chat:42", NOW)
    session.commit()
    response = client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Spray confirmed on P1" in response.text
    assert 'hx-swap="outerHTML"' in response.text


def test_history_page_does_not_poll_or_drop_its_cursor(client, farm):  # noqa: F811
    page = client.get("/activity/", params={"before": NOW.isoformat()}).text
    assert 'id="activity-feed"' in page
    assert "data-poll-feed" not in page


def test_polling_preserves_farm_context_and_selected_columns(client, session, farm):  # noqa: F811
    _spray(session, farm, ["P1"], NOW)
    session.commit()
    client.get("/activity/columns", params=[("show", "who")])
    response = client.get(f"/activity/?farm={farm.id}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert f'hx-get="/activity/?farm={farm.id}"' in response.text
    assert 'data-col="who"' in response.text
    assert 'data-col="product"' not in response.text
