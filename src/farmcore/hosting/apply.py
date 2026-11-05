"""Apply one declared installation with the stock Compose file, and report what it is doing.

Docker is reached only as `docker` subprocesses with structured arguments. The resolved Compose
configuration is never asked for, and secrets reach Compose only through a private env file.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from farmcore.hosting.inventory import Declaration

READY, NOT_READY, NOT_PROVISIONED, REFUSED = "ready", "not ready", "not provisioned", "refused"
# Stock, from the checkout the operator runs; an inventory never names another.
COMPOSE_FILE = Path(__file__).resolve().parents[3] / "docker-compose.yml"
# What the pinned image itself says it is, compared with what its server answers.
_RELEASE_PROBE = (
    "import json, farmcore; from farmcore.core.startup import code_revisions; "
    "print(json.dumps({'release': farmcore.__version__, 'heads': list(code_revisions()[0])}))"
)
# Docker's own reach and nothing else: Compose interpolation prefers the shell over the env file,
# and COMPOSE_* would pick another file, project or profile.
_DOCKER_ENVIRONMENT = (
    "PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
)  # fmt: skip
MAX_SECRET_CHARACTERS = 256
_MAX_DIAGNOSTIC_CHARACTERS = 2000
_SECRET_VALUE = re.compile(rf"[A-Za-z0-9._~-]{{32,{MAX_SECRET_CHARACTERS}}}")
OWNER_LABEL = "farmcore.fleet.owner"
_COMPOSE_PROJECT = "com.docker.compose.project"
# docker-compose.yml's named volumes, the claim first; and its services.
_VOLUMES = ("pgdata", "whisper-cache", "backups")
_SERVICES = "db|migrate|app|backup|bot"
# Narrow on purpose: `.Config.Env` holds secrets and is never read.
_CONTAINER_FACTS = (
    '{"name":{{json .Name}},"networks":{{json .NetworkSettings.Networks}},'
    '"ports":{{json .HostConfig.PortBindings}},"status":{{json .State.Status}},'
    '"health":{{json (index .State "Health")}},"image":{{json .Image}},'
    '"memory":{{json .HostConfig.Memory}},"cpus":{{json .HostConfig.NanoCpus}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)
_MISMATCH = "/health says another "
_RECORD_DRAFT = re.compile(r"\.ownership\.[0-9a-f]{8}")
_inherited_locks: list[int] = []
_ID_LINE = re.compile(r"^installation (\S+)$", re.MULTILINE)


class Refused(Exception):
    """Nothing was changed; the message is safe to print."""


class StepFailed(Exception):
    pass


def _docker_environment() -> dict[str, str]:
    return {name: os.environ[name] for name in _DOCKER_ENVIRONMENT if name in os.environ}


def _project(declaration: Declaration) -> str:
    return f"farmcore-fleet-{declaration.slug}"


@dataclass(frozen=True)
class _Paths:
    root: Path
    slug: str

    @property
    def directory(self) -> Path:
        return self.root / "installations" / self.slug

    @property
    def record(self) -> Path:
        return self.directory / "ownership.json"

    @property
    def env(self) -> Path:
        return self.directory / "compose.env"

    @property
    def receipts(self) -> Path:
        return self.directory / "receipts.jsonl"


def _check_root(root: Path) -> None:
    if not root.is_absolute():
        raise Refused("--state-root must be an absolute path")
    if not root.exists():
        raise Refused("--state-root does not exist; create it with mode 0700")
    if root != root.resolve():
        raise Refused("--state-root must be its real path, not reached through a symlink")
    found = os.stat(root)
    if not stat.S_ISDIR(found.st_mode) or stat.S_IMODE(found.st_mode) != 0o700:
        raise Refused("--state-root must be a directory with mode 0700")
    if found.st_uid != os.getuid():
        raise Refused("--state-root must belong to the operator running this")
    if any((folder / ".git").exists() for folder in (root, *root.parents)):
        raise Refused("--state-root must be outside every checkout")


def _check_private_directory(found: os.stat_result, shown: str) -> None:
    if (
        not stat.S_ISDIR(found.st_mode)
        or stat.S_IMODE(found.st_mode) != 0o700
        or found.st_uid != os.getuid()
    ):
        raise Refused(f"{shown} must be a private real directory: mode 0700, the operator's")


def _private_directory(root: Path, *parts: str) -> Path:
    """Below the state root, one level at a time: made 0700, and refused if it is anything else."""
    path = root
    for part in parts:
        path = path / part
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
        _check_private_directory(os.lstat(path), str(path.relative_to(root)))
    return path


def _write_private(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(content)
    os.replace(temporary, path)


def _existing_secret(root: Path, name: str, reference: str) -> str | None:
    """The value at a reference, or None if there is none yet. Never repeated in a refusal."""
    path = root / "secrets" / reference
    for above in reversed(path.parents):
        if above in root.parents or above == root:
            continue
        try:
            _check_private_directory(os.lstat(above), f"secrets.{name}: {above.relative_to(root)}")
        except FileNotFoundError:
            return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(f"secrets.{name}: must be a regular file, not a symlink") from None
    with os.fdopen(fd) as source:
        found = os.fstat(source.fileno())
        if not stat.S_ISREG(found.st_mode) or stat.S_IMODE(found.st_mode) != 0o600:
            raise Refused(f"secrets.{name}: must be a regular file with mode 0600")
        value = source.read(MAX_SECRET_CHARACTERS + 2).removesuffix("\n")
    if not _SECRET_VALUE.fullmatch(value):
        raise Refused(f"secrets.{name}: must be 32-256 letters, digits or . _ ~ -")
    return value


def _secret_values(declaration: Declaration, root: Path) -> tuple[str, ...]:
    return tuple(
        value
        for name, reference in declaration.secrets.model_dump().items()
        if (value := _existing_secret(root, name, reference)) is not None
    )


def _checked_secrets(declaration: Declaration, root: Path) -> None:
    for name, reference in declaration.secrets.model_dump().items():
        _existing_secret(root, name, reference)


def _secret(root: Path, name: str, reference: str) -> str:
    """Generated once: a second writer's value wins the name, and both go on using it."""
    if (value := _existing_secret(root, name, reference)) is not None:
        return value
    *folders, file_name = reference.split("/")
    path = _private_directory(root, "secrets", *folders) / file_name
    temporary = path.with_name(f".{file_name}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(secrets.token_hex(24))
    with contextlib.suppress(FileExistsError):
        os.link(temporary, path)
    os.unlink(temporary)
    value = _existing_secret(root, name, reference)
    assert value is not None
    return value


def _env_file(declaration: Declaration, root: Path) -> str:
    limits = declaration.limits
    values = {
        "POSTGRES_PASSWORD": _secret(
            root, "postgres_password", declaration.secrets.postgres_password
        ),
        "RUNTIME_DATABASE_PASSWORD": _secret(
            root, "runtime_database_password", declaration.secrets.runtime_database_password
        ),
        "ADMIN_TOKEN": _secret(root, "admin_token", declaration.secrets.admin_token),
        "FARMCORE_IMAGE": declaration.images.app,
        "POSTGIS_IMAGE": declaration.images.database,
        "FARMCORE_BIND": declaration.binding.host,
        "FARMCORE_PORT": str(declaration.binding.port),
        "PUBLIC_BASE_URL": f"https://{declaration.domain}",
        "FARMCORE_APP_MEMORY": f"{limits.app_memory_mib}m",
        "FARMCORE_APP_CPUS": f"{limits.app_cpu_millis / 1000:.3f}",
        "FARMCORE_DATABASE_MEMORY": f"{limits.database_memory_mib}m",
        "FARMCORE_DATABASE_CPUS": f"{limits.database_cpu_millis / 1000:.3f}",
    }
    return "".join(f"{name}={value}\n" for name, value in values.items())


@contextlib.contextmanager
def _installation_lock(paths: _Paths) -> Iterator[None]:
    """One apply per installation. Every Docker child inherits the descriptor, so a killed apply's
    Compose keeps it until that Compose has exited, and a retry cannot run beside it."""
    fd = os.open(paths.directory / "apply.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(
                "another apply of this installation, or a Compose it started, is still running"
            ) from None
        _inherited_locks.append(fd)
        try:
            yield
        finally:
            _inherited_locks.remove(fd)
    finally:
        os.close(fd)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        env=_docker_environment(),
        pass_fds=tuple(_inherited_locks),
    )


