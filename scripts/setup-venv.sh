#!/usr/bin/env bash
# Compatibility entry point: core dependencies now share the repo environment.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install.sh" --no-backend "$@"
