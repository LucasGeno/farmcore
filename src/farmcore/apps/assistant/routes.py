"""`GET /assistant/ask`, `GET /assistant/voice`, `POST /assistant/transcribe`.

Ask: the visible read-only tools; when Gemini is configured (`model_configured`: Vertex extractor
with usable auth)
and the viewer is the admin (never anonymous visitors, never tour mode), `llm.VertexToolChooser`
picks the tool and its declared string arguments; on no credentials, a non-admin or tour viewer,
a model error or timeout, no call, or a tool name that is not offered, the keyword router picks
instead (`via` says which). Either way the answer is the tool's own text, facts and links, read
as actor `web:assistant`.

Voice: the panel records in the browser and posts the clip here; the chat app's local Whisper
transcriber turns it into text the farmer edits before pressing Ask. Admin only (never in tour
mode: the middleware refuses the POST, `/voice` hides the mic), and the mic shows only when the
local model files are present (checked once per process). Rate-limited with ask; the body is
capped by Content-Length and while streaming, before any parsing; the clip by size and duration.
An unreadable clip is a 422; no speech model on the host → 503 "Voice needs the speech service."
"""

import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import av
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from farmcore.apps.assistant import conversations, limits, llm
from farmcore.apps.assistant.tools import PLAN
from farmcore.apps.chat import conversation as clarification
from farmcore.apps.chat import handlers as chat
from farmcore.apps.chat.assistant import router as intent
from farmcore.apps.chat.assistant.fallback import choose
from farmcore.apps.chat.audit import DbAuditSink, audit_ref
from farmcore.apps.chat.conversation import epoch
from farmcore.apps.chat.dto import Callback, Incoming, Outgoing
from farmcore.apps.chat.transcribe import WhisperTranscriber
from farmcore.apps.fields.tools import mentioned_parcel, parcels
from farmcore.apps.forecast import availability
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core.access import Repo, ScopedRepository, default_farm
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import Farm
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_clock import require_admin
from farmcore.core.tools import ToolContext, ToolResult, call_tool, collect_tools
from farmcore.core.urls import with_farm
from farmcore.web import is_admin, templates

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_ARG_CHARS = 80
MAX_AUDIO_BYTES = 2_000_000  # ~1 min of browser Opus
MAX_AUDIO_SECONDS = 60
MAX_BODY_BYTES = MAX_AUDIO_BYTES + 64_000  # the clip plus multipart headers
NO_SPEECH = "Voice needs the speech service."
ALLOWED = {
    "operations_last_spray",
    "advisor_today",
    "advisor_parcel_status",
    "fields_list_parcels",
    "operations_recent_sprays",
}
# Anonymous visitors and tour visitors never write: a spray report gets this instead of a guess.
LOG_ELSEWHERE = (
    "Logging a spray is for the signed-in farmer, or send it to the farm's Telegram bot. "
    "Here you can ask about your records."
)
LOG_OTHER_FARM = "Spray logging here is set up for the main demo farm only."
CANCEL_WORDS = {"/cancel", "cancel", "start over"}
SPRAY_TURN = "spray_log"  # routing metadata only: a follow-up never re-runs a write


