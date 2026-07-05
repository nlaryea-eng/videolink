#!/usr/bin/env bash
set -euo pipefail

APP_SUPPORT_DIR="${HOME}/Library/Application Support/VideoLink"
VENV_DIR="${APP_SUPPORT_DIR}/venv"

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "error: python3.12 is not available on PATH." >&2
  echo "Install Python 3.12, then rerun scripts/setup-venv.sh." >&2
  exit 1
fi

mkdir -p "${APP_SUPPORT_DIR}"

echo "Creating Python 3.12 virtual environment:"
echo "  ${VENV_DIR}"
python3.12 -m venv "${VENV_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip setuptools wheel
python -m pip install fastapi "uvicorn[standard]" websockets httpx pydantic

echo
echo "Core subtitle service dependencies installed."
echo "No ASR models, ffmpeg, ffprobe, Python, Homebrew, or Docker were installed."
echo "Install a live backend explicitly, for example:"
echo "  scripts/install-live-backend.sh --backend mlx"
