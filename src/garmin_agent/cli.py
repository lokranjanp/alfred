"""Command line entrypoints for the local dashboard, MFA and macOS service."""

import argparse
import asyncio
import getpass
import os
from pathlib import Path

from .storage import Secrets, Store, data_directory


def main():
    parser = argparse.ArgumentParser(description="Garmin Morning — daily wellness, at 10 AM")
    parser.add_argument("--data-dir", type=Path, help="Override local state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local dashboard service")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("auth", help="One-time Garmin login with terminal MFA")
    run = commands.add_parser("run", help="Generate today's report")
    run.add_argument("--dry-run", action="store_true", help="Save a preview without sending email")
    run.add_argument("--output", type=Path, help="Write the rendered preview/report HTML to a file")
    schedule = commands.add_parser("schedule", help="Manage the macOS LaunchAgent")
    schedule.add_argument("action", choices=["install", "status", "uninstall"])
    schedule.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.data_dir:
        os.environ["GARMIN_AGENT_DATA_DIR"] = str(args.data_dir.expanduser().resolve())
    # Health measurements must not be sent to a tracing service by inherited defaults.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    store = Store(data_directory())
    try:
        if args.command == "serve":
            import uvicorn

            from .web import create_app

            print(f"Dashboard: http://127.0.0.1:{args.port}")
            uvicorn.run(create_app(store), host="127.0.0.1", port=args.port, workers=1)
        elif args.command == "auth":
            import garth

            session = Path(store.preferences().mcp.session_path).expanduser()
            session.mkdir(parents=True, exist_ok=True, mode=0o700)
            session.chmod(0o700)
            garth.login(input("Garmin email: ").strip(), getpass.getpass("Garmin password: "))
            garth.save(str(session))
            for file in session.iterdir():
                if file.is_file():
                    file.chmod(0o600)
            print("Session saved. Test Garmin in the dashboard's Connections tab.")
        elif args.command == "run":
            from .engine import Engine

            row = asyncio.run(Engine(store, Secrets()).run(preview=args.dry_run))
            if args.output and row.get("html"):
                args.output.write_text(row["html"], encoding="utf-8")
            print(f"{row['status']}: {row['id']}")
            if row.get("error"):
                print(row["error"])
            if row["status"] in ("failed", "uncertain"):
                parser.exit(1)
        elif args.command == "schedule":
            from .launchd import manage

            print(manage(args.action, store, args.port))
    except KeyboardInterrupt:
        parser.exit(130, "Stopped.\n")
    except Exception:
        parser.exit(
            1,
            "Operation failed. Check dashboard run status, connection settings, "
            "and file/Keychain permissions.\n",
        )