@router.get("/ask")
def ask(
    request: Request,
    repo: Repo,
    q: str = Query(min_length=1, max_length=500),
    conversation: str | None = Query(default=None, min_length=1, max_length=64),
):
    manifests = visible_manifests(request)
    if "assistant" not in {m.name for m in manifests}:
        raise HTTPException(404)
    token, allowed = limits.take(request.app, request.cookies.get(limits.COOKIE))
    if not allowed:
        return response(
            request, token, {"text": "Too many questions. Try again in a minute.", "links": []}, 429
        )
    if intent.intent(q) == intent.LOG:
        return response(request, token, {"text": LOG_ELSEWHERE, "links": [], "tool": None})
    scoped = ScopedRepository(repo.session, repo.farm_id, repo.grant, "web:assistant", "assistant")
    settings = request.app.state.settings
    ctx = ToolContext(
        scoped, get_clock(repo.session, settings).now(), ZoneInfo(settings.display_tz)
    )
    generation = epoch(repo.session)
    key = _conversation_key(request, repo, token, conversation)
    history = conversations.load(request.app, key, generation)
    tools = [t for t in collect_tools(manifests) if t.name in ALLOWED and t.scope.endswith(":read")]
    if any(m.name == "forecast" for m in manifests):
        tools.append(PLAN)
    has_fields = any(m.name == "fields" for m in manifests)
    parcel = mentioned_parcel(scoped, q) if has_fields else None
    alias_match = re.search(r"\bP\d+\b", q, re.I)
    explicit_alias = alias_match.group().upper() if alias_match else None
    if explicit_alias and has_fields:
        parcel = mentioned_parcel(scoped, explicit_alias)
    refers_to_parcel = bool(
        re.search(r"\b(it|there|that parcel|this parcel|same parcel)\b", q, re.I)
    )
    followup = bool(history) and bool(
        re.match(r"^(and\b|what about\b|how about\b|et\b|same\b)", q.strip(), re.I)
        or refers_to_parcel
        or (
            parcel
            and q.strip().rstrip("?").casefold()
            in {parcel.alias.casefold(), parcel.name.casefold()}
        )
    )
    # "Is it protected?" after a parcel-free turn ("Which days are good?") still means the
    # parcel named most recently in this conversation.
    previous_parcel = next((t.parcel for t in reversed(history) if t.parcel), None)
    if followup and refers_to_parcel and not parcel and not explicit_alias and has_fields:
        parcel = mentioned_parcel(scoped, previous_parcel) if previous_parcel else None
    tool, model_args, via = None, {}, "keywords"
    # No model call from anonymous visitors; `is_admin` is also false in tour mode.
    # Farms flagged `no_model` (e.g. third-party data under restrictive terms) never reach the
    # model, not even as a typed question.
    no_model = bool(((repo.session.get(Farm, repo.farm_id).props) or {}).get("no_model"))
    chooser = chooser_for(request.app) if is_admin(request) and not no_model else None
    if chooser is not None and tools:
        choice = None
        try:
            with audit_ref(f"web:assistant:{repo.farm_id}"):
                question = q
                if history:
                    question = json.dumps(
                        {
                            "previous_turns": [
                                {
                                    "question": t.question,
                                    "selected_tool": t.tool,
                                    "parcel": t.parcel,
                                }
                                for t in history
                            ],
                            "current_question": q,
                        },
                        ensure_ascii=False,
                    )
                choice = chooser.choose(question, tools)
        except Exception as exc:
            logger.warning("assistant model failed (%s); keyword router", type(exc).__name__)
        offered = {t.name: t for t in tools}
        if choice is not None and choice.name in offered:
            tool, via = offered[choice.name], "model"
            model_args = {
                k: v[:MAX_ARG_CHARS]
                for k, v in choice.args.items()
                if k in tool.params and isinstance(v, str)
            }
            named = model_args.get("parcel")
            if named and has_fields and not explicit_alias and (not followup or refers_to_parcel):
                parcel = parcel or mentioned_parcel(scoped, named)
    if tool is None:
        tool = choose(tools, q, names_parcel=parcel is not None)
        if tool is None and followup:
            tool = choose(tools, history[-1].question, names_parcel=bool(parcel or explicit_alias))
            if tool is None:
                tool = next((t for t in tools if t.name == history[-1].tool), None)
    text, links, name = (
        "I can't answer that yet. Ask about a last spray, spray advice or good days.",
        (),
        None,
    )
    facts = {}
    if tool:
        args = {k: v for k, v in model_args.items() if k != "parcel"}
        if "parcel" in tool.params:
            args["parcel"] = explicit_alias or (parcel.alias if parcel else "")
        if tool.name == "assistant_plan":
            args["busy_days"] = tuple(
                d.isoformat()
                for d in availability.parse(
                    request.cookies.get(availability.COOKIE.format(farm_id=repo.farm_id)),
                    ctx.now.astimezone(ctx.tz).date(),
                )
            )
        with repo.session.begin_nested():
            if tool.name.startswith("advisor_"):
                from farmcore.apps.advisor.service import unavailable_reason

                reason = unavailable_reason(scoped)
            else:
                reason = None
            result = (
                ToolResult({"unavailable": reason}, reason)
                if reason
                else call_tool(tool, ctx, args)
            )
        text, links, name, facts = result.text, result.links, tool.name, result.facts
    conversations.save(
        request.app,
        key,
        generation,
        [*history, conversations.Turn(q, name, parcel.alias if parcel else None)],
    )
    return response(
        request,
        token,
        {
            "fresh_conversation": not history,
            "text": text,
            "facts": facts,
            "links": [(label, with_farm(path, repo.farm_id)) for label, path in links],
            "tool": name,
            "via": via,
        },
    )


