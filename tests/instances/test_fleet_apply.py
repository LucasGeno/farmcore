"""`farmcore fleet apply|status`: the operator CLI drives the stock Compose file for two disposable
installations, proved over real HTTPS with real cookies. Resources belong to this run alone: slugs
are random, and teardown removes exactly what those slugs name.

Seams (approved spec, Testing Decisions 1 and 2): the CLI as a real process, a real temporary state
root, real Docker, and curl through a local TLS-terminating proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.instances.conftest import REPO, docker_available, free_port, postgis_image
from tests.instances.support import FARMCORE

LIMITS = {
    "app_memory_mib": 768,
    "app_cpu_millis": 1500,
    "database_memory_mib": 512,
    "database_cpu_millis": 700,
}


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@pytest.fixture(scope="module")
def pinned(image) -> Iterator[tuple[str, str]]:
    """The release under test and PostGIS, each as `repo@sha256:...`. A local build has no digest
    until it is pushed once, so a loopback registry made here gives it one, then goes away."""
    run = secrets.token_hex(4)
    port = free_port()
    registry = f"farmcore-fleet-registry-{run}"
    known = docker("image", "inspect", "registry:2", check=False).returncode == 0
    docker("run", "-d", "--rm", "--name", registry, "-p", f"127.0.0.1:{port}:5000", "registry:2")
    tag = f"127.0.0.1:{port}/farmcore-fleet-{run}:release"
    made = []
    try:
        docker("tag", image, tag)
        made.append(tag)
        for attempt in range(20):
            if docker("push", tag, check=attempt == 19).returncode == 0:
                break
        digests = json.loads(
            docker("image", "inspect", image, "-f", "{{json .RepoDigests}}").stdout
        )
        app = next(digest for digest in digests if digest.startswith(tag.rsplit(":", 1)[0]))
        made.append(app)
        docker("pull", postgis_image(), check=False)
        digests = json.loads(
            docker("image", "inspect", postgis_image(), "-f", "{{json .RepoDigests}}").stdout
        )
        yield app, digests[0]
    finally:
        docker("rm", "-f", "-v", registry, check=False)
        for name in [*made, *DERIVED]:
            docker("image", "rm", "-f", name, check=False)
        if not known:
            docker("image", "rm", "-f", "registry:2", check=False)


@dataclass
class Fleet:
    """An operator's private inventory and state root, and what this run may remove."""

    root: Path
    inventory: Path
    state: Path
    slugs: list[str] = field(default_factory=list)

    def declare(self, name: str, images: tuple[str, str], **changes) -> str:
        """A declaration on a free loopback port; its slug."""
        slug = f"t{secrets.token_hex(3)}-{name}"
        self.slugs.append(slug)
        self.write(slug, images, port=free_port(), **changes)
        return slug

    def write(self, slug: str, images: tuple[str, str], port: int, **limits) -> None:
        document = {
            "schema_version": 1,
            "slug": slug,
            "domain": f"{slug}.example.test",
            "images": {"app": images[0], "database": images[1]},
            "binding": {"host": "127.0.0.1", "port": port},
            "limits": {**LIMITS, **limits},
            "secrets": {
                "postgres_password": f"{slug}/postgres",
                "runtime_database_password": f"{slug}/runtime",
                "admin_token": f"{slug}/admin",
            },
        }
        (self.inventory / f"{slug}.yaml").write_text(json.dumps(document))

    def declared(self, slug: str) -> dict:
        return json.loads((self.inventory / f"{slug}.yaml").read_text())

    def cli(
        self, command: str, slug: str, *options: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                FARMCORE, "fleet", command, str(self.inventory), slug,
                "--state-root", str(self.state), "--test-domains", *options,
            ],
            capture_output=True, text=True, timeout=600, env=env or dict(os.environ),
        )  # fmt: skip

    def start(self, command: str, slug: str, env: dict[str, str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                FARMCORE, "fleet", command, str(self.inventory), slug,
                "--state-root", str(self.state), "--test-domains",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )  # fmt: skip

    def remove_everything_this_run_made(self) -> None:
        for slug in self.slugs:
            project = f"farmcore-fleet-{slug}"
            names = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}")
            for container in names.stdout.split():
                docker("rm", "-f", "-v", container, check=False)
            # By id: two racing creators can leave two networks of one name.
            for network in docker(
                "network", "ls", "-q", "--filter", f"name={project}_default"
            ).stdout.split():
                docker("network", "rm", network, check=False)
            for volume in ("pgdata", "whisper-cache", "backups"):
                docker("volume", "rm", f"{project}_{volume}", check=False)


def new_fleet(root: Path) -> Fleet:
    root.mkdir(exist_ok=True)
    inventory = root / "inventory"
    state = root / "state"
    inventory.mkdir()
    state.mkdir(mode=0o700)
    return Fleet(root, inventory, state)


@pytest.fixture
def own_fleet(tmp_path) -> Iterator[Fleet]:
    """One test's fleet, removed however the test ends: a failed refusal may have started one."""
    made = new_fleet(tmp_path)
    try:
        yield made
    finally:
        made.remove_everything_this_run_made()


@pytest.fixture(scope="module")
def fleet(tmp_path_factory) -> Iterator[Fleet]:
    made = new_fleet(tmp_path_factory.mktemp("fleet"))
    try:
        yield made
    finally:
        made.remove_everything_this_run_made()


@pytest.fixture(scope="module")
def alpha(fleet, pinned) -> str:
    slug = fleet.declare("a", pinned)
    fleet.applied_alpha = fleet.cli("apply", slug)
    return slug


FAKE_DIGESTS = (
    f"registry.example.test/app@sha256:{'1' * 64}",
    f"registry.example.test/db@sha256:{'2' * 64}",
)


def docker_that_must_not_run(directory: Path) -> tuple[dict[str, str], Path]:
    """An environment whose `docker` only records that it was called; and that record."""
    called = directory / "docker-was-called"
    shim = directory / "shim"
    shim.mkdir()
    (shim / "docker").write_text(f'#!/bin/sh\ntouch "{called}"\nexit 99\n')
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}, called


def docker_without_compose(directory: Path) -> dict[str, str]:
    """An environment whose real `docker` cannot run Compose: claims happen, startup does not."""
    shim = directory / "no-compose"
    shim.mkdir(exist_ok=True)
    real = shutil.which("docker")
    (shim / "docker").write_text(
        f'#!/bin/sh\nif [ "$1" = compose ]; then exit 98; fi\nexec "{real}" "$@"\n'
    )
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def docker_held_at(directory: Path, command: str, release: Path) -> tuple[dict[str, str], Path]:
    """An environment whose real `docker` stops at `command` until `release` exists; and the file
    that says it got there. The shim stays a child of the CLI meanwhile, like a running Compose."""
    shim = directory / f"held-{secrets.token_hex(3)}"
    shim.mkdir()
    arrived = shim / "arrived"
    (shim / "docker").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = {command} ]; then\n'
        f'  touch "{arrived}"\n'
        f'  while [ ! -e "{release}" ]; do sleep 0.05; done\n'
        f"fi\n"
        f'exec "{shutil.which("docker")}" "$@"\n'
    )
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}, arrived


def wait_for(path: Path, deadline_s: float = 60) -> None:
    deadline = time.monotonic() + deadline_s
    while not path.exists():
        assert time.monotonic() < deadline, f"never reached {path.name}"
        time.sleep(0.05)


def _relative(fleet: Fleet) -> str:
    return "relative/state"


def _missing(fleet: Fleet) -> str:
    return str(fleet.root / "absent")


def _through_a_symlink(fleet: Fleet) -> str:
    link = fleet.root / "link"
    link.symlink_to(fleet.state)
    return str(link)


