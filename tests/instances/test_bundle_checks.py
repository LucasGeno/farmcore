"""The decisions a restore takes about bytes before it touches a server, and the backup's
output checks: pure, no database and no `age`.

A bundle is untrusted (anyone holding the owner's public key can encrypt one), so these
decide whether it is even read: the files can be opened, the archive holds exactly two plain
members and never makes restore write more than it holds, the manifest is what `farmcore
backup` writes, the schema is one this release knows, and the dump holds no object
FarmCore's own schema lacks. `test_restore.py` proves the CLI runs them before staging.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from farmcore.core.commands import backup, restore
from farmcore.core.commands.backup import BundleError
from farmcore.core.commands.migrate import alembic_config
from tests.instances.support import HEAD, OUTCOMES

DUMP = b"PGDMP" + bytes(range(256)) * 16


def manifest(**changes) -> dict:
    """What `farmcore backup` writes for `DUMP`."""
    return {
        "format": "farmcore-backup/1",
        "installation_id": str(uuid.uuid4()),
        "release": "0.1.0",
        "modules": {"chat": {"version": "0.1.0", "state": "installed"}},
        "alembic_heads": [HEAD],
        "created_at": "2026-10-20T12:00:00.123456+00:00",
        "dump": {
            "name": "farmcore.dump",
            "bytes": len(DUMP),
            "sha256": hashlib.sha256(DUMP).hexdigest(),
        },
        **changes,
    }


RAW = json.dumps(manifest()).encode()


def tarinfo(name: str, data: bytes = b"", type_=tarfile.REGTYPE, **attrs) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.size = len(data) if type_ == tarfile.REGTYPE else 0
    for key, value in attrs.items():
        setattr(info, key, value)
    return info


def raw_tar(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def archive(meta: dict | bytes | None = None, dump: bytes = DUMP) -> bytes:
    raw = meta if isinstance(meta, bytes) else json.dumps(meta or manifest()).encode()
    return raw_tar([(tarinfo("manifest.json", raw), raw), (tarinfo("farmcore.dump", dump), dump)])


def sparse_bomb(expands_to: int) -> bytes:
    """An old-GNU sparse `farmcore.dump`: 512 stored bytes that extract to `expands_to`."""
    raw = json.dumps(manifest()).encode()
    head = tarinfo("manifest.json", raw).tobuf(tarfile.GNU_FORMAT) + raw
    head += b"\0" * (-len(head) % 512)
    hdr = bytearray(512)
    hdr[0:13] = b"farmcore.dump"
    hdr[100:108] = b"0000600\0"
    hdr[108:116] = b"0000000\0"
    hdr[116:124] = b"0000000\0"
    hdr[124:136] = b"%011o\0" % 512
    hdr[136:148] = b"%011o\0" % 0
    hdr[156:157] = b"S"
    hdr[257:265] = b"ustar  \0"
    hdr[386:398] = b"%011o\0" % (expands_to - 512)
    hdr[398:410] = b"%011o\0" % 512
    hdr[483:495] = b"%011o\0" % expands_to
    hdr[148:156] = b" " * 8
    hdr[148:156] = b"%06o\0 " % sum(hdr)
    return head + bytes(hdr) + b"x" * 512 + b"\0" * 1024


def pax_sparse() -> bytes:
    """A PAX 0.1 sparse `farmcore.dump` (GNU.sparse.* keywords on a regular member)."""
    raw = json.dumps(manifest()).encode()
    info = tarinfo("farmcore.dump", DUMP[:512])
    info.pax_headers = {
        "GNU.sparse.size": str(1 << 31),
        "GNU.sparse.numblocks": "1",
        "GNU.sparse.map": f"{(1 << 31) - 512},512",
    }
    return raw_tar([(tarinfo("manifest.json", raw), raw), (info, DUMP[:512])])


def huge_declared_size() -> bytes:
    """`farmcore.dump`'s header claims 64 GiB; the archive carries only 4 KiB of it."""
    raw = json.dumps(manifest()).encode()
    head = tarinfo("manifest.json", raw).tobuf(tarfile.PAX_FORMAT) + raw
    head += b"\0" * (-len(head) % 512)
    hdr = tarfile.TarInfo("farmcore.dump")
    hdr.size = 64 << 30
    return head + hdr.tobuf(tarfile.PAX_FORMAT) + DUMP[:4096]


def verify(tmp_path: Path, plain: bytes) -> tuple[dict, Path, Path]:
    path = tmp_path / "bundle.tar"
    path.write_bytes(plain)
    into = tmp_path / "into"
    into.mkdir()
    found, dump = restore.verify_plain(path, into)
    assert not path.exists()  # the decrypted archive is gone before the dump is hashed
    return found, dump, into


# --- F1: the files, before anything is decrypted ---------------------------------------------


def _missing(tmp: Path) -> tuple[Path, Path]:
    return tmp / "nothing-here", tmp / "owner.key"


def _locked_key(tmp: Path) -> tuple[Path, Path]:
    (tmp / "owner.key").chmod(0)
    return tmp / "a.age", tmp / "owner.key"