@router.post("/message", dependencies=[Depends(require_admin)])
def message(
    request: Request,
    repo: Repo,
    q: str = Form(min_length=1, max_length=500),
    conversation: str | None = Form(default=None, min_length=1, max_length=64),
):
    """The signed-in farmer's panel: a spray report (or the reply to its clarifying question)
    takes the chat app's draft → ✅ path; anything else is answered exactly like `GET /ask`."""
    names = {m.name for m in visible_manifests(request)}
    if "assistant" not in names:
        raise HTTPException(404)
    text = q.strip()
    if "chat" not in names or not _spray_turn(request, repo, text):
        return ask(request, repo, q, conversation)
    token, allowed = limits.take(request.app, request.cookies.get(limits.COOKIE))
    if not allowed:
        return response(request, token, {"text": "Too many messages. Try again in a minute."}, 429)
    if repo.farm_id != default_farm(repo.session, request.app.state.settings).id:
        return response(request, token, {"kind": "reply", "text": LOG_OTHER_FARM, "actions": []})
    message_id = time.time_ns() // 1000  # unique per message: the draft's idempotency key
    out = chat.on_message(
        chat_deps(request.app), Incoming(chat.WEB_CHAT_ID, text, None, message_id)
    )
    # The panel keeps one conversation: a spray turn counts, so the next question is not "fresh".
    key = _conversation_key(request, repo, token, conversation)
    generation = epoch(repo.session)
    history = conversations.load(request.app, key, generation)
    conversations.save(
        request.app, key, generation, [*history, conversations.Turn(text, SPRAY_TURN, None)]
    )
    return response(request, token, _web_reply(request, repo, out))


def _conversation_key(request: Request, repo: Repo, token: str, conversation: str | None):
    # Browser cookie has 122 bits of random entropy; the tab id separates simultaneous panels.
    # Grants/viewer mode and farm are part of the key, never shared across a scope change.
    if not conversation:
        return None
    return (token, conversation, repo.farm_id, repo.grant.id, is_admin(request))


@router.post("/confirm", dependencies=[Depends(require_admin)])
def confirm(
    request: Request, repo: Repo, action: str = Form(pattern=r"^(confirm|cancel):\d{1,12}$")
):
    """✅ / ❌ on a web draft; the chat app's ownership, replay and merge checks apply."""
    if "chat" not in {m.name for m in visible_manifests(request)}:
        raise HTTPException(404)
    out = chat.on_callback(chat_deps(request.app), Callback(chat.WEB_CHAT_ID, action))
    token = request.cookies.get(limits.COOKIE) or ""
    return response(request, token, _web_reply(request, repo, out))


