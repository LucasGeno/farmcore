"""One bounded, expiring clarification per farm/chat; no long-term memory or new tables."""

from datetime import UTC, datetime, timedelta

from farmcore.apps.chat.extractor.base import (
    NOTE_TIME_MISSING,
    DraftOperation,
    ProductDose,
)
from farmcore.apps.chat.push import chat_props, set_chat_props
from farmcore.core.logs import lock_farm
from farmcore.core.models.canonical import Farm
from farmcore.core.models.kernel import ClockState

KEY = "clarifications"
TTL = timedelta(minutes=15)
MAX_TURNS = 6
MAX_CHARS = 2000


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


def load(session, farm_id, chat_id, now):
    farm = session.get(Farm, farm_id, populate_existing=True)
    state = (chat_props(farm).get(KEY) or {}).get(str(chat_id))
    if not state:
        return None
    if state.get("epoch") != epoch(session):
        return None
    if datetime.now(UTC) - datetime.fromisoformat(state["updated_at"]) > TTL:
        return None
    return state


def save(session, farm_id, chat_id, now, draft, history):
    lock_farm(session, farm_id)
    farm = session.get(Farm, farm_id, with_for_update=True, populate_existing=True)
    stamp = datetime.now(UTC)
    states = {
        key: value
        for key, value in (chat_props(farm).get(KEY) or {}).items()
        if stamp - datetime.fromisoformat(value["updated_at"]) <= TTL
    }
    # The private demo allowlist is small; bound persisted state even on a busy farm.
    states = dict(list(states.items())[-31:])
    states[str(chat_id)] = {
        "updated_at": stamp.isoformat(),
        "clock": now.isoformat(),
        "epoch": epoch(session),
        "draft": draft.model_dump(mode="json"),
        "history": history[-MAX_TURNS:],
    }
    set_chat_props(farm, **{KEY: states})


def clear(session, farm_id, chat_id):
    lock_farm(session, farm_id)
    farm = session.get(Farm, farm_id, with_for_update=True, populate_existing=True)
    states = dict(chat_props(farm).get(KEY) or {})
    states.pop(str(chat_id), None)
    set_chat_props(farm, **{KEY: states})


def reset(session, farm):
    """Manifest hook: demo reset preserves the chat binding, but clears unfinished input."""
    chat = chat_props(farm)
    if KEY in chat:
        farm.props = {**(farm.props or {}), "chat": {k: v for k, v in chat.items() if k != KEY}}


def continue_draft(current: DraftOperation, state):
    if not state:
        return current
    previous = DraftOperation.model_validate(state["draft"])
    values = {}
    for key in ("parcels", "products", "dose_value", "dose_unit"):
        value = getattr(current, key)
        values[key] = value if value is not None and value != [] else getattr(previous, key)
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
    if NOTE_TIME_MISSING in current.notes:
        values["ts_start"] = previous.ts_start
        values["notes"] = [n for n in current.notes if n != NOTE_TIME_MISSING]
        values["notes"] += [
            n for n in previous.notes if n.startswith("time ") or n == NOTE_TIME_MISSING
        ]
    return current.model_copy(update=values)
