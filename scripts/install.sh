#!/usr/bin/env bash
# VideoLink environment-aware installer.
#
# Detects the current machine, tells you exactly what system packages are
# missing (it never runs sudo for you), then either creates a fresh repo-local
# virtualenv or reuses an interpreter you already have, and wires up the local
# subtitle service and a live ASR backend. Safe to re-run.
#
# Interactive by default. For automation:
#   scripts/install.sh --yes [--python PATH] [--backend NAME] [--no-backend]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ASSUME_YES=0
CHOSEN_PYTHON="${VIDEOLINK_PYTHON:-}"
CHOSEN_BACKEND=""
INSTALL_BACKEND=1

usage() {
  cat <<'USAGE'
Usage:
  scripts/install.sh [options]

Options:
  --yes                 Non-interactive; accept every recommended default.
  --python PATH         Reuse this interpreter for the repo-local venv instead
                        of creating one from the auto-detected Python.
  --backend NAME        Live ASR backend to install (mlx|whispercpp|faster-whisper).
                        Default: mlx on Apple Silicon, faster-whisper elsewhere.
  --no-backend          Skip ASR backend installation (set it up later).
  -h, --help            Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --python)
      [[ $# -ge 2 && -n "$2" ]] || { echo "error: --python requires a path." >&2; exit 2; }
      CHOSEN_PYTHON="$2"; shift 2 ;;
    --python=*)
      CHOSEN_PYTHON="${1#*=}"
      [[ -n "${CHOSEN_PYTHON}" ]] || { echo "error: --python requires a path." >&2; exit 2; }
      shift ;;
    --backend)
      [[ $# -ge 2 && -n "$2" ]] || { echo "error: --backend requires a name." >&2; exit 2; }
      CHOSEN_BACKEND="$2"; shift 2 ;;
    --backend=*)
      CHOSEN_BACKEND="${1#*=}"
      [[ -n "${CHOSEN_BACKEND}" ]] || { echo "error: --backend requires a name." >&2; exit 2; }
      shift ;;
    --no-backend) INSTALL_BACKEND=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "${CHOSEN_BACKEND}" ]]; then
  case "${CHOSEN_BACKEND}" in
    mlx|whispercpp|whisper-cpp|faster-whisper|faster_whisper) ;;
    *) echo "error: unsupported backend '${CHOSEN_BACKEND}'." >&2; exit 2 ;;
  esac
  if [[ "${INSTALL_BACKEND}" -eq 0 ]]; then
    echo "error: --backend and --no-backend cannot be used together." >&2
    exit 2
  fi
fi

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

# Ask a yes/no question. Honors --yes and non-tty stdin by taking the default.
confirm() {
  local prompt="$1" default="${2:-Y}" reply
  if [[ "${ASSUME_YES}" -eq 1 || ! -t 0 ]]; then
    [[ "${default}" == "Y" ]]
    return
  fi
  local hint="[Y/n]"; [[ "${default}" == "Y" ]] || hint="[y/N]"
  read -r -p "  ${prompt} ${hint} " reply || reply=""
  reply="${reply:-${default}}"
  [[ "${reply}" =~ ^[Yy] ]]
}

# Read a free-text answer, or the default when non-interactive.
ask() {
  local prompt="$1" default="${2:-}" reply
  if [[ "${ASSUME_YES}" -eq 1 || ! -t 0 ]]; then
    printf '%s\n' "${default}"; return
  fi
  read -r -p "  ${prompt} " reply || reply=""
  printf '%s\n' "${reply:-${default}}"
}

# Look for a binary outside PATH in the usual install locations. Prints the
# first hit, if any. This is the "search for existing tools" option.
search_binary() {
  local name="$1" dir candidate
  local dirs=(
    /usr/bin /usr/local/bin /bin /opt/homebrew/bin /usr/local/opt
    "${HOME}/.local/bin" "${HOME}/bin" /snap/bin /var/lib/flatpak/exports/bin
    /Applications/IINA.app/Contents/MacOS
  )
  for dir in "${dirs[@]}"; do
    for candidate in "${dir}/${name}" "${dir}/${name}-cli"; do
      [[ -x "${candidate}" ]] && { printf '%s\n' "${candidate}"; return 0; }
    done
  done
  return 1
}

detect_pkg_manager() {
  local mgr
  local managers=(apt-get apt dnf pacman zypper brew)
  [[ "$(uname -s)" == "Darwin" ]] && managers=(brew)
  for mgr in "${managers[@]}"; do
    command -v "${mgr}" >/dev/null 2>&1 && { printf '%s\n' "${mgr}"; return 0; }
  done
  return 1
}

