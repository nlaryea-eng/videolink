#!/usr/bin/env bash
set -euo pipefail

SPIEGEL_URL="https://www.spiegel.de/ausland/spiegel-tv-vom-29-06-2026-jugendgangs-in-frankreich-a-a9b769e3-8b11-4b9b-899a-9cdcf708aee3"
PORT="${VIDEOLINK_SUBTITLE_PORT:-8765}"

echo "=== Spiegel Live Subtitles Proof-Point Test ==="
echo "Verifying subtitle service health..."

if ! curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" > /dev/null; then
  echo "ERROR: Subtitle service is not running. Please start it first."
  exit 1
fi
echo "Service is healthy."

echo "Starting videolink.py workflow in background..."
echo "Please click 'Live subtitles' when prompted."
source "/Users/neilllaryea/Workspace/Video script/.venv/bin/activate"

python3 videolink.py --subtitles live "$SPIEGEL_URL" &
PID=$!

echo "Waiting for session to be registered..."
SESSION_ID=""
for i in {1..30}; do
    HEALTH_DATA=$(curl -s "http://127.0.0.1:${PORT}/health" || true)
    if [[ "$HEALTH_DATA" == *"session_id"* ]]; then
        SESSION_ID=$(python3 -c "import sys, json; val = json.loads(sys.stdin.read()).get('session_id'); print(val if val else '')" <<< "$HEALTH_DATA")
        if [[ -n "$SESSION_ID" ]]; then
            break
        fi
    fi
    sleep 1
done

if [[ -z "$SESSION_ID" ]]; then
    echo "ERROR: Timed out waiting for session creation."
    exit 1
fi

echo "Created Session ID: $SESSION_ID"
echo "Monitoring IPC and Rendering status..."

SUCCESS=0
for i in {1..60}; do
    STATUS=$(curl -s "http://127.0.0.1:${PORT}/sessions/${SESSION_ID}/status" || echo "{}")
    RENDER_ACTIVE=$(python3 -c "import sys, json; print(json.loads(sys.stdin.read()).get('iina_rendering_active', False))" <<< "$STATUS")

    if [[ "$RENDER_ACTIVE" == "True" ]]; then
        SUCCESS=1
        break
    fi
    sleep 1
done

echo ""
STATUS=$(curl -s "http://127.0.0.1:${PORT}/sessions/${SESSION_ID}/status" || echo "{}")

if [[ $SUCCESS -eq 1 ]]; then
    echo "SUCCESS: iina_rendering_active is True! Real IPC rendering achieved."
else
    echo "FAILED: iina_rendering_active never became True."
fi

echo "Snapshot of Final Status:"
python3 -c "import sys, json; print(json.dumps(json.loads(sys.stdin.read()), indent=2))" <<< "$STATUS"

echo ""
echo "To monitor manually, run:"
echo "curl -s http://127.0.0.1:${PORT}/sessions/${SESSION_ID}/status"
