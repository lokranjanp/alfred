import asyncio
import smtplib
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import TODAY

from garmin_agent import ai
from garmin_agent.analysis import normalize
from garmin_agent.delivery import DeliveryUncertain, Mailer
from garmin_agent.garmin import GarminError, activity_date, collect, decode_result
from garmin_agent.models import MealChoice, Preferences, Recommendations


def test_decodes_json_and_json_error_shapes():
    result = SimpleNamespace(is_error=False, data='{"steps":0}')
    assert decode_result(result) == {"steps": 0}
    result = SimpleNamespace(
        is_error=False, data=None, content=[SimpleNamespace(type="text", text="[1,2]")]
    )
    assert decode_result(result) == [1, 2]
    with pytest.raises(GarminError):
        decode_result(SimpleNamespace(is_error=False, data='{"error":"PRIVATE INFO"}'))
    with pytest.raises(GarminError):
        decode_result(SimpleNamespace(is_error=True))
    with pytest.raises(GarminError):
        decode_result(SimpleNamespace(is_error=False, data="not json"))


def test_activity_dates_use_profile_timezone_and_cross_midnight():
    activity = {"startTimeGMT": "2026-11-08T20:00:00Z", "startTimeLocal": "2026-11-08 20:00:00"}
    assert activity_date(activity, "Asia/Kolkata") == date(2026, 11, 9)
    assert activity_date(activity, "America/New_York") == date(2026, 11, 8)
    assert activity_date({}, "Asia/Kolkata") is None


async def test_real_stdio_mcp_client_with_offline_server():
    prefs = Preferences()
    prefs.mcp.command = sys.executable
    prefs.mcp.args = [str(Path(__file__).with_name("fake_mcp.py"))]
    raw = await collect(prefs, TODAY)
    assert raw["summaries"][0]["data"]["restingHeartRate"] == 52
    assert raw["activities"][0]["distance"] == 3000
    assert not raw["warnings"]


async def test_activity_pagination_and_partial_errors():
    class Client:
        def __init__(self, *args):
            self.pages = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, tool, **args):
            if tool == "garmin_activities":
                start = args["start"]
                if start == 0:
                    return [
                        {"activityId": i, "startTimeLocal": "2026-11-09 12:00:00"}
                        for i in range(50)
                    ]
                return [{"activityId": 100, "startTimeLocal": "2026-11-08 12:00:00"}]
            if tool == "garmin_activity_details":
                return {"summaryDTO": {"distance": 1000}}
            if tool == "garmin_daily_summary":
                raise GarminError("Not synced")
            return []

    raw = await collect(Preferences(), TODAY, Client)
    assert len(raw["activities"]) == 1
    assert raw["activities"][0]["activityId"] == 100
    assert raw["warnings"]


def test_smtp_uncertain_vs_explicit_rejection(store, secrets):
    class Client:
        error = None

        def send_message(self, message):
            assert message.is_multipart()
            assert len(message.get_payload()) == 2
            if self.error:
                raise self.error

        def quit(self):
            raise OSError("QUIT response lost after successful send")

        def close(self):
            pass

    client = Client()
    mailer = Mailer(store.preferences(), secrets)
    mailer.connect = lambda: client
    mailer.send("daily-2026-11-09", {"day": {"report_date": "2026-11-09"}}, "<p>x</p>", "x")
    client.error = ConnectionResetError("Response lost")
    with pytest.raises(DeliveryUncertain):
        mailer.send("daily-2026-11-09", {"day": {"report_date": "2026-11-09"}}, "html", "text")
    client.error = smtplib.SMTPDataError(550, b"Rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        mailer.send("daily-2026-11-09", {"day": {"report_date": "2026-11-09"}}, "html", "text")


async def test_gemini_invalid_output_repaired_and_identifiers_omitted(
    store, raw, secrets, monkeypatch
):
    calls = []

    class FakeModel:
        def with_structured_output(self, schema, method):
            assert method == "json_schema"
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            return Recommendations(
                overview="Steady",
                sleep_analysis="Eight hours",
                meals=[
                    MealChoice(
                        meal="breakfast",
                        food_id="missing" if len(calls) == 1 else "oats",
                        servings=1,
                    )
                ],
            )

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(ai, "model", lambda *args: FakeModel())
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    rec = await ai.recommend(
        store.preferences(), normalize(raw, store.preferences(), TODAY), store.foods(), secrets
    )
    assert rec.meals[0].food_id == "oats"
    assert len(calls) == 2
    prompt = calls[0][1].content
    assert "alex@example.com" not in prompt
    assert "Evening walk" not in prompt
    assert "smtp" not in prompt


def test_installed_gemini_adapter_accepts_report_schema(secrets):
    # Building the client/schema does not make a network request.
    secrets.set("gemini", "offline-schema-construction")
    client = ai.model(Preferences(), secrets)
    assert client.with_structured_output(Recommendations, method="json_schema") is not None
