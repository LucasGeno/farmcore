"""Private bytes live in the database, so one `pg_dump` is the whole installation.

A static guard over the source: every call that can write a file (open for writing, including
`os.open`, `aiofiles` and zip archives; write_*; temporary files and directories; copies, moves
and links; downloads to disk; exports) must be a known kind of write in a known file, with a
reason it is not persisted private data. A new route, job or command that
writes a file fails here until it either stores its bytes in the database or is reviewed and
listed below with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "farmcore"

# Calls that write a file whatever object they are called on.
WRITERS = {
    "write_bytes", "write_text", "mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
    "SpooledTemporaryFile", "TemporaryDirectory", "symlink_to", "hardlink_to", "touch", "mkdir",
    "makedirs", "download_to_drive", "savez", "tofile", "to_parquet", "to_excel",
}  # fmt: skip
# Calls that write only on these modules (`props.copy()` and `session.save()` do not).
MODULE_WRITERS = {
    "shutil": {"copy", "copy2", "copyfile", "copytree", "move"},
    "os": {"open", "rename", "replace", "link", "symlink", "mkfifo", "makedirs", "mkdir"},
}
WRITE_MODE = set("wax+")

TEMPORARY = "request-scoped temporary directory, removed when the request or command ends"
# (file under src/farmcore, call) -> why it is not persisted private data. Keyed by file, not
# function, so renaming a function never needs this list touched; a new kind of write in a file
# does.
ALLOWED = {
    ("apps/assistant/routes.py", "TemporaryDirectory"): TEMPORARY,
    ("apps/assistant/routes.py", "write_bytes"): TEMPORARY,
    ("apps/chat/bot.py", "TemporaryDirectory"): TEMPORARY,
    ("apps/chat/bot.py", "download_to_drive"): TEMPORARY,
    ("apps/forecast/sources.py", "mkdir"): "open-data cache (DATA_DIR)",
    ("apps/forecast/sources.py", "write_text"): "open-data cache (DATA_DIR)",
    ("apps/operations/commands.py", "mkdir"): "synthetic demo file (`isoxml-demo` CLI)",
    ("apps/operations/commands.py", "write_bytes"): "synthetic demo file (`isoxml-demo` CLI)",
    ("core/commands/backup.py", "TemporaryDirectory"): "plaintext dump, removed afterwards",
    ("core/commands/backup.py", "write_text"): "manifest in the temporary directory",
    ("core/commands/backup.py", "open"): "the tar streamed into age: encrypted",
    ("core/commands/backup.py", "os.replace"): "the finished encrypted bundle takes its name",
    ("core/commands/restore.py", "open"): "dump in the temporary directory",
    ("core/commands/restore.py", "write_text"): "entry list in the temporary directory",
    ("core/commands/restore.py", "TemporaryDirectory"): "plaintext dump, removed afterwards",
}


def _writes_mode(call: ast.Call) -> bool:
    """`open(path, "w")`, `path.open("wb")`, `open(..., mode=...)`: a write mode, or a mode
    that is not a literal (it could be one)."""
    modes = [k.value for k in call.keywords if k.arg == "mode"]
    if isinstance(call.func, ast.Name) or (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in {"aiofiles", "io", "codecs", "gzip", "bz2", "lzma", "zipfile"}
    ):
        modes += call.args[1:2]
    else:
        modes += [a for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return any(
        not (isinstance(m, ast.Constant) and isinstance(m.value, str))
        or bool(WRITE_MODE & set(m.value))
        for m in modes
    )


def file_writes(source: str, name: str) -> set[tuple[str, str]]:
    """Every kind of call in `source` that can write a file: (name, call)."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(
            func.value, ast.Name
        ) else None  # fmt: skip
        if owner in MODULE_WRITERS and call in MODULE_WRITERS[owner]:
            found.add((name, f"{owner}.{call}"))
        elif call in WRITERS or (call in {"open", "ZipFile"} and _writes_mode(node)):
            found.add((name, call))
    return found


def test_no_route_job_or_command_persists_private_bytes_outside_the_database():
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        found |= file_writes(path.read_text(), path.relative_to(SRC).as_posix())

    unreviewed = sorted(found - ALLOWED.keys())
    assert not unreviewed, (
        "These calls can write files. Private bytes belong in the database; if this "
        f"is not private data, list it in ALLOWED with the reason: {unreviewed}"
    )
    assert not sorted(ALLOWED.keys() - found), "stale ALLOWED entries: remove them"


def test_the_guard_sees_planted_writes_and_ignores_reads():
    """One control: the guard must not go blind to a route keeping an upload on disk, or to
    archive, low-level and async writes."""
    planted = """
@router.post("/evidence")
async def upload(file: UploadFile):
    with open(settings.data_dir / file.filename, "wb") as out:
        out.write(await file.read())

def job(ctx):
    Path("x").write_bytes(b"")

(root / "y").open(mode="ab")
open(path, mode)
zipfile.ZipFile(path, "w")
os.open(path, os.O_WRONLY | os.O_CREAT)
async with aiofiles.open(path, "wb") as f: ...
shutil.move(a, b)
"""
    harmless = (
        "open(p)\nopen(p, 'rb')\nzf.open(name)\np.open('r')\nzipfile.ZipFile(path)\n"
        "props.copy()\nconversation.save(session)\nd.move(1)\n"
    )

    assert {call for _, call in file_writes(planted, "apps/x/routes.py")} == {
        "open", "write_bytes", "ZipFile", "os.open", "shutil.move",
    }  # fmt: skip
    assert file_writes(harmless, "m.py") == set()