def _closed_directory(tmp: Path) -> tuple[Path, Path]:
    closed = tmp / "closed"
    closed.mkdir()
    (tmp / "a.age").rename(closed / "a.age")
    closed.chmod(0)
    return closed / "a.age", tmp / "owner.key"


def _plain_dump(tmp: Path) -> tuple[Path, Path]:
    (tmp / "a.age").write_bytes(DUMP)
    return tmp / "a.age", tmp / "owner.key"


@pytest.mark.parametrize(
    ("files", "outcomes"),
    [
        (_missing, ["no bundle"]),
        (_locked_key, ["unreadable key", "user hint"]),
        (_closed_directory, ["unreadable bundle", "user hint"]),
        (_plain_dump, ["older release"]),
    ],
    ids=["no bundle", "unreadable key", "directory it cannot search", "plain dump"],
)
def test_restore_names_the_file_and_the_fix_before_decrypting(tmp_path, files, outcomes):
    """The compose uid 10001 case: a permissions problem must not read as a wrong key."""
    (tmp_path / "a.age").write_bytes(b"age-encryption.org/v1\n...")
    (tmp_path / "owner.key").write_text("AGE-SECRET-KEY-1...")
    bundle, key = files(tmp_path)
    try:
        with pytest.raises(BundleError) as refused:
            restore.preflight(bundle, key)
    finally:
        for path in (tmp_path, tmp_path / "closed", tmp_path / "owner.key"):
            if path.exists() or path.is_symlink():
                path.chmod(0o700)

    for outcome in outcomes:
        assert OUTCOMES[outcome] in str(refused.value)
    assert OUTCOMES["cannot decrypt"] not in str(refused.value)


# --- F2: the archive -------------------------------------------------------------------------


SHAPES = {
    "extra member": lambda: raw_tar([
        (tarinfo("manifest.json", RAW), RAW), (tarinfo("farmcore.dump", DUMP), DUMP),
        (tarinfo("extra.sh", b"#!/bin/sh\n"), b"#!/bin/sh\n"),
    ]),
    "symlinked dump": lambda: raw_tar([
        (tarinfo("manifest.json", RAW), RAW),
        (tarinfo("farmcore.dump", type_=tarfile.SYMTYPE, linkname="../../x"), b""),
    ]),
    "sparse dump (old GNU)": lambda: sparse_bomb(2 << 30),
    "sparse dump (PAX)": pax_sparse,
    "dump larger than the archive": huge_declared_size,
    "manifest over 1 MiB": lambda: archive(b" " * (1 << 20) + json.dumps(manifest()).encode()),
    "not a tar": lambda: b"just some text\n" * 100,
    "manifest not JSON": lambda: archive(b"\x00not json"),
}  # fmt: skip


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_only_two_plain_members_are_read_and_never_more_than_the_archive_holds(tmp_path, shape):
    """A forged archive must not fill the disk (the sparse bomb found in review) or smuggle a
    link or an extra file."""
    plain = SHAPES[shape]()

    with pytest.raises(BundleError, match=OUTCOMES["not a backup"]):
        verify(tmp_path, plain)

    written = sum(p.lstat().st_size for p in (tmp_path / "into").rglob("*"))
    assert written <= len(plain)
    assert not any(p.is_symlink() for p in (tmp_path / "into").rglob("*"))


# --- F3: the manifest ------------------------------------------------------------------------


def _without(key: str) -> dict:
    meta = manifest()
    del meta[key]
    return meta