def _readable_by_others(fleet: Fleet) -> str:
    fleet.state.chmod(0o755)
    return str(fleet.state)


def _inside_a_checkout(fleet: Fleet) -> str:
    (fleet.root / ".git").mkdir()
    return str(fleet.state)


@pytest.mark.parametrize(
    ("make_root", "says"),
    [
        (_relative, "absolute"),
        (_missing, "does not exist"),
        (_through_a_symlink, "symlink"),
        (_readable_by_others, "0700"),
        (_inside_a_checkout, "checkout"),
    ],
)
def test_an_unsafe_state_root_is_refused_before_anything_happens(make_root, says, tmp_path):
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    env, called = docker_that_must_not_run(tmp_path)
    fleet.state = Path(make_root(fleet))
    result = fleet.cli("apply", "alpha", env=env)
    assert result.returncode == 1, result.stderr
    assert says in result.stderr and "Traceback" not in result.stderr
    assert not called.exists()
    assert not (tmp_path / "state" / "installations").exists()


def _loose_secret(path: Path) -> None:
    path.write_text("a" * 48)
    path.chmod(0o644)


def _symlinked_secret(path: Path) -> None:
    target = path.parent.parent / "elsewhere"
    target.write_text("a" * 48)
    target.chmod(0o600)
    path.symlink_to(target)


def _unusable_secret(path: Path) -> None:
    path.write_text("short $value " + "SENTINEL-SECRET")
    path.chmod(0o600)


@pytest.mark.parametrize("plant", [_loose_secret, _symlinked_secret, _unusable_secret])
def test_a_secret_file_that_is_not_private_and_usable_is_refused_and_not_repeated(plant, tmp_path):
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    folder = fleet.state / "secrets" / "alpha"
    folder.mkdir(parents=True, mode=0o700)
    (fleet.state / "secrets").chmod(0o700)
    plant(folder / "postgres")
    before = os.lstat(folder / "postgres")
    env, called = docker_that_must_not_run(tmp_path)
    result = fleet.cli("apply", "alpha", env=env)
    assert result.returncode == 1, result.stderr
    assert "secrets.postgres_password" in result.stderr and "Traceback" not in result.stderr
    assert "SENTINEL-SECRET" not in result.stdout + result.stderr
    assert not called.exists()
    assert os.lstat(folder / "postgres").st_mtime_ns == before.st_mtime_ns


def test_apply_and_status_refuse_an_invalid_inventory_or_an_undeclared_slug(tmp_path):
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    env, called = docker_that_must_not_run(tmp_path)
    for command in ("apply", "status"):
        undeclared = fleet.cli(command, "other", env=env)
        assert undeclared.returncode == 1 and "not declared" in undeclared.stderr
    (fleet.inventory / "alpha.yaml").write_text("slug: [")
    invalid = fleet.cli("apply", "alpha", env=env)
    assert invalid.returncode == 1 and "not valid YAML" in invalid.stderr
    assert not called.exists() and not list(fleet.state.iterdir())


# Whatever an operator's shell happens to hold must not reach an installation.
POLLUTED = {
    "POSTGRES_PASSWORD": "polluted-postgres-password",
    "RUNTIME_DATABASE_PASSWORD": "polluted-runtime-password",
    "ADMIN_TOKEN": "polluted-admin-token-polluted-admin-token",
    "FARMCORE_BIND": "0.0.0.0",
    "FARMCORE_IMAGE": "polluted/image:latest",
    "COMPOSE_PROFILES": "telegram",
    "COMPOSE_PROJECT_NAME": "polluted",
    "COMPOSE_FILE": "/nonexistent/polluted.yml",
    "TELEGRAM_BOT_TOKEN": "123456:polluted",
    "TELEGRAM_ALLOWED_USER_IDS": "1",
    "EXTRACTOR": "vertex",
    "VERTEX_PROJECT": "polluted",
    "WHISPER_MODEL": "polluted",
    "DEMO_MODE": "true",
    "CLOCK_MODE": "live",
}


@pytest.fixture(scope="module")
def beta(fleet, pinned) -> str:
    """Applied from a polluted shell, so each check on B also covers that environment."""
    slug = fleet.declare("b", pinned)
    fleet.applied_beta = fleet.cli("apply", slug, env={**os.environ, **POLLUTED})
    assert fleet.applied_beta.returncode == 0, fleet.applied_beta.stderr
    return slug


def containers(slug: str, service: str | None = None) -> list[str]:
    filters = ["--filter", f"label=com.docker.compose.project=farmcore-fleet-{slug}"]
    if service:
        filters += ["--filter", f"label=com.docker.compose.service={service}"]
    return docker("ps", "-aq", *filters).stdout.split()


def container(slug: str, service: str) -> str:
    found = containers(slug, service)
    assert len(found) == 1, (slug, service, found)
    return found[0]


def in_app(slug: str, *command: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["docker", "exec", "-i", container(slug, "app"), *command],
        input=stdin, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def inspect(item: str, template: str) -> object:
    return json.loads(docker("inspect", "-f", "{{json " + template + "}}", item).stdout)


def reaches(slug: str, host: str) -> bool:
    probe = (
        "import socket, sys\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n"
        f"    s.connect(({host!r}, 5432))\n"
        "except OSError:\n"
        "    sys.exit(7)\n"
    )
    result = subprocess.run(
        ["docker", "exec", container(slug, "app"), "python", "-c", probe], capture_output=True
    )
    assert result.returncode in (0, 7), result.stderr
    return result.returncode == 0


class TlsProxy:
    """A co-located proxy: real TLS outside, the declared loopback binding inside, like production's
    but on a random port (`curl --connect-to` keeps the URL and cookies on the bare host name)."""

    def __init__(self, directory: Path) -> None:
        self.certificate, key = directory / "tls.crt", directory / "tls.key"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", self.certificate,
                "-days", "1", "-subj", "/CN=fleet-test", "-addext",
                "subjectAltName=DNS:*.example.test",
            ],
            check=True, capture_output=True,
        )  # fmt: skip
        self.routes: dict[str, int] = {}
        self.port = free_port()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, key)
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve() -> None:
            asyncio.set_event_loop(self._loop)
            start = asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=context)
            self._server = self._loop.run_until_complete(start)
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        assert ready.wait(10)

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            host = next(
                line.split(b":", 1)[1].strip().decode().split(":")[0]
                for line in lines
                if line.lower().startswith(b"host:")
            )
            upstream, sink = await asyncio.open_connection("127.0.0.1", self.routes[host])
            kept = [
                line
                for line in lines[1:]
                if line and not line.lower().startswith((b"connection:", b"x-forwarded-"))
            ]
            sink.write(
                b"\r\n".join([lines[0], *kept, b"X-Forwarded-Proto: https", b"Connection: close"])
                + b"\r\n\r\n"
            )

            async def pump(source, target) -> None:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
                if target.can_write_eof():
                    target.write_eof()

            await asyncio.gather(pump(reader, sink), pump(upstream, writer))
        except (OSError, KeyError, StopIteration, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)


@pytest.fixture(scope="module")
def tls(tmp_path_factory) -> Iterator[TlsProxy]:
    proxy = TlsProxy(tmp_path_factory.mktemp("tls"))
    try:
        yield proxy
    finally:
        proxy.close()


