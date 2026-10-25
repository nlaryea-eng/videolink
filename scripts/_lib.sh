#!/usr/bin/env bash
# Shared helpers for VideoLink scripts. Source this; do not execute it.
#
# Keeps the macOS behavior identical while letting the same scripts run on
# Linux: the app-support directory and the Python interpreter are resolved per
# platform instead of hardcoded.

# Per-user data directory. Mirrors resolver_core.app_support_dir() so the shell
# scripts and the Python service agree on one location.
#   - VIDEOLINK_APP_SUPPORT_DIR override wins everywhere.
#   - macOS: ~/Library/Application Support/VideoLink (unchanged).
#   - else: $XDG_DATA_HOME/VideoLink or ~/.local/share/VideoLink.
videolink_app_support_dir() {
  if [[ -n "${VIDEOLINK_APP_SUPPORT_DIR:-}" ]]; then
    case "${VIDEOLINK_APP_SUPPORT_DIR}" in
      "~") printf '%s\n' "${HOME}" ;;
      "~/"*) printf '%s/%s\n' "${HOME}" "${VIDEOLINK_APP_SUPPORT_DIR#\~/}" ;;
      *) printf '%s\n' "${VIDEOLINK_APP_SUPPORT_DIR}" ;;
    esac
    return
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf '%s\n' "${HOME}/Library/Application Support/VideoLink"
    return
  fi
  case "${XDG_DATA_HOME:-}" in
    /*) printf '%s/VideoLink\n' "${XDG_DATA_HOME}" ;;
    *) printf '%s\n' "${HOME}/.local/share/VideoLink" ;;
  esac
}

videolink_python_usable() {
  local candidate="${1:-}"
  [[ -n "${candidate}" ]] || return 1
  command -v "${candidate}" >/dev/null 2>&1 || return 1
  "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

# Echo a usable Python interpreter (>= 3.10). Prefers python3.12 to match the
# macOS recommendation, then any python3 that meets the floor. Returns non-zero
# with nothing printed when none qualifies.
videolink_pick_python() {
  if [[ -n "${VIDEOLINK_PYTHON:-}" ]]; then
    if videolink_python_usable "${VIDEOLINK_PYTHON}"; then
      command -v "${VIDEOLINK_PYTHON}"
      return 0
    fi
    return 1
  fi
  local candidate
  for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
    if videolink_python_usable "${candidate}"; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

# One environment shared by Shortcuts, the service, and regression tests.
videolink_repo_dir() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

videolink_uv() {
  local repo candidate
  repo="$(videolink_repo_dir)"
  for candidate in "${VIDEOLINK_UV:-}" "${repo}/.venv/bin/uv" "${HOME}/.local/bin/uv" /opt/homebrew/bin/uv; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done
  command -v uv 2>/dev/null || {
    echo "error: install uv, then rerun scripts/install.sh (https://docs.astral.sh/uv/)." >&2
    return 1
  }
}