MANIFESTS = {
    "format": (manifest(format="farmcore-backup/99"), "bundle format"),
    "missing field": (_without("release"), "'release'"),
    "no schema revision": (manifest(alembic_heads=[]), "'alembic_heads'"),
    "dump hash": (manifest(dump={**manifest()["dump"], "sha256": "0" * 64}), "SHA-256"),
    "release not a version": (manifest(release="1.0; DROP"), "'release'"),
    "release of 5,000 digits": (manifest(release="9" * 5000), "'release'"),
    "unknown app, escaped": (
        manifest(modules={"fc\x1b[2Japp": {"version": "1.0.0", "state": "installed"}}),
        "fc\\x1b[2Japp",
    ),
    "installation id not canonical": (
        manifest(installation_id="\x1b[2J00000000-0000-4000-8000-000000000000"),
        "'installation_id'",
    ),
    "created_at with a control separator": (
        manifest(created_at="2026-10-20\x9b12:00:00+00:00"), "'created_at'"
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", list(MANIFESTS), ids=list(MANIFESTS))
def test_a_manifest_restore_cannot_trust_is_refused_and_never_printed_raw(tmp_path, case):
    meta, names = MANIFESTS[case]

    with pytest.raises(BundleError) as refused:
        verify(tmp_path, archive(meta))

    message = str(refused.value)
    assert names in message
    assert "\x1b" not in message and "\x9b" not in message


# --- F4: compatibility, decided by the schema ------------------------------------------------


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


@pytest.mark.parametrize(
    ("heads", "in_dump", "says"),
    [
        ([HEAD], [HEAD, "other_0001"], "does not match the manifest"),
        (["zz\x1b[2Jfuture"], ["zz\x1b[2Jfuture"], "zz\\x1b[2Jfuture"),
    ],
    ids=["dump heads differ", "unknown head"],
)
def test_only_a_schema_this_release_knows_reaches_pg_restore(script, heads, in_dump, says):
    with pytest.raises(BundleError) as refused:
        restore.check_compatible(manifest(alembic_heads=heads), in_dump, script)

    message = str(refused.value)
    assert says in message and "\x1b" not in message
    if heads != [HEAD]:
        assert OUTCOMES["incompatible"] in message


# --- F5: the dump's objects ------------------------------------------------------------------

# `pg_restore --list` of FarmCore's own schema at a revision (abridged), as `restore` dumps it.
SCHEMA = """\
;
; Archive created at 2026-10-21 08:00:00 UTC
;
4400; 0 0 ENCODING - ENCODING
4401; 0 0 STDSTRINGS - STDSTRINGS
226; 1259 17210 TABLE public farm farmcore
227; 1259 17209 SEQUENCE public farm_id_seq farmcore
4402; 0 0 SEQUENCE OWNED BY public farm_id_seq farmcore
4200; 2604 17213 DEFAULT public farm id farmcore
4300; 2606 17215 CONSTRAINT public farm farm_pkey farmcore
4410; 0 17210 TABLE DATA public farm farmcore
4411; 0 0 SEQUENCE SET public farm_id_seq farmcore
"""
# The same schema from another server: other ids, another owner.
SAME_ELSEWHERE = SCHEMA.replace("farmcore\n", "alice\n").replace(" 172", " 883")

FORGED = {
    "function": "250; 1255 17300 FUNCTION public fc_pwn() alice\n",
    "trigger on a FarmCore table": "4301; 2620 17301 TRIGGER public farm fc_pwn alice\n",
    "table named with a terminal escape": "228; 1259 17302 TABLE public fc\x1b[2Jx alice\n",
}


@pytest.mark.parametrize("extra", [*FORGED, None], ids=[*FORGED, "same schema, other server"])
def test_a_dump_object_farmcores_schema_lacks_refuses_the_bundle(extra):
    """Bundle SQL must never run as the superuser (iso 02 round 1); and ids and owners differ
    between any two dumps of one schema, so those alone must never refuse a good bundle."""
    if extra is None:
        entries = restore.check_objects(SAME_ELSEWHERE, SCHEMA)
        assert ("4410; 0 88310 TABLE DATA public farm alice", "TABLE DATA public farm") in entries
        return
    with pytest.raises(BundleError, match=OUTCOMES["foreign objects"]) as refused:
        restore.check_objects(SAME_ELSEWHERE + FORGED[extra], SCHEMA)

    assert "\x1b" not in str(refused.value)
    if "escape" in extra:
        assert "fc\\x1b[2Jx" in str(refused.value)


# --- D4: the backup's output -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "says"),
    [("no recipient", "recipient"), ("private key", "age public keys"),
     ("existing file", "already exists"), ("unwritable directory", "cannot write")],
)  # fmt: skip
def test_backup_refuses_its_output_before_anything_runs(tmp_path, case, says):
    """No overwritten bundle, and never a pasted private key echoed into a log."""
    secret = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"
    recipients, output = (
        ["age1zvkyg2lqzraa2lnjvqej32nkuu0ues2s82hzrye869xeexvn73equnujwj"],
        (tmp_path / "a.age"),
    )
    if case == "no recipient":
        recipients = []
    elif case == "private key":
        recipients = [secret]
    elif case == "existing file":
        output.write_bytes(b"an older bundle")
    else:
        output = tmp_path / "locked" / "a.age"
        output.parent.mkdir(mode=0o500)

    try:
        with pytest.raises(BundleError) as refused:
            backup.check_output(output, recipients)
    finally:
        output.parent.chmod(0o700)  # so tmp_path can be cleaned up whatever happened

    assert says in str(refused.value) and secret not in str(refused.value)
    if case == "existing file":
        assert output.read_bytes() == b"an older bundle"


# --- J4: plaintext a killed restore left -----------------------------------------------------


def test_the_temp_dir_sweep_removes_only_stale_restore_directories_and_never_follows_a_link(
    private_tmp, tmp_path
):
    """SIGKILL skips the cleanup of a directory that holds rows in clear."""
    old = time.time() - 7 * 3600
    stale = private_tmp / "farmcore-restore-killed"
    stale.mkdir()
    (stale / "farmcore.dump").write_bytes(b"PGDMP plaintext rows")
    (private_tmp / "farmcore-restore-running").mkdir()
    (private_tmp / "unrelated").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept").write_text("not a restore's")
    link = private_tmp / "farmcore-restore-link"
    link.symlink_to(elsewhere, target_is_directory=True)
    for path in (stale / "farmcore.dump", stale, private_tmp / "unrelated", elsewhere):
        os.utime(path, (old, old))
    os.utime(link, (old, old), follow_symlinks=False)

    restore.sweep_temp()

    left = sorted(p.name for p in private_tmp.iterdir())
    assert left == ["farmcore-restore-link", "farmcore-restore-running", "unrelated"]
    assert (elsewhere / "kept").read_text() == "not a restore's"
