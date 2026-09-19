#!/usr/bin/env bash
# Install (or remove) an autostart entry so the VideoLink subtitle service runs
# at login and stays up.
#
#   macOS: a launchd LaunchAgent in ~/Library/LaunchAgents.
#   Linux: a systemd user service in ~/.config/systemd/user.
#
# Both wrap scripts/run-subtitle-service.sh, so the venv, port, and backend
# selection stay identical to a manual launch.
#
# Usage:
#   scripts/install-autostart.sh            # install and start now
#   scripts/install-autostart.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_SUPPORT_DIR="$(videolink_app_support_dir)"
VENV_DIR="${APP_SUPPORT_DIR}/venv"
RUNNER="${SCRIPT_DIR}/run-subtitle-service.sh"
LABEL="com.videolink.subtitle-service"
LOG_DIR="${APP_SUPPORT_DIR}/logs"

UNINSTALL=0
for arg in "$@"; do
  case "${arg}" in
    --uninstall|--remove) UNINSTALL=1 ;;
    -h|--help)
      sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

install_macos() {
  local plist="${HOME}/Library/LaunchAgents/${LABEL}.plist"
  local domain="gui/$(id -u)"

  if [[ "${UNINSTALL}" -eq 1 ]]; then
    launchctl bootout "${domain}/${LABEL}" 2>/dev/null || true
    rm -f "${plist}"
    echo "Removed LaunchAgent ${LABEL}."
    return
  fi

  mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"
  cat >"${plist}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${RUNNER}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>VIDEOLINK_SUBTITLE_PORT</key>
        <string>${VIDEOLINK_SUBTITLE_PORT:-8765}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/subtitle-service.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/subtitle-service.err.log</string>
</dict>
</plist>
PLIST

  # Reload cleanly so a re-run picks up any changes.
  launchctl bootout "${domain}/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "${domain}" "${plist}"
  launchctl enable "${domain}/${LABEL}" 2>/dev/null || true

  echo "Installed LaunchAgent: ${plist}"
  echo "The subtitle service now starts at login and restarts if it exits."
  echo "Logs: ${LOG_DIR}/subtitle-service.{out,err}.log"
}

install_linux() {
  local unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
  local unit="${unit_dir}/videolink-subtitle.service"

  if [[ "${UNINSTALL}" -eq 1 ]]; then
    systemctl --user disable --now videolink-subtitle.service 2>/dev/null || true
    rm -f "${unit}"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed systemd user service videolink-subtitle.service."
    return
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found; cannot install a systemd user service." >&2
    echo "Start the service manually or add it to your desktop's autostart:" >&2
    echo "  ${RUNNER}" >&2
    exit 1
  fi

  mkdir -p "${unit_dir}" "${LOG_DIR}"
  cat >"${unit}" <<UNIT
[Unit]
Description=VideoLink live subtitle service
After=network.target

[Service]
Type=exec
WorkingDirectory=${REPO_DIR}
Environment=VIDEOLINK_SUBTITLE_PORT=${VIDEOLINK_SUBTITLE_PORT:-8765}
ExecStart=${RUNNER}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT

  systemctl --user daemon-reload
  systemctl --user enable --now videolink-subtitle.service
  echo "Installed systemd user service: ${unit}"
  echo "The subtitle service now starts at login and restarts on failure."
  echo "Enable start-at-boot without login:  loginctl enable-linger \"\$USER\""
  echo "Status:  systemctl --user status videolink-subtitle.service"
}

if [[ "${UNINSTALL}" -ne 1 && ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "error: app virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/setup-venv.sh (or scripts/install.sh) first." >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin) install_macos ;;
  Linux)  install_linux ;;
  *)
    echo "error: unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac
