"""Deterministic normalization, date selection, baselines, and calorie arithmetic."""

import math
import re
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from .models import Activity, Food, NormalizedDay, Observation, Preferences, Recommendations


def get(data, *names):
    if not isinstance(data, dict):
        return None
    canonical = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in data.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in canonical:
            return canonical[key]
    return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def items(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def record_date(data):
    return str(get(data, "calendar_date", "date") or "")[:10]


def timestamp(value, tz):
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / 1000, timezone.utc)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz))
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


WELLNESS = [
    ("rhr", "Resting heart rate", "restingHeartRate", "bpm"),
    ("hr_min", "Minimum heart rate", "minHeartRate", "bpm"),
    ("hr_max", "Maximum heart rate", "maxHeartRate", "bpm"),
    ("stress", "Average stress", "averageStressLevel", "/100"),
    ("body_battery_high", "Body Battery high", "bodyBatteryHighestValue", "/100"),
    ("body_battery_low", "Body Battery low", "bodyBatteryLowestValue", "/100"),
    ("spo2", "Average SpO₂", "averageSpo2", "%"),
    ("respiration", "Waking respiration", "avgWakingRespirationValue", "breaths/min"),
    ("steps", "Steps", "totalSteps", "steps"),
    ("calories", "Energy burned (Garmin estimate)", "totalKilocalories", "kcal"),
    ("active_calories", "Active energy (Garmin estimate)", "activeKilocalories", "kcal"),
]


def normalize(raw, preferences: Preferences, today: date):
    yesterday = today - timedelta(days=1)
    day = NormalizedDay(
        report_date=today,
        wellness_date=yesterday,
        timezone=preferences.timezone,
        warnings=list(raw.get("warnings", [])),
    )
    summaries = {x["date"]: x["data"] for x in raw.get("summaries", [])}

    def add(key, label, value, unit, history=(), target=yesterday):
        values = [v for v in history if v is not None]
        day.metrics.append(
            Observation(
                key=key,
                label=label,
                value=value,
                unit=unit,
                date=target,
                baseline=round(mean(values), 2) if len(values) >= 3 else None,
                samples=len(values),
            )
        )

    previous = [(yesterday - timedelta(days=i)).isoformat() for i in range(1, 8)]
    for key, label, source, unit in WELLNESS:
        add(
            key,
            label,
            number(get(summaries.get(yesterday.isoformat()), source)),
            unit,
            [number(get(summaries.get(d), source)) for d in previous],
        )

    for metric, key, source, label, unit in [
        ("steps", "steps", "total_steps", "Steps", "steps"),
        ("hydration", "hydration", "value_in_ml", "Hydration logged", "ml"),
        ("intensity_minutes", "moderate_minutes", "moderate_value", "Moderate activity", "min"),
        ("intensity_minutes", "vigorous_minutes", "vigorous_value", "Vigorous activity", "min"),
    ]:
        series = {record_date(v): v for v in items(raw.get("series", {}).get(metric))}
        value = number(get(series.get(yesterday.isoformat()), source))
        existing = next((m for m in day.metrics if m.key == key), None)
        if existing is not None:
            if existing.value is None:
                existing.value = value
            history = [number(get(series.get(d), source)) for d in previous]
            history = [v for v in history if v is not None]
            if len(history) > existing.samples:
                existing.samples = len(history)
                existing.baseline = round(mean(history), 2) if len(history) >= 3 else None
            continue
        add(key, label, value, unit, [number(get(series.get(d), source)) for d in previous])

    hrv = {record_date(v): v for v in items(raw.get("hrv"))}
    add(
        "hrv",
        "Last night's HRV",
        number(get(hrv.get(today.isoformat()), "last_night_avg")),
        "ms",
        [
            number(get(hrv.get((today - timedelta(days=i)).isoformat()), "last_night_avg"))
            for i in range(1, 8)
        ],
        today,
    )

    sleeps = {}
    for record in items(raw.get("sleep")):
        dto = get(record, "daily_sleep_dto") or record
        if isinstance(dto, dict):
            sleeps[record_date(dto)] = dto
    sleep = sleeps.get(today.isoformat(), {})
    end = timestamp(get(sleep, "sleep_end_timestamp_gmt"), preferences.timezone)
    if end and end.date() != today:
        sleep = {}
    if sleep:
        start = timestamp(get(sleep, "sleep_start_timestamp_gmt"), preferences.timezone)
        day.sleep = {
            "date": today.isoformat(),
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "hours": _scaled(get(sleep, "sleep_time_seconds"), 3600),
            "deep_minutes": _scaled(get(sleep, "deep_sleep_seconds"), 60),
            "rem_minutes": _scaled(get(sleep, "rem_sleep_seconds"), 60),
            "light_minutes": _scaled(get(sleep, "light_sleep_seconds"), 60),
            "awake_minutes": _scaled(get(sleep, "awake_sleep_seconds"), 60),
            "awakenings": number(get(sleep, "awake_count")),
            "score": number(get(get(get(sleep, "sleep_scores"), "overall"), "value")),
        }
    else:
        day.warnings.append("Last night's sleep is unavailable; an older night is not substituted.")
    add(
        "sleep_hours",
        "Last night's sleep",
        day.sleep.get("hours"),
        "hours",
        [
            _scaled(
                get(sleeps.get((today - timedelta(days=i)).isoformat()), "sleep_time_seconds"), 3600
            )
            for i in range(1, 8)
        ],
        today,
    )
    weights = sorted(
        (
            v
            for v in items(raw.get("weight"))
            if record_date(v) and record_date(v) <= yesterday.isoformat()
        ),
        key=record_date,
    )
    if weights:
        latest = weights[-1]
        add(
            "weight",
            "Latest recorded weight",
            _scaled(get(latest, "weight"), 1000),
            "kg",
            target=date.fromisoformat(record_date(latest)),
        )
    for a in items(raw.get("activities")):
        kind = get(get(a, "activity_type"), "type_key") or "activity"
        day.activities.append(
            Activity(
                name=str(get(a, "activity_name") or kind),
                kind=str(kind),
                minutes=_scaled(get(a, "duration"), 60),
                distance_km=_scaled(get(a, "distance"), 1000),
                calories=number(get(a, "calories")),
                average_hr=number(get(a, "average_hr")),
            )
        )
    day.warnings = list(dict.fromkeys(day.warnings))
    return day


