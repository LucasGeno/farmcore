"""Open-Meteo forecast runs as normalised weather receipts (expand step, core_0008).

`mirror_run` copies one stored `forecast_run` and its values into `weather_receipt` and
`weather_forecast`. The forecast tables stay what every consumer reads; this is the adapter the
provider port reads from and checks against.

- The request URL and `endpoint_kind` come from the run's own props (written by the fetch,
  never with the key), not rebuilt here.
- A live receipt's `issued_at` is only the receipt's identity: its model run is unknown (None).
- Periods: rain is the hour's total and a gust the hour's maximum, both ending at the
  timestamp; the other variables are instantaneous.
- Values become decimals through their shortest float text, so 0.1 stays 0.1.
- Saved Open-Meteo responses are CC BY 4.0; any other source states no licence and is stored as
  `unknown`, not redistributable. The payload is the run's canonical JSON, so mirroring a run
  again returns its receipt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.forecast import jobs, sources
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.models import ForecastValue as StoredValue
from farmcore.core.models.weather import WeatherReceipt
from farmcore.core.weather.contract import AuthField, Fetched, ForecastValue, Licence, Request
from farmcore.core.weather.store import record_receipt, store_forecast
from farmcore.core.weather.variables import BY_LEGACY

PROVIDER = "open-meteo"
AUTH = (AuthField("apikey", secret=True, description="Open-Meteo customer API key"),)
LIVE_LICENCE = Licence("CC-BY-4.0", "Weather data by Open-Meteo.com (CC BY 4.0)", True)
SINGLE_RUNS_LICENCE = Licence("CC-BY-4.0", sources.ATTRIBUTION, True)
# Open-Meteo hourly: precipitation is the preceding-hour sum and wind gusts the preceding-hour
# maximum; temperature, humidity and wind speed are instantaneous (https://open-meteo.com/en/docs).
PERIODS = {"rr1_mm": timedelta(hours=1), "fx_kmh": timedelta(hours=1)}


def licence_for(run: ForecastRun) -> Licence:
    if run.source == jobs.SOURCE:
        return LIVE_LICENCE
    if run.source.startswith(f"{sources.SINGLE_RUNS}/"):
        return SINGLE_RUNS_LICENCE
    return Licence(None, (run.props or {}).get("attribution"), False)


def mirror_run(session: Session, run: ForecastRun) -> WeatherReceipt:
    props = run.props or {}
    values = session.scalars(
        select(StoredValue)
        .where(StoredValue.run_id == run.id)
        .order_by(StoredValue.ts, StoredValue.variable)
    ).all()
    model_run = None if props.get("issued_at_kind") == "receipt" else run.issued_at
    model = props.get("model") or run.source
    fetched_at = props.get("fetched_at")
    payload = json.dumps(
        {
            "source": run.source,
            "issued_at": run.issued_at.isoformat(),
            "values": [[v.ts.isoformat(), v.variable, repr(v.value)] for v in values],
        },
        sort_keys=True,
    ).encode()
    fetched = Fetched(
        request=Request(
            dataset=run.source,
            url=props.get("request_url"),
            endpoint_kind=props.get("endpoint_kind"),
        ),
        fetched_at=datetime.fromisoformat(fetched_at) if fetched_at else run.available_at,
        payload=payload,
        values=[
            ForecastValue(
                model=model,
                run=model_run,
                valid_time=v.ts,
                variable=BY_LEGACY[v.variable].name,
                unit=BY_LEGACY[v.variable].unit,
                value=Decimal(repr(v.value)),
                period_start=v.ts - PERIODS.get(v.variable, timedelta(0)),
                period_end=v.ts,
            )
            for v in values
        ],
    )
    receipt = record_receipt(
        session,
        provider=PROVIDER,
        licence=licence_for(run),
        fetched=fetched,
        secret_fields=[f.name for f in AUTH if f.secret],
        farm_id=run.farm_id,
    )
    store_forecast(session, receipt, fetched.values)
    return receipt
