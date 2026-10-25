# VideoLink

Copy a video page URL, press **Control–Option–Command–V** in Apple Shortcuts,
choose a video when needed, and watch in IINA. Optional live subtitles are
transcribed locally using MLX on Apple Silicon.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
[IINA](https://iina.io/), and ffmpeg first. Keep your existing ffmpeg installation;
there is no need to replace `ffmpeg-full`.

```sh
scripts/install.sh
```

The installer uses Python 3.12 and the committed `uv.lock`. It creates or reuses
one repo-local `.venv` for the shortcut, service, and tests, and installs MLX on
Apple Silicon. uv can provision Python if it is missing. ASR models download only
when subtitles are first used. Declared dependencies are kept intact, including
MLX Whisper's torch dependency, so dependency checks remain useful.

Your existing Apple Shortcuts command stays valid, including its absolute paths:

```sh
/absolute/path/to/videolink/.venv/bin/python /absolute/path/to/videolink/videolink.py --subtitle-prompt
```

Keep whatever subtitle flag your shortcut already uses. Do not call uv from the
shortcut: playback uses the prepared environment and never resolves or updates
packages on a keypress. The shortcut must be able to find your existing ffmpeg
and IINA tools; its PATH can differ from Terminal.

## Maintain

```sh
scripts/update.sh                 # refresh lock, install, and run regression tests
scripts/doctor-subtitles.sh       # imports, versions, media tools, backend, service
.venv/bin/python -m unittest test_videolink_core test_tooling
.venv/bin/uv pip check            # verify declared dependencies
```

Review and commit `pyproject.toml` and `uv.lock` together after updates. Ordinary
installation uses the lockfile without upgrading packages. `update.sh` upgrades
all locked dependencies. Install preserves the last selected backend, saved in
`.venv/videolink-backend`; first setup defaults to MLX on Apple Silicon and
faster-whisper elsewhere. The profile is recorded only after dependency setup
succeeds. Existing extra packages are retained to avoid disrupting an older
environment; uv manages the dependencies declared by the selected profile.

## Live subtitles

```sh
scripts/run-subtitle-service.sh
curl http://127.0.0.1:8765/health
```

To start the service at login:

```sh
scripts/install-autostart.sh
```

Autostart uses the same repo environment. An existing installation that already
launches `scripts/run-subtitle-service.sh` picks up the new environment on its
next restart. The old environment in `~/Library/Application Support/VideoLink/venv`
is no longer used by these scripts; it is left untouched.

Choose the shortcut's existing subtitle mode:

```sh
.venv/bin/python videolink.py --subtitle-prompt # choose none or live
.venv/bin/python videolink.py --subtitle-auto   # request live subtitles directly
.venv/bin/python videolink.py --subtitles none
```

The default live model is `small`; set `VIDEOLINK_LIVE_MODEL=base` for lower
resource use. Captions are attached as an actual subtitle track through IINA's
mpv IPC socket. On slow transcription, the service drops stale queued audio and
resynchronizes to playback. WhisperX refinement is unfinished and is not part of
the standard setup. Its old installer now explains this and installs nothing.

The service binds to localhost. Transcription runs locally, without cloud ASR or
telemetry. Media retrieval and initial model downloads require network access.
Generated subtitles and logs live in `~/Library/Application Support/VideoLink`.
`VIDEOLINK_APP_SUPPORT_DIR` overrides that data location, not the Python environment.
`VIDEOLINK_SUBTITLE_PORT` overrides port 8765. Pass environment overrides when
starting the service; autostart only records the port by default.

## Other profiles

Linux playback through mpv and the existing alternative transcription backends
remain available:

```sh
scripts/install.sh --no-backend
scripts/install.sh --backend faster-whisper
scripts/install.sh --backend whispercpp
```

`--no-backend` installs the core service and CLI without adding an ASR backend;
it does not uninstall previously installed backends. Actual backend selection
continues to follow the service's availability policy and `VIDEOLINK_ASR_BACKEND`
override. To select a particular installed backend, set that variable explicitly.
Whisper.cpp CLI setups also need `WHISPER_CPP_COMMAND` and `WHISPER_CPP_MODEL`.
Linux needs ffmpeg, mpv, and optionally wl-clipboard or xclip/xsel.

The old `setup-venv.sh` and `install-live-backend.sh` commands delegate to the
unified installer. `requirements.txt` is a generated core-only pip compatibility export;
normal installation and updates use uv and its lockfile.

## Troubleshooting

- Missing imports: run `scripts/install.sh` from this checkout, then doctor.
- No live captions: check doctor and `/health`; confirm ffmpeg can read the media
  and that IINA's IPC socket is available.
- Port occupied: check the process on the configured port before starting another
  service instance.
- Shortcut works differently from Terminal: keep absolute repo/interpreter paths
  and verify the shortcut's PATH includes your media tools.

The manual `test_iina_socket.py` opens IINA and requires a live media URL. It is
separate from the regression suite and is not run by updates.