def _read(*args: str) -> str:
    """The output of a read-only `docker` command; a failure there stops the run unchanged."""
    result = _docker(*args)
    if result.returncode != 0:
        raise Refused(f"cannot read Docker: `docker {args[0]}` exit {result.returncode}")
    return result.stdout


class _Compose:
    def __init__(self, declaration: Declaration, paths: _Paths, hidden: tuple[str, ...]) -> None:
        self._hidden = hidden
        self._base = (
            "compose",
            "-p", _project(declaration),
            "-f", str(COMPOSE_FILE),
            "--project-directory", str(paths.directory),
            "--env-file", str(paths.env),
        )  # fmt: skip

    def __call__(self, step: str, *args: str) -> str:
        result = _docker(*self._base, *args)
        if result.returncode != 0:
            said = result.stderr
            for value in self._hidden:  # first: a cut could leave part of a value unrecognizable
                said = said.replace(value, "<redacted>")
            said = said[-_MAX_DIAGNOSTIC_CHARACTERS:].strip()
            raise StepFailed(f"{step}: docker compose exit {result.returncode}: {said}")
        return result.stdout

    def one_shot(self, step: str, *command: str) -> str:
        return self(step, "run", "--rm", "--no-deps", "-T", "migrate", *command)


@dataclass(frozen=True)
class _Live:
    """What Docker holds under this installation's names right now."""

    volumes: dict[str, dict[str, str]]
    network: tuple[str, dict[str, str]] | None
    containers: dict[str, dict[str, Any]]


