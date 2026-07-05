# VideoLink

VideoLink extracts a video URL from the clipboard or a supplied page URL, opens
it in IINA, and can optionally hand the selected media URL to a local subtitle
service.

## Recommended MacBook Air M4 Configuration

Use a native macOS installation:

- Existing `ffmpeg-full` on `PATH`
- Existing `ffprobe` on `PATH`
- Existing Python 3.12 as `python3.12`
- Python 3.12 virtual environment at `~/Library/Application Support/VideoLink/venv`
- MLX Whisper for live subtitles on Apple Silicon
- WhisperX only as optional final alignment/refinement support

Do not use Docker for normal operation. Docker can be useful for development or
reproducibility, but it is not recommended for low-latency live subtitles on
Apple Silicon because it adds virtualization overhead and complicates access to
native Apple Silicon acceleration.

## Diagnostics

Run:

```sh
scripts/doctor-subtitles.sh
```

The doctor script verifies:

- `ffmpeg -version`
- `ffprobe -version`
- `python3.12 --version`
- the app virtual environment
- MLX Whisper import availability
- whisper.cpp configuration/import availability
- faster-whisper import availability
- optional WhisperX refinement availability
- local subtitle service `/health`, if the service is running

Missing dependencies are reported as actionable errors. The scripts do not
install Homebrew, ffmpeg, ffprobe, Python, Docker, or ASR models automatically.

## Native Setup

Create the app-managed Python 3.12 environment and install only the core local
service dependencies:

```sh
scripts/setup-venv.sh
```

This creates:

```text
~/Library/Application Support/VideoLink/venv
```

and installs:

```text
fastapi uvicorn[standard] websockets httpx pydantic
```

No Whisper models are downloaded during setup.

## Live ASR Backend Installation

On Apple Silicon, the default live backend installer target is MLX Whisper:

```sh
scripts/install-live-backend.sh --backend mlx
```

Other explicit live backend choices:

```sh
scripts/install-live-backend.sh --backend whispercpp
scripts/install-live-backend.sh --backend faster-whisper
```

Backend selection for live subtitles is:

1. MLX Whisper on Apple Silicon
2. whisper.cpp with Metal support when configured
3. faster-whisper CPU mode
4. mock ASR only when `VIDEOLINK_ENABLE_MOCK_ASR=1`

The service reports the selected backend through `/health` and `/status`.
It does not claim acceleration unless the selected backend is actually
available and selected.

## Optional Refinement Backend

WhisperX is optional. It is intended for word-level timestamp alignment, cleanup
after full-file transcription, and polished final `.srt` generation. It is not
required for live subtitles and is not enabled in the default live path.

Install it only when needed:

```sh
scripts/install-refinement-backend.sh --backend whisperx
```

Diarization is intentionally out of scope for version 1.

## Model Policy

Defaults are conservative for a fanless MacBook Air:

- Live subtitles: `small`
- Low-latency or battery-saving mode: `base`
- Lowest latency with lower accuracy: `tiny`
- Offline compilation/refinement: `medium`

Do not default to `large` or larger variants on a MacBook Air. Sustained
`medium` or larger model work may warm the machine and cause throttling during
long videos.

If the live pipeline falls behind playback, the intended degradation policy is:

1. Reduce processing-ahead buffer size.
2. Switch from `small` to `base` only when automatic fallback is enabled.
3. Notify once and record the switch in logs and service status.

## Processing Split

During playback:

```text
MLX Whisper or Metal-native backend
-> low-latency subtitle events
-> IINA overlay
```

Afterward, or during "Compile subtitles first":

```text
optional WhisperX alignment
-> improved timing
-> final SRT export
```

## Running The Subtitle Service

Start the service:

```sh
scripts/run-subtitle-service.sh
```

Check status:

```sh
curl http://127.0.0.1:8765/health
```

Start a live caption session:

```sh
curl -X POST http://127.0.0.1:8765/sessions \
  -H 'Content-Type: application/json' \
  -d '{"media_url":"https://example.com/video.m3u8","iina_ipc_path":"/tmp/videolink-iina.sock"}'
```

`POST /sessions` starts the real pipeline:

```text
ffmpeg/ffprobe
-> IINA current playback time
-> ffmpeg seek to playback time minus a small preroll
-> mono 16 kHz PCM chunks
-> selected ASR backend
-> language detection
-> caption cues
-> growing SRT file
-> WebSocket events
-> IINA mpv IPC subtitle track (sub-add/sub-reload) when an IPC path is supplied
```

