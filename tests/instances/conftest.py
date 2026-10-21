"""Disposable PostGIS servers for installation tests.

Installation tests create cluster-wide roles, so they never use the shared dev server: each test
session starts its own throwaway container on a random localhost port and removes it at the end.
Tests skip (and say so) when Docker is not available.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError


def postgis_image() -> str:
    if os.environ.get("POSTGIS_IMAGE"):
        return os.environ["POSTGIS_IMAGE"]
    # postgis/postgis is amd64-only; imresamu/postgis is a multi-arch build of the same image.
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    return "imresamu/postgis:17-3.5" if arm else "postgis/postgis:17-3.5"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    superuser: str
    password: str
    container: str = ""

    def log(self) -> str:
        """The server log: every statement it received (`log_statement=all`)."""
        out = subprocess.run(["docker", "logs", self.container], capture_output=True, text=True)
        return out.stdout + out.stderr

    def url(self, database: str, user: str | None = None, password: str | None = None) -> str:
        return URL.create(
            "postgresql+psycopg",
            username=user or self.superuser,
            password=password or self.password,
            host=self.host,
            port=self.port,
            database=database,
        ).render_as_string(hide_password=False)


def _wait(url: str, deadline: float) -> None:
    engine = create_engine(url)
    try:
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT postgis_version()"))
                return
            except OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_server() -> Iterator[Server]:
    """One throwaway PostGIS container for the session (random port, random password)."""
    if not docker_available():
        pytest.skip("needs a Docker daemon")
    name = f"farmcore-iso-test-{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(16)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_USER=farmcore", "-e", f"POSTGRES_PASSWORD={password}",
            "-e", "POSTGRES_DB=farmcore", "-p", "127.0.0.1::5432", postgis_image(),
            "postgres", "-c", "log_statement=all",
        ],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        out = subprocess.run(
            ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout
        port = int(out.split()[0].rsplit(":", 1)[1])
        server = Server("127.0.0.1", port, "farmcore", password, name)
        _wait(server.url("farmcore"), time.monotonic() + 90)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


# --- Real Compose projects from one image -----------------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def inspect(container: str) -> dict:
    out = subprocess.run(
        ["docker", "inspect", container], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)[0]


@dataclass
class Project:
    """One installation: the stock docker-compose.yml under its own project name and env file."""

    name: str
    env_file: Path
    env: dict[str, str]
    files: tuple[Path, ...] = ()

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.name, "--env-file", str(self.env_file)]
        if self.files:
            cmd += ["-f", str(REPO / "docker-compose.yml")]
            for f in self.files:
                cmd += ["-f", str(f)]
        return subprocess.run(
            [*cmd, *args], cwd=REPO, capture_output=True, text=True, check=check, timeout=600
        )

    def container(self, service: str) -> str:
        return self.compose("ps", "-a", "-q", service).stdout.strip()

    def exec_health(self) -> dict:
        """GET /health from inside the app container (works without any published port)."""
        probe = (
            "import urllib.request; "
            "r = urllib.request.urlopen('http://localhost:8000/health', timeout=5); "
            "print(r.read().decode())"
        )
        out = self.compose("exec", "-T", "app", "python", "-c", probe).stdout
        return json.loads(out)

    def health(self) -> dict:
        port = self.env["FARMCORE_PORT"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            return json.loads(resp.read())

    def up(self, deadline_s: float = 240) -> None:
        started = self.compose("up", "-d", check=False)
        if started.returncode != 0:
            raise AssertionError(
                f"{self.name}: docker compose up failed:\n{started.stderr[-4000:]}"
            )
        deadline = time.monotonic() + deadline_s
        while inspect(self.container("app"))["State"].get("Health", {}).get("Status") != "healthy":
            if time.monotonic() > deadline:
                logs = self.compose("logs", "--no-color", check=False).stdout[-4000:]
                raise AssertionError(f"{self.name} never became healthy:\n{logs}")
            time.sleep(2)

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="session")
def image() -> Iterator[str]:
    """The release under test: built once from this checkout, removed at the end."""
    if not docker_available():
        pytest.skip("needs a Docker daemon")
    tag = f"farmcore-iso-test:{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(REPO)], check=True, capture_output=True)
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.fixture(scope="session")
def new_project(image, tmp_path_factory) -> Iterator:
    """Factory: a new Compose project with generated name and secrets; all torn down at the end."""
    made: list[Project] = []

    def make(*files: Path, **env_overrides: str) -> Project:
        env = {
            "POSTGRES_PASSWORD": secrets.token_hex(16),
            "RUNTIME_DATABASE_PASSWORD": secrets.token_hex(16),
            "ADMIN_TOKEN": secrets.token_hex(24),
            "FARMCORE_PORT": str(free_port()),
            "FARMCORE_IMAGE": image,
            "POSTGIS_IMAGE": postgis_image(),
            **env_overrides,
        }
        name = f"fc-iso-{uuid.uuid4().hex[:8]}"
        path = tmp_path_factory.mktemp(name) / ".env"
        path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        made.append(Project(name, path, env, files))
        return made[-1]

    try:
        yield make
    finally:
        for p in made:
            p.down()


@pytest.fixture
def fresh_db(pg_server: Server) -> Iterator[str]:
    """A new, empty database (PostGIS enabled) on the throwaway server; its name."""
    name = f"inst_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_server.url("farmcore"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    db = create_engine(pg_server.url(name))
    with db.begin() as conn:
        conn.execute(text("CREATE EXTENSION postgis"))
    db.dispose()
    try:
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
