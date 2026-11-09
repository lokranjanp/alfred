from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from garmin_agent.launchd import plist
from garmin_agent.models import Preferences
from garmin_agent.scheduler import due, next_slot
from garmin_agent.web import create_app


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_schedule_local_time_catchup_and_no_backlog(store):
    store.set("schedule", {"enabled": True, "first_date": "2026-11-09"})
    assert not due(store, utc("2026-11-09T04:29:59"))  # 09:59:59 India
    assert due(store, utc("2026-11-09T04:30:00"))
    assert due(store, utc("2026-11-09T17:00:00"))  # same-day wake
    store.start_run("daily-2026-11-09", "2026-11-09", False)
    store.update_run("daily-2026-11-09", status="sent")
    assert not due(store, utc("2026-11-09T17:00:00"))
    assert not due(store, utc("2026-11-09T19:00:00"))  # next date, before 10 AM
    assert due(store, utc("2026-11-10T04:30:00"))
    store.set("schedule", {"enabled": False})
    assert not due(store, utc("2026-11-10T04:30:00"))


def test_new_schedule_and_dst():
    prefs = Preferences(timezone="America/New_York")
    before = next_slot(prefs, utc("2026-10-31T15:00:00"))
    assert before.isoformat() == "2026-11-01T10:00:00-05:00"
    after = next_slot(prefs, utc("2026-03-07T16:00:00"))
    assert after.isoformat() == "2026-03-08T10:00:00-04:00"


def test_failed_run_cooldown_and_uncertain_block(store):
    now = datetime.now(timezone.utc)
    local = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    afternoon = local.replace(hour=15, minute=0).astimezone(timezone.utc)
    store.set("schedule", {"enabled": True, "first_date": "2000-01-01"})
    run_id = f"daily-{local.date()}"
    store.start_run(run_id, str(local.date()), False)
    store.update_run(run_id, status="failed")
    with store.connect() as db:
        db.execute("UPDATE runs SET updated_at=? WHERE id=?", (afternoon.isoformat(), run_id))
    assert not due(store, afternoon + timedelta(minutes=10))
    assert due(store, afternoon + timedelta(minutes=31))
    store.update_run(run_id, status="uncertain")
    assert not due(store, afternoon + timedelta(hours=1))


def client_for(store, secrets):
    client = TestClient(create_app(store, secrets, start_scheduler=False))
    page = client.get("/")
    assert page.status_code == 200
    client.headers.update({"x-csrf-token": client.cookies["csrf"]})
    return client


def test_dashboard_api_and_csrf(store, secrets):
    client = client_for(store, secrets)
    assert "Your morning, made clearer" in client.get("/").text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/api/health").json()["ok"]
    body = client.get("/api/preferences").json()
    assert client.put("/api/preferences", json=body).status_code == 200
    assert (
        client.put("/api/preferences", json=body, headers={"x-csrf-token": "wrong"}).status_code
        == 403
    )
    assert (
        client.put(
            "/api/preferences", json=body, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]
    assert client.get("/reports/missing").status_code == 404


def test_food_crud_and_secret_never_returned(store, secrets):
    client = client_for(store, secrets)
    response = client.post("/api/foods", json={"name": "Rice", "serving": "bowl", "calories": 200})
    assert response.status_code == 201
    food = response.json()
    food["calories"] = 220
    assert client.put("/api/foods/" + food["id"], json=food).json()["calories"] == 220
    assert client.delete("/api/foods/" + food["id"]).status_code == 200
    assert client.delete("/api/foods/missing").status_code == 404
    assert client.put("/api/secrets/gemini", json={"value": "TEST-SECRET"}).status_code == 200
    assert secrets.get("gemini") == "TEST-SECRET"
    assert "TEST-SECRET" not in client.get("/api/status").text
    assert "TEST-SECRET" not in client.get("/api/preferences").text
    assert client.put("/api/secrets/unknown", json={"value": "x"}).status_code == 404


def test_connection_edit_disables_schedule_but_goal_edit_keeps_it(store, secrets):
    client = client_for(store, secrets)
    store.set("schedule", {"enabled": True, "first_date": "2000-01-01"})
    prefs = client.get("/api/preferences").json()
    prefs["goals"] = "Consistency"
    assert not client.put("/api/preferences", json=prefs).json()["schedule_disabled"]
    assert store.get("schedule")["enabled"]
    prefs["smtp"]["host"] = "new.example.com"
    assert client.put("/api/preferences", json=prefs).json()["schedule_disabled"]
    assert not store.get("schedule")["enabled"]


def test_schedule_requires_working_connections(store, secrets, monkeypatch):
    client = client_for(store, secrets)

    class BrokenClient:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            raise RuntimeError("SECRET AUTH ERROR")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("garmin_agent.web.GarminClient", BrokenClient)
    response = client.post("/api/schedule", json={"enabled": True})
    assert response.status_code == 400
    assert "SECRET" not in response.text
    assert not store.get("schedule", {}).get("enabled")
    assert client.post("/api/schedule", json={"enabled": False}).status_code == 200


def test_launchagent_uses_absolute_paths_and_private_tracing_defaults(tmp_path):
    value = plist(tmp_path / "python", tmp_path)
    assert value["KeepAlive"]
    assert value["ProgramArguments"][0].startswith("/")
    assert value["ProgramArguments"][-1] == "8765"
    assert value["EnvironmentVariables"]["LANGSMITH_TRACING"] == "false"


def test_enable_schedule_after_all_connections_pass(store, secrets, monkeypatch):
    import garmin_agent.web as web

    class Client:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, *args):
            return {"ok": True}

    class Mailer:
        def __init__(self, *args):
            pass

        def test(self):
            pass

    async def gemini(*args):
        pass

    monkeypatch.setattr(web, "GarminClient", Client)
    monkeypatch.setattr(web, "Mailer", Mailer)
    monkeypatch.setattr(web.ai, "test_connection", gemini)
    client = client_for(store, secrets)
    response = client.post("/api/schedule", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["enabled"]
    assert len(store.get("integration_tests")) == 3
    assert client.get("/api/status").json()["next_run"]
