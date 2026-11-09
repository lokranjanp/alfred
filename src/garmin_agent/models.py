"""Validated configuration and report contracts. Secrets never enter these models."""

from datetime import date
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SMTPSettings(Model):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    sender: EmailStr | None = None
    security: Literal["starttls", "ssl"] = "starttls"


class MCPSettings(Model):
    command: str = str(Path.home() / "PycharmProjects/garmin_mcp/.venv/bin/python")
    args: list[str] = Field(default_factory=lambda: ["-m", "garmin_mcp"])
    session_path: str = str(Path.home() / ".garth")


class Preferences(Model):
    name: str = Field(default="", max_length=100)
    recipient: EmailStr | None = None
    timezone: str = "Asia/Kolkata"
    units: Literal["metric", "imperial"] = "metric"
    goals: str = Field(default="General fitness and consistent recovery", max_length=2000)
    activities: list[str] = Field(default_factory=lambda: ["walking"])
    exercise_minutes: int = Field(default=30, ge=5, le=240)
    equipment: str = Field(default="", max_length=1000)
    limitations: str = Field(default="", max_length=2000)
    diet_tags: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    hydration_ml: int | None = Field(default=None, ge=250, le=6000)
    sleep_goal_hours: float = Field(default=8, ge=4, le=12)
    gemini_model: str = Field(default="gemini-3.7-flash", min_length=1, max_length=150)
    smtp: SMTPSettings = Field(default_factory=SMTPSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, e.g. Asia/Kolkata") from exc
        return value

    @field_validator("activities", "diet_tags", "allergies")
    @classmethod
    def clean_tags(cls, values):
        if len(values) > 50:
            raise ValueError("Use at most 50 entries")
        return sorted({s.strip().lower()[:100] for s in values if s.strip()})


class Food(Model):
    id: str = ""
    name: str = Field(min_length=1, max_length=150)
    serving: str = Field(min_length=1, max_length=150)
    calories: float = Field(ge=0, le=10000)
    ingredients: str = Field(default="", max_length=2000)
    diet_tags: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    meal_types: list[Literal["breakfast", "lunch", "dinner", "snack"]] = Field(
        default_factory=lambda: ["breakfast", "lunch", "dinner", "snack"]
    )

    @field_validator("diet_tags", "allergens")
    @classmethod
    def clean_tags(cls, values):
        return Preferences.clean_tags(values)


def eligible_foods(foods: list[Food], preferences: Preferences) -> list[Food]:
    return [
        food
        for food in foods
        if not set(food.allergens) & set(preferences.allergies)
        and set(preferences.diet_tags).issubset(food.diet_tags)
    ]


class Observation(Model):
    key: str
    label: str
    value: float | None = None
    unit: str = ""
    baseline: float | None = None
    samples: int = 0
    date: date


class Activity(Model):
    name: str
    kind: str
    minutes: float | None = None
    distance_km: float | None = None
    calories: float | None = None
    average_hr: float | None = None


class NormalizedDay(Model):
    report_date: date
    wellness_date: date
    timezone: str
    metrics: list[Observation] = Field(default_factory=list)
    sleep: dict = Field(default_factory=dict)
    activities: list[Activity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExerciseSuggestion(Model):
    activity: str = Field(max_length=200)
    minutes: int = Field(ge=0, le=240)
    intensity: Literal["rest", "easy", "moderate", "hard"]
    reason: str = Field(max_length=1500)
    alternative: str = Field(max_length=1000)


class MealChoice(Model):
    meal: Literal["breakfast", "lunch", "dinner", "snack"]
    food_id: str
    servings: float = Field(gt=0, le=5)


class Recommendations(Model):
    overview: str = Field(max_length=2500)
    sleep_analysis: str = Field(max_length=2000)
    exercise: ExerciseSuggestion | None = None
    meals: list[MealChoice] = Field(default_factory=list, max_length=12)
    food_note: str = Field(default="", max_length=1500)
    hydration: str = Field(default="", max_length=1000)
