"""Local durable storage. One process-wide flock protects report side effects."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import Food, Preferences


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def data_directory():
    return Path(
        os.environ.get(
            "GARMIN_AGENT_DATA_DIR", Path.home() / "Library/Application Support/GarminMorning"
        )
    ).expanduser()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.db = root / "app.sqlite"
        self.checkpoints = root / "checkpoints.sqlite"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS foods (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, day TEXT NOT NULL, preview INTEGER NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              report TEXT, html TEXT, text TEXT, error TEXT);
            """)
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def preferences(self):
        return Preferences.model_validate(self.get("preferences", {}))

    def foods(self):
        with self.connect() as db:
            return [
                Food.model_validate_json(r[0])
                for r in db.execute("SELECT value FROM foods ORDER BY id").fetchall()
            ]

    def save_food(self, food):
        food = food.model_copy(update={"id": food.id or str(uuid4())})
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO foods VALUES (?,?)", (food.id, food.model_dump_json())
            )
        return food

    def delete_food(self, food_id):
        with self.connect() as db:
            return db.execute("DELETE FROM foods WHERE id=?", (food_id,)).rowcount

    def start_run(self, run_id, day, preview):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                (run_id, day, preview, "running", now_iso(), now_iso()),
            )

    def update_run(self, run_id, **fields):
        allowed = {"status", "report", "html", "text", "error"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown run field")
        fields["updated_at"] = now_iso()
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                (*fields.values(), run_id),
            )

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def history(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,day,preview,status,updated_at,error,html IS NOT NULL AS has_report FROM runs "
                    "ORDER BY created_at DESC LIMIT 60"
                ).fetchall()
            ]

    @contextmanager
    def report_lock(self):
        import fcntl

        with (self.root / "report.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "A report is already running. Check its status shortly."
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Secrets:
    """Use native Keychain; environment variables are an explicit deployment override."""

    names = {"gemini": "GEMINI_API_KEY", "smtp": "SMTP_PASSWORD"}

    def get(self, name):
        if name not in self.names:
            raise ValueError("Unknown secret")
        if value := os.environ.get(self.names[name]):
            return value
        import keyring

        return keyring.get_password("GarminMorning", name)

    def set(self, name, value):
        if name not in self.names or not value or len(value) > 10000:
            raise ValueError("Invalid secret")
        import keyring

        keyring.set_password("GarminMorning", name, value)
