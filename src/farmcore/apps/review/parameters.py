"""Static typed parameter catalog. Bounds are engineering limits, not accuracy claims."""

from datetime import date

from farmcore.apps.advisor.engine.config import AdvisorConfig

# Explicit keyed constraints; source strings and assumed flags remain those of AdvisorConfig.
BOUNDS = {
    "downy_window_h": (1, 168),
    "downy_rain_min_mm": (0.1, 200),
    "downy_temp_min_c": (-10, 40),
    "downy_event_max_days": (0.1, 30),
    "incub_tmin": (-10, 35),
    "incub_humid_rh_hourly": (0, 100),
    "incub_alert_pct": (1, 100),
    "incub_symptoms_pct": (1, 200),
    "gt_band_min_c": (-10, 45),
    "gt_band_max_c": (-10, 45),
    "gt_min_hours": (1, 24),
    "gt_hot_c": (20, 55),
    "gt_onset": (1, 100),
    "gt_step_pre_onset": (1, 100),
    "gt_delta_qualify": (1, 100),
    "gt_delta_qualify_hot": (0, 100),
    "gt_delta_fail": (-100, 0),
    "gt_delta_fail_hot": (-100, 0),
    "gt_index_min": (0, 100),
    "gt_index_max": (1, 100),
    "gt_low_max": (0, 100),
    "gt_moderate_max": (0, 100),
    "gt_interval_low_days": (0.1, 30),
    "gt_interval_moderate_days": (0.1, 30),
    "gt_interval_high_days": (0.1, 30),
    "tankmix_cover_ending_days": (0, 10),
    "protection_contact_days": (0.1, 30),
    "protection_penetrant_days": (0.1, 30),
    "protection_rapid_growth_days": (0.1, 30),
    "washoff_copper_mm": (0.1, 200),
    "washoff_sulphur_mm": (0.1, 200),
    "window_wind_max_kmh": (0.1, 100),
    "window_rr1_max_mm_h": (0.1, 100),
    "window_rain_lookahead_h": (1, 24),
    "window_rain_next_max_mm": (0, 100),
    "window_sulphur_tmax_c": (0, 50),
    "window_min_open_hours": (1, 24),
    "window_horizon_h": (1, 168),
    "calendar.downy_interval_days": (0.1, 30),
    "calendar.downy_interval_rapid_growth_days": (0.1, 30),
    "calendar.sulphur_interval_days": (0.1, 30),
    "calendar.merge_days": (0, 30),
    "calendar.eval_hour_local": (0, 23),
    "calendar.window_day_start_hour_local": (0, 23),
    "calendar.window_day_end_hour_local": (1, 24),
    "daily.downy_days": (1, 14),
    "daily.incub_daily_rh_split": (0, 100),
    "daily.window_wind_max_ms": (0.1, 30),
    "daily.window_rain_max_mm": (0, 100),
    "daily.window_rain_next_max_mm": (0, 100),
    "daily.t_min_hour_local": (0, 23),
    "daily.t_max_hour_local": (0, 23),
}


def catalog(config: AdvisorConfig) -> dict:
    baseline = AdvisorConfig.for_region_year(config.region, config.year)
    result = {}
    for key in (*config.threshold_names(), *config.nested_threshold_names()):
        threshold = config.threshold(key)
        value = threshold.value
        is_date = isinstance(value, date)
        bounds = (
            (date(value.year, 1, 1).isoformat(), date(value.year, 12, 31).isoformat())
            if is_date
            else BOUNDS[key]
        )
        original = baseline.threshold(key).value
        result[key] = {
            "baseline_value": original.isoformat() if isinstance(original, date) else original,
            "modified": value != original,
            "value": value.isoformat() if is_date else value,
            "source": threshold.source,
            "assumed": threshold.assumed,
            "unit": threshold.unit,
            "type": "date" if is_date else ("integer" if type(value) is int else "number"),
            "min": bounds[0],
            "max": bounds[1],
            "bounds_basis": "Engineering validation limits; not field accuracy evidence",
        }
    return result


def apply_overrides(config: AdvisorConfig, overrides: dict) -> AdvisorConfig:
    """Validate scalar values and cross-field relations, then fully reconstruct typed config."""
    import math

    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("Provide at least one supported parameter override")
    available = catalog(config)
    payload = config.model_dump(mode="json")
    for key, value in overrides.items():
        if key not in available:
            raise ValueError(f"Unsupported parameter: {key}")
        spec = available[key]
        if spec["type"] == "date":
            if type(value) is not str:
                raise ValueError(f"{key}: expected ISO date")
            value = date.fromisoformat(value).isoformat()
        else:
            if type(value) not in (int, float) or (
                type(value) is float and not math.isfinite(value)
            ):
                raise ValueError(f"{key}: expected finite number")
            if spec["type"] == "integer" and type(value) is not int:
                raise ValueError(f"{key}: expected integer")
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"{key}: outside engineering bounds")
        target = payload
        parts = key.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]]["value"] = value
    effective = AdvisorConfig.model_validate(payload)

    def v(name):
        return effective.threshold(name).value

    ordered = (
        v("gt_band_min_c") < v("gt_band_max_c") <= v("gt_hot_c")
        and v("gt_index_min")
        <= v("gt_low_max")
        < v("gt_moderate_max")
        < v("gt_onset")
        <= v("gt_index_max")
        and v("window_min_open_hours") <= v("window_horizon_h")
        and v("calendar.window_day_start_hour_local") < v("calendar.window_day_end_hour_local")
        and v("season_gate") <= v("rapid_growth_start") <= v("rapid_growth_end")
        and v("season_gate") <= v("powdery_start") < v("powdery_end") <= v("calendar.bunch_closure")
        and v("incub_alert_pct") <= v("incub_symptoms_pct")
        and v("gt_interval_high_days")
        <= v("gt_interval_moderate_days")
        <= v("gt_interval_low_days")
    )
    if not ordered:
        raise ValueError("Parameter ordering is inconsistent")
    return effective
