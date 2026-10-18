"""Register checks, demo scene: `farmcore seed --scene B`, replay to 10 Apr 2024, the P5/P6 voice
copper spray confirmed (2 kg/ha), then the register cards show the copper and label checks."""

from __future__ import annotations

import html
import re

import pytest

from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


def _lines(body: str) -> list[tuple[str, str, str]]:
    found = re.findall(
        r'<li data-check="([^"]+)" data-status="([^"]+)"[^>]*>(.*?)</li>', body, re.S
    )
    return [
        (k, s, " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split())) for k, s, t in found
    ]


@pytest.mark.data
def test_scene_b_voice_spray_shows_a_copper_tick_then_9_may_adds_to_it(
    client, session, settings, seeded, step
) -> None:
    seeded("B")
    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 06:00"), "copper-contact")

    lines = _lines(client.get("/operations/register").text)

    # Two voice sprays (one per parcel), the 9 May seed spray is still ahead of the clock.
    copper = [line for line in lines if line[0] == "copper"]
    assert copper == [("copper", "ok", "OK Copper 0.4 of 4 kg Cu/ha this year")] * 2
    assert sum(1 for k, s, t in lines if k.startswith("applications:") and "1 of 5" in t) == 2

    # Scene B's 9 May copper spray (one log per parcel) adds 0.4 on every parcel: P5 and P6 are at
    # 0.8 kg Cu/ha, the other parcels at 0.4.
    step("2024-05-09 09:00")
    lines = _lines(client.get("/operations/register").text)
    copper = sorted(t for k, s, t in lines if k == "copper")
    assert {s for k, s, t in lines if k == "copper"} == {"ok"}
    assert copper.count("OK Copper 0.8 of 4 kg Cu/ha this year") == 2
    assert copper.count("OK Copper 0.4 of 4 kg Cu/ha this year") == len(copper) - 2 > 2