class Browser:
    """curl with one real cookie jar, on one installation's bare domain over real TLS."""

    def __init__(self, proxy: TlsProxy, domain: str, jar: Path) -> None:
        self.proxy, self.domain, self.jar = proxy, domain, jar

    def request(self, path: str, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "curl", "-sS", "--cacert", str(self.proxy.certificate),
                "--connect-to", f"{self.domain}:443:127.0.0.1:{self.proxy.port}",
                "-b", str(self.jar), "-c", str(self.jar), *options,
                f"https://{self.domain}{path}",
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )  # fmt: skip

    def status(self, path: str) -> int:
        return int(self.request(path, "-o", "/dev/null", "-w", "%{http_code}").stdout)

    def sign_in(self, login: str, password: str) -> int:
        page = self.request("/login").stdout
        csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
        return int(
            self.request(
                "/login", "-o", "/dev/null", "-w", "%{http_code}",
                "-H", f"Origin: https://{self.domain}",
                "--data-urlencode", f"login={login}",
                "--data-urlencode", f"password={password}",
                "--data-urlencode", f"csrf_token={csrf}",
            ).stdout
        )  # fmt: skip

    def cookies(self) -> list[list[str]]:
        """Each jar line's fields: `#HttpOnly_<domain>`, host-only, path, secure, expiry, name."""
        return [line.split("\t") for line in self.jar.read_text().splitlines() if "\t" in line]


def set_up_grower(slug: str, farm: str, password: str) -> str:
    """A farm and a member through the app's own CLI; the farm's id."""
    farm_id = in_app(slug, "farmcore", "farms", "create", farm).split()[1]
    in_app(
        slug, "farmcore", "users", "create", "grower", "--farm-id", farm_id, "--password-stdin",
        stdin=password + "\n",
    )  # fmt: skip
    return farm_id


def receipt(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


@pytest.mark.slow
@pytest.mark.skipif(not docker_available(), reason="needs a Docker daemon")
def test_apply_starts_a_declared_installation_and_reports_it_ready(fleet, alpha):
    result = fleet.applied_alpha
    assert result.returncode == 0, result.stderr
    report = receipt(result)
    assert report["state"] == "ready"
    assert report["release"] == "0.1.0" and report["alembic_heads"] == ["review_0001"]
    assert report["effects"] == "paused"
    assert report["installation_id"]

    status = fleet.cli("status", alpha)
    assert status.returncode == 0, status.stderr
    assert receipt(status)["installation_id"] == report["installation_id"]


@pytest.mark.slow
def test_two_installations_share_nothing_and_a_cookie_or_query_cannot_cross(
    fleet, alpha, beta, tls, tmp_path
):
    domain_a, domain_b = fleet.declared(alpha)["domain"], fleet.declared(beta)["domain"]
    tls.routes[domain_a] = fleet.declared(alpha)["binding"]["port"]
    tls.routes[domain_b] = fleet.declared(beta)["binding"]["port"]

    # Different generated identity and credentials.
    assert (
        receipt(fleet.applied_alpha)["installation_id"]
        != receipt(fleet.applied_beta)["installation_id"]
    )
    for name in ("postgres", "runtime", "admin"):
        ours = (fleet.state / "secrets" / alpha / name).read_text()
        assert len(ours) >= 32 and ours != (fleet.state / "secrets" / beta / name).read_text()

    # Only the declared loopback binding is published, by the app and nothing else.
    for slug in (alpha, beta):
        published = {}
        for item in containers(slug):
            for inner, bound in (inspect(item, ".NetworkSettings.Ports") or {}).items():
                if bound:
                    published[inner] = [(b["HostIp"], int(b["HostPort"])) for b in bound]
        binding = fleet.declared(slug)["binding"]
        assert published == {"8000/tcp": [("127.0.0.1", binding["port"])]}

    # A reaches its own database and cannot reach B's, by name or by address.
    network_b = f"farmcore-fleet-{beta}_default"
    b_db = container(beta, "db")
    address = inspect(b_db, ".NetworkSettings.Networks")[network_b]["IPAddress"]
    assert reaches(alpha, "db")
    assert not reaches(alpha, f"farmcore-fleet-{beta}-db-1")
    assert not reaches(alpha, address)

    # Real HTTPS: each grower signs in on their own host and sees their own farm's data.
    password_a, password_b = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    farm_a = set_up_grower(alpha, "Synthetic farm A", password_a)
    farm_b = set_up_grower(beta, "Synthetic farm B", password_b)
    browser_a = Browser(tls, domain_a, tmp_path / "a.jar")
    browser_b = Browser(tls, domain_b, tmp_path / "b.jar")
    assert browser_a.sign_in("grower", password_a) == 303
    assert browser_b.sign_in("grower", password_b) == 303
    assert browser_a.status(f"/fields/parcels.geojson?farm={farm_a}") == 200
    assert browser_b.status(f"/fields/parcels.geojson?farm={farm_b}") == 200
    assert "Synthetic farm B" not in in_app(alpha, "farmcore", "farms", "list")
    assert "Synthetic farm A" not in in_app(beta, "farmcore", "farms", "list")

    # A's password does not sign in on B unless B has the same one, and it does not.
    assert Browser(tls, domain_b, tmp_path / "wrong.jar").sign_in("grower", password_a) == 401

    # Host-only: no Domain, so curl records it for this exact host; Secure, set over real TLS.
    sessions = [c for c in browser_a.cookies() if "farmcore_session" in c[5]]
    assert len(sessions) == 1
    assert sessions[0][0] == f"#HttpOnly_{domain_a}" and sessions[0][1] == "FALSE"
    assert sessions[0][3] == "TRUE"
    # Replayed at B's host on purpose, A's own session cookie opens nothing there.
    stolen = tmp_path / "stolen.jar"
    stolen.write_text(browser_a.jar.read_text().replace(domain_a, domain_b))
    assert Browser(tls, domain_b, stolen).status(f"/fields/parcels.geojson?farm={farm_b}") == 403


@pytest.mark.slow
def test_a_polluted_shell_reaches_no_installation_and_every_connector_stays_off(fleet, beta):
    assert {
        inspect(item, '(index .Config.Labels "com.docker.compose.service")')
        for item in containers(beta)
    } == {"db", "migrate", "app", "backup"}
    environment = dict(
        line.split("=", 1) for line in in_app(beta, "env").splitlines() if "=" in line
    )
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "VERTEX_PROJECT",
        "WHISPER_MODEL",
    ):
        assert environment[name] == "", name
    assert environment["EXTRACTOR"] == "regex"
    assert environment["DEMO_MODE"] == "false"
    assert environment["CLOCK_MODE"] == "replay"
    assert "polluted" not in environment["DATABASE_URL"]
    assert "polluted" not in environment["ADMIN_TOKEN"]
    assert in_app(beta, "farmcore", "effects").strip() == "external effects: paused"


def cgroup(item: str, name: str) -> str:
    return docker("exec", item, "cat", f"/sys/fs/cgroup/{name}").stdout.strip()


@pytest.mark.slow
def test_containers_enforce_the_declared_limits(fleet, alpha):
    app, db = container(alpha, "app"), container(alpha, "db")
    assert cgroup(app, "memory.max") == str(768 * 1024 * 1024)
    assert cgroup(app, "cpu.max") == "150000 100000"
    assert cgroup(db, "memory.max") == str(512 * 1024 * 1024)
    assert cgroup(db, "cpu.max") == "70000 100000"
    # Backup and migrate run the release too: bounded by the app's declaration.
    for service in ("backup", "migrate"):
        item = container(alpha, service)
        assert inspect(item, ".HostConfig.Memory") == 768 * 1024 * 1024
        assert inspect(item, ".HostConfig.NanoCpus") == 1_500_000_000


COMPOSE_PROJECT = "com.docker.compose.project"


def _foreign_volume(project: str, image: str) -> tuple[str, str]:
    name = f"{project}_pgdata"
    docker("volume", "create", "--label", f"{COMPOSE_PROJECT}={project}", name)
    return "volume", name


def _foreign_backups_volume(project: str, image: str) -> tuple[str, str]:
    name = f"{project}_backups"
    docker("volume", "create", name)
    return "volume", name