def _labels(kind: str, name: str) -> dict[str, str]:
    return json.loads(_read(kind, "inspect", "-f", "{{json .Labels}}", name)) or {}


def _read_live(project: str) -> _Live:
    listed = set(_read("volume", "ls", "-q", "--filter", f"name={project}_").split())
    volumes = {
        name: json.loads(
            _read(
                "volume",
                "inspect",
                "-f",
                '{"labels":{{json .Labels}},"created":{{json .CreatedAt}}}',
                name,
            )
        )
        for name in (f"{project}_{volume}" for volume in _VOLUMES)
        if name in listed
    }
    network_name = f"{project}_default"
    network = None
    networks = _read(
        "network",
        "ls",
        "--no-trunc",
        "--format",
        "{{.ID}} {{.Name}}",
        "--filter",
        f"name={network_name}",
    )
    for line in networks.splitlines():
        network_id, name = line.split(maxsplit=1)
        if name == network_name:
            network = (network_id, _labels("network", network_id))
    by_label = _read("ps", "-aq", "--no-trunc", "--filter", f"label={_COMPOSE_PROJECT}={project}")
    by_name = _read(
        "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}", "--filter", f"name={project}-"
    )
    own_name = re.compile(rf"{re.escape(project)}-(?:{_SERVICES})-(?:run-[0-9a-f]+|[0-9]+)")
    found = set(by_label.split()) | {
        line.split()[0] for line in by_name.splitlines() if own_name.fullmatch(line.split()[1])
    }
    containers = {
        item: json.loads(_read("inspect", "-f", _CONTAINER_FACTS, item)) for item in sorted(found)
    }
    return _Live(volumes, network, containers)


def _ownership_problems(project: str, record: dict[str, Any] | None, live: _Live) -> list[str]:
    """Why this state root may not act on what Docker holds. Labels can be copied, so they never
    prove anything alone: each resource must also match what was saved about it, a volume's creation
    time, a network's id, a container's id. A resource with nothing saved is not adopted, even if
    it is what a killed apply had just made: nothing can tell it from a replacement."""
    if record is None:
        return [
            f"{kind} {name} exists and this state root has no record of it: not adopting"
            for kind, name in (
                *(("volume", name) for name in live.volumes),
                *((("network", f"{project}_default"),) if live.network else ()),
                *(("container", facts["name"].lstrip("/")) for facts in live.containers.values()),
            )
        ]
    owner, problems = record["owner_id"], []
    saved_volumes = record.get("volumes", {})
    for name, facts in live.volumes.items():
        if (facts["labels"] or {}).get(OWNER_LABEL) != owner:
            problems.append(f"volume {name} is not this installation's: not adopting")
        elif saved_volumes.get(name) != facts["created"]:
            problems.append(f"volume {name} is not the one saved when claimed: not adopting")
    if live.network and (
        live.network[1].get(OWNER_LABEL) != owner or live.network[0] != record.get("network_id")
    ):
        problems.append(f"network {project}_default is not the one saved: not adopting")
    known = set(record.get("containers", ()))
    for item, facts in live.containers.items():
        if item not in known:
            name = facts["name"].lstrip("/")
            problems.append(f"container {name} is not one this installation made: not adopting")
    if problems or not record.get("claimed"):
        return problems
    missing = [
        name for name in (f"{project}_{volume}" for volume in _VOLUMES) if name not in live.volumes
    ]
    if live.network is None or live.network[0] != record["network_id"]:
        missing.append(f"{project}_default")
    return [f"drift: {name} is missing or replaced; refusing to recreate it" for name in missing]


