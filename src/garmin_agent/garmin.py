"""MCP-only Garmin collection. No raw Connect API access or model-controlled tools."""

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from .models import Preferences

REQUIRED_TOOLS = {
    "garmin_resume_session",
    "garmin_user_profile",
    "garmin_user_settings",
    "garmin_daily_summary",
    "garmin_data",
    "garmin_metric",
    "garmin_activities",
    "garmin_activity_details",
}


class GarminError(RuntimeError):
    pass


def decode_result(result):
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise GarminError("Garmin tool returned an error; check the session and connection.")
    data = getattr(result, "data", None)
    if data is None:
        content = getattr(result, "content", [])
        data = next((c.text for c in content if getattr(c, "type", None) == "text"), None)
    for _ in range(3):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                raise GarminError("Garmin returned invalid JSON.") from exc
    if isinstance(data, dict) and "error" in data:
        raise GarminError("Garmin data request failed; check authentication and device sync.")
    return data


class GarminClient:
    def __init__(self, preferences: Preferences):
        self.settings = preferences.mcp
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        transport = StdioTransport(
            command=self.settings.command,
            args=self.settings.args,
            env={"GARTH_SESSION_PATH": self.settings.session_path},
            keep_alive=False,
        )
        try:
            self.client = await self.stack.enter_async_context(Client(transport, timeout=40))
            available = {tool.name for tool in await self.client.list_tools()}
            if missing := REQUIRED_TOOLS - available:
                raise GarminError("MCP is missing tools: " + ", ".join(sorted(missing)))
            await self.call("garmin_resume_session", session_path=self.settings.session_path)
            return self
        except Exception as exc:
            await self.stack.aclose()
            if isinstance(exc, GarminError):
                raise
            raise GarminError(
                "Cannot connect to Garmin MCP. Check its executable and saved session; "
                "run garmin-agent auth if the session has expired."
            ) from exc

    async def __aexit__(self, *args):
        await self.stack.aclose()

    async def call(self, tool, **kwargs):
        for attempt in range(3):
            try:
                return decode_result(await self.client.call_tool(tool, kwargs))
            except Exception as exc:
                if attempt == 2:
                    raise GarminError(
                        f"{tool} unavailable. Check Garmin session and sync."
                    ) from exc
                await asyncio.sleep(2**attempt)


def activity_date(activity, tz):
    """Prefer an actual UTC instant over the device's naive local clock."""
    try:
        if timestamp := activity.get("startTimeGMT") or activity.get("start_time_gmt"):
            instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return instant.astimezone(ZoneInfo(tz)).date()
        return date.fromisoformat(
            (activity.get("startTimeLocal") or activity.get("start_time_local"))[:10]
        )
    except (TypeError, ValueError):
        return None


async def collect(preferences: Preferences, today: date, client_factory=GarminClient):
    yesterday = today - timedelta(days=1)
    result = {
        "summaries": [],
        "sleep": [],
        "hrv": [],
        "weight": [],
        "activities": [],
        "series": {},
        "warnings": [],
    }
    try:
        async with client_factory(preferences) as client:

            async def optional(label, tool, **args):
                try:
                    return await client.call(tool, **args)
                except Exception:
                    result["warnings"].append(f"{label} unavailable; check Garmin sync/session.")
                    return None

            for offset in range(8):
                day = yesterday - timedelta(days=offset)
                data = await optional(
                    f"Wellness {day}", "garmin_daily_summary", day=day.isoformat()
                )
                result["summaries"].append({"date": day.isoformat(), "data": data})
            result["sleep"] = (
                await optional(
                    "Sleep", "garmin_data", data_type="sleep", end_date=today.isoformat(), days=8
                )
                or []
            )
            result["hrv"] = (
                await optional(
                    "HRV", "garmin_metric", metric="hrv", end_date=today.isoformat(), count=8
                )
                or []
            )
            result["weight"] = (
                await optional(
                    "Weight",
                    "garmin_data",
                    data_type="weight",
                    end_date=yesterday.isoformat(),
                    days=30,
                )
                or []
            )
            for metric in ("steps", "hydration", "intensity_minutes"):
                result["series"][metric] = (
                    await optional(
                        metric,
                        "garmin_metric",
                        metric=metric,
                        end_date=yesterday.isoformat(),
                        count=8,
                    )
                    or []
                )
            seen = set()
            for start in range(0, 1000, 50):
                page = await optional("Activities", "garmin_activities", start=start, limit=50)
                if not isinstance(page, list):
                    if page is not None:
                        result["warnings"].append("Activity response was incomplete.")
                    break
                new_ids = set()
                for item in page:
                    if not isinstance(item, dict):
                        continue
                    activity_id = item.get("activityId") or item.get("activity_id")
                    if activity_id is None or str(activity_id) in seen:
                        continue
                    new_ids.add(str(activity_id))
                    if activity_date(item, preferences.timezone) == yesterday:
                        details = await optional(
                            "Activity details", "garmin_activity_details", activity_id=activity_id
                        )
                        merged = dict(item)
                        if isinstance(details, dict) and "message" not in details:
                            merged.update(details.get("summaryDTO", {}))
                        result["activities"].append(merged)
                seen.update(new_ids)
                dates = [
                    activity_date(a, preferences.timezone) for a in page if isinstance(a, dict)
                ]
                # Garmin returns newest first. Finish after crossing the target day.
                if len(page) < 50 or (dates and all(d and d < yesterday for d in dates)):
                    break
                if not new_ids:
                    result["warnings"].append(
                        "Activity pagination repeated; list may be incomplete."
                    )
                    break
            else:
                result["warnings"].append(
                    "Activity pagination limit reached; list may be incomplete."
                )
    except Exception:
        result["warnings"].append(
            "Garmin connection unavailable. Check the MCP path and run "
            "garmin-agent auth to restore an expired session."
        )
    return result