def _foreign_network(project: str, image: str) -> tuple[str, str]:
    name = f"{project}_default"
    docker("network", "create", "--label", f"{COMPOSE_PROJECT}={project}", name)
    return "network", name


def _foreign_labelled_container(project: str, image: str) -> tuple[str, str]:
    name = f"unrelated-{secrets.token_hex(4)}"
    docker("create", "--name", name, "--label", f"{COMPOSE_PROJECT}={project}", image)
    return "container", name


def _foreign_named_container(project: str, image: str) -> tuple[str, str]:
    name = f"{project}-db-1"
    docker("create", "--name", name, image)
    return "container", name


def remove_foreign(kind: str, name: str) -> None:
    command = {"volume": ("volume", "rm"), "network": ("network", "rm")}.get(kind, ("rm", "-f"))
    docker(*command, name, check=False)  # the fleet's own teardown may have taken it by name


def untouched_snapshot(kind: str, name: str) -> str:
    return docker(*(() if kind == "container" else (kind,)), "inspect", name).stdout


@pytest.mark.slow
@pytest.mark.parametrize(
    "plant",
    [
        _foreign_volume,
        _foreign_backups_volume,
        _foreign_network,
        _foreign_labelled_container,
        _foreign_named_container,
    ],
)
def test_a_foreign_resource_of_the_project_stops_apply_before_any_change(plant, pinned, own_fleet):
    fleet = own_fleet
    slug = fleet.declare("f", pinned)
    project = f"farmcore-fleet-{slug}"
    kind, name = plant(project, pinned[1])
    try:
        before = untouched_snapshot(kind, name)
        result = fleet.cli("apply", slug)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "not adopting" in result.stderr
        assert untouched_snapshot(kind, name) == before
        assert not (fleet.state / "installations" / slug / "ownership.json").exists()
        assert len(docker("volume", "ls", "-q", "--filter", f"name={project}_").stdout.split()) == (
            1 if kind == "volume" else 0
        )
    finally:
        fleet.remove_everything_this_run_made()  # first: a container may hold what was planted
        remove_foreign(kind, name)


@pytest.mark.slow
def test_an_occupied_port_stops_apply_before_any_change(pinned, own_fleet):
    fleet = own_fleet
    slug = fleet.declare("p", pinned)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", fleet.declared(slug)["binding"]["port"]))
        holder.listen()
        result = fleet.cli("apply", slug)
    assert result.returncode == 1 and "binding" in result.stderr, result.stderr
    assert not docker("volume", "ls", "-q", "--filter", f"name=farmcore-fleet-{slug}_").stdout


@pytest.mark.slow
def test_a_declared_image_that_is_not_on_this_host_is_refused_before_any_change(own_fleet):
    fleet = own_fleet
    fleet.slugs.append(slug := f"t{secrets.token_hex(3)}-i")
    fleet.write(slug, FAKE_DIGESTS, port=free_port())
    result = fleet.cli("apply", slug)
    assert result.returncode == 1 and "docker pull" in result.stderr, result.stderr
    assert not docker("volume", "ls", "-q", "--filter", f"name=farmcore-fleet-{slug}_").stdout


@pytest.mark.slow
def test_a_neighbouring_slug_is_not_mistaken_for_this_installation(pinned, own_fleet, tmp_path):
    fleet = own_fleet
    slug = fleet.declare("n", pinned)
    neighbour = f"farmcore-fleet-{slug}-other-db-1"
    docker("create", "--name", neighbour, pinned[1])
    try:
        result = fleet.cli("apply", slug, env=docker_without_compose(tmp_path))
        assert result.returncode == 3, result.stderr
    finally:
        docker("rm", "-f", neighbour)
        fleet.remove_everything_this_run_made()


@pytest.mark.slow
def test_a_lost_record_or_another_state_root_never_adopts_a_live_installation(
    fleet, alpha, tmp_path
):
    other = new_fleet(tmp_path)
    shutil.copy(fleet.inventory / f"{alpha}.yaml", other.inventory)
    before = docker("volume", "inspect", f"farmcore-fleet-{alpha}_pgdata").stdout
    result = other.cli("apply", alpha)
    assert result.returncode == 1 and "not adopting" in result.stderr, result.stderr
    assert docker("volume", "inspect", f"farmcore-fleet-{alpha}_pgdata").stdout == before
    assert fleet.cli("status", alpha).returncode == 0


@pytest.mark.slow
def test_a_replaced_or_missing_resource_of_a_claimed_installation_is_drift_never_repair(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("d", pinned)
    project = f"farmcore-fleet-{slug}"
    env = docker_without_compose(tmp_path)
    try:
        assert fleet.cli("apply", slug, env=env).returncode == 3
        docker("volume", "rm", f"{project}_pgdata")
        missing = fleet.cli("apply", slug, env=env)
        assert missing.returncode == 1 and "drift" in missing.stderr, missing.stderr
        assert not docker("volume", "ls", "-q", "--filter", f"name={project}_pgdata").stdout
        docker("volume", "create", "--label", "farmcore.fleet.owner=other", f"{project}_pgdata")
        replaced = fleet.cli("apply", slug, env=env)
        assert replaced.returncode == 1 and "not adopting" in replaced.stderr, replaced.stderr
    finally:
        fleet.remove_everything_this_run_made()


@pytest.mark.slow
def test_a_second_apply_in_the_same_state_root_refuses_while_the_first_runs(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("l", pinned)
    release = tmp_path / "release"
    env, arrived = docker_held_at(tmp_path, "volume", release)
    # Held at its first `docker volume` read, the start of preflight: the lock must be held by then.
    first = fleet.start("apply", slug, env)
    try:
        wait_for(arrived)
        second = fleet.cli("apply", slug, env=docker_without_compose(tmp_path))
        assert second.returncode == 1, second.stdout + second.stderr
        assert "another apply" in second.stderr
    finally:
        first.kill()
        first.communicate(timeout=30)
        release.touch()
        fleet.remove_everything_this_run_made()


@pytest.mark.slow
def test_a_killed_apply_keeps_the_lock_until_the_compose_it_started_has_exited(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("k", pinned)
    release = tmp_path / "release"
    env, arrived = docker_held_at(tmp_path, "compose", release)
    first = fleet.start("apply", slug, env)
    try:
        wait_for(arrived)
        first.send_signal(signal.SIGKILL)
        first.wait(10)
        refused = fleet.cli("apply", slug, env=docker_without_compose(tmp_path))
        assert refused.returncode == 1 and "another apply" in refused.stderr, refused.stderr
    finally:
        release.touch()
    try:
        deadline = time.monotonic() + 60
        while True:  # the orphan shim sees the release file and gives the lock back on exit
            retried = fleet.cli("apply", slug, env=docker_without_compose(tmp_path))
            if "another apply" not in retried.stderr or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        assert retried.returncode == 3, retried.stdout + retried.stderr
    finally:
        fleet.remove_everything_this_run_made()


@pytest.mark.slow
def test_two_state_roots_racing_for_one_slug_end_with_one_owner_and_the_loser_refuses(
    pinned, own_fleet, tmp_path
):
    one, two = own_fleet, new_fleet(tmp_path / "2")
    slug = one.declare("r", pinned)
    shutil.copy(one.inventory / f"{slug}.yaml", two.inventory)
    gate = tmp_path / "gate"
    gate.mkdir()
    shim = tmp_path / "barrier"
    shim.mkdir()
    # Both have passed preflight when they reach the first `volume create`; only then go on.
    (shim / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = compose ]; then exit 98; fi\n'
        "if [ \"$1 $2\" = 'volume create' ]; then\n"
        f'  touch "{gate}/$PPID"\n'
        f'  while [ "$(ls "{gate}" | wc -l)" -lt 2 ]; do sleep 0.01; done\n'
        "fi\n"
        f'exec "{shutil.which("docker")}" "$@"\n'
    )
    (shim / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    try:
        racers = [one.start("apply", slug, env), two.start("apply", slug, env)]
        outcomes = [racer.communicate(timeout=120) for racer in racers]
        codes = sorted(racer.returncode for racer in racers)
        assert codes == [1, 3], outcomes
        loser = outcomes[[racer.returncode for racer in racers].index(1)][1]
        assert "another state root" in loser
        winner = (one, two)[[racer.returncode for racer in racers].index(3)]
        record = json.loads((winner.state / "installations" / slug / "ownership.json").read_text())
        labels = inspect(f"farmcore-fleet-{slug}_pgdata", ".Labels")
        assert labels["farmcore.fleet.owner"] == record["owner_id"]
        networks = docker("network", "ls", "-q", "--filter", f"name=farmcore-fleet-{slug}_default")
        assert len(networks.stdout.split()) == 1
    finally:
        one.remove_everything_this_run_made()


def secret_bytes(fleet: Fleet, slug: str) -> dict[str, str]:
    return {
        name: (fleet.state / "secrets" / slug / name).read_text()
        for name in ("postgres", "runtime", "admin")
    }


def record_of(fleet: Fleet, slug: str) -> dict:
    return json.loads((fleet.state / "installations" / slug / "ownership.json").read_text())


@pytest.mark.slow
def test_a_repeated_apply_keeps_credentials_identity_data_and_an_operators_effects_choice(
    fleet, alpha
):
    first = receipt(fleet.applied_alpha)
    credentials, owner = secret_bytes(fleet, alpha), record_of(fleet, alpha)["owner_id"]
    in_app(alpha, "farmcore", "farms", "create", "Kept across apply")
    app, db = container(alpha, "app"), container(alpha, "db")
    # The operator's explicit choice, made as the database owner like the docs say.
    docker("exec", container(alpha, "backup"), "farmcore", "effects", "enable")
    try:
        again = fleet.cli("apply", alpha)
        assert again.returncode == 0, again.stderr
        report = receipt(again)
        assert report["installation_id"] == first["installation_id"]
        assert report["effects"] == "enabled"
        assert in_app(alpha, "farmcore", "effects").strip() == "external effects: enabled"
        assert receipt(fleet.cli("status", alpha))["effects"] == "enabled"
    finally:
        docker("exec", container(alpha, "backup"), "farmcore", "effects", "pause")
    assert receipt(fleet.cli("status", alpha))["effects"] == "paused"  # live, not what was saved
    assert secret_bytes(fleet, alpha) == credentials
    assert record_of(fleet, alpha)["owner_id"] == owner
    assert "Kept across apply" in in_app(alpha, "farmcore", "farms", "list")
    assert (container(alpha, "app"), container(alpha, "db")) == (app, db)  # not even recreated
    assert len(containers(alpha)) == 4


@pytest.mark.slow
@pytest.mark.parametrize(
    ("section", "field", "value", "says"),
    [
        ("binding", "port", None, "binding"),
        ("limits", "app_memory_mib", 1024, "limits"),
        ("images", "app", f"registry.example.test/app@sha256:{'3' * 64}", "images"),
    ],
)
def test_a_changed_declaration_is_refused_and_changes_nothing(
    fleet, alpha, section, field, value, says
):
    original = fleet.declared(alpha)
    changed = json.loads(json.dumps(original))
    changed[section][field] = free_port() if value is None else value
    app = container(alpha, "app")
    (fleet.inventory / f"{alpha}.yaml").write_text(json.dumps(changed))
    try:
        result = fleet.cli("apply", alpha)
    finally:
        (fleet.inventory / f"{alpha}.yaml").write_text(json.dumps(original))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "drift" in result.stderr and says in result.stderr
    assert container(alpha, "app") == app
    assert fleet.cli("status", alpha).returncode == 0
    (fleet.inventory / f"{alpha}.yaml").write_text(json.dumps(changed))
    try:
        status = fleet.cli("status", alpha)
    finally:
        (fleet.inventory / f"{alpha}.yaml").write_text(json.dumps(original))
    assert status.returncode == 3 and "drift" in " ".join(receipt(status)["problems"])


@pytest.mark.slow
def test_a_record_from_another_docker_daemon_is_refused(pinned, own_fleet, tmp_path):
    fleet = own_fleet
    slug = fleet.declare("o", pinned)
    env = docker_without_compose(tmp_path)
    try:
        assert fleet.cli("apply", slug, env=env).returncode == 3
        path = fleet.state / "installations" / slug / "ownership.json"
        record = json.loads(path.read_text())
        path.write_text(json.dumps({**record, "daemon_id": "another-daemon"}))
        result = fleet.cli("apply", slug, env=env)
        assert result.returncode == 1 and "another Docker daemon" in result.stderr, result.stderr
    finally:
        fleet.remove_everything_this_run_made()


GOOD_HEALTH = {
    "db": "ok",
    "installation_id": "saved-id",
    "release": "0.1.0",
    "alembic_heads": ["review_0001"],
    "state": "ok",
}
SAVED = {"installation_id": "saved-id", "release": "0.1.0", "heads": ["review_0001"]}


@pytest.mark.parametrize(
    ("health", "says"),
    [
        ((200, GOOD_HEALTH), None),
        (None, "does not answer"),
        ((503, {**GOOD_HEALTH, "state": "failed"}), "503"),
        ((200, {**GOOD_HEALTH, "installation_id": "other"}), "installation_id"),
        ((200, {**GOOD_HEALTH, "release": "9.9.9"}), "release"),
        ((200, {**GOOD_HEALTH, "alembic_heads": ["other"]}), "alembic_heads"),
        ((200, {**GOOD_HEALTH, "state": "pending"}), "state"),
        ((200, ["not", "a", "report"]), "not a health report"),
    ],
)
def test_health_counts_as_ready_only_if_it_agrees_with_the_saved_identity(health, says):
    from farmcore.hosting.apply import identity_problems

    problems = identity_problems(SAVED, health)
    assert (not problems) if says is None else says in " ".join(problems)


def status_of_nothing(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    env, _ = docker_that_must_not_run(tmp_path)
    return fleet.cli("status", "alpha", env=env)


def test_status_of_an_installation_never_applied_says_so_and_touches_nothing(tmp_path):
    result = status_of_nothing(tmp_path)
    assert result.returncode == 3, result.stderr
    assert receipt(result)["state"] == "not provisioned"
    assert not (tmp_path / "state" / "installations").exists()


DERIVED: list[str] = []


def derived_image(pinned_app: str, name: str, directory: Path, dockerfile: str) -> str:
    """Another release in this run's registry: built from the one under test, pinned by digest."""
    repository = pinned_app.split("@")[0]
    directory.mkdir(exist_ok=True)
    (directory / "Dockerfile").write_text(f"FROM {pinned_app}\n{dockerfile}")
    tag = f"{repository}:{name}"
    docker("build", "-q", "-t", tag, str(directory))
    DERIVED.append(tag)
    docker("push", tag)
    digests = json.loads(docker("image", "inspect", tag, "-f", "{{json .RepoDigests}}").stdout)
    pinned = next(digest for digest in digests if digest.startswith(f"{repository}@"))
    DERIVED.append(pinned)
    return pinned


FORGED_SERVER = """
import http.server, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"db": "ok", "installation_id": "forged", "release": "0.1.0",
                           "alembic_heads": ["review_0001"], "state": "ok"}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

http.server.HTTPServer(("", 8000), Handler).serve_forever()
"""


@pytest.mark.slow
def test_a_docker_healthy_app_with_a_forged_health_is_not_ready_and_b_stays_healthy(
    fleet, beta, pinned, tls, own_fleet, tmp_path
):
    (tmp_path / "forged").mkdir()
    (tmp_path / "forged" / "forged.py").write_text(FORGED_SERVER)
    # COPY keeps the mode, and the image runs as a non-root user: readable whatever the umask.
    (tmp_path / "forged" / "forged.py").chmod(0o644)
    forged = derived_image(
        pinned[0], "forged", tmp_path / "forged",
        'COPY forged.py /tmp/forged.py\nCMD ["python", "/tmp/forged.py"]\n',
    )  # fmt: skip
    slug = own_fleet.declare("x", (forged, pinned[1]))
    before = receipt(fleet.cli("status", beta))
    result = own_fleet.cli("apply", slug, "--readiness-seconds", "60")
    assert result.returncode == 3, result.stdout + result.stderr
    report = receipt(result)
    assert report["state"] == "not ready"
    assert "installation_id" in " ".join(report["problems"])
    assert own_fleet.cli("status", slug).returncode == 3
    # The other installation: same identity, and it answers over real TLS.
    domain = fleet.declared(beta)["domain"]
    tls.routes[domain] = fleet.declared(beta)["binding"]["port"]
    assert Browser(tls, domain, tmp_path / "b.jar").status("/health") == 200
    assert receipt(fleet.cli("status", beta))["installation_id"] == before["installation_id"]


@pytest.mark.slow
def test_stopping_a_leaves_b_healthy_and_a_retry_brings_a_back_with_the_same_identity(
    fleet, alpha, beta, tls, tmp_path
):
    identity = receipt(fleet.cli("status", alpha))["installation_id"]
    domain = fleet.declared(beta)["domain"]
    tls.routes[domain] = fleet.declared(beta)["binding"]["port"]
    docker("stop", container(alpha, "app"))
    stopped = fleet.cli("status", alpha)
    assert stopped.returncode == 3 and receipt(stopped)["state"] == "not ready"
    assert Browser(tls, domain, tmp_path / "b.jar").status("/health") == 200
    assert fleet.cli("status", beta).returncode == 0
    back = fleet.cli("apply", alpha)
    assert back.returncode == 0, back.stdout + back.stderr
    assert receipt(back)["installation_id"] == identity


def hand_run_compose(fleet: Fleet, slug: str, **shell: str) -> None:
    """The stock Compose run by hand with a shell override, outside any apply."""
    paths = fleet.state / "installations" / slug
    subprocess.run(
        [
            "docker", "compose", "-p", f"farmcore-fleet-{slug}",
            "-f", str(REPO / "docker-compose.yml"), "--project-directory", str(paths),
            "--env-file", str(paths / "compose.env"),
            "up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "120",
            "--no-deps", "app",
        ],
        env={**os.environ, **shell}, check=True, capture_output=True,
    )  # fmt: skip


def _lowered_limit(fleet, slug, other_slug, pinned, tmp_path):
    app = container(slug, "app")
    expected = inspect(app, ".HostConfig.Memory")
    docker("update", "--memory", "600m", "--memory-swap", "600m", app)

    def restore() -> None:
        docker("update", "--memory", str(expected), "--memory-swap", str(expected), app)

    return restore, lambda: inspect(app, ".HostConfig.Memory") == 600 * 1024 * 1024


def _connected_to_another_network(fleet, slug, other_slug, pinned, tmp_path):
    app = container(slug, "app")
    network = f"farmcore-fleet-{other_slug}_default"
    docker("network", "connect", network, app)
    # The harm this is about: A's app now reaches B's database.
    assert reaches(slug, f"farmcore-fleet-{other_slug}-db-1")

    def restore() -> None:
        docker("network", "disconnect", network, app)

    return restore, lambda: network in inspect(app, ".NetworkSettings.Networks")


def _replaced_by_hand(**shell):
    def make(fleet, slug, app):
        hand_run_compose(fleet, slug, **shell)
        replaced = container(slug, "app")
        assert replaced != app

        def restore() -> None:
            docker("rm", "-f", replaced)

        return restore, lambda: container(slug, "app") == replaced

    return make


def _other_image(fleet, slug, other_slug, pinned, tmp_path):
    other = derived_image(pinned[0], "relabelled", tmp_path / "other", "LABEL relabelled=yes\n")
    return _replaced_by_hand(FARMCORE_IMAGE=other)(fleet, slug, container(slug, "app"))


def _widened_binding(fleet, slug, other_slug, pinned, tmp_path):
    return _replaced_by_hand(FARMCORE_BIND="0.0.0.0")(fleet, slug, container(slug, "app"))


@pytest.mark.slow
@pytest.mark.parametrize(
    ("change", "says"),
    [
        (_lowered_limit, "limits"),
        (_connected_to_another_network, "network"),
        (_other_image, "image"),
        (_widened_binding, "binding"),
    ],
)
def test_runtime_drift_is_not_ready_and_apply_refuses_it_without_repair(
    change, says, fleet, alpha, beta, pinned, tmp_path
):
    """Each of these still answers /health correctly: only the runtime evidence can tell."""
    restore, still_changed = change(fleet, alpha, beta, pinned, tmp_path)
    try:
        status = fleet.cli("status", alpha)
        assert status.returncode == 3, status.stdout + status.stderr
        assert says in " ".join(receipt(status)["problems"])
        applied = fleet.cli("apply", alpha, "--readiness-seconds", "1")
        assert applied.returncode == 1, applied.stdout + applied.stderr
        assert says in applied.stderr
        assert still_changed()  # refused, so the changed resource is exactly as it was
    finally:
        restore()
    back = fleet.cli("apply", alpha)
    assert back.returncode == 0, back.stdout + back.stderr
    assert fleet.cli("status", alpha).returncode == 0


def docker_failing_noisily(directory: Path, env_file: Path) -> dict[str, str]:
    """A Compose that fails and, as a bad error message might, repeats the whole env file, then
    one secret again exactly where keeping only the output's last 2000 characters would cut it."""
    shim = directory / "noisy"
    shim.mkdir()
    line = "the operator can act on this line\n"
    padding = 2000 - 24 - len(line)  # 24 of the 48-character secret stay inside the window
    (shim / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = compose ]; then\n'
        f'  cat "{env_file}" >&2\n'
        f"  sed -n 's/^ADMIN_TOKEN=//p' \"{env_file}\" | tr -d '\\n' >&2\n"
        f"  printf '%{padding}s' '' >&2\n"
        f"  printf '{line.strip()}\\n' >&2\n"
        "  exit 1\n"
        "fi\n"
        f'exec "{shutil.which("docker")}" "$@"\n'
    )
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def fragments(value: str, size: int = 12) -> set[str]:
    return {value[start : start + size] for start in range(len(value) - size + 1)}


@pytest.mark.slow
def test_a_failed_step_is_diagnosed_without_a_secret_in_any_output_or_receipt(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("s", pinned)
    env_file = fleet.state / "installations" / slug / "compose.env"
    result = fleet.cli("apply", slug, env=docker_failing_noisily(tmp_path, env_file))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "the operator can act on this line" in result.stdout
    planned = subprocess.run(
        [FARMCORE, "fleet", "plan", str(fleet.inventory), "--test-domains"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    receipts = fleet.state / "installations" / slug / "receipts.jsonl"
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o600
    last = json.loads(receipts.read_text().splitlines()[-1])
    assert last["state"] == "not ready" and last["at"]
    record = (fleet.state / "installations" / slug / "ownership.json").read_text()
    outputs = (result.stdout, result.stderr, receipts.read_text(), planned.stdout, record)
    for value in secret_bytes(fleet, slug).values():
        for fragment in fragments(value):
            assert not any(fragment in output for output in outputs)
    for private in (env_file, fleet.state / "secrets" / slug / "postgres"):
        assert stat.S_IMODE(private.stat().st_mode) == 0o600


def docker_killing_the_cli(directory: Path, when: str) -> dict[str, str]:
    """A real `docker` that SIGKILLs the CLI at one startup boundary: before the database starts,
    before the app starts, or just after the app was started."""
    shim = directory / when
    shim.mkdir(exist_ok=True)
    real = shutil.which("docker")
    is_up = 'case " $* " in *" up -d "*) ;; *) exec "' + real + '" "$@" ;; esac\n'
    is_db = 'case " $* " in *" db ") DB=1 ;; *) DB=0 ;; esac\n'
    body = {
        "before-database": "if [ $DB = 1 ]; then kill -9 $PPID; exit 137; fi\n",
        "before-app": "if [ $DB = 0 ]; then kill -9 $PPID; exit 137; fi\n",
        "after-app": f'"{real}" "$@"\nif [ $DB = 0 ]; then kill -9 $PPID; fi\nexit 0\n',
    }[when]
    (shim / "docker").write_text(f'#!/bin/sh\n{is_up}{is_db}{body}exec "{real}" "$@"\n')
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def running(slug: str) -> list[str]:
    return docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.project=farmcore-fleet-{slug}"
    ).stdout.split()


def effects_setting(slug: str) -> str:
    return docker(
        "exec", container(slug, "db"), "psql", "-U", "farmcore", "-d", "farmcore", "-Atc",
        "SELECT setconfig FROM pg_db_role_setting",
    ).stdout  # fmt: skip


@pytest.mark.slow
def test_interrupted_at_each_boundary_a_retry_converges_on_the_same_owned_installation(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("i", pinned)

    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "before-database"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    credentials, owner = secret_bytes(fleet, slug), record_of(fleet, slug)["owner_id"]
    assert running(slug) == []

    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "before-app"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert container(slug, "app") not in running(slug)
    assert "paused" in effects_setting(slug)
    identity = record_of(fleet, slug)["installation_id"]

    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "after-app"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert container(slug, "app") in running(slug)
    assert "paused" in effects_setting(slug)

    done = fleet.cli("apply", slug)
    assert done.returncode == 0, done.stdout + done.stderr
    assert receipt(done)["installation_id"] == identity
    in_app(slug, "farmcore", "farms", "create", "Kept across interruption")

    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "after-app"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    done = fleet.cli("apply", slug)
    assert done.returncode == 0, done.stdout + done.stderr

    assert secret_bytes(fleet, slug) == credentials
    assert record_of(fleet, slug)["owner_id"] == owner
    assert receipt(done)["installation_id"] == identity
    assert "Kept across interruption" in in_app(slug, "farmcore", "farms", "list")
    assert len(containers(slug)) == 4
    project = f"farmcore-fleet-{slug}"
    assert (
        len(docker("network", "ls", "-q", "--filter", f"name={project}_default").stdout.split())
        == 1
    )
    assert len(docker("volume", "ls", "-q", "--filter", f"name={project}_").stdout.split()) == 3


def run_foreign_app(project: str, image: str, name: str) -> str:
    """A running stranger carrying every public mark of this project's app, on its own network."""
    return docker(
        "run", "-d", "--name", name, "--entrypoint", "sleep",
        "--label", f"{COMPOSE_PROJECT}={project}",
        "--label", "com.docker.compose.service=app",
        "--network", f"{project}_default", image, "300",
    ).stdout.strip()  # fmt: skip


def docker_logging_exec(directory: Path) -> tuple[dict[str, str], Path]:
    """A real `docker` that notes every `docker exec`; and that note."""
    shim, log = directory / "logging", directory / "exec.log"
    shim.mkdir()
    (shim / "docker").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = exec ]; then echo "$*" >> "{log}"; fi\n'
        f'exec "{shutil.which("docker")}" "$@"\n'
    )
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}, log


@pytest.mark.slow
def test_a_recreated_volume_with_the_copied_owner_label_is_refused_as_replaced(
    pinned, own_fleet, tmp_path
):
    """The owner id is readable by anyone who can inspect the volume: it cannot prove the data."""
    fleet = own_fleet
    slug = fleet.declare("v", pinned)
    project = f"farmcore-fleet-{slug}"
    env = docker_without_compose(tmp_path)
    assert fleet.cli("apply", slug, env=env).returncode == 3
    name = f"{project}_pgdata"
    labels = inspect(name, ".Labels")
    created = inspect(name, ".CreatedAt")
    arguments = [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]
    while inspect(name, ".CreatedAt") == created:  # CreatedAt has whole seconds
        docker("volume", "rm", name)
        docker("volume", "create", *arguments, name)
    result = fleet.cli("apply", slug, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not adopting" in result.stderr and "pgdata" in result.stderr


@pytest.mark.slow
def test_an_unknown_container_with_this_projects_labels_on_its_network_is_never_acted_on(
    pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("c", pinned)
    project = f"farmcore-fleet-{slug}"
    assert fleet.cli("apply", slug, env=docker_without_compose(tmp_path)).returncode == 3
    stranger = run_foreign_app(project, pinned[1], f"unrelated-{slug}")
    env, execs = docker_logging_exec(tmp_path)
    applied = fleet.cli("apply", slug, "--readiness-seconds", "1", env=env)
    assert applied.returncode == 1, applied.stdout + applied.stderr
    assert "not adopting" in applied.stderr
    status = fleet.cli("status", slug, env=env)
    assert status.returncode == 3, status.stdout + status.stderr
    assert receipt(status)["effects"] is None
    assert not execs.exists()
    assert containers(slug, "app") == [stranger[:12]]  # neither replaced nor stopped
    assert inspect(stranger, ".State.Status") == "running"


@pytest.mark.slow
def test_a_stranger_arriving_during_an_interrupted_apply_is_not_taken_for_its_container(
    pinned, own_fleet, tmp_path
):
    """Re-entry must accept what the killed apply started, and nothing else on that account."""
    fleet = own_fleet
    slug = fleet.declare("e", pinned)
    project = f"farmcore-fleet-{slug}"
    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "before-app"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    stranger = run_foreign_app(project, pinned[1], f"unrelated-{slug}")
    env, execs = docker_logging_exec(tmp_path)
    interrupted = fleet.cli("status", slug, env=env)
    assert interrupted.returncode == 3 and receipt(interrupted)["effects"] is None
    assert not execs.exists()
    result = fleet.cli("apply", slug, "--readiness-seconds", "1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not adopting" in result.stderr
    assert inspect(stranger, ".State.Status") == "running"


@pytest.mark.slow
def test_status_on_another_daemon_or_with_drift_runs_nothing_in_any_container(
    fleet, alpha, tmp_path
):
    path = fleet.state / "installations" / alpha / "ownership.json"
    original = path.read_text()
    path.write_text(json.dumps({**json.loads(original), "daemon_id": "another-daemon"}))
    env, execs = docker_logging_exec(tmp_path)
    try:
        status = fleet.cli("status", alpha, env=env)
    finally:
        path.write_text(original)
    assert status.returncode == 3, status.stdout + status.stderr
    assert receipt(status)["effects"] is None and "another Docker daemon" in " ".join(
        receipt(status)["problems"]
    )
    assert not execs.exists()


def test_a_state_directory_with_unknown_content_and_no_record_is_refused_untouched(tmp_path):
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    folder = fleet.state / "installations"
    folder.mkdir(mode=0o700)
    (folder / "alpha").mkdir(mode=0o700)
    planted = {"compose.env": "UNRELATED=1\n", "notes.txt": "someone else's\n"}
    for name, content in planted.items():
        (folder / "alpha" / name).write_text(content)
    env, called = docker_that_must_not_run(tmp_path)
    result = fleet.cli("apply", "alpha", env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not adopting" in result.stderr and "Traceback" not in result.stderr
    assert not called.exists()
    assert {path.name: path.read_text() for path in (folder / "alpha").iterdir()} == planted


def _secrets_folder_is_a_symlink(state: Path, slug: str, outside: Path) -> None:
    (state / "secrets").mkdir(mode=0o700)
    (state / "secrets" / slug).symlink_to(outside)


def _secrets_root_is_a_symlink(state: Path, slug: str, outside: Path) -> None:
    holder = outside.parent / "holder"
    holder.mkdir(mode=0o700)
    (holder / slug).symlink_to(outside)
    (state / "secrets").symlink_to(holder)


def _secrets_folder_is_loose(state: Path, slug: str, outside: Path) -> None:
    (state / "secrets").mkdir(mode=0o700)
    shutil.copytree(outside, state / "secrets" / slug)
    (state / "secrets" / slug).chmod(0o755)


@pytest.mark.parametrize(
    "plant",
    [_secrets_folder_is_a_symlink, _secrets_root_is_a_symlink, _secrets_folder_is_loose],
)
def test_secret_storage_above_the_file_must_be_private_real_directories(plant, tmp_path):
    """Even when every value is already there and valid, so nothing would be created."""
    fleet = new_fleet(tmp_path)
    fleet.write("alpha", FAKE_DIGESTS, port=free_port())
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    for name in ("postgres", "runtime", "admin"):
        (outside / name).write_text("a" * 48)
        (outside / name).chmod(0o600)
    plant(fleet.state, "alpha", outside)
    env, called = docker_that_must_not_run(tmp_path)
    result = fleet.cli("apply", "alpha", env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "secrets" in result.stderr and "Traceback" not in result.stderr
    assert not called.exists()


def _deleted(path: Path) -> None:
    path.unlink()


def _rewritten(path: Path) -> None:
    path.write_text("b" * 48)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "change"), [("admin", _deleted), ("runtime", _deleted), ("postgres", _rewritten)]
)
def test_a_missing_or_changed_secret_of_a_recorded_installation_is_refused_never_regenerated(
    name, change, pinned, own_fleet, tmp_path
):
    fleet = own_fleet
    slug = fleet.declare("z", pinned)
    env = docker_without_compose(tmp_path)
    assert fleet.cli("apply", slug, env=env).returncode == 3
    path = fleet.state / "secrets" / slug / name
    original = path.read_text()
    env_file = fleet.state / "installations" / slug / "compose.env"
    before = env_file.read_text()
    change(path)
    result = fleet.cli("apply", slug, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "drift" in result.stderr and "secrets." in result.stderr
    assert env_file.read_text() == before
    assert not path.exists() if change is _deleted else path.read_text() == "b" * 48
    status = fleet.cli("status", slug, env=env)
    assert status.returncode == 3 and "secrets." in " ".join(receipt(status)["problems"])
    receipts = (fleet.state / "installations" / slug / "receipts.jsonl").read_text()
    assert original not in receipts + result.stdout + result.stderr + status.stdout


def docker_killing_after(directory: Path, *command: str) -> dict[str, str]:
    """A real `docker` that SIGKILLs the CLI right after `command` ran for real: a resource exists,
    and the apply never got to save anything about it."""
    shim = directory / "kill-after"
    shim.mkdir(exist_ok=True)
    real = shutil.which("docker")
    prefix = " ".join(command)
    (shim / "docker").write_text(
        "#!/bin/sh\n"
        f'"{real}" "$@"\n'
        "rc=$?\n"
        f'case "$*" in "{prefix}"*) kill -9 $PPID ;; esac\n'
        "exit $rc\n"
    )
    (shim / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def _swap_volume(project: str) -> str:
    """Replaced by something else wearing the same labels."""
    name = f"{project}_pgdata"
    labels = inspect(name, ".Labels")
    arguments = [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]
    created = inspect(name, ".CreatedAt")
    while inspect(name, ".CreatedAt") == created:
        docker("volume", "rm", name)
        docker("volume", "create", *arguments, name)
    return name


def _swap_network(project: str) -> str:
    name = f"{project}_default"
    labels = inspect(name, ".Labels")
    docker("network", "rm", name)
    arguments = [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]
    docker("network", "create", *arguments, name)
    return name


@pytest.mark.slow
@pytest.mark.parametrize(
    ("command", "swap", "kind"),
    [
        (("volume", "create"), _swap_volume, "volume"),
        (("network", "create"), _swap_network, "network"),
    ],
)
def test_a_resource_created_but_not_yet_saved_when_apply_was_killed_is_not_adopted(
    command, swap, kind, pinned, own_fleet, tmp_path
):
    """Nothing saved tells it from a replacement with copied labels, so a retry must refuse."""
    fleet = own_fleet
    slug = fleet.declare("g", pinned)
    project = f"farmcore-fleet-{slug}"
    killed = fleet.cli("apply", slug, env=docker_killing_after(tmp_path, *command))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    name = swap(project)
    before = docker(kind, "inspect", name).stdout
    result = fleet.cli("apply", slug, "--readiness-seconds", "1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not adopting" in result.stderr and name in result.stderr
    assert docker(kind, "inspect", name).stdout == before
    assert containers(slug) == []
    status = fleet.cli("status", slug)
    assert status.returncode == 3, status.stdout + status.stderr


@pytest.mark.slow
def test_a_stranger_copying_every_public_mark_of_the_app_during_a_killed_repeat_is_not_adopted(
    pinned, own_fleet, tmp_path
):
    """Project, service, one-off and configuration-hash labels, network, image, limits and binding:
    all of it readable by anyone, so none of it may stand in for the saved container id."""
    fleet = own_fleet
    slug = fleet.declare("h", pinned)
    assert fleet.cli("apply", slug).returncode == 0
    known = set(record_of(fleet, slug)["containers"])
    app = container(slug, "app")
    killed = fleet.cli("apply", slug, env=docker_killing_the_cli(tmp_path, "before-database"))
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    labels = inspect(app, ".Config.Labels")
    docker("stop", app)
    docker("rm", app)
    arguments = [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]
    stranger = docker(
        "run", "-d", "--name", f"farmcore-fleet-{slug}-app-1", "--entrypoint", "sleep",
        "--memory", f"{LIMITS['app_memory_mib']}m", "--cpus", "1.5",
        "-p", f"127.0.0.1:{fleet.declared(slug)['binding']['port']}:8000",
        "--network", f"farmcore-fleet-{slug}_default", *arguments, pinned[0], "300",
    ).stdout.strip()  # fmt: skip
    env, execs = docker_logging_exec(tmp_path)
    result = fleet.cli("apply", slug, "--readiness-seconds", "1", env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not adopting" in result.stderr
    assert not execs.exists()
    assert inspect(stranger, ".State.Status") == "running"
    assert stranger not in record_of(fleet, slug)["containers"]
    assert set(record_of(fleet, slug)["containers"]) == known


@pytest.mark.slow
def test_containers_created_but_not_yet_saved_when_apply_was_killed_are_refused_until_removed(
    pinned, own_fleet, tmp_path
):
    """Safe refusal, not convergence, in this one small window; nothing has started in it."""
    fleet = own_fleet
    slug = fleet.declare("w", pinned)
    project = f"farmcore-fleet-{slug}"
    env = docker_killing_after(tmp_path, "compose", "-p", project)
    killed = fleet.cli("apply", slug, env=env)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    made = sorted(containers(slug))
    assert made and running(slug) == []
    refused = fleet.cli("apply", slug, "--readiness-seconds", "1")
    assert refused.returncode == 1 and "not adopting" in refused.stderr, refused.stderr
    assert sorted(containers(slug)) == made and running(slug) == []
    for item in made:  # what the doc tells an operator to do once they have looked
        docker("rm", item)
    done = fleet.cli("apply", slug)
    assert done.returncode == 0, done.stdout + done.stderr
