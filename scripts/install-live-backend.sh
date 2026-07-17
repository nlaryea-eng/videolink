#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APP_SUPPORT_DIR="$(videolink_app_support_dir)"
VENV_DIR="${APP_SUPPORT_DIR}/venv"
PY="${VENV_DIR}/bin/python"

usage() {
  cat <<USAGE
Usage:
  scripts/install-live-backend.sh [--backend mlx|whispercpp|faster-whisper]

Installs only the requested live ASR backend into:
  $(videolink_app_support_dir)/venv

Default: mlx on Apple Silicon; faster-whisper elsewhere.
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

if [[ -z "${backend}" ]]; then
  if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
    backend="mlx"
  else
    backend="faster-whisper"
  fi
fi

if [[ ! -x "${PY}" ]]; then
  echo "error: app virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/setup-venv.sh first." >&2
  exit 1
fi

case "${backend}" in
  mlx)
    package=(mlx-whisper)
    import_name="mlx_whisper"
    label="MLX Whisper"
    ;;
  whispercpp|whisper-cpp)
    package=(pywhispercpp)
    import_name="pywhispercpp"
    label="whisper.cpp Python binding"
    ;;
  faster-whisper|faster_whisper)
    package=(faster-whisper)
    import_name="faster_whisper"
    label="faster-whisper CPU fallback"
    ;;
  *)
    echo "error: unsupported backend '${backend}'. Use mlx, whispercpp, or faster-whisper." >&2
    exit 2
    ;;
esac

echo "Installing ${label} into ${VENV_DIR}"
"${PY}" -m pip install "${package[@]}"

if "${PY}" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('${import_name}') else 1)"; then
  echo "${label} import check passed."
else
  echo "error: ${label} installed but import check failed (${import_name})." >&2
  exit 1
fi

if [[ "${backend}" == "whispercpp" || "${backend}" == "whisper-cpp" ]]; then
  echo
  echo "For an external whisper.cpp Metal build, set:"
  echo "  WHISPER_CPP_COMMAND=/path/to/whisper-cli"
  echo "  WHISPER_CPP_MODEL=/path/to/ggml-small.bin"
  echo "  WHISPER_CPP_METAL=1"
fi
