"""Offline STDIO integration fixture. No Garmin credentials or network calls."""

import json

from fastmcp import FastMCP

mcp = FastMCP("Offline Garmin")


@mcp.tool()
def garmin_resume_session(session_path: str = "") -> str:
    return json.dumps({"status": "ok"})


@mcp.tool()
def garmin_user_profile() -> str:
    return json.dumps({"display_name": "Offline User"})


@mcp.tool()
def garmin_user_settings() -> str:
    return json.dumps({"units": "metric"})


@mcp.tool()
def garmin_daily_summary(day: str) -> str:
    return json.dumps({"calendarDate": day, "restingHeartRate": 52, "totalSteps": 8000})


@mcp.tool()
def garmin_data(
    data_type: str, day: str | None = None, end_date: str | None = None, days: int = 7
) -> str:
    return "[]"


@mcp.tool()
def garmin_metric(
    metric: str, period: str = "daily", end_date: str | None = None, count: int = 7
) -> str:
    return "[]"


@mcp.tool()
def garmin_activities(start: int = 0, limit: int = 20) -> str:
    return (
        json.dumps(
            [
                {
                    "activityId": 42,
                    "activityName": "Walk",
                    "startTimeGMT": "2026-11-08 12:00:00",
                    "duration": 1800,
                }
            ]
        )
        if start == 0
        else "[]"
    )


@mcp.tool()
def garmin_activity_details(activity_id: str | int) -> str:
    return json.dumps({"summaryDTO": {"distance": 3000}})


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
