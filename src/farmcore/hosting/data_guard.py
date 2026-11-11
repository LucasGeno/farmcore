"""The data guard's fleet check, run by `scripts/check.sh`: no private declaration in git.

Staged blobs go through the fleet's own strict reader, so a document counts as a declaration in
every spelling `farmcore fleet plan` would accept, not only one a text search recognizes. Only
paths are printed: a refused file may hold a private host name or a pasted secret.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

from farmcore.hosting.inventory import MAX_DOCUMENT_BYTES, is_fixture_domain, read_root_mapping

_EXAMPLES = "examples/fleet/"
_SUBMODULE_MODE = "160000"


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _staged_yaml() -> Iterator[tuple[str, bytes | None]]:
    """Each staged YAML path and its content; None for a blob too large to be a declaration."""
    for entry in _git("ls-files", "--cached", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        info, _, raw_path = entry.partition(b"\t")
        mode, blob, _ = info.decode().split()
        path = raw_path.decode(errors="surrogateescape")
        if mode == _SUBMODULE_MODE or not path.lower().endswith((".yaml", ".yml")):
            continue
        too_large = int(_git("cat-file", "-s", blob)) > MAX_DOCUMENT_BYTES
        yield path, None if too_large else _git("cat-file", "blob", blob)


def _must_not_be_tracked(path: str, content: bytes | None) -> bool:
    root = None if content is None else read_root_mapping(content)
    if path.startswith(_EXAMPLES):
        domain = root.get("domain") if root else None
        return not (isinstance(domain, str) and is_fixture_domain(domain))
    return root is not None and "schema_version" in root


def main() -> int:
    refused = [path for path, content in _staged_yaml() if _must_not_be_tracked(path, content)]
    if refused:
        print(
            "data guard: fleet declarations must stay private; examples use <name>.example.test:",
            file=sys.stderr,
        )
        for path in refused:
            print(path if path.isascii() and path.isprintable() else ascii(path), file=sys.stderr)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
