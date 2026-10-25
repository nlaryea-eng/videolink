#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_SUPPORT_DIR="$(videolink_app_support_dir)"
VENV_DIR="$(videolink_repo_dir)/.venv"
PY="${VENV_DIR}/bin/python"
PORT="${VIDEOLINK_SUBTITLE_PORT:-8765}"

if [[ ! -x "${PY}" ]]; then
  echo "error: app virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/setup-venv.sh first." >&2
  exit 1
fi

cd "${REPO_DIR}"

echo "Starting VideoLink subtitle service on http://127.0.0.1:${PORT}"
echo "Backend selection: MLX Whisper -> whisper.cpp -> faster-whisper CPU"
exec "${PY}" -m uvicorn subtitle_service:app --host 127.0.0.1 --port "${PORT}"
