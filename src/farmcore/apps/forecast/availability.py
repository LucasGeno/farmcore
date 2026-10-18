"""Browser-local farm availability, never farm data or an agronomy rule."""

from datetime import date

COOKIE = "farmcore_forecast_busy_{farm_id}"
ONE_YEAR = 365 * 24 * 3600
# Even with cookie quoting/attributes this stays below a browser's 4 KB cookie limit.
MAX_DATES = 180
MAX_COOKIE_LENGTH = 3000


def parse(value: str | None, today: date) -> set[date]:
    if not value or len(value) > MAX_COOKIE_LENGTH:
        return set()
    days = set()
    for token in value.split(","):
        try:
            day = date.fromisoformat(token)
        except ValueError:
            continue
        if day.isoformat() == token and day >= today:
            days.add(day)
    return set(sorted(days)[:MAX_DATES])


def serialize(days: set[date]) -> str:
    return ",".join(day.isoformat() for day in sorted(days))
