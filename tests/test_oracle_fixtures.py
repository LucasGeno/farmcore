"""Oracle fixtures: stored outputs of copyleft implementations, generated out of process.

`tools/oracles` runs GPL/AGPL implementations in an optional container and writes the JSON
files under `tests/fixtures/oracles`. Tests only read those files. The package must never
import the harness, and every fixture must say where its numbers came from.
"""

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "farmcore"
FIXTURES = ROOT / "tests" / "fixtures" / "oracles"


def harness_references(source: str) -> set[str]:
    """Imports of the top-level `tools` package, and string paths into the harness."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "tools"}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "tools":
                found.add(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= {
                node.value for marker in ("tools.oracles", "tools/oracles") if marker in node.value
            }
    return found


def test_the_package_imports_nothing_from_the_oracle_harness():
    planted = "from tools.oracles import degday\nimportlib.import_module('tools.oracles.x')\n"
    assert harness_references(planted) == {"tools.oracles", "tools.oracles.x"}
    assert not harness_references("from farmcore.tools import x\nimport toolsy\n")

    leaks = {
        str(path.relative_to(SRC)): refs
        for path in sorted(SRC.rglob("*.py"))
        if (refs := harness_references(path.read_text(encoding="utf-8")))
    }
    assert not leaks, f"src/farmcore must not use the copyleft oracle harness: {leaks}"


# Imports every farmcore module with the repository root importable, recording any attempt to
# import the top-level `tools` package, however the name was built. Then plants a constructed
# import as the control.
RUNTIME_PROBE = """
import importlib, json, pkgutil, sys

attempts = []

class Watch:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "tools":
            attempts.append(name)

sys.meta_path.insert(0, Watch())
import farmcore

failed = []
for info in pkgutil.walk_packages(farmcore.__path__, "farmcore.", onerror=failed.append):
    try:
        importlib.import_module(info.name)
    except Exception as e:
        failed.append(f"{info.name}: {e!r}")
package = sorted(set(attempts))
try:
    importlib.import_module("to" + "ols" + ".oracles")
except ImportError:
    pass
print(json.dumps({"failed": failed, "package": package, "control": sorted(set(attempts))}))
"""


@pytest.mark.slow  # imports all of src/farmcore in a fresh interpreter (several seconds)
def test_importing_every_package_module_never_loads_the_oracle_harness():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")])}
    run = subprocess.run(
        [sys.executable, "-c", RUNTIME_PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["control"], "the probe did not see a constructed import of tools.oracles"
    assert not result["failed"], f"modules that could not be imported: {result['failed']}"
    assert not result["package"], f"src/farmcore imports the oracle harness: {result['package']}"


def test_every_oracle_fixture_records_its_generator_licence_parameters_and_inputs():
    fixtures = sorted(FIXTURES.rglob("*.json"))
    assert fixtures, "no oracle fixtures found"
    for path in fixtures:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        where = path.relative_to(FIXTURES)
        generator = fixture.get("generator", {})
        for key in ("name", "version", "licence"):
            assert generator.get(key), f"{where}: generator.{key} is missing"
        for key in ("parameters", "inputs", "outputs"):
            assert fixture.get(key), f"{where}: {key} is missing"
        if shared := fixture["inputs"].get("file"):
            digest = hashlib.sha256((FIXTURES / shared).read_bytes()).hexdigest()
            assert digest == fixture["inputs"]["sha256"], f"{where}: {shared} has changed"
