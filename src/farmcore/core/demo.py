"""Shared demo preset contract. Apps declare objects via Manifest.demo_presets."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from farmcore.core.manifest import Manifest, import_string


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    clock_target: datetime  # aware Europe/Paris wall time
    scene: str | None = None  # module:attr -> apply(session, farm, now), idempotent

    def __post_init__(self) -> None:
        if (
            self.clock_target.utcoffset() is None
            or getattr(self.clock_target.tzinfo, "key", None) != "Europe/Paris"
        ):
            raise ValueError("preset clock_target must be aware Europe/Paris time")


def collect_presets(manifests: Iterable[Manifest]) -> list[Preset]:
    presets: list[Preset] = []
    keys: set[str] = set()
    for manifest in manifests:
        for path in manifest.demo_presets:
            preset = import_string(path)
            if not isinstance(preset, Preset):
                raise TypeError(f"{path} must resolve to a Preset")
            if preset.key in keys:
                raise ValueError(f"duplicate demo preset key: {preset.key}")
            keys.add(preset.key)
            presets.append(preset)
    return presets
