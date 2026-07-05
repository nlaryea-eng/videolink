#!/usr/bin/env bash
set -u

APP_SUPPORT_DIR="${HOME}/Library/Application Support/VideoLink"
VENV_DIR="${APP_SUPPORT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
PORT="${VIDEOLINK_SUBTITLE_PORT:-8765}"

available() {
  command -v "$1" >/dev/null 2>&1
}

print_status() {
  local label="$1"
  local status="$2"
  printf "  %-30s %s\n" "${label}:" "${status}"
}

python_import_available() {
  local module="$1"
  if [[ ! -x "${PY}" ]]; then
    return 1
  fi
  "${PY}" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('${module}') else 1)" >/dev/null 2>&1
}

whispercpp_available() {
  if [[ -n "${WHISPER_CPP_COMMAND:-}" && -n "${WHISPER_CPP_MODEL:-}" ]]; then
    return 0
  fi
  if available whisper-cli || available whisper-cpp; then
    [[ -n "${WHISPER_CPP_MODEL:-}" ]]
    return
  fi
  python_import_available pywhispercpp
}

apple_silicon() {
  [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]
}

service_health() {
  if available curl; then
    curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" 2>/dev/null
    return
  fi
  if available python3.12; then
    python3.12 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=1).read().decode())" 2>/dev/null
    return
  fi
  return 1
}

echo "VideoLink Subtitle Diagnostics"
echo "System dependencies"

if available ffmpeg; then
  print_status "ffmpeg" "available ($(ffmpeg -version 2>/dev/null | head -n 1))"
else
  print_status "ffmpeg" "missing - install ffmpeg-full and ensure ffmpeg is on PATH"
fi

if available ffprobe; then
  print_status "ffprobe" "available ($(ffprobe -version 2>/dev/null | head -n 1))"
else
  print_status "ffprobe" "missing - install ffmpeg-full and ensure ffprobe is on PATH"
fi

if available python3.12; then
  print_status "Python 3.12" "available ($(python3.12 --version 2>&1))"
else
  print_status "Python 3.12" "missing - install Python 3.12 and ensure python3.12 is on PATH"
fi

if [[ -x "${PY}" ]]; then
  print_status "App virtual environment" "available (${VENV_DIR})"
else
  print_status "App virtual environment" "missing - run scripts/setup-venv.sh"
fi

echo "Live transcription"
if python_import_available mlx_whisper; then
  mlx_status="available"
else
  mlx_status="not installed"
fi
print_status "MLX Whisper" "${mlx_status}"

if whispercpp_available; then
  whispercpp_status="available"
else
  whispercpp_status="not configured"
fi
print_status "whisper.cpp" "${whispercpp_status}"

if python_import_available faster_whisper; then
  faster_status="available"
else
  faster_status="not installed"
fi
print_status "faster-whisper" "${faster_status}"

if apple_silicon && [[ "${mlx_status}" == "available" ]]; then
  selected="MLX Whisper"
elif [[ "${whispercpp_status}" == "available" ]]; then
  selected="whisper.cpp"
elif [[ "${faster_status}" == "available" ]]; then
  selected="faster-whisper CPU"
else
  selected="none - run scripts/install-live-backend.sh --backend mlx"
fi
print_status "Selected default backend" "${selected}"
print_status "Default live model" "${VIDEOLINK_LIVE_MODEL:-small}"
print_status "Model cache" "downloads deferred until subtitle mode starts"

echo "Optional refinement"
if python_import_available whisperx; then
  print_status "WhisperX" "available"
  print_status "Status" "live subtitles available independently; refinement installed"
else
  print_status "WhisperX" "not installed"
  print_status "Status" "live subtitles do not require WhisperX"
fi

echo "Local service"
health_json="$(service_health || true)"
if [[ -n "${health_json}" ]]; then
  print_status "Status" "running at http://127.0.0.1:${PORT}"
  print_status "Health" "${health_json}"
else
  print_status "Status" "not running"
fi