def _spray_turn(request: Request, repo: Repo, text: str) -> bool:
    """A spray report, a cancel, or any non-question while a web clarification is open."""
    said = intent.intent(text)
    if said == intent.LOG or text.casefold() == "/confirm":
        return True
    if said == intent.QUESTION:
        return False
    now = get_clock(repo.session, request.app.state.settings).now()
    open_draft = clarification.load(repo.session, repo.farm_id, chat.WEB_CHAT_ID, now)
    return open_draft is not None or (
        text.casefold() in CANCEL_WORDS and _has_pending(repo.session, request)
    )


def _has_pending(session, request) -> bool:
    from farmcore.core.models.canonical import Log, LogSource

    prefix = chat._chat_key(chat.WEB_CHAT_ID)
    return (
        session.query(LogSource.id)
        .join(Log, Log.id == LogSource.log_id)
        .filter(Log.status == "pending", LogSource.external_key.startswith(prefix))
        .first()
        is not None
    )


def _web_reply(request: Request, repo: Repo, out: Outgoing) -> dict:
    """The chat reply for the panel: confirm/cancel become actions, links stay on this site."""
    actions = [{"label": label, "action": data} for label, data in out.buttons]
    lines = [
        line
        for line in out.text.splitlines()
        if not line.startswith(tuple(f"{label}: " for label, _ in chat.CONFIRM_LINKS))
    ]
    logged = out.text.startswith("✅")
    links = (
        [(label, with_farm(path, repo.farm_id)) for label, path in chat.CONFIRM_LINKS]
        if logged
        else []
    )
    if any(data.startswith(f"{chat.CONFIRM}:") for _, data in out.buttons):
        kind = "draft"
    elif logged or out.text.startswith("❌"):
        kind = "done"
    else:
        now = get_clock(repo.session, request.app.state.settings).now()
        repo.session.expire_all()
        open_draft = clarification.load(repo.session, repo.farm_id, chat.WEB_CHAT_ID, now)
        kind = "clarify" if open_draft is not None else "reply"
    return {"kind": kind, "text": "\n".join(lines), "actions": actions, "links": links}


def chat_deps(app) -> chat.ChatDeps:
    """The chat app's handlers for the web panel, built once: the configured extractor (Gemini
    when set up, else the local parser), the active apps' jobs and the saved-weather reader."""
    deps = getattr(app.state, "assistant_chat_deps", None)
    if deps is None:
        deps = app.state.assistant_chat_deps = chat.build_deps(
            app.state.settings,
            app.state.session_factory,
            list(app.state.manifests),
            weather_reader=getattr(app.state, "spray_weather_reader", None),
        )
    return deps


def chooser_for(app) -> llm.ToolChooser | None:
    """The configured tool chooser: a test's `app.state.assistant_chooser`, else a Vertex one
    when the chat extractor is Vertex (built once per app), else None (keyword router)."""
    chooser = getattr(app.state, "assistant_chooser", None)
    if chooser is not None:
        return chooser
    settings = app.state.settings
    if not model_configured(settings):
        return None
    app.state.assistant_chooser = llm.VertexToolChooser(
        settings, DbAuditSink(app.state.session_factory)
    )
    return app.state.assistant_chooser


