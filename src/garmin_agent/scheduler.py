"""Timezone-aware 10 AM scheduling, including same-day catch-up."""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def next_slot(preferences, now):
    local = now.astimezone(ZoneInfo(preferences.timezone))
    target = datetime.combine(local.date(), time(10), local.tzinfo)
    return target if local < target else target + timedelta(days=1)


def due(store, now):
    schedule = store.get("schedule", {})
    if not schedule.get("enabled"):
        return False
    prefs = store.preferences()
    local = now.astimezone(ZoneInfo(prefs.timezone))
    first = schedule.get("first_date", "9999-12-31")
    if local.date().isoformat() < first or local.hour < 10:
        return False
    row = store.run(f"daily-{local.date().isoformat()}")
    if row and row["status"] in ("sent", "uncertain"):
        return False
    if row and row["status"] == "failed":
        # Avoid continually retrying rejected credentials or rate-limited services.
        elapsed = now - datetime.fromisoformat(row["updated_at"])
        return elapsed.total_seconds() >= 1800
    return True


async def scheduler_loop(store, engine, stop):
    while not stop.is_set():
        try:
            if due(store, datetime.now(timezone.utc)):
                await engine.run()
        except Exception:
            logger.warning("Daily report incomplete. See dashboard run status for details.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass
