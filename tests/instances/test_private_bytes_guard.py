"""Private bytes live in the database (spec D2), so one `pg_dump` is the whole installation.

A static guard over the source: every call that can write a file (open for writing, write_*,
temporary files and directories, copies, moves, downloads to disk, saves, exports) must be a
known site with a reason it is not persisted private data. A new route, job or command that
writes a file fails here until it either stores its bytes in the database or is reviewed and
listed below with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "farmcore"

WRITERS = {
    "write_bytes", "write_text", "mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
    "SpooledTemporaryFile", "TemporaryDirectory", "copy", "copy2", "copyfile", "copytree",
    "move", "rename", "link", "symlink_to", "hardlink_to", "touch", "mkdir", "makedirs",
    "download_to_drive", "save", "savez", "tofile", "to_csv", "to_parquet", "to_excel",
}  # fmt: skip
WRITE_MODE = set("wax+")

TEMPORARY = "request-scoped temporary directory, removed when the request or command ends"
# (file under src/farmcore, enclosing function, call) -> why it is not persisted private data.
ALLOWED = {
    ("apps/assistant/routes.py", "ask", "save"): "in-memory conversation (app.state)",
    ("apps/assistant/routes.py", "message", "save"): "in-memory conversation (app.state)",
    ("apps/assistant/routes.py", "_transcribe", "TemporaryDirectory"): TEMPORARY,
    ("apps/assistant/routes.py", "_transcribe", "write_bytes"): TEMPORARY,
    ("apps/chat/bot.py", "BotRuntime.on_voice", "TemporaryDirectory"): TEMPORARY,
    ("apps/chat/bot.py", "BotRuntime.on_voice", "download_to_drive"): TEMPORARY,
    ("apps/chat/handlers.py", "_on_message", "save"): "conversation row in the database",
    ("apps/forecast/sources.py", "fetch_single_runs", "mkdir"): "open-data cache (DATA_DIR)",
    ("apps/forecast/sources.py", "fetch_single_runs", "write_text"): "open-data cache (DATA_DIR)",
    ("apps/operations/commands.py", "register.isoxml_demo_", "mkdir"): "synthetic demo file",
    ("apps/operations/commands.py", "register.isoxml_demo_", "write_bytes"): "synthetic demo file",
    ("apps/operations/routes.py", "register_page", "to_csv"): "returns text, writes no file",
    ("apps/operations/routes.py", "register_csv", "to_csv"): "returns text, writes no file",
    ("core/commands/backup.py", "create_bundle", "TemporaryDirectory"): "plaintext, removed",
    ("core/commands/backup.py", "create_bundle", "write_text"): "manifest in the temporary dir",
    ("core/commands/backup.py", "create_bundle", "open"): "tar stream into age: encrypted",
    ("core/commands/restore.py", "unpack", "open"): "dump in the temporary directory",
    ("core/commands/restore.py", "pg_restore", "write_text"): "entry list, temporary directory",
    ("core/commands/restore.py", "restore_bundle", "TemporaryDirectory"): "plaintext, removed",
}


def _writes_mode(call: ast.Call) -> bool:
    """`open(path, "w")`, `path.open("wb")`, `open(..., mode=...)`: a write mode, or a mode
    that is not a literal (it could be one)."""
    modes = [k.value for k in call.keywords if k.arg == "mode"]
    if isinstance(call.func, ast.Name):
        modes += call.args[1:2]
    else:
        modes += [a for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return any(
        not (isinstance(m, ast.Constant) and isinstance(m.value, str))
        or bool(WRITE_MODE & set(m.value))
        for m in modes
    )


def file_writes(source: str, name: str) -> set[tuple[str, str, str]]:
    """Every call in `source` that can write a file: (name, enclosing function, call)."""
    found: set[tuple[str, str, str]] = set()
    scope: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _scoped(self, node) -> None:
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            call = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if call in WRITERS or (call == "open" and _writes_mode(node)):
                found.add((name, ".".join(scope) or "<module>", call))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def test_no_route_job_or_command_persists_private_bytes_outside_the_database():
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        found |= file_writes(path.read_text(), path.relative_to(SRC).as_posix())

    unreviewed = sorted(found - ALLOWED.keys())
    assert not unreviewed, (
        "These calls can write files. Private bytes belong in the database (spec D2); if this "
        f"is not private data, list it in ALLOWED with the reason: {unreviewed}"
    )
    assert not sorted(ALLOWED.keys() - found), "stale ALLOWED entries: remove them"


def test_the_guard_sees_a_route_that_keeps_an_upload_on_disk():
    planted = """
@router.post("/evidence")
async def upload(file: UploadFile):
    with open(settings.data_dir / file.filename, "wb") as out:
        out.write(await file.read())

def job(ctx):
    Path("x").write_bytes(b"")

(root / "y").open(mode="ab")
"""
    assert file_writes(planted, "apps/x/routes.py") == {
        ("apps/x/routes.py", "upload", "open"),
        ("apps/x/routes.py", "job", "write_bytes"),
        ("apps/x/routes.py", "<module>", "open"),
    }
    assert file_writes("def f():\n    open(path, mode)\n", "m.py") == {("m.py", "f", "open")}
    assert file_writes("open(p)\nopen(p, 'rb')\nzf.open(name)\np.open('r')\n", "m.py") == set()