def _runtime_drift(declaration: Declaration, record: dict[str, Any], live: _Live) -> list[str]:
    """What the running configuration has that the declaration does not. Each of these can still
    answer /health correctly. A stopped container is fine: only its configuration is read here."""
    limits = declaration.limits
    roles = {
        "db": ("database", limits.database_memory_mib, limits.database_cpu_millis),
        **{
            service: ("app", limits.app_memory_mib, limits.app_cpu_millis)
            for service in ("migrate", "app", "backup")
        },
    }
    network = f"{_project(declaration)}_default"
    binding = {"HostIp": declaration.binding.host, "HostPort": str(declaration.binding.port)}
    problems = []
    for facts in live.containers.values():
        service = facts["service"]
        if service not in roles:
            continue
        role, memory_mib, cpu_millis = roles[service]
        if facts["image"] != record.get("images", {}).get(role):
            problems.append(f"drift: {service} runs another image than was declared")
        if facts["memory"] != memory_mib * 1024 * 1024 or facts["cpus"] != cpu_millis * 1_000_000:
            problems.append(f"drift: {service} does not have its declared limits")
        attached = facts["networks"] or {}
        network_ids = {item.get("NetworkID") for item in attached.values()} - {"", None}
        if set(attached) != {network} or not network_ids <= {record.get("network_id")}:
            problems.append(f"drift: {service} is attached to another network")
        published = facts["ports"] or {}
        if published != ({"8000/tcp": [binding]} if service == "app" else {}):
            problems.append(f"drift: {service} does not publish exactly the declared binding")
    return problems


def _binding_problem(declaration: Declaration, live: _Live) -> str | None:
    """A foreign listener, unless this installation's own running app is what holds the binding."""
    wanted = [{"HostIp": declaration.binding.host, "HostPort": str(declaration.binding.port)}]
    if any(
        facts["status"] == "running" and (facts["ports"] or {}).get("8000/tcp") == wanted
        for facts in live.containers.values()
    ):
        return None
    with socket.socket() as probe:
        try:
            probe.bind((declaration.binding.host, declaration.binding.port))
        except OSError:
            return "the declared binding is in use by something else; not adopting"
    return None


def _image_ids(declaration: Declaration) -> dict[str, str]:
    ids = {}
    for role, image in declaration.images.model_dump().items():
        found = _docker("image", "inspect", "-f", "{{.Id}}", image)
        if found.returncode != 0:
            raise Refused(f"images.{role} is not on this host; run `docker pull` for it first")
        ids[role] = found.stdout.strip()
    return ids


