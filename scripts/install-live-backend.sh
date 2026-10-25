#!/usr/bin/env bash
# Compatibility entry point; dependencies are declared in pyproject.toml.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install.sh" "$@"