# Print the install command for the detected package manager and given packages.
install_hint() {
  local mgr="$1"; shift
  local pkgs="$*"
  case "${mgr}" in
    brew)              printf 'brew install %s\n' "${pkgs}" ;;
    apt|apt-get)       printf 'sudo apt-get install -y %s\n' "${pkgs}" ;;
    dnf)               printf 'sudo dnf install -y %s\n' "${pkgs}" ;;
    pacman)            printf 'sudo pacman -S --needed %s\n' "${pkgs}" ;;
    zypper)            printf 'sudo zypper install -y %s\n' "${pkgs}" ;;
    *)                 printf '# install these with your package manager: %s\n' "${pkgs}" ;;
  esac
}

OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}" in
  Darwin|Linux) ;;
  *) echo "error: unsupported operating system '${OS}' (supported: macOS and Linux)." >&2; exit 1 ;;
esac
IS_MAC=0; [[ "${OS}" == "Darwin" ]] && IS_MAC=1
PKG_MGR="$(detect_pkg_manager || true)"

echo
bold "VideoLink installer"
info "OS:              ${OS} (${ARCH})"
info "Session:         ${XDG_SESSION_TYPE:-n/a}${DISPLAY:+, DISPLAY=${DISPLAY}}${WAYLAND_DISPLAY:+, WAYLAND=${WAYLAND_DISPLAY}}"
info "Package manager: ${PKG_MGR:-none detected}"
info "Data directory:  $(videolink_app_support_dir)"
echo

# --- 1. System dependencies -------------------------------------------------
bold "1. System dependencies"
MISSING_PKGS=()

# Player: mpv on Linux, IINA on macOS. IINA is an mpv GUI frontend, so the
# subtitle service's mpv IPC commands work against either.
PLAYER_PKG="mpv"; [[ "${IS_MAC}" -eq 1 ]] && PLAYER_PKG="--cask iina"
if [[ "${IS_MAC}" -eq 1 ]] && command -v iina >/dev/null 2>&1; then
  ok "Player: IINA ($(command -v iina))"
elif [[ "${IS_MAC}" -eq 1 ]] && command -v iina-cli >/dev/null 2>&1; then
  ok "Player: IINA ($(command -v iina-cli))"
elif [[ "${IS_MAC}" -eq 1 && -x /Applications/IINA.app/Contents/MacOS/iina-cli ]]; then
  ok "Player: IINA (/Applications/IINA.app)"
elif command -v mpv >/dev/null 2>&1; then
  ok "Player: mpv ($(command -v mpv))"
else
  warn "Player: not found (need mpv on Linux / IINA on macOS)"
  if found="$(search_binary mpv || search_binary iina || true)" && [[ -n "${found}" ]]; then
    info "Found a player outside PATH at: ${found}"
    info "Add its directory to PATH before running VideoLink."
  fi
  MISSING_PKGS+=("${PLAYER_PKG}")
fi

for tool in ffmpeg ffprobe; do
  if command -v "${tool}" >/dev/null 2>&1; then
    ok "${tool}: $(command -v "${tool}")"
  else
    warn "${tool}: not found"
    if found="$(search_binary "${tool}" || true)" && [[ -n "${found}" ]]; then
      info "Found ${tool} outside PATH at: ${found} (add its directory to PATH)."
    fi
    MISSING_PKGS+=("ffmpeg")   # the ffmpeg package provides both binaries
  fi
done

# Clipboard is optional: VideoLink copies the resolved link, but falls back to
# printing it when no clipboard tool is present.
clipboard_pkg=""
if command -v pbcopy >/dev/null 2>&1 || command -v wl-copy >/dev/null 2>&1 \
   || command -v xclip >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1; then
  ok "Clipboard: available"
