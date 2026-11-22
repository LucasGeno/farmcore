"""Open-Meteo forecast runs as normalised weather receipts (expand step, core_0008).

`mirror_run` copies one stored `forecast_run` and its values into `weather_receipt` and
`weather_forecast`: `store.import_runs` calls it for every run it writes that did not come
through the provider already (saved files, the CLI's Single Runs), so the forecast views read
every run from the normalised schema.

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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.forecast import provider, sources
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.models import ForecastValue as StoredValue
from farmcore.core.models.weather import WeatherReceipt
from farmcore.core.weather.contract import Fetched, Licence, Request, Tier
from farmcore.core.weather.store import record_receipt, store_forecast

PROVIDER, AUTH, LIVE_LICENCE = provider.PROVIDER, provider.AUTH, provider.LICENCE
SINGLE_RUNS_LICENCE = Licence("CC-BY-4.0", sources.ATTRIBUTION, True)


def licence_for(run: ForecastRun) -> Licence:
    if run.source == provider.LIVE_DATASET:
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
        values=provider.forecast_values(
            [(v.ts, v.variable, v.value) for v in values], model, model_run
        ),
    )
    receipt = record_receipt(
        session,
        provider=PROVIDER,
        licence=licence_for(run),
        fetched=fetched,
        secret_fields=[f.name for f in AUTH if f.secret],
        farm_id=run.farm_id,
        tier=Tier.MODEL,  # every mirrored run is a model forecast, whatever its file source
    )
    store_forecast(session, receipt, fetched.values)
    return receipt
