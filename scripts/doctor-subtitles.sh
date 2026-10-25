#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"
PY="$(videolink_repo_dir)/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "Missing .venv. Run scripts/install.sh." >&2
  exit 1
fi
exec "${PY}" "${SCRIPT_DIR}/doctor.py"
