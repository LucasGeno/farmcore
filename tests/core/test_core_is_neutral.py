"""Core holds no crop-protection terms (spec D6, AC 1): no contact/penetrant product type, no
downy/powdery target, no advice decision. `spray` names a record type only. Crop protection
(the advisor app) owns those words; core renders what apps contribute.

The guard reads every string constant in `src/farmcore/core` (Python) and every quoted SQL word
inside one. Allowed, each for its reason:
- `core/manifest.py`: `spray`, `protected` and `wait` are also the shell's icon names and colour
  tokens (`farmcore.css`), which every app's tiles use; they name a look, not a decision.
- `spray` as the record type: `LOG_TYPES` (canonical), the spray record's merge policy (logs)
  and the activity filter and icon for spray records (activity).
Blind spot: templates (`core/templates`, the shared UI kit's decision chip) are not read."""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import farmcore

CORE = Path(farmcore.__file__).parent / "core"
FORBIDDEN = {"contact", "penetrant", "downy", "powdery", "spray", "wait", "protected"}
ALLOWED = Counter(
    {
        ("manifest.py", "spray"): 2,  # icon name and colour token
        ("manifest.py", "protected"): 2,
        ("manifest.py", "wait"): 2,
        ("models/canonical.py", "spray"): 1,  # LOG_TYPES
        ("logs.py", "spray"): 1,  # the spray record's merge policy
        ("activity.py", "spray"): 4,  # the spray-record filter, its event type and icon
    }
)
_QUOTED = re.compile(r"'([a-z]+)'")


def _words(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN:
                found.append(node.value)
            found += [w for w in _QUOTED.findall(node.value) if w in FORBIDDEN]
    return found


def test_core_sources_hold_no_crop_protection_literal() -> None:
    found = Counter(
        (str(path.relative_to(CORE)), word)
        for path in sorted(CORE.rglob("*.py"))
        for word in _words(path)
    )

    assert found - ALLOWED == Counter(), "crop protection's words belong to the advisor app"
    assert ALLOWED - found == Counter(), "an allowance is no longer needed"
