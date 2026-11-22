"""Write the synthetic hourly season that the downy mildew oracles run on.

FarmCore's own deterministic weather (no randomness, no third-party observations), so the
input is free to commit and every oracle sees the same hours. It is shaped to exercise the
models, not to look like any real site:

- temperature: a seasonal ramp from about 3 degC (January) to 20 degC (end of July), with a
  +/-5 degC sine through the day (minimum 03:00, maximum 15:00 UTC);
- rain: an event every 5 days at 18:00 UTC; every third one is heavy (6 h at 2 mm/h), the
  others light (3 h at 0.5 mm/h);
- relative humidity: 98 % while it rains and 95 % for 3 h after, otherwise 75 % -/+ 15 %
  through the day;
- leaf wetness: minutes wet in the hour, 60 when it rains or relative humidity >= 90 %, else 0.
  This is a harness rule, not an agronomic wetness estimate.

Usage: python3 tools/oracles/inputs/make_season.py <output csv>
"""

import csv
import math
import sys
from datetime import UTC, datetime, timedelta

START = datetime(2025, 1, 1, tzinfo=UTC)
DAYS = 212  # 1 January to 31 July 2025


def rain_mm(day: int, hour: int) -> float:
    if day % 5 or hour < 18:
        return 0.0
    heavy = (day // 5) % 3 == 0
    duration, rate = (6, 2.0) if heavy else (3, 0.5)
    return rate if hour < 18 + duration else 0.0


def hours():
    for step in range(DAYS * 24):
        day, hour = divmod(step, 24)
        seasonal = 3 + 17 * (1 - math.cos(math.pi * day / DAYS)) / 2
        diurnal = math.sin(2 * math.pi * (hour - 9) / 24)
        temp = round(seasonal + 5 * diurnal, 2)
        rain = rain_mm(day, hour)
        after_rain = any(rain_mm(*divmod(step - back, 24)) for back in (1, 2, 3)) and step >= 3
        if rain:
            rh = 98.0
        elif after_rain:
            rh = 95.0
        else:
            rh = round(75 - 15 * diurnal, 1)
        wet = 60 if rain or rh >= 90 else 0
        time = (START + timedelta(hours=step)).strftime("%Y-%m-%dT%H:%M:%SZ")
        yield time, temp, rh, rain, wet


def main(path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f, lineterminator="\n")
        out.writerow(["time_utc", "temp_c", "rh_pct", "rain_mm", "leaf_wet_min"])
        out.writerows(hours())


if __name__ == "__main__":
    main(sys.argv[1])
