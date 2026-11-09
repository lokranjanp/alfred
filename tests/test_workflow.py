import asyncio
import json
from datetime import datetime, timezone

import pytest
from conftest import TODAY

from garmin_agent.delivery import DeliveryUncertain
from garmin_agent.engine import Engine
from garmin_agent.models import ExerciseSuggestion, MealChoice, Recommendations

NOW = datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)


async def suggestions(prefs, day, foods, secrets):
    return Recommendations(
        overview="A steady day",
        sleep_analysis="An eight-hour night",
        exercise=ExerciseSuggestion(
            activity="walking", minutes=20, intensity="easy", reason="Recovery", alternative="Rest"
        ),
        meals=[MealChoice(meal="breakfast", food_id="oats", servings=1.5)],
    )


def make_engine(store, secrets, raw, sends, *, recommender=suggestions, failure=None):
    calls = []

    async def collect(*args):
        calls.append("collect")
        return raw

    class Mailer:
        def __init__(self, prefs, secrets):
            self.prefs = prefs

        def send(self, run_id, report, html, text):
            sends.append((run_id, report, html, text, self.prefs.smtp.host))
            if failure:
                failure()

    return Engine(
        store, secrets, collector=collect, recommender=recommender, mailer_factory=Mailer
    ), calls


async def test_preview_and_daily_idempotency(store, secrets, raw):
    sends = []
    engine, calls = make_engine(store, secrets, raw, sends)
    preview = await engine.run(preview=True, today=TODAY, now=NOW)
    assert preview["status"] == "preview"
    assert not sends
    report = json.loads(preview["report"])
    assert report["meal_calories"] == 525
    assert report["day"]["wellness_date"] == "2026-11-08"
    assert "525" in preview["html"] and "525" in preview["text"]
    assert report["delayed"]
    row = await engine.run(today=TODAY, now=NOW)
    assert row["status"] == "sent"
    await engine.run(today=TODAY, now=NOW)
    assert len(sends) == 1
    assert len(calls) == 2  # preview + daily, no second delivery collection
    store.delete_food("oats")
    assert json.loads(store.run(row["id"])["report"])["foods"][0]["calories"] == 350


async def test_resume_delivery_checkpoint_without_recollecting(store, secrets, raw):
    sends = []
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("SMTP auth failed")

    engine, calls = make_engine(store, secrets, raw, sends, failure=fail_once)
    with pytest.raises(RuntimeError):
        await engine.run(today=TODAY, now=NOW)
    prefs = store.preferences()
    prefs.smtp.host = "corrected.example.com"
    store.set("preferences", prefs.model_dump(mode="json"))
    row = await engine.run(today=TODAY, now=NOW)
    assert row["status"] == "sent"
    assert len(calls) == 1
    assert sends[-1][-1] == "corrected.example.com"


async def test_unknown_delivery_never_resent(store, secrets, raw):
    sends = []

    def uncertain():
        raise DeliveryUncertain("Delivery unknown")

    engine, _ = make_engine(store, secrets, raw, sends, failure=uncertain)
    with pytest.raises(DeliveryUncertain):
        await engine.run(today=TODAY, now=NOW)
    assert (await engine.run(today=TODAY, now=NOW))["status"] == "uncertain"
    assert len(sends) == 1


async def test_crash_while_sending_becomes_unknown(store, secrets, raw):
    store.start_run("daily-2026-11-09", TODAY.isoformat(), False)
    store.update_run("daily-2026-11-09", status="sending")
    sends = []
    engine, calls = make_engine(store, secrets, raw, sends)
    assert (await engine.run(today=TODAY, now=NOW))["status"] == "uncertain"
    assert not sends and not calls


async def test_model_failure_falls_back_to_factual_report(store, secrets, raw):
    async def broken(*args):
        raise RuntimeError("Model down")

    engine, _ = make_engine(store, secrets, raw, [], recommender=broken)
    row = await engine.run(preview=True, today=TODAY, now=NOW)
    report = json.loads(row["report"])
    assert not report["ai_available"]
    assert report["meals"] == []
    assert "50" in row["html"]


async def test_no_data_skips_model_and_produces_missing_data_report(store, secrets):
    async def never(*args):
        pytest.fail("Model must not be invoked with no data")

    engine, _ = make_engine(store, secrets, {}, [], recommender=never)
    row = await engine.run(preview=True, today=TODAY, now=NOW)
    assert "Garmin data is unavailable" in row["html"]
    assert "Unavailable" in row["text"]


async def test_overlapping_runs_are_rejected(store, secrets, raw):
    started, finish = asyncio.Event(), asyncio.Event()

    async def collect(*args):
        started.set()
        await finish.wait()
        return raw

    engine = Engine(store, secrets, collector=collect, recommender=suggestions)
    task = asyncio.create_task(engine.run(preview=True, today=TODAY, now=NOW))
    await started.wait()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await engine.run(preview=True, today=TODAY, now=NOW)
    finally:
        finish.set()
        await task


async def test_report_html_escapes_user_and_model_content(store, secrets, raw):
    prefs = store.preferences()
    prefs.name = '<script>alert("x")</script>'
    store.set("preferences", prefs.model_dump(mode="json"))
    engine, _ = make_engine(store, secrets, raw, [])
    row = await engine.run(preview=True, today=TODAY, now=NOW)
    assert "<script>" not in row["html"]
    assert "&lt;script&gt;" in row["html"]
