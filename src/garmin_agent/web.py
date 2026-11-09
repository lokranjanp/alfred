"""Loopback dashboard and local JSON API. All mutations require a same-origin CSRF token."""

import asyncio
import secrets as secure
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ai
from .delivery import Mailer, environment
from .engine import Engine
from .garmin import GarminClient
from .models import Food, Model, Preferences
from .scheduler import next_slot, scheduler_loop
from .storage import Secrets, Store, data_directory


class SecretInput(Model):
    value: str = Field(min_length=1, max_length=10000)


class RunInput(Model):
    preview: bool = True


class ScheduleInput(Model):
    enabled: bool


def create_app(store=None, secrets=None, engine=None, *, start_scheduler=True):
    store = store or Store(data_directory())
    secrets = secrets or Secrets()
    engine = engine or Engine(store, secrets)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler_loop(store, engine, stop)) if start_scheduler else None
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="Garmin Morning", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.engine = store, engine
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            token, cookie = request.headers.get("x-csrf-token", ""), request.cookies.get("csrf", "")
            if (
                (origin and origin != expected)
                or not cookie
                or not secure.compare_digest(token, cookie)
            ):
                return JSONResponse(
                    {"detail": "Reload the dashboard and try again."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        token = request.cookies.get("csrf") or secure.token_urlsafe(32)
        response = HTMLResponse(environment.get_template("dashboard.html").render(csrf=token))
        response.set_cookie("csrf", token, httponly=True, samesite="strict")
        return response

    @app.get("/api/preferences")
    async def preferences():
        return store.preferences()

    @app.put("/api/preferences")
    async def save_preferences(prefs: Preferences):
        old = store.preferences()
        store.set("preferences", prefs.model_dump(mode="json"))
        changed = any(
            getattr(old, key) != getattr(prefs, key)
            for key in ("smtp", "mcp", "recipient", "gemini_model")
        )
        if changed:
            store.set("integration_tests", {})
            store.set("schedule", {"enabled": False})
        elif prefs.timezone != old.timezone and store.get("schedule", {}).get("enabled"):
            store.set(
                "schedule",
                {
                    "enabled": True,
                    "first_date": next_slot(prefs, datetime.now(timezone.utc)).date().isoformat(),
                },
            )
        return {"ok": True, "schedule_disabled": changed}

    @app.put("/api/secrets/{name}")
    async def save_secret(name: str, body: SecretInput):
        if name not in Secrets.names:
            raise HTTPException(404, "Unknown connection")
        try:
            await asyncio.to_thread(secrets.set, name, body.value)
        except Exception:
            raise HTTPException(
                400, "Could not save to macOS Keychain. Check Keychain access."
            ) from None
        store.set("integration_tests", {})
        store.set("schedule", {"enabled": False})
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        prefs = store.preferences()
        schedule = store.get("schedule", {"enabled": False})
        return {
            "schedule": schedule,
            "timezone": prefs.timezone,
            "next_run": next_slot(prefs, datetime.now(timezone.utc)).isoformat()
            if schedule.get("enabled")
            else None,
            "integration_tests": store.get("integration_tests", {}),
            "last_run": next(iter(store.history()), None),
        }

    async def test_integration(name):
        prefs = store.preferences()
        try:
            if name == "garmin":
                async with GarminClient(prefs) as client:
                    await client.call("garmin_user_profile")
                    await client.call("garmin_user_settings")
            elif name == "gemini":
                await ai.test_connection(prefs, secrets)
            elif name == "smtp":
                await asyncio.to_thread(Mailer(prefs, secrets).test)
            else:
                raise HTTPException(404, "Unknown integration")
        except HTTPException:
            raise
        except Exception:
            message = {
                "garmin": "Check MCP executable/session, then run garmin-agent auth.",
                "gemini": "Check Gemini API key, model ID and quota.",
                "smtp": "Check SMTP host, TLS mode, username and app password.",
            }[name]
            tests = store.get("integration_tests", {})
            tests[name] = {"ok": False, "message": message}
            store.set("integration_tests", tests)
            raise HTTPException(400, message) from None
        tests = store.get("integration_tests", {})
        tests[name] = {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()}
        store.set("integration_tests", tests)
        return {"ok": True}

    @app.post("/api/integrations/{name}/test")
    async def test(name: str):
        return await test_integration(name)

    @app.get("/api/foods")
    async def foods():
        return store.foods()

    @app.post("/api/foods", status_code=201)
    async def add_food(food: Food):
        return store.save_food(food.model_copy(update={"id": ""}))

    @app.put("/api/foods/{food_id}")
    async def update_food(food_id: str, food: Food):
        if not any(f.id == food_id for f in store.foods()):
            raise HTTPException(404, "Food not found")
        return store.save_food(food.model_copy(update={"id": food_id}))

    @app.delete("/api/foods/{food_id}")
    async def delete_food(food_id: str):
        if not store.delete_food(food_id):
            raise HTTPException(404, "Food not found")
        return {"ok": True}

    @app.get("/api/reports")
    async def reports():
        return store.history()

    @app.get("/fragments/history", response_class=HTMLResponse)
    async def history():
        return environment.get_template("history.html").render(runs=store.history())

    @app.post("/api/reports/run")
    async def run(body: RunInput):
        if not body.preview and not store.preferences().recipient:
            raise HTTPException(400, "Save an email recipient first.")
        try:
            row = await engine.run(preview=body.preview)
            return {"id": row["id"], "status": row["status"]}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(
                500, "Report interrupted. Check run history and connections."
            ) from None

    @app.get("/reports/{run_id}")
    async def report(run_id: str, format: str = "html"):
        row = store.run(run_id)
        if not row or not row["html"]:
            raise HTTPException(404, "Report is not ready")
        return PlainTextResponse(row["text"]) if format == "text" else HTMLResponse(row["html"])

    @app.post("/api/schedule")
    async def schedule(body: ScheduleInput):
        prefs = store.preferences()
        if body.enabled:
            if not prefs.recipient:
                raise HTTPException(400, "Save your email recipient in Preferences first.")
            for name in ("garmin", "gemini", "smtp"):
                await test_integration(name)
            first = next_slot(prefs, datetime.now(timezone.utc))
            store.set("schedule", {"enabled": True, "first_date": first.date().isoformat()})
        else:
            store.set("schedule", {"enabled": False})
        return store.get("schedule")

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    return app
