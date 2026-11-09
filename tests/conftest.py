from datetime import date, datetime, timedelta, timezone

import pytest

from garmin_agent.models import Food, Preferences
from garmin_agent.storage import Store

TODAY = date(2026, 11, 9)


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value


@pytest.fixture
def secrets():
    return MemorySecrets()


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state")
    prefs = Preferences(name="Alex", recipient="alex@example.com")
    store.set("preferences", prefs.model_dump(mode="json"))
    store.save_food(
        Food(
            id="oats",
            name="Oats & banana",
            serving="1 bowl",
            calories=350,
            diet_tags=["vegetarian"],
            allergens=["dairy"],
        )
    )
    return store


@pytest.fixture
def raw():
    summaries = []
    for offset in range(8):
        d = TODAY - timedelta(days=offset + 1)
        summaries.append(
            {
                "date": d.isoformat(),
                "data": {
                    "calendarDate": d.isoformat(),
                    "restingHeartRate": 50 + offset,
                    "minHeartRate": 44,
                    "maxHeartRate": 150,
                    "totalSteps": 7000,
                    "averageStressLevel": 20,
                    "bodyBatteryHighestValue": 90,
                    "bodyBatteryLowestValue": 20,
                    "averageSpo2": 97,
                    "totalKilocalories": 2100,
                    "activeKilocalories": 300,
                },
            }
        )
    sleeps = []
    for offset in range(8):
        d = TODAY - timedelta(days=offset)
        end = datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(hours=1.5)
        start = end - timedelta(hours=8)
        sleeps.append(
            {
                "daily_sleep_dto": {
                    "calendar_date": d.isoformat(),
                    "sleep_time_seconds": 28800,
                    "sleep_start_timestamp_gmt": int(start.timestamp() * 1000),
                    "sleep_end_timestamp_gmt": int(end.timestamp() * 1000),
                    "deep_sleep_seconds": 5400,
                    "light_sleep_seconds": 18000,
                    "rem_sleep_seconds": 5400,
                    "awake_sleep_seconds": 1200,
                    "awake_count": 2,
                    "sleep_scores": {"overall": {"value": 85}},
                }
            }
        )
    return {
        "summaries": summaries,
        "sleep": sleeps,
        "hrv": [
            {"calendar_date": (TODAY - timedelta(days=i)).isoformat(), "last_night_avg": 45 + i}
            for i in range(8)
        ],
        "series": {
            "hydration": [{"calendar_date": "2026-11-08", "value_in_ml": 2000}],
            "intensity_minutes": [
                {"calendar_date": "2026-11-08", "moderate_value": 0, "vigorous_value": 0}
            ],
        },
        "weight": [{"calendar_date": "2026-11-06", "weight": 70000}],
        "activities": [
            {
                "activityId": 1,
                "activityName": "Evening walk",
                "activityType": {"typeKey": "walking"},
                "duration": 1800,
                "distance": 3000,
                "calories": 150,
            }
        ],
        "warnings": [],
    }