elif [[ "${IS_MAC}" -eq 0 ]]; then
  warn "Clipboard: none found (optional). To copy resolved links to the clipboard,"
  if [[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    clipboard_pkg="wl-clipboard"
  else
    clipboard_pkg="xclip"
  fi
  info "install ${clipboard_pkg} (otherwise the link is printed to the terminal)."
fi

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  # De-duplicate (ffmpeg may be added twice).
  # Bash 3.2 (still the macOS system default) has no readarray/mapfile.
  UNIQUE_PKGS=()
  while IFS= read -r package; do
    [[ -n "${package}" ]] && UNIQUE_PKGS+=("${package}")
  done < <(printf '%s\n' "${MISSING_PKGS[@]}" "${clipboard_pkg}" | awk 'NF && !seen[$0]++')
  echo
  warn "Some system packages are missing. Install them, then re-run this script:"
  info "$(install_hint "${PKG_MGR:-none}" "${UNIQUE_PKGS[*]}")"
  info "(This installer never runs sudo for you.)"
  if ! confirm "Continue with Python setup anyway?" "Y"; then
    echo "Stopping so you can install system packages first."; exit 0
  fi
fi
echo

# --- 2. Python environment --------------------------------------------------
bold "2. Python environment (repo-local .venv for the CLI, tests, yt-dlp)"
VENV_DIR="${REPO_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"

resolve_python() {
  # Priority: explicit --python, then auto-detected, then interactive search.
  if [[ -n "${CHOSEN_PYTHON}" ]]; then
    videolink_python_usable "${CHOSEN_PYTHON}" \
      && { command -v "${CHOSEN_PYTHON}"; return 0; }
    echo "error: --python '${CHOSEN_PYTHON}' is not an executable Python 3.10+." >&2
    return 1
  fi
  local detected
  detected="$(videolink_pick_python || true)"
  if [[ -n "${detected}" && ( "${ASSUME_YES}" -eq 1 || ! -t 0 ) ]]; then
    printf '%s\n' "${detected}"; return 0
  fi
  if [[ -n "${detected}" ]]; then
    info "Detected interpreter: ${detected} ($(${detected} --version 2>&1))" >&2
    if confirm "Use it to create the repo-local .venv?" "Y"; then
      printf '%s\n' "${detected}"; return 0
    fi
  else
    warn "No Python 3.10+ found on PATH." >&2
  fi
  # Reuse path: list every python we can find and let the user pick one.
  info "Searching for other Python interpreters..." >&2
  local names=(python3.13 python3.12 python3.11 python3.10 python3 python)
  local found=() n p
  for n in "${names[@]}"; do
    p="$(command -v "${n}" 2>/dev/null || true)"
    [[ -n "${p}" ]] && found+=("${p} ($(${p} --version 2>&1))")
  done
  if [[ ${#found[@]} -gt 0 ]]; then
    info "Available: ${found[*]}" >&2
  fi
  local answer
  answer="$(ask "Enter a python interpreter path (or blank to abort):" "")"
  [[ -n "${answer}" ]] && videolink_python_usable "${answer}" \
    && { command -v "${answer}"; return 0; }
  return 1
}

if [[ -x "${VENV_PY}" ]] && videolink_python_usable "${VENV_PY}" \
  && confirm "Repo-local .venv already exists. Reuse it?" "Y"; then
  ok "Reusing ${VENV_DIR}"
else
  if ! PYTHON="$(resolve_python)"; then
    echo "error: could not resolve a Python 3.10+ interpreter." >&2
    echo "Install Python 3.12 (or 3.10+) and re-run, or pass --python PATH." >&2
    exit 1
  fi
  info "Creating ${VENV_DIR} with $("${PYTHON}" --version 2>&1)"
  "${PYTHON}" -m venv "${VENV_DIR}"
fi

info "Installing CLI dependencies (yt-dlp) into .venv"
"${VENV_PY}" -m pip install --upgrade pip >/dev/null
"${VENV_PY}" -m pip install -r "${REPO_DIR}/requirements.txt"
ok "Repo-local environment ready."
echo

# --- 3. Subtitle service environment + backend ------------------------------
bold "3. Local subtitle service"
if confirm "Set up the subtitle-service venv and a live ASR backend?" "Y"; then
  "${SCRIPT_DIR}/setup-venv.sh"
  if [[ "${INSTALL_BACKEND}" -eq 1 ]]; then
    backend_args=()
    [[ -n "${CHOSEN_BACKEND}" ]] && backend_args=(--backend "${CHOSEN_BACKEND}")
    echo
    "${SCRIPT_DIR}/install-live-backend.sh" "${backend_args[@]}"
  else
    info "Skipping ASR backend (--no-backend). Install later with:"
    info "  scripts/install-live-backend.sh"
  fi
else
  info "Skipped. The CLI still works for open-in-player without subtitles."
fi
echo

# --- 4. Diagnostics ---------------------------------------------------------
bold "4. Diagnostics"
"${SCRIPT_DIR}/doctor-subtitles.sh" || true
echo
bold "Done."
info "Resolve + open a video:      ./.venv/bin/python videolink.py <page-or-media-url>"
info "Start the subtitle service:  scripts/run-subtitle-service.sh"
info "Live subtitles from the CLI: ./.venv/bin/python videolink.py --subtitle-auto <url>"
