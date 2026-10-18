"""External ids: `(source, key) -> canonical row`, so every import and seed is idempotent.

`ext_id.model` stores the target's table name; `source` is a `source.code` (e.g. `farmcore-demo`).
Re-running `upsert` with the same `(source, key)` updates the row, never duplicates it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.core.models.base import Base
from farmcore.core.models.canonical import ExtId, Source


def ensure_source(session: Session, code: str, name: str, kind: str, licence: str) -> Source:
    """Get-or-create the `source` row for `code`; name, kind and licence are kept current."""
    source = session.scalar(select(Source).where(Source.code == code))
    if source is None:
        source = Source(code=code, name=name, kind=kind, licence=licence)
        session.add(source)
    else:
        source.name, source.kind, source.licence = name, kind, licence
    session.flush()
    return source


def _model_for(table_name: str) -> type[Base]:
    for mapper in Base.registry.mappers:
        if mapper.local_table is not None and mapper.local_table.name == table_name:
            return mapper.class_
    raise LookupError(f"no mapped model for table {table_name!r}")


def _ext_row(session: Session, source: str, key: str) -> ExtId | None:
    return session.scalar(select(ExtId).where(ExtId.source == source, ExtId.key == key))


def lookup(session: Session, source: str, key: str) -> Any | None:
    """The canonical row mapped to `(source, key)`, or None (unknown key or deleted row)."""
    ext = _ext_row(session, source, key)
    if ext is None:
        return None
    return session.get(_model_for(ext.model), ext.res_id)


def upsert[M: Base](
    session: Session, *, source: str, key: str, model: type[M], values: dict[str, Any]
) -> tuple[M, bool]:
    """Create or update the `model` row mapped to `(source, key)`; returns `(row, created)`."""
    table = model.__table__.name
    ext = _ext_row(session, source, key)
    obj = session.get(model, ext.res_id) if ext is not None and ext.model == table else None
    created = obj is None
    if created:
        obj = model(**values)
        session.add(obj)
    else:
        for name, value in values.items():
            setattr(obj, name, value)
    session.flush()
    if ext is None:
        session.add(ExtId(source=source, key=key, model=table, res_id=obj.id))
    else:
        ext.model, ext.res_id = table, obj.id
    session.flush()
    return obj, created
