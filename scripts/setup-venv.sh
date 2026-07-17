#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APP_SUPPORT_DIR="$(videolink_app_support_dir)"
VENV_DIR="${APP_SUPPORT_DIR}/venv"

if ! PYTHON="$(videolink_pick_python)"; then
  echo "error: no Python 3.10+ interpreter found on PATH." >&2
  echo "Install Python 3.12 (or 3.10+), then rerun scripts/setup-venv.sh." >&2
  exit 1
fi

mkdir -p "${APP_SUPPORT_DIR}"

echo "Creating Python virtual environment with $("${PYTHON}" --version 2>&1):"
echo "  ${VENV_DIR}"
"${PYTHON}" -m venv "${VENV_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip setuptools wheel
python -m pip install fastapi "uvicorn[standard]" websockets httpx pydantic

echo
echo "Core subtitle service dependencies installed."
echo "No ASR models, ffmpeg, ffprobe, Python, Homebrew, or Docker were installed."
echo "Install a live backend explicitly (default backend is chosen per platform):"
echo "  scripts/install-live-backend.sh          # mlx on Apple Silicon, faster-whisper elsewhere"
