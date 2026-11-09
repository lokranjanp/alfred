from copy import deepcopy
from datetime import date

import pytest
from conftest import TODAY
from pydantic import ValidationError

from garmin_agent.analysis import display_metric, meal_totals, normalize, validate_recommendations
from garmin_agent.models import (
    ExerciseSuggestion,
    Food,
    MealChoice,
    Preferences,
    Recommendations,
    eligible_foods,
)


def test_dates_baselines_units_and_zero(raw):
    result = normalize(raw, Preferences(), TODAY)
    values = {m.key: m for m in result.metrics}
    assert result.wellness_date == date(2026, 11, 8)
    assert result.sleep["end"].startswith("2026-11-09T07:00")
    assert values["rhr"].value == 50
    assert values["rhr"].baseline == 54
    assert values["hrv"].date == TODAY
    assert values["hrv"].baseline == 49
    assert values["moderate_minutes"].value == 0
    assert values["respiration"].value is None
    assert values["weight"].value == 70
    assert values["weight"].date == date(2026, 11, 6)
    assert result.activities[0].distance_km == 3
    assert display_metric(values["weight"].model_dump(), "imperial")["display_value"] == "154.3"


def test_missing_sleep_does_not_use_previous_night(raw):
    raw["sleep"] = raw["sleep"][1:]
    result = normalize(raw, Preferences(), TODAY)
    assert not result.sleep
    assert any("older night" in w for w in result.warnings)


def test_timestamp_date_must_match_sleep_date(raw):
    raw["sleep"][0]["daily_sleep_dto"]["sleep_end_timestamp_gmt"] -= 86400000
    assert not normalize(raw, Preferences(), TODAY).sleep


def test_insufficient_baseline_and_negative_sentinels(raw):
    raw["summaries"] = raw["summaries"][:3]
    raw["summaries"][0]["data"]["averageStressLevel"] = -1
    values = {m.key: m for m in normalize(raw, Preferences(), TODAY).metrics}
    assert values["stress"].value is None
    assert values["rhr"].baseline is None
    assert values["rhr"].samples == 2


def test_catalog_constraints_and_deterministic_calories(raw):
    foods = [
        Food(
            id="oats",
            name="Oats",
            serving="bowl",
            calories=350,
            diet_tags=["VEGETARIAN"],
            allergens=["Dairy"],
        ),
        Food(
            id="rice", name="Rice", serving="bowl", calories=200, diet_tags=["vegetarian", "vegan"]
        ),
    ]
    prefs = Preferences(diet_tags=["vegetarian"], allergies=["dairy"])
    assert [f.id for f in eligible_foods(foods, prefs)] == ["rice"]
    rec = Recommendations(
        overview="Plan",
        sleep_analysis="Good sleep",
        meals=[MealChoice(meal="lunch", food_id="rice", servings=1.5)],
    )
    eligible = eligible_foods(foods, prefs)
    validate_recommendations(rec, eligible, prefs, normalize(raw, prefs, TODAY))
    meals, total = meal_totals(rec, eligible)
    assert meals[0]["calories"] == 300
    assert total == 300
    old = deepcopy(meals)
    foods[1].calories = 999
    assert old[0]["calories"] == 300
    rec.meals[0].food_id = "oats"
    with pytest.raises(ValueError):
        validate_recommendations(rec, eligible, prefs, normalize(raw, prefs, TODAY))


def test_invalid_exercise_and_meal_types_rejected(raw):
    prefs = Preferences()
    day = normalize(raw, prefs, TODAY)
    rec = Recommendations(
        overview="Plan",
        sleep_analysis="Sleep",
        exercise=ExerciseSuggestion(
            activity="walking", minutes=31, intensity="easy", reason="Recovery", alternative="Rest"
        ),
    )
    with pytest.raises(ValueError):
        validate_recommendations(rec, [], prefs, day)
    rec.exercise.minutes = 30
    rec.exercise.intensity = "hard"
    day.sleep = {}
    with pytest.raises(ValueError):
        validate_recommendations(rec, [], prefs, day)
    rec.exercise = None
    rec.meals = [MealChoice(meal="dinner", food_id="a", servings=1)]
    with pytest.raises(ValueError):
        validate_recommendations(
            rec,
            [Food(id="a", name="Toast", serving="slice", calories=100, meal_types=["breakfast"])],
            prefs,
            day,
        )


def test_schema_rejects_invalid_timezones_and_nonfinite_calories():
    with pytest.raises(ValidationError):
        Preferences(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        Food(name="bad", serving="bowl", calories=float("nan"))


def test_steps_history_falls_back_to_metric_series(raw):
    raw["summaries"] = []
    raw["series"]["steps"] = [
        {"calendar_date": f"2026-11-{d:02}", "total_steps": 1000 + d} for d in range(1, 9)
    ]
    steps = next(m for m in normalize(raw, Preferences(), TODAY).metrics if m.key == "steps")
    assert steps.value == 1008
    assert steps.baseline == 1004
    assert steps.samples == 7
