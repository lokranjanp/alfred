"""Durable LangGraph workflow and exactly-one active delivery per report date."""

import asyncio
import json
from datetime import date, datetime, timezone
from typing import TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from . import ai, garmin
from .analysis import meal_totals, normalize, validate_recommendations
from .delivery import DeliveryUncertain, Mailer, render
from .models import Food, NormalizedDay, Preferences, Recommendations, eligible_foods


class State(TypedDict, total=False):
    run_id: str
    today: str
    preview: bool
    generated_at: str
    preferences: dict
    foods: list[dict]
    raw: dict
    day: dict
    recommendations: dict
    ai_available: bool
    report: dict
    html: str
    text: str


class Engine:
    def __init__(
        self,
        store,
        secrets,
        collector=garmin.collect,
        recommender=ai.recommend,
        mailer_factory=Mailer,
    ):
        self.store, self.secrets = store, secrets
        self.collector, self.recommender = collector, recommender
        self.mailer_factory = mailer_factory

    def graph(self, saver):
        async def load(state):
            prefs = self.store.preferences()
            return {
                "preferences": prefs.model_dump(mode="json"),
                "foods": [f.model_dump() for f in self.store.foods()],
            }

        async def collect(state):
            raw = await self.collector(
                Preferences.model_validate(state["preferences"]), date.fromisoformat(state["today"])
            )
            return {"raw": raw}

        async def calculate(state):
            day = normalize(
                state["raw"],
                Preferences.model_validate(state["preferences"]),
                date.fromisoformat(state["today"]),
            )
            return {"day": day.model_dump(mode="json"), "raw": {}}

        async def generate(state):
            prefs, day, foods = context(state)
            try:
                rec = await self.recommender(prefs, day, eligible_foods(foods, prefs), self.secrets)
                return {"recommendations": rec.model_dump(), "ai_available": True}
            except Exception:
                return {"recommendations": ai.fallback(day).model_dump(), "ai_available": False}

        async def unavailable(state):
            day = NormalizedDay.model_validate(state["day"])
            return {
                "recommendations": ai.fallback(
                    day, "Garmin data is unavailable. Sync your watch and check the saved session."
                ).model_dump(),
                "ai_available": False,
            }

        async def validate(state):
            prefs, day, foods = context(state)
            try:
                rec = validate_recommendations(
                    Recommendations.model_validate(state["recommendations"]),
                    eligible_foods(foods, prefs),
                    prefs,
                    day,
                )
                return {"recommendations": rec.model_dump()}
            except ValueError:
                return {"recommendations": ai.fallback(day).model_dump(), "ai_available": False}

        async def format_report(state):
            prefs, day, foods = context(state)
            rec = Recommendations.model_validate(state["recommendations"])
            meals, calories = meal_totals(rec, foods)
            generated = datetime.fromisoformat(state["generated_at"]).astimezone(
                ZoneInfo(prefs.timezone)
            )
            report = {
                "day": day.model_dump(mode="json"),
                "recommendations": rec.model_dump(),
                "preferences": state["preferences"],
                "foods": state["foods"],
                "meals": meals,
                "meal_calories": calories,
                "ai_available": state["ai_available"],
                "preview": state["preview"],
                "generated_at": generated.isoformat(),
                "delayed": (generated.hour, generated.minute) > (10, 1),
            }
            html, text = render(report)
            self.store.update_run(
                state["run_id"],
                status="ready",
                report=json.dumps(report),
                html=html,
                text=text,
                error=None,
            )
            return {"report": report, "html": html, "text": text}

        async def deliver(state):
            row = self.store.run(state["run_id"])
            if row["status"] in ("sent", "uncertain"):
                return {}
            if state["preview"]:
                self.store.update_run(state["run_id"], status="preview")
                return {}
            self.store.update_run(state["run_id"], status="sending")
            try:
                prefs = Preferences.model_validate(state["preferences"])
                # SMTP settings can be corrected between retries while report content stays fixed.
                current = self.store.preferences()
                prefs.smtp = current.smtp
                prefs.recipient = current.recipient
                mailer = self.mailer_factory(prefs, self.secrets)
                await asyncio.to_thread(
                    mailer.send, state["run_id"], state["report"], state["html"], state["text"]
                )
            except DeliveryUncertain as exc:
                self.store.update_run(state["run_id"], status="uncertain", error=str(exc))
                raise
            except Exception:
                self.store.update_run(
                    state["run_id"],
                    status="failed",
                    error="Email delivery failed. Check SMTP connection settings.",
                )
                raise RuntimeError(
                    "Email delivery failed. Check SMTP connection settings."
                ) from None
            self.store.update_run(state["run_id"], status="sent", error=None)
            return {}

        builder = StateGraph(State)
        for name, node in [
            ("load", load),
            ("collect", collect),
            ("calculate", calculate),
            ("generate", generate),
            ("unavailable", unavailable),
            ("validate", validate),
            ("render", format_report),
            ("deliver", deliver),
        ]:
            builder.add_node(name, node)
        builder.add_edge(START, "load")
        builder.add_edge("load", "collect")
        builder.add_edge("collect", "calculate")
        builder.add_conditional_edges(
            "calculate",
            lambda s: (
                "generate"
                if any(m["value"] is not None for m in s["day"]["metrics"])
                or s["day"]["activities"]
                else "unavailable"
            ),
        )
        builder.add_edge("generate", "validate")
        builder.add_edge("unavailable", "validate")
        builder.add_edge("validate", "render")
        builder.add_edge("render", "deliver")
        builder.add_edge("deliver", END)
        return builder.compile(checkpointer=saver)

    async def run(self, *, preview=False, today=None, now=None):
        now = now or datetime.now(timezone.utc)
        prefs = self.store.preferences()
        today = today or now.astimezone(ZoneInfo(prefs.timezone)).date()
        run_id = f"preview-{uuid4()}" if preview else f"daily-{today.isoformat()}"
        with self.store.report_lock():
            row = self.store.run(run_id)
            if row and row["status"] in ("sent", "uncertain"):
                return row
            if row and row["status"] == "sending":
                self.store.update_run(
                    run_id,
                    status="uncertain",
                    error="The app stopped during email delivery. Check your mailbox; automatic resend is disabled.",
                )
                return self.store.run(run_id)
            self.store.start_run(run_id, today.isoformat(), preview)
            async with AsyncSqliteSaver.from_conn_string(str(self.store.checkpoints)) as saver:
                graph = self.graph(saver)
                config = {"configurable": {"thread_id": run_id}}
                snapshot = await graph.aget_state(config)
                state = (
                    None
                    if snapshot.next
                    else State(
                        run_id=run_id,
                        today=today.isoformat(),
                        preview=preview,
                        generated_at=now.isoformat(),
                    )
                )
                try:
                    await graph.ainvoke(state, config)
                except Exception:
                    row = self.store.run(run_id)
                    if row["status"] not in ("uncertain", "failed"):
                        self.store.update_run(
                            run_id,
                            status="failed",
                            error="Report interrupted. Check connections and retry.",
                        )
                    raise
            self.store.checkpoints.chmod(0o600)
        return self.store.run(run_id)


def context(state):
    return (
        Preferences.model_validate(state["preferences"]),
        NormalizedDay.model_validate(state["day"]),
        [Food.model_validate(f) for f in state["foods"]],
    )
