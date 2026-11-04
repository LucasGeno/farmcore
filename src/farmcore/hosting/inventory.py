"""The private fleet inventory: one bounded, data-only YAML document per installation.

Documents are read from the YAML event stream, never constructed by a YAML loader: only mappings
with string keys, sequences, strings and decimal integers exist here. A problem names the file,
a known field or a line, and never repeats a value or an unknown key: a secret pasted in the wrong
place must not reach a terminal or log.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from yaml.events import (
    AliasEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    Event,
    MappingEndEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
)
from yaml.nodes import ScalarNode
from yaml.resolver import Resolver

MAX_INSTALLATIONS = 64
MAX_DOCUMENT_BYTES = 16 * 1024
MAX_NESTING = 3
MAX_NODES = 256
MAX_SCALAR_CHARACTERS = 512
MAX_REPORTED_PROBLEMS = 50

_STR_TAG = "tag:yaml.org,2002:str"
_INT_TAG = "tag:yaml.org,2002:int"
# YAML 1.1 also reads 06511 as octal, 56:41 as base 60 and 3_401 as 3401.
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,17})")
_RESOLVER = Resolver()

_SLUG = re.compile(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_DOMAIN_CHARACTERS = 253
# Pinned by digest alone: a tag beside the digest would only suggest it means something.
_IMAGE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
)
_SECRET_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){1,3}")
# Loopback, or a private network behind the proxy: RFC 1918 and the 100.64.0.0/10 Tailscale uses.
_BINDABLE_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
# Refuse typos, not a capacity statement: what a host can carry is measured, never read from here.
MIN_MEMORY_MIB, MAX_MEMORY_MIB = 256, 65536
MIN_CPU_MILLIS, MAX_CPU_MILLIS = 100, 64000
MIN_PORT, MAX_PORT = 1024, 65535
# A reserved name (RFC 2606) for disposable fixtures: valid only under --test-domains.
_FIXTURE_SUFFIX = "example.test"
_DECLARATION_FILE = re.compile(r"[a-z0-9-]+\.yaml")
_PLAIN_FILE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class InventoryError(Exception):
    """Every problem found, so an operator fixes an inventory in one pass."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


class _Refused(Exception):
    pass


def _at(event: Event) -> str:
    return f"line {event.start_mark.line + 1}"


def _plain_value(event: ScalarEvent) -> str | int | None:
    """The string or decimal integer a plain scalar means, or None for anything else in YAML."""
    if len(event.value) > MAX_SCALAR_CHARACTERS:
        raise _Refused(f"{_at(event)}: value too long")
    tag = _RESOLVER.resolve(ScalarNode, event.value, (True, False))
    if tag == _STR_TAG:
        return event.value
    if tag == _INT_TAG and _DECIMAL.fullmatch(event.value):
        return int(event.value)
    return None


class _Reader:
    def __init__(self, events: Iterator[Event]) -> None:
        self._events = events
        self._nodes = 0
        self.key_lines: dict[tuple[str | int, ...], int] = {}

    def document(self) -> Any:
        next(self._events)  # StreamStartEvent
        start = next(self._events)
        if not isinstance(start, DocumentStartEvent):
            raise _Refused("must hold exactly one document")
        if start.version or start.tags:
            raise _Refused(f"{_at(start)}: a directive is not allowed")
        value = self._node(next(self._events), ())
        if not isinstance(next(self._events), DocumentEndEvent) or not isinstance(
            next(self._events), StreamEndEvent
        ):
            raise _Refused("must hold exactly one document")
        return value

    def _node(self, event: Event, path: tuple[str | int, ...]) -> Any:
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise _Refused(f"{_at(event)}: too many values")
        if isinstance(event, AliasEvent) or event.anchor:
            raise _Refused(f"{_at(event)}: anchors and aliases are not allowed")
        if event.tag:
            raise _Refused(f"{_at(event)}: tags are not allowed")
        if isinstance(event, ScalarEvent):
            return self._scalar(event)
        if len(path) >= MAX_NESTING:
            raise _Refused(f"{_at(event)}: nested too deeply")
        if isinstance(event, SequenceStartEvent):
            return self._sequence(path)
        return self._mapping(path)

    def _scalar(self, event: ScalarEvent) -> str | int:
        if not event.implicit[0]:  # quoted or block: always a string, and never resolved
            if len(event.value) > MAX_SCALAR_CHARACTERS:
                raise _Refused(f"{_at(event)}: value too long")
            return event.value
        value = _plain_value(event)
        if value is None:
            raise _Refused(f"{_at(event)}: unsupported value; quote text, use decimal integers")
        return value

    def _sequence(self, path: tuple[str | int, ...]) -> list[Any]:
        items: list[Any] = []
        while not isinstance(event := next(self._events), SequenceEndEvent):
            items.append(self._node(event, (*path, len(items))))
        return items

    def _mapping(self, path: tuple[str | int, ...]) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        while not isinstance(event := next(self._events), MappingEndEvent):
            self._nodes += 1
            if not isinstance(event, ScalarEvent) or event.anchor or event.tag:
                raise _Refused(f"{_at(event)}: keys must be plain strings")
            key = self._scalar(event) if event.implicit[1] else _plain_value(event)
            if not isinstance(key, str):
                raise _Refused(f"{_at(event)}: keys must be plain strings")
            if key in mapping:
                raise _Refused(f"{_at(event)}: duplicate key")
            self.key_lines[(*path, key)] = event.start_mark.line + 1
            mapping[key] = self._node(next(self._events), (*path, key))
        return mapping