IINA does not reliably render mpv's OSD for commands sent over the IPC
socket (see [iina/iina#3243](https://github.com/iina/iina/issues/3243)), so
captions are added as a real subtitle track (`sub-add`) pointed at the
growing SRT file and refreshed with `sub-reload` as new cues are appended,
rather than using `show-text`.

The response may initially report `state: "starting"`. It is not considered
active until caption generation has produced cues; `iina_rendering_active` is
only true after an IPC render command succeeds.

For live playback, VideoLink reads IINA's `time-pos` over the IPC socket before
starting ASR and sends that as `start_offset_seconds`. The service also checks
the player clock during processing; if playback gets ahead of generated cues,
it restarts ffmpeg from the current playback time with a small preroll instead
of continuing to transcribe stale backlog.

Stream events:

```sh
websocat ws://127.0.0.1:8765/events
```

Stop or seek:

```sh
curl -X POST http://127.0.0.1:8765/sessions/stop
curl -X POST http://127.0.0.1:8765/sessions/<session_id>/seek \
  -H 'Content-Type: application/json' \
  -d '{"position_seconds":120}'
```

For development smoke tests only, force the mock backend:

```sh
VIDEOLINK_ASR_BACKEND=mock VIDEOLINK_MOCK_TRANSCRIPT="test caption" scripts/run-subtitle-service.sh
```

Example status shape:

```json
{
  "state": "running",
  "backend": {
    "name": "mlx-whisper",
    "device": "apple_silicon",
    "model": "small",
    "acceleration_active": true
  },
  "processed_until": 91.2,
  "latency_seconds": 2.1,
  "caption_generation_active": true,
  "iina_rendering_active": true,
  "srt_path": "/Users/you/Library/Application Support/VideoLink/subtitles/session.srt",
  "events_url": "/sessions/session/events"
}
```

The service uses `ffprobe` to inspect local files and remote media URLs, and it
constructs `ffmpeg` subprocess calls as argument lists. It decodes ASR input as
mono 16 kHz signed 16-bit PCM and never concatenates untrusted URLs into shell
commands.

Remote HTTPS media, HLS `.m3u8`, DASH `.mpd`, and local files are supported
where the local ffmpeg build supports them. Reconnect options are applied only
to remote sources.

## Shortcut Flow

The existing Control-Option-Command-V shortcut flow is unchanged by default:
copy a page URL, trigger the shortcut, choose a video when needed, copy the
resolved link, and open it in IINA.

To ask before starting live subtitles from the shortcut script, call:

```sh
./videolink.py --subtitle-prompt
```

To start live subtitles automatically when the local service is healthy, call:

```sh
./videolink.py --subtitle-auto
```

The prompt mode surfaces a simple service/backend status and avoids exposing
Python stack traces to the user. Auto mode skips the dialog and falls back to
normal IINA playback if the subtitle service or live backend is unavailable.

## Latency and Performance Expectations

Target performance metrics on Apple Silicon (M-series, specifically fanless like MacBook Air M4):
- **Typical visible subtitle latency:** 2–4 seconds
- **Maximum sustainable visible latency:** under 6 seconds
- **Processing speed ratio:** greater than 1.0 (faster than real time) whenever possible

If the service falls behind, it prioritizes low latency by dropping stale queued audio and maintaining near live playback over accumulating a growing delayed backlog.

## Common Failures

- **Service port already in use:** Occurs if `subtitle_service.py` is already running or another app uses port 8765. Stop the conflicting process.
- **`ModuleNotFoundError: yt_dlp`:** You are running `videolink.py` from the wrong Python environment. Ensure you activate the project's local `.venv`.
- **IINA IPC renderer not attached:** IINA was launched but the IPC socket didn't become available. The system will fall back to playback without subtitles.
- **Remote URL expired or ffmpeg cannot read stream:** Ensure the source media link is still valid.
- **No captions because source has no usable audio:** If the video has no audio track or only silence, the service filters out the empty chunks, resulting in no captions.

## Privacy Statement

VideoLink subtitle processing is strictly designed for privacy and local processing:
- **Localhost only:** The subtitle service runs entirely on your local machine.
- **No cloud transcription:** Transcriptions are performed locally via MLX Whisper or the configured backend.
- **No telemetry:** We do not track or log your usage.
- **No upload:** Audio data, media URLs, and generated captions are never uploaded or shared externally.