def _save_new_record(paths: _Paths, record: dict[str, Any]) -> None:
    temporary = paths.record.with_name(f".ownership.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(record, out, indent=2)
    os.link(temporary, paths.record)
    os.unlink(temporary)


def _create_owned(project: str, record: dict[str, Any], paths: _Paths) -> None:
    """pgdata first, read back: the daemon keeps the first creator's labels, so of two state roots
    racing for one slug exactly one reads its own owner id here, and only that one goes on. Each
    volume's creation time is saved at once: from then on it, not a label, tells a replacement."""
    owner_label = f"{OWNER_LABEL}={record['owner_id']}"
    project_label = f"{_COMPOSE_PROJECT}={project}"
    for volume in _VOLUMES:
        name = f"{project}_{volume}"
        created = _docker(
            "volume", "create", "--label", owner_label, "--label", project_label,
            "--label", f"com.docker.compose.volume={volume}", name,
        )  # fmt: skip
        if created.returncode != 0:
            raise StepFailed(f"create volume {volume}: docker exit {created.returncode}")
        if _labels("volume", name).get(OWNER_LABEL) != record["owner_id"]:
            raise Refused(f"volume {name} belongs to another state root: not adopting")
        when = _read("volume", "inspect", "-f", "{{.CreatedAt}}", name).strip()
        record.setdefault("volumes", {})[name] = when
        _write_private(paths.record, json.dumps(record, indent=2))
    live = _read_live(project)
    if live.network is None:
        created = _docker(
            "network", "create", "--label", owner_label, "--label", project_label,
            "--label", "com.docker.compose.network=default", f"{project}_default",
        )  # fmt: skip
        if created.returncode != 0:
            raise StepFailed(f"create network: docker exit {created.returncode}")
        live = _read_live(project)
        assert live.network is not None
        record["network_id"] = live.network[0]
        _write_private(paths.record, json.dumps(record, indent=2))
    record["claimed"] = True
    _write_private(paths.record, json.dumps(record, indent=2))


def _health(declaration: Declaration) -> tuple[int, dict[str, Any]] | None:
    connection = http.client.HTTPConnection(
        declaration.binding.host, declaration.binding.port, timeout=5
    )
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        return response.status, json.loads(response.read(64 * 1024))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        connection.close()


def identity_problems(saved: dict[str, Any], health: tuple[int, Any] | None) -> list[str]:
    """Why an answer on the binding is not this installation at its saved release and schema."""
    if health is None:
        return ["the binding does not answer /health"]
    status, body = health
    if not isinstance(body, dict):
        return [f"/health answered {status}, not a health report"]
    problems = [
        f"{_MISMATCH}{field} differs from the saved installation"
        for field, expected in (
            ("installation_id", saved.get("installation_id")),
            ("release", saved.get("release")),
            ("alembic_heads", saved.get("heads")),
        )
        if body.get(field) != expected
    ]
    if status != 200:
        problems.append(f"/health answered {status}")
    elif body.get("state") != "ok":
        problems.append("/health reports a state other than ok")
    return problems


def _container_problems(declaration: Declaration, record: dict[str, Any], live: _Live) -> list[str]:
    problems = _runtime_drift(declaration, record, live)
    for service in ("app", "db"):
        running = [
            facts
            for facts in live.containers.values()
            if facts["service"] == service and facts["status"] == "running"
        ]
        if not running:
            problems.append(f"{service} is not running")
        elif (running[0]["health"] or {}).get("Status") != "healthy":
            problems.append(f"{service} is not healthy")
    return problems


def _readiness(declaration: Declaration, record: dict[str, Any]) -> list[str]:
    project = _project(declaration)
    live = _read_live(project)
    return [
        *_ownership_problems(project, record, live),
        *_container_problems(declaration, record, live),
        *identity_problems(record, _health(declaration)),
    ]


def _live_effects(record: dict[str, Any], live: _Live) -> str | None:
    """The setting as the running app reads it; None if it cannot be asked. Only ever in a
    container this record knows to be its own."""
    for item in record.get("containers", ()):
        facts = live.containers.get(item)
        if facts and facts["service"] == "app" and facts["status"] == "running":
            result = _docker("exec", item, "farmcore", "effects")
            return result.stdout.split()[-1] if result.returncode == 0 and result.stdout else None
    return None


def _report(
    declaration: Declaration, saved: dict[str, Any], state: str, problems: list[str]
) -> dict[str, Any]:
    return {
        "slug": declaration.slug,
        "state": state,
        "installation_id": saved.get("installation_id"),
        "release": saved.get("release"),
        "alembic_heads": saved.get("heads"),
        "effects": saved.get("effects"),
        "problems": problems,
    }


def _start(
    declaration: Declaration, paths: _Paths, saved: dict[str, Any], hidden: tuple[str, ...]
) -> None:
    """Containers made, and their ids saved, before any of them starts. Then the database, effects
    paused and read back, the schema; only then the app, so an interrupted first run can never leave
    an unpaused app. Pausing stops once confirmed: after
    that the setting is the operator's, and a repeated apply reports it instead."""
    compose = _Compose(declaration, paths, hidden)
    start = ("--no-build", "--pull", "never")
    saved["starting"] = True
    _write_private(paths.record, json.dumps(saved, indent=2))
    compose("create containers", "create", *start)
    project = _project(declaration)
    saved["containers"] = sorted(
        item
        for item, facts in _read_live(project).containers.items()
        # Compose has just made or kept these names, which the daemon gives to one container only.
        if facts["name"] == f"/{project}-{facts['service']}-1"
    )
    _write_private(paths.record, json.dumps(saved, indent=2))
    compose("database start", "up", "-d", *start, "--wait", "--wait-timeout", "120", "db")
    if not saved.get("effects_paused_once"):
        compose.one_shot("pause effects", "farmcore", "effects", "pause")
    saved["effects"] = compose.one_shot("read effects", "farmcore", "effects").split()[-1]
    if not saved.get("effects_paused_once"):
        if saved["effects"] != "paused":
            raise StepFailed("pause effects: the setting did not read back as paused")
        saved["effects_paused_once"] = True
    migrated = _ID_LINE.search(compose.one_shot("migrate", "farmcore", "migrate"))
    if migrated is None:
        raise StepFailed("migrate: no installation id in its output")
    if saved.setdefault("installation_id", migrated.group(1)) != migrated.group(1):
        raise StepFailed("migrate: the database holds another installation id than was saved")
    try:
        pinned = json.loads(compose.one_shot("read release", "python", "-c", _RELEASE_PROBE))
    except ValueError:
        raise StepFailed("read release: the pinned image did not say what it is") from None
    saved.setdefault("release", pinned["release"])
    saved.setdefault("heads", pinned["heads"])
    _write_private(paths.record, json.dumps(saved, indent=2))
    compose("start", "up", "-d", *start)
    saved["starting"] = False
    _write_private(paths.record, json.dumps(saved, indent=2))


def _declared_facts(declaration: Declaration) -> dict[str, Any]:
    """What a repeated apply must find unchanged: references and digests, never a secret."""
    return {
        **declaration.model_dump(exclude={"schema_version", "slug"}),
        "compose_sha256": hashlib.sha256(COMPOSE_FILE.read_bytes()).hexdigest(),
    }


def _read_record(paths: _Paths, declaration: Declaration) -> dict[str, Any] | None:
    try:
        record = json.loads(paths.record.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Refused("ownership.json cannot be read; fix or restore it") from None
    if record.get("slug") != declaration.slug or record.get("project") != _project(declaration):
        raise Refused("ownership.json names another installation; fix or restore it")
    return record


def _secret_evidence(record: dict[str, Any], value: str) -> str:
    return hashlib.sha256(f"{record['owner_id']}:{value}".encode()).hexdigest()


def _secret_drift(record: dict[str, Any], declaration: Declaration, root: Path) -> list[str]:
    """A database holds what it was first given, so a missing or changed file is never replaced."""
    problems = []
    for name, reference in declaration.secrets.model_dump().items():
        saved = record.get("secrets", {}).get(name)
        if saved is None:
            continue
        value = _existing_secret(root, name, reference)
        if value is None:
            problems.append(f"drift: secrets.{name} is missing; refusing to generate another")
        elif _secret_evidence(record, value) != saved:
            problems.append(f"drift: secrets.{name} changed since this installation first used it")
    return problems


def _drift_problems(
    record: dict[str, Any], declaration: Declaration, root: Path, daemon: str
) -> list[str]:
    if record["daemon_id"] != daemon:
        return ["this record belongs to another Docker daemon"]
    saved, now = record["declared"], _declared_facts(declaration)
    return [
        *(
            f"drift: {field} differs from what this installation was applied with; "
            "upgrades and moves are separate operations"
            for field in sorted(now)
            if saved.get(field) != now[field]
        ),
        *_secret_drift(record, declaration, root),
    ]


def _append_receipt(paths: _Paths, report: dict[str, Any]) -> None:
    """One line per run: what it concluded, never a secret."""
    line = json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **report})
    fd = os.open(paths.receipts, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(line + "\n")


def _check_not_someone_elses_directory(paths: _Paths) -> None:
    """Without a record, a directory may hold only what a run stopped before its record leaves."""
    if not paths.directory.is_dir() or paths.directory.is_symlink() or paths.record.exists():
        return
    for entry in paths.directory.iterdir():
        if entry.name not in ("apply.lock", "receipts.jsonl") and not _RECORD_DRAFT.fullmatch(
            entry.name
        ):
            raise Refused(
                f"installations/{paths.slug} holds files this tool did not write: not adopting"
            )


def apply_installation(declaration: Declaration, root: Path, readiness_seconds: int) -> dict:
    _check_root(root)
    _checked_secrets(declaration, root)
    paths = _Paths(root, declaration.slug)
    _check_not_someone_elses_directory(paths)
    _private_directory(root, "installations", declaration.slug)
    try:
        with _installation_lock(paths):
            report = _apply_locked(declaration, root, paths, readiness_seconds)
    except Refused as exc:
        _append_receipt(paths, {"slug": declaration.slug, "state": REFUSED, "problems": [str(exc)]})
        raise
    _append_receipt(paths, report)
    return report


def _apply_locked(
    declaration: Declaration, root: Path, paths: _Paths, readiness_seconds: int
) -> dict:
    project = _project(declaration)
    existing = _read_record(paths, declaration)
    daemon = _read("info", "-f", "{{.ID}}").strip()
    if existing and (problems := _drift_problems(existing, declaration, root, daemon)):
        raise Refused("; ".join(problems))
    images = _image_ids(declaration)
    live = _read_live(project)
    problems = _ownership_problems(project, existing, live)
    if existing:
        problems += _runtime_drift(declaration, existing, live)
    if problems:
        raise Refused("; ".join(problems))
    if problem := _binding_problem(declaration, live):
        raise Refused(problem)
    record = existing or {
        "slug": declaration.slug,
        "project": project,
        "owner_id": secrets.token_hex(16),
        "daemon_id": daemon,
        "declared": _declared_facts(declaration),
        "images": images,
        "claimed": False,
    }
    if existing is None:
        _save_new_record(paths, record)
    _write_private(paths.env, _env_file(declaration, root))
    hidden = _secret_values(declaration, root)
    if "secrets" not in record:  # before anything is started with them
        record["secrets"] = {
            name: _secret_evidence(record, value)
            for name, value in zip(declaration.secrets.model_dump(), hidden, strict=True)
        }
        _write_private(paths.record, json.dumps(record, indent=2))
    try:
        if not record["claimed"]:
            _create_owned(project, record, paths)
    except StepFailed as exc:
        return _report(declaration, record, NOT_READY, [str(exc)])
    try:  # from here something may have started: a failed read is `not ready`, never a refusal
        _start(declaration, paths, record, hidden)
        deadline = time.monotonic() + readiness_seconds
        while problems := _readiness(declaration, record):
            settled = any(problem.startswith(_MISMATCH) for problem in problems)
            if settled or time.monotonic() > deadline:
                return _report(declaration, record, NOT_READY, problems)
            time.sleep(1)
    except (StepFailed, Refused) as exc:
        return _report(declaration, record, NOT_READY, [str(exc)])
    return _report(declaration, record, READY, [])


def installation_status(declaration: Declaration, root: Path) -> dict:
    """Reads only. Nothing runs in a container unless this record knows it and owns all it sees."""
    _check_root(root)
    paths = _Paths(root, declaration.slug)
    record = _read_record(paths, declaration)
    if record is None:
        return _report(declaration, {}, NOT_PROVISIONED, [])
    daemon = _read("info", "-f", "{{.ID}}").strip()
    unowned = _drift_problems(record, declaration, root, daemon)
    if not unowned and record.get("starting"):
        unowned = ["an apply did not finish; run apply again"]
    if unowned:
        return _report(declaration, {**record, "effects": None}, NOT_READY, unowned)
    project = _project(declaration)
    live = _read_live(project)
    if unowned := _ownership_problems(project, record, live):
        problems = [*unowned, *_runtime_drift(declaration, record, live)]
        return _report(declaration, {**record, "effects": None}, NOT_READY, problems)
    problems = _readiness(declaration, record)
    effects = _live_effects(record, live)
    return _report(
        declaration, {**record, "effects": effects}, NOT_READY if problems else READY, problems
    )
