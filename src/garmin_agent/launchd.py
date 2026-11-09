"""A per-user macOS service; no root daemon or login-shell environment assumptions."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.garminmorning.agent"


def plist(executable, data_dir, port=8765):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "-m", "garmin_agent", "serve", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "WorkingDirectory": str(data_dir),
        "EnvironmentVariables": {
            "GARMIN_AGENT_DATA_DIR": str(data_dir),
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        },
        "StandardOutPath": str(data_dir / "service.log"),
        "StandardErrorPath": str(data_dir / "service-error.log"),
    }


def manage(action, store, port=8765):
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent management is supported on macOS only.")
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if action == "install":
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
        destination.write_bytes(plistlib.dumps(plist(Path(sys.executable), store.root, port)))
        destination.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
        return f"Installed {destination}. Scheduling is controlled in the dashboard."
    if action == "uninstall":
        subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
        destination.unlink(missing_ok=True)
        return "LaunchAgent removed. Preferences and history retained."
    result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    return "LaunchAgent is loaded." if result.returncode == 0 else "LaunchAgent is not loaded."
