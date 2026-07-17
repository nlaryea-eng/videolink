#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APP_SUPPORT_DIR="$(videolink_app_support_dir)"
VENV_DIR="${APP_SUPPORT_DIR}/venv"
PY="${VENV_DIR}/bin/python"

usage() {
  cat <<'USAGE'
Usage:
  scripts/install-refinement-backend.sh --backend whisperx

WhisperX is optional subtitle refinement/alignment support. It is not required
for basic live subtitles.
USAGE
}

backend=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)
      [[ $# -ge 2 && -n "$2" ]] || { echo "error: --backend requires a name." >&2; exit 2; }
      backend="$2"
      shift 2
      ;;
    --backend=*)
      backend="${1#*=}"
      [[ -n "${backend}" ]] || { echo "error: --backend requires a name." >&2; exit 2; }
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${backend}" != "whisperx" ]]; then
  echo "error: refinement backend must be whisperx." >&2
  usage >&2
  exit 2
fi

if [[ ! -x "${PY}" ]]; then
  echo "error: app virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/setup-venv.sh first." >&2
  exit 1
fi

echo "Installing optional WhisperX refinement/alignment support into ${VENV_DIR}"
"${PY}" -m pip install whisperx

if "${PY}" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('whisperx') else 1)"; then
  echo "WhisperX import check passed."
  echo "Live subtitles still use the live backend policy; WhisperX is for refinement."
else
  echo "error: WhisperX installed but import check failed." >&2
  exit 1
fi
