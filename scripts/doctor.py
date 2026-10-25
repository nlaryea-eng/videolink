"""Read-only diagnostics for the environment used by Apple Shortcuts."""
from __future__ import annotations

import importlib
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
failures = []


def report(label, value):
    print(f"  {label:22} {value}")


def missing(label, message):
    failures.append(label)
    report(label, f"MISSING — {message}")


print("VideoLink · Mac shortcut and local subtitles")
report("Python", f"{platform.python_version()} · {sys.executable}")
if sys.version_info[:2] != (3, 12):
    missing("Python version", "use Python 3.12")
for module, package in [("yt_dlp", "yt-dlp"), ("fastapi", "fastapi"),
                        ("uvicorn", "uvicorn"), ("pydantic", "pydantic")]:
    try:
        importlib.import_module(module)
        report(package, importlib.metadata.version(package))
    except Exception as error:
        missing(package, str(error))
for tool in ("ffmpeg", "ffprobe"):
    found = shutil.which(tool)
    if found:
        report(tool, found)
    else:
        missing(tool, "install ffmpeg and ensure it is on PATH")
if platform.system() == "Darwin":
    player = (shutil.which("iina") or shutil.which("iina-cli")
              or ("/Applications/IINA.app/Contents/MacOS/iina-cli"
                  if Path("/Applications/IINA.app/Contents/MacOS/iina-cli").is_file() else None))
else:
    player = shutil.which("mpv")
if player:
    report("Player", player)
else:
    missing("Player", "install IINA on macOS or mpv on Linux")

marker = ROOT / ".venv" / "videolink-backend"
backend = marker.read_text().strip() if marker.exists() else "unconfigured"
report("Installed profile", backend)
if backend != "none":
    try:
        import subtitle_service
        # Use the service's own selection and environment overrides.
        _, info, _ = subtitle_service.select_live_backend()
        status = asdict(info)
        if not info.available:
            missing("Subtitle backend", "check the installed backend import and GPU access")
        report("Subtitle backend", json.dumps(status))
        modules = {"mlx-whisper": "mlx_whisper", "faster-whisper": "faster_whisper", "whisper.cpp": "pywhispercpp"}
        module = modules.get(info.name)
        if module and not (info.name == "whisper.cpp" and os.environ.get("WHISPER_CPP_COMMAND")):
            importlib.import_module(module)
    except Exception as error:
        missing("Subtitle backend", str(error))

port = os.environ.get("VIDEOLINK_SUBTITLE_PORT", "8765")
try:
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
        health = json.load(response)
    report("Service", f"running on port {port}")
    report("Service health", json.dumps(health))
except Exception:
    report("Service", "not running (start scripts/run-subtitle-service.sh)")
report("Shortcut", "Control–Option–Command–V · existing command preserved")
if failures:
    print("Fix the missing items above, then rerun scripts/doctor-subtitles.sh.")
    raise SystemExit(1)
print("Checks passed.")