def _scaled(value, divisor):
    n = number(value)
    return round(n / divisor, 2) if n is not None else None


def validate_recommendations(rec: Recommendations, foods: list[Food], prefs, day):
    catalog = {f.id: f for f in foods}
    for choice in rec.meals:
        if choice.food_id not in catalog or choice.meal not in catalog[choice.food_id].meal_types:
            raise ValueError("Meal selection does not match eligible catalog entries")
    if rec.exercise:
        if rec.exercise.minutes > prefs.exercise_minutes:
            raise ValueError("Exercise exceeds the user's available time")
        if rec.exercise.activity.strip().lower() not in {*prefs.activities, "rest"}:
            raise ValueError("Exercise must use a preferred activity or rest")
        if rec.exercise.intensity == "hard" and (
            not day.sleep or next(m for m in day.metrics if m.key == "hrv").value is None
        ):
            raise ValueError("Insufficient recovery data for hard exercise")
    return rec


def meal_totals(rec, foods):
    catalog = {f.id: f for f in foods}
    meals = []
    for choice in rec.meals:
        food = catalog[choice.food_id]
        meals.append(
            {
                **choice.model_dump(),
                "name": food.name,
                "serving": food.serving,
                "calories": round(food.calories * choice.servings, 1),
            }
        )
    return meals, round(sum(m["calories"] for m in meals), 1)


def display_metric(metric, units):
    value, baseline, unit = metric["value"], metric["baseline"], metric["unit"]
    if units == "imperial" and unit in ("kg", "ml"):
        factor, unit = (2.2046226218, "lb") if unit == "kg" else (1 / 29.5735296, "fl oz")
        value = value * factor if value is not None else None
        baseline = baseline * factor if baseline is not None else None
    return {
        **metric,
        "display_value": f"{value:,.1f}".removesuffix(".0") if value is not None else "Unavailable",
        "display_baseline": f"{baseline:,.1f}" if baseline is not None else None,
        "display_unit": unit,
    }
