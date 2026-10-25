#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"
cd "$(videolink_repo_dir)"
UV="$(videolink_uv)"
"${UV}" lock --upgrade
"${UV}" export --locked --no-header --no-dev --no-hashes --no-emit-project --output-file requirements.txt >/dev/null
"${SCRIPT_DIR}/install.sh" "$@"
.venv/bin/python -m unittest test_videolink_core test_tooling