def model_configured(settings) -> bool:
    """Vertex is the extractor and its auth has what it needs on this host: a token, an ADC key
    file, or the gcloud CLI. Nothing is called; a rejected credential still falls back per ask."""
    if settings.extractor != "vertex" or not settings.vertex_project:
        return False
    if settings.vertex_auth == "token":
        return settings.vertex_access_token is not None
    if settings.vertex_auth == "adc":
        return os.path.isfile(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    return shutil.which("gcloud") is not None


def model_ready(request: Request) -> bool:
    """Whether this viewer's questions go to Gemini (the panel's AI-assisted hint): the same
    rule as `ask`."""
    return is_admin(request) and chooser_for(request.app) is not None


# The shell's assistant panel reads this; registered when the app's routes load.
templates.env.globals["assistant_model_ready"] = model_ready


def transcriber_for(app):
    transcriber = getattr(app.state, "assistant_transcriber", None)
    if transcriber is None:
        # Local files only: the web process never downloads a model.
        transcriber = WhisperTranscriber(app.state.settings, local_files_only=True)
        app.state.assistant_transcriber = transcriber
    return transcriber


def local_model_available(settings) -> bool:
    """Whether `settings.whisper_model` loads from local files: a model directory, or a complete
    copy in the Hugging Face cache (no network: `local_files_only`)."""
    name = settings.whisper_model
    if Path(name).is_dir():
        return True
    try:
        from faster_whisper import utils

        utils.download_model(name, local_files_only=True)
    except Exception:
        return False
    return True


def speech_available(app) -> bool:
    """A test's injected transcriber, else the local model check, made once per process."""
    if getattr(app.state, "assistant_transcriber", None) is not None:
        return True
    ready = getattr(app.state, "assistant_speech_ready", None)
    if ready is None:
        ready = app.state.assistant_speech_ready = local_model_available(app.state.settings)
    return ready


@router.get("/voice")
def voice(request: Request):
    # Admin only (never tour mode), and only when the speech model loads in this process.
    allowed = is_admin(request) and speech_available(request.app)
    return JSONResponse(
        {"voice": allowed, "max_seconds": MAX_AUDIO_SECONDS, "unavailable": NO_SPEECH},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/transcribe", dependencies=[Depends(require_admin)])
async def transcribe(request: Request, repo: Repo):
    token, allowed = limits.take(request.app, request.cookies.get(limits.COOKIE))
    if not allowed:
        return response(request, token, {"detail": "Too many requests. Try again soon."}, 429)
    form = await _capped_form(request)
    audio = form.get("audio")
    if not isinstance(audio, UploadFile):
        raise HTTPException(422, "Send the recording as `audio`.")
    text = await run_in_threadpool(_transcribe, request, repo, audio)
    return response(request, token, {"text": text})


async def _capped_form(request: Request):
    """The multipart form, refused with 413 before parsing when the body is too large: by its
    Content-Length, or (chunked uploads) as soon as the streamed bytes pass the cap."""
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isdigit():
            raise HTTPException(400, "Bad Content-Length.")
        if int(length) > MAX_BODY_BYTES:
            raise HTTPException(413, "Recording too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Recording too large.")

    async def replay():
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    return await Request(request.scope, replay).form(max_files=1, max_fields=1)


def _transcribe(request: Request, repo: ScopedRepository, audio: UploadFile) -> str:
    if not (audio.content_type or "").startswith("audio/"):
        raise HTTPException(415, "Send an audio recording.")
    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording too large.")
    if not data:
        raise HTTPException(422, "Empty recording.")
    scoped = ScopedRepository(repo.session, repo.farm_id, repo.grant, "web:assistant", "assistant")
    hints = [p.name for p in parcels(scoped)] + [
        p.name for p in products_for_farm(scoped, scope="fields:read")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "question.audio"
        path.write_bytes(data)
        try:
            transcript = transcriber_for(request.app).transcribe(path, hints)
        except av.FFmpegError as exc:
            raise HTTPException(422, "Could not read the recording.") from exc
        except Exception as exc:
            logger.warning("assistant transcription failed (%s)", type(exc).__name__)
            raise HTTPException(503, NO_SPEECH) from exc
    if transcript.seconds > MAX_AUDIO_SECONDS:
        raise HTTPException(413, f"Recording longer than {MAX_AUDIO_SECONDS} seconds.")
    return transcript.text[:500]


def response(request, token, body, status=200):
    result = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    result.set_cookie(
        limits.COOKIE,
        token,
        max_age=3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    if status == 429:
        result.headers["Retry-After"] = str(limits.WINDOW)
    return result
