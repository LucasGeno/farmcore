"""A chat's unfinished capture is an open draft (`core/drafts.py`); there is no other state.

The open draft of one actor on one farm that still names something `missing` is the
clarification the actor's next message continues: each turn revises it (a revision bump), and it
expires `TTL` after its last turn. It carries the extracted operation and up to `MAX_TURNS`
recent turns (`capture`), never older records or other chats. The demo epoch is stored with it:
after a demo reset or a clock jump the old draft is no longer continued.
"""

from datetime import timedelta

from sqlalchemy import select

from farmcore.apps.chat.extractor.base import DraftOperation, ProductDose
from farmcore.core.drafts import wall_now
from farmcore.core.logs import lock_farm
from farmcore.core.models.canonical import Farm
from farmcore.core.models.drafts import Draft
from farmcore.core.models.kernel import ClockState

TTL = timedelta(minutes=15)
MAX_TURNS = 6
MAX_CHARS = 2000
DEMO_RESET = "demo-reset"


def epoch(session):
    row = session.get(ClockState, 1, populate_existing=True)
    if row is None:
        return "uninitialized"
    generation = ((row.job_runs or {}).get("demo") or {}).get("generation", 0)
    return f"{row.mode}:{row.replay_now}:{generation}"


def unchanged(session, farm_id, expected):
    # Serialize with demo resets and push jobs; no transaction waits on the model call.
    lock_farm(session, farm_id)
    session.get(Farm, farm_id, with_for_update=True, populate_existing=True)
    return epoch(session) == expected


def open_captures(session, farm_id, actor) -> list[Draft]:
    """The actor's open, unexpired conversational drafts on the farm, oldest first."""
    return list(
        session.scalars(
            select(Draft)
            .where(
                Draft.farm_id == farm_id,
                Draft.actor == actor,
                Draft.status == "open",
                Draft.expires_at > wall_now(),
                Draft.payload.has_key("capture"),
            )
            .order_by(Draft.id)
            .execution_options(populate_existing=True)
        )
    )


def load(session, farm_id, actor) -> Draft | None:
    """The clarification this actor's next message continues: its latest open draft that still
    misses something, from the current demo epoch."""
    current = epoch(session)
    for draft in reversed(open_captures(session, farm_id, actor)):
        capture = draft.payload.get("capture") or {}
        if draft.payload.get("missing") and capture.get("epoch") == current:
            return draft
    return None


def state(draft: Draft | None):
    """The extractor's view of a clarification: its operation so far and recent turns."""
    if draft is None:
        return None
    capture = draft.payload["capture"]
    return {"draft": capture["operation"], "history": capture["history"]}


def held(session, farm_id, actor, request_key) -> Draft | None:
    """The draft that already took this message (Telegram re-delivers unanswered ones)."""
    return session.scalar(
        select(Draft)
        .where(
            Draft.farm_id == farm_id,
            Draft.actor == actor,
            (Draft.request_key == request_key)
            | Draft.payload["capture"]["messages"].has_key(request_key),
        )
        .order_by(Draft.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


def reset(session, farm):
    """Manifest hook: demo reset keeps the chat binding but cancels unfinished captures."""
    now = wall_now()
    for draft in session.scalars(
        select(Draft).where(
            Draft.farm_id == farm.id,
            Draft.status == "open",
            Draft.payload.has_key("capture"),
        )
    ):
        draft.status, draft.finalized_at, draft.finalized_by = "cancelled", now, DEMO_RESET


def continue_draft(current: DraftOperation, state):
    if not state:
        return current
    previous = DraftOperation.model_validate(state["draft"])
    values = {}
    for key in ("parcels", "products", "dose_value", "dose_unit"):
        value = getattr(current, key)
        values[key] = value if value is not None and value != [] else getattr(previous, key)
    # A short reply ("P5", "gross") carries no type: keep the unfinished operation's, unless it
    # plainly reports a spray (a product or a dose). An explicit irrigation/observation wins.
    stated_spray = bool(current.products or current.product_doses or current.dose_value is not None)
    if current.type != "spray" or previous.type == "spray" or stated_spray:
        values["type"] = current.type
    else:
        values["type"] = previous.type
    for key in ("amount_value", "amount_unit", "basis", "observation"):
        value = getattr(current, key)
        values[key] = value if value not in (None, "") else getattr(previous, key)
    # A short reply does not restate the date: keep the unfinished operation's own start time.
    if current.ts_start is None and previous.ts_start is not None:
        values["ts_start"] = previous.ts_start
    if values["type"] == "irrigate":
        values.update(
            products=[], product_doses=[], dose_value=None, dose_unit=None, observation=None
        )
    elif values["type"] == "observation":
        values.update(
            products=[],
            product_doses=[],
            dose_value=None,
            dose_unit=None,
            amount_value=None,
            amount_unit=None,
            basis=None,
        )
    elif values["type"] == "spray":
        values.update(amount_value=None, amount_unit=None, basis=None, observation=None)
    if not current.product_doses and values["products"] == previous.products:
        values["product_doses"] = previous.product_doses
    question = state["history"][-1]["content"] if state["history"] else ""
    if (
        question.startswith(("How much ", "Please repeat the dose"))
        and previous.products
        and (not current.products or set(current.products) <= set(previous.products))
    ):
        values["products"] = previous.products
        rates = {dose.product: dose for dose in previous.product_doses}
        rates.update({dose.product: dose for dose in current.product_doses})
        named = current.products or [name for name in previous.products if name in question]
        if not named and len(previous.products) == 1:
            named = previous.products
        if current.dose_value is not None and current.dose_unit is not None:
            for name in named:
                rates[name] = ProductDose(
                    product=name, value=current.dose_value, unit=current.dose_unit
                )
        values["product_doses"] = list(rates.values())
    return current.model_copy(update=values)
