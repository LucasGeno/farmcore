"""Owner-only file import and parcel review, mounted by the onboarding manifest."""

import hashlib
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from farmcore.apps.onboarding.importers import parsers, service
from farmcore.core import drafts
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.models.canonical import Parcel
from farmcore.core.models.drafts import Draft
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_drafts import load, request_actor
from farmcore.web import context_url, render

router = APIRouter(dependencies=[Depends(require_farm_owner)])


@router.get("/import")
def page(request: Request, repo: Repo):
    return render(request, "onboarding/import.html")


@router.post("/import")
async def upload(request: Request, repo: Repo):
    # Bound chunked uploads too, before the multipart parser can spool to disk.
    limit = parsers.MAX_BYTES + 64_000  # multipart headers and two mapping fields
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isdigit():
            raise HTTPException(400, "Invalid Content-Length.")
        if int(length) > limit:
            raise HTTPException(413, "Parcel upload exceeds the size limit.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Parcel upload exceeds the size limit.")
        body.extend(chunk)

    async def replay():
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    async with Request(request.scope, replay).form(max_files=1, max_fields=3) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "Choose a parcel file.")
        name_field, crop_field = form.get("name_field", ""), form.get("crop_field", "")
        if not isinstance(name_field, str) or not isinstance(crop_field, str):
            raise HTTPException(422, "Attribute columns must be text.")
        return await run_in_threadpool(_propose, request, repo, file, name_field, crop_field)


def _propose(request, repo, file, name_field, crop_field):
    try:
        data = file.file.read(parsers.MAX_BYTES + 1)
        if len(data) > parsers.MAX_BYTES:
            raise ValueError("File exceeds the 5 MB limit.")
        items = parsers.parse(data, file.filename, name_field=name_field, crop_field=crop_field)
        existing = list(
            repo.execute(
                select(Parcel).where(Parcel.farm_id == repo.farm_id),
                scope="fields:read",
                resource="parcel",
            ).scalars()
        )
        items = [item for item in items if service.matching(existing, item) is None]
        if not items:
            return render(
                request, "onboarding/import.html", message="All parcels are already imported."
            )
        provenance = {
            "source_type": "file",
            "details": {},
            "file_name": file.filename,
            "file_sha256": hashlib.sha256(data).hexdigest(),
        }
        payload = service.validate(repo.session, repo.farm_id, {"parcels": items})
        pending = repo.execute(
            select(Draft).where(
                Draft.farm_id == repo.farm_id,
                Draft.kind == "parcels",
                Draft.actor == request_actor(request),
                Draft.status == "open",
                Draft.provenance == provenance,
            ),
            scope="logs:write",
            resource="draft",
        ).scalars()
        for held in pending:
            if not drafts.is_expired(held) and held.payload == payload:
                return RedirectResponse(context_url(request, f"/drafts/{held.id}"), status_code=303)
        draft, _ = drafts.propose(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            actor=request_actor(request),
            kind="parcels",
            payload=payload,
            provenance=provenance,
            request_key=uuid4().hex,
        )
    except (ValueError, drafts.DraftError) as exc:
        response = render(request, "onboarding/import.html", error=str(exc))
        response.status_code = 422
        return response
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)


@router.get("/import/review")
def review(request: Request, repo: Repo, draft: int):
    row = load(request, repo, draft)
    if row.kind != "parcels":
        raise HTTPException(404, "Parcel draft not found.")
    return render(request, "onboarding/_import_review.html", draft=row)