def _read_document(raw: bytes) -> tuple[dict[str, Any], dict[tuple[str | int, ...], int]]:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise _Refused(f"larger than {MAX_DOCUMENT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refused("not UTF-8") from None
    try:
        reader = _Reader(yaml.parse(text, Loader=yaml.SafeLoader))
        value = reader.document()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark else ""
        raise _Refused(f"{where}not valid YAML") from None
    if not isinstance(value, dict):
        raise _Refused("must be a mapping")
    return value, reader.key_lines


def read_root_mapping(raw: bytes) -> dict[str, Any] | None:
    """A document's root mapping under the strict reader, or None if the reader refuses it."""
    try:
        return _read_document(raw)[0]
    except _Refused:
        return None


def _matching(pattern: re.Pattern[str], expected: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"must be {expected}")
        return value

    return check


def _check_slug(value: str) -> str:
    if not _SLUG.fullmatch(value) or "--" in value:
        raise ValueError("must be 1-32 lowercase letters, digits and single hyphens")
    return value


def _check_domain(value: str) -> str:
    labels = value.split(".")
    if (
        len(value) > MAX_DOMAIN_CHARACTERS
        or len(labels) < 2
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        or any(label.startswith("xn--") for label in labels)
        or not labels[-1].isalpha()
    ):
        raise ValueError("must be one lowercase host name: no wildcard, port, scheme or IP")
    return value


def _check_host(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError("must be an IPv4 address") from None
    if not any(address in network for network in _BINDABLE_NETWORKS):
        raise ValueError("must be a loopback or private address")
    return value


_Image = Annotated[
    str, AfterValidator(_matching(_IMAGE, "an image pinned as <repository>@sha256:<digest>"))
]
_SecretReference = Annotated[
    str,
    AfterValidator(_matching(_SECRET_REFERENCE, "a relative path below the secrets folder")),
]
_MemoryMib = Annotated[int, Field(ge=MIN_MEMORY_MIB, le=MAX_MEMORY_MIB)]
_CpuMillis = Annotated[int, Field(ge=MIN_CPU_MILLIS, le=MAX_CPU_MILLIS)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Images(_Strict):
    app: _Image
    database: _Image


class Binding(_Strict):
    host: Annotated[str, AfterValidator(_check_host)]
    port: Annotated[int, Field(ge=MIN_PORT, le=MAX_PORT)]


class Limits(_Strict):
    app_memory_mib: _MemoryMib
    app_cpu_millis: _CpuMillis
    database_memory_mib: _MemoryMib
    database_cpu_millis: _CpuMillis


class Secrets(_Strict):
    """Where each secret is, relative to the operator's secrets folder; never a value."""

    postgres_password: _SecretReference
    runtime_database_password: _SecretReference
    admin_token: _SecretReference


class Declaration(_Strict):
    schema_version: Literal[1]
    slug: Annotated[str, AfterValidator(_check_slug)]
    domain: Annotated[str, AfterValidator(_check_domain)]
    images: Images
    binding: Binding
    limits: Limits
    secrets: Secrets


def _schema_problems(
    error: ValidationError, key_lines: dict[tuple[str | int, ...], int]
) -> list[str]:
    problems = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = item["loc"]
        if item["type"] == "extra_forbidden":
            # By line only: the key is operator input, and may be a pasted secret.
            section = ".".join(map(str, location[:-1]))
            prefix = f"{section}: " if section else ""
            problems.append(f"{prefix}unknown field at line {key_lines[location]}")
        else:
            message = item["msg"].removeprefix("Value error, ")
            problems.append(f"{'.'.join(map(str, location))}: {message}")
    return problems


def _is_under(domain: str, suffix: str) -> bool:
    return domain.endswith(f".{suffix}")


def is_fixture_domain(domain: str) -> bool:
    try:
        _check_domain(domain)
    except ValueError:
        return False
    return _is_under(domain, _FIXTURE_SUFFIX)


@dataclass(frozen=True)
class DomainPolicy:
    """Which domains this run accepts: an operator's allowlist, or fixtures and nothing else."""

    test_domains: bool
    suffixes: tuple[str, ...] = ()

    @classmethod
    def chosen(cls, *, test_domains: bool, suffixes: list[str]) -> DomainPolicy:
        if test_domains and suffixes:
            raise InventoryError(["use --test-domains or --allow-domain-suffix, not both"])
        if not test_domains and not suffixes:
            raise InventoryError(
                ["name the allowed domains with --allow-domain-suffix, or use --test-domains"]
            )
        for suffix in suffixes:
            try:
                _check_domain(suffix)
            except ValueError:
                raise InventoryError(
                    ["--allow-domain-suffix: must be one lowercase domain name"]
                ) from None
            if suffix == _FIXTURE_SUFFIX or _is_under(suffix, _FIXTURE_SUFFIX):
                raise InventoryError([f"{_FIXTURE_SUFFIX} is only valid with --test-domains"])
        return cls(test_domains, tuple(suffixes))

    def problem(self, domain: str) -> str | None:
        if self.test_domains:
            if _is_under(domain, _FIXTURE_SUFFIX):
                return None
            return f"domain: test mode allows only <name>.{_FIXTURE_SUFFIX}"
        if any(_is_under(domain, suffix) for suffix in self.suffixes):
            return None
        return "domain: not under an allowed suffix"


def _own_problems(declaration: Declaration, file_name: str, policy: DomainPolicy) -> list[str]:
    """Rules across one declaration's fields."""
    problems = []
    if domain_problem := policy.problem(declaration.domain):
        problems.append(domain_problem)
    if file_name != f"{declaration.slug}.yaml":
        problems.append("slug: the file name must be <slug>.yaml")
    references = declaration.secrets.model_dump()
    for name, reference in references.items():
        if not reference.startswith(f"{declaration.slug}/"):
            problems.append(f"secrets.{name}: must be inside this installation's own folder")
    if len(set(references.values())) != len(references):
        problems.append("secrets: each secret must have its own file")
    return problems


def _validated(
    raw: dict[str, Any],
    key_lines: dict[tuple[str | int, ...], int],
    file_name: str,
    policy: DomainPolicy,
) -> tuple[Declaration | None, list[str]]:
    try:
        declaration = Declaration.model_validate(raw)
    except ValidationError as exc:
        return None, _schema_problems(exc, key_lines)
    return declaration, _own_problems(declaration, file_name, policy)


def _read_file(name: str, folder_fd: int) -> bytes:
    """At most one byte over the limit. A FIFO or device is never opened for blocking I/O."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        if not stat.S_ISREG(os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_mode):
            raise _Refused("must be a regular file")
        fd = os.open(name, flags, dir_fd=folder_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _Refused("must be a regular file")
            return os.read(fd, MAX_DOCUMENT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _Refused(f"cannot read: {exc.strerror}") from None


def _shown(name: str) -> str:
    """A file name as safe to print: escaped unless it is already plain."""
    return name if _PLAIN_FILE_NAME.fullmatch(name) else ascii(name)


def _duplicate_problems(declared: list[tuple[str, Declaration]]) -> list[str]:
    """The same slug, domain or port twice, whichever files say it. A port counts as taken across
    the fleet: nothing here knows which addresses share a host."""
    problems = []
    claims: dict[tuple[str, str | int], str] = {}
    for name, declaration in declared:
        for field, value in (
            ("slug", declaration.slug),
            ("domain", declaration.domain),
            ("binding.port", declaration.binding.port),
        ):
            first = claims.setdefault((field, value), name)
            if first != name:
                problems.append(f"{name}: {field}: already declared by {first}")
    return problems


def _refusal(problems: list[str]) -> InventoryError:
    if len(problems) <= MAX_REPORTED_PROBLEMS:
        return InventoryError(problems)
    hidden = len(problems) - MAX_REPORTED_PROBLEMS
    return InventoryError([*problems[:MAX_REPORTED_PROBLEMS], f"... and {hidden} more"])


def read_inventory(folder: Path, policy: DomainPolicy) -> list[Declaration]:
    try:
        folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InventoryError([f"cannot read the inventory folder: {exc.strerror}"]) from None
    try:
        try:
            with os.scandir(folder_fd) as entries:
                names = sorted(entry.name for entry in islice(entries, MAX_INSTALLATIONS + 1))
        except OSError as exc:
            raise InventoryError([f"cannot read the inventory folder: {exc.strerror}"]) from None
        if not names:
            raise InventoryError(["no installations: the inventory folder is empty"])
        if len(names) > MAX_INSTALLATIONS:
            raise InventoryError(
                [f"too many entries in the inventory folder (limit {MAX_INSTALLATIONS})"]
            )
        declared: list[tuple[str, Declaration]] = []
        problems = []
        for name in names:
            try:
                if not _DECLARATION_FILE.fullmatch(name):
                    raise _Refused("the folder holds only <slug>.yaml declarations")
                raw, key_lines = _read_document(_read_file(name, folder_fd))
            except _Refused as exc:
                problems.append(f"{_shown(name)}: {exc}")
                continue
            declaration, own = _validated(raw, key_lines, name, policy)
            problems.extend(f"{_shown(name)}: {problem}" for problem in own)
            if declaration is not None:
                declared.append((name, declaration))
    finally:
        os.close(folder_fd)
    problems.extend(_duplicate_problems(declared))
    if problems:
        raise _refusal(problems)
    return [declaration for _, declaration in declared]
