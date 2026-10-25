#!/usr/bin/env bash
# Locked setup; keeps the Apple Shortcuts .venv/bin/python entry point stable.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"
cd "$(videolink_repo_dir)"
UV="$(videolink_uv)"
backend=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) shift ;;
    --no-backend) backend=none; shift ;;
    --backend) backend="${2:?--backend needs a name}"; shift 2 ;;
    --backend=*) backend="${1#*=}"; shift ;;
    --python) export UV_PYTHON="${2:?--python needs a path}"; shift 2 ;;
    -h|--help)
      echo "Usage: scripts/install.sh [--no-backend | --backend mlx|faster-whisper|whispercpp] [--python PATH]"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "${backend}" && -f .venv/videolink-backend ]]; then
  backend="$(cat .venv/videolink-backend)"
fi
if [[ -z "${backend}" ]]; then
  if [[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]; then
    backend=mlx
  else
    backend=faster-whisper
  fi
fi
case "${backend}" in
  none|mlx|faster-whisper|whispercpp) ;;
  *) echo "Unsupported backend: ${backend}" >&2; exit 2 ;;
esac
args=(sync --locked --inexact)
[[ "${backend}" == none ]] || args+=(--extra "${backend}")
"${UV}" "${args[@]}"
printf '%s\n' "${backend}" > .venv/videolink-backend
"${SCRIPT_DIR}/doctor-subtitles.sh"
echo "Ready. Your Control–Option–Command–V shortcut command stays the same."
