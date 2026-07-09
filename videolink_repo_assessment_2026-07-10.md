# VideoLink Repo Assessment - Latency to Visible Subtitles

Assessed: 2026-07-10, cwd `/Users/neilllaryea/Workspace/videolink`

Current tracked HEAD: `b678730 docs: prepare videolink for public clone`

Scope: full checkout review with emphasis on minimizing the delay between triggering the shortcut and seeing live subtitles in IINA.

## Executive Summary

The current active architecture is much stronger than the older untracked `videolink_assessment.md` suggests. The repo now has a pure resolver core, zero-network fast paths, a shared deadline model, concurrent candidate probes, stream URL reuse for live subtitles, IINA IPC integration, and a local FastAPI subtitle service.

The biggest remaining latency is no longer the common resolver path. For direct media and common platform URLs, local resolution is effectively bounded by Python process startup and import time. The remaining visible-subtitle delay is mostly in the live-subtitle path:

1. resolving real display/audio streams for ffmpeg,
2. ensuring the local subtitle service is already running,
3. paying any first-use ASR model load,
4. waiting for a full audio chunk,
5. transcribing it,
6. writing/reloading the growing SRT in IINA.

The fastest practical path is: always-on service, warm ASR backend, resolver cache, direct audio stream reuse, first chunk smaller than the steady-state chunk, and real first-render metrics.

## Current Latency Path

Hotkey flow in `videolink.py`:

1. Parse args and read clipboard if no URL is supplied.
2. Normalize and validate the URL.
3. `extract_with_timeout()` runs `extract_videos_traced()` with a 12s cap.
4. If live subtitles might be used, `begin_stream_resolution()` starts resolving real stream URLs in the background.
5. `resolve_subtitle_mode()` chooses no subtitles vs live subtitles.
6. Live mode waits for stream resolution, creates a subtitle service session, opens IINA with an IPC socket, and starts the service session.
7. The service starts ffmpeg, reads PCM chunks, transcribes chunks, writes SRT, and asks IINA to add or reload the subtitle track.

The code already avoids several avoidable delays:

- `resolver_core.fast_path_videos()` returns direct media and most platform video URLs without network.
- fallback page resolution fetches once, ranks candidates, probes embeds concurrently, and uses a shared `Deadline`.
- `ydl_extract()` caches full yt-dlp info for later live stream resolution in the same process.
- `begin_stream_resolution()` overlaps stream resolution with the subtitle prompt.
- `resolve_stream_urls()` chooses an audio-only stream for ffmpeg when available, avoiding unnecessary video download for ASR.

Measured local/offline timing from this checkout:

- `import videolink`: about 0.150s
- first `specific_extractor_name()` for a YouTube URL: about 0.015s
- warm fast-path extraction for a YouTube URL: below printed precision
- direct media fast-path: about 0.000011s

These timings do not include macOS Shortcuts startup, IINA launch, network resolution, model load, or transcription.

## Findings

### F1. Service was not running during assessment

`scripts/doctor-subtitles.sh` reported ffmpeg, ffprobe, Python 3.12, the app-managed service venv, and MLX Whisper as available, but the local subtitle service was not running.

Impact: live subtitles cannot start until the service is running. If the shortcut expects live subtitles, this is the first operational requirement. On a real shortcut path, startup-on-demand would add avoidable seconds and potentially model warmup delay.

Recommendation: install a launchd user agent or equivalent keepalive wrapper for `scripts/run-subtitle-service.sh`, then treat `/health` as a readiness gate.

### F2. First visible caption is chunk-bound

`StartSubtitleRequest.chunk_seconds` defaults to `2.5`. `ffmpeg_reader_thread()` blocks until it reads a full chunk before ASR sees any audio. That makes first-caption latency at least roughly one chunk plus ffmpeg startup, ASR time, SRT write, and IINA reload time.

Impact: even with perfect resolution and a warm model, subtitles cannot appear instantly. The current lower bound is dominated by the chunk size.

Recommendation: use a smaller first chunk, then larger steady-state chunks. A good target is an initial 1.0-1.5s chunk followed by 2.0-2.5s chunks. This requires code changes because the current Pydantic field enforces `ge=2.0`.

### F3. `VIDEOLINK_CHUNK_SECONDS` is currently ineffective

`DEFAULT_CHUNK_SECONDS` reads `VIDEOLINK_CHUNK_SECONDS`, but `StartSubtitleRequest` hardcodes `chunk_seconds=2.5`.

Verified:

```text
VIDEOLINK_CHUNK_SECONDS=2.0
DEFAULT_CHUNK_SECONDS=2.0
request_default_chunk_seconds=2.5
```

Impact: latency experiments that tune `VIDEOLINK_CHUNK_SECONDS` do not change new session defaults.

Recommendation: change the field default to `Field(default=DEFAULT_CHUNK_SECONDS, ...)`, expose a CLI option such as `--subtitle-chunk-seconds`, and add a regression test.

### F4. ASR model warmup is the largest likely cold-path cost

Backends are selected at session creation, but real model work happens on the first transcription chunk. The pipeline also calls `session.backend.close()` at session end. For backends with persistent model objects, this prevents reuse across videos.

Impact: the first subtitle session after service start can pay model import/load/download/cache validation in the critical path. Subsequent sessions may also lose reusable model state.

Recommendation: keep the selected backend object alive in the service, add a `/warmup` endpoint that runs a tiny synthetic transcription after service start, and do not close reusable backend state after every completed session unless the model changes.

### F5. Live `--subtitle-auto` still often needs stream resolution before IINA opens

The fast path can return a platform page URL immediately, but live subtitles need ffmpeg-readable audio. If the extraction phase fast-pathed a platform URL, `_RESOLVED_INFO_CACHE` has no full yt-dlp info, so `resolve_stream_urls()` may still do a full yt-dlp resolve before session creation and IINA launch.

Impact: for automatic subtitles, there is no human prompt time to hide stream resolution behind. The common "platform URL with live auto" case can still wait on network resolution.

Recommendation: move resolver work into a persistent local resolver service or into `subtitle_service.py`, backed by URL+yt-dlp-version cache keys. Keep yt-dlp extractor state and cookies warm, and return display/audio URLs directly for live mode.

### F6. The current service exposes latency fields, but one advertised metric is not updated

`latency_seconds`, `processing_speed_ratio`, `dropped_stale_audio_seconds`, and `first_rendered_caption_at` are maintained. `queue_seconds` is included in status and the README, but no assignment updates it.

Impact: the service does not currently give a complete enough picture of first-render latency or backlog for tuning.

Recommendation: record `hotkey_start_at`, `session_created_at`, `iina_launched_at`, `ffmpeg_started_at`, `first_chunk_read_at`, `first_asr_finished_at`, and `first_rendered_caption_at`. Compute `queue_seconds` from pending audio duration, and report `first_render_latency_seconds`.

### F7. Smoke test script points at an old checkout

`scripts/test-spiegel-live-subtitles.sh` activates:

```text
/Users/neilllaryea/Workspace/Video script/.venv/bin/activate
```

Impact: the live proof-point test is not reliable for this repo at `/Users/neilllaryea/Workspace/videolink`.

Recommendation: derive `REPO_DIR` from the script path and activate `${REPO_DIR}/.venv/bin/activate`.

### F8. Legacy and prototype files need ownership decisions

Tracked `script.py` is an older standalone extractor with independent network timeouts and heuristics. Untracked `videolink2_resolver.py` and `videolink_assessment.md` look like previous prototype/assessment artifacts.

Impact: future work can accidentally revive stale logic or stale conclusions.

Recommendation: either document `script.py` as deprecated, delete it, or add a test preventing shortcut docs from referencing it. Decide whether `videolink2_resolver.py` should be integrated into the active resolver daemon plan or removed.

## How To Minimize Shortcut-To-Subtitle Latency

Priority order:

1. Keep the subtitle service always running.
   - Add a launchd user agent.
   - Fail fast if `/health` is unavailable.
   - Do not start the service from the shortcut path.

2. Warm and retain the ASR backend.
   - Add `/warmup`.
   - Run warmup at service start or login.
   - Reuse loaded backend/model state across sessions.
   - Use `base` or `tiny` for lowest latency when accuracy tradeoff is acceptable; use `small` when quality matters more.

3. Fix chunk configurability and reduce the first chunk.
   - Make `VIDEOLINK_CHUNK_SECONDS` actually set the default.
   - Add a CLI payload option for chunk size.
   - Add a distinct first-chunk size, ideally 1.0-1.5s.
   - Keep steady-state chunks around 2.0-2.5s unless the backend falls behind.

4. Make resolver/cache persistent.
   - Move `POST /resolve` into the local service or add a small resolver daemon.
   - Cache successful display/audio stream selections with `resolver_core.cache_key()`.
   - Cache negative outcomes briefly.
   - Reuse a shared yt-dlp object/cookie state for probes.
   - Use the active `resolver_core` functions as the source of truth; do not revive the older `script.py` flow.

5. Start IINA/socket work as early as the stream contract allows.
   - Today, live mode waits for stream resolution before session creation and IINA launch.
   - Once a persistent resolver can return stream selections quickly, this becomes less important.
   - If stream resolution remains slow, consider creating the IPC socket and launching IINA earlier, then attaching subtitles once audio resolution is ready.

6. Add first-render observability.
   - `VIDEOLINK_TIMINGS=1` currently marks extraction, stream resolution, session creation, IINA launch, and session start from the client side.
   - It does not measure "first subtitle actually rendered".
   - Use service timestamps to make p50/p95 first-render latency measurable.

7. Repair the live smoke test.
   - Fix the hardcoded venv path.
   - Have it report first-render latency, not just eventual `iina_rendering_active`.
   - Run it with `VIDEOLINK_ASR_BACKEND=mock` for fast structural tests and MLX for real latency tests.

## Expected Latency After Changes

With service running, warm ASR, cached stream resolution, and direct media/platform URL:

- IINA launch target: under 1s after shortcut trigger.
- First visible caption target with current 2.5s chunks: about 2.5-4s.
- First visible caption target with warm model and 1.0-1.5s first chunk: about 1.8-3s, depending on ffmpeg startup, source buffering, ASR backend speed, and speech timing.

Without a running service or with a cold model, visible subtitles can easily exceed the README target even if resolver extraction is fast.

## Validation Run

Commands run:

```sh
./.venv/bin/python -m unittest test_videolink_core -v
./.venv/bin/python -m py_compile videolink.py resolver_core.py test_videolink_core.py
python3 -m py_compile subtitle_service.py
scripts/doctor-subtitles.sh
'/Users/neilllaryea/Library/Application Support/VideoLink/venv/bin/python' -m unittest test_videolink_core.SubtitleServiceStatusTests -v
'/Users/neilllaryea/Library/Application Support/VideoLink/venv/bin/python' -m py_compile subtitle_service.py
```

Results:

- Repo `.venv` test run: 39 tests passed, 2 skipped. The skipped tests require app-managed subtitle service dependencies.
- App-managed service venv subtitle status tests: 2 tests passed.
- Compile checks passed.
- Doctor: ffmpeg, ffprobe, Python 3.12, app venv, and MLX Whisper available; subtitle service not running.
- Full combined test file intentionally fails under the app-managed service venv because that environment does not install `yt-dlp`; this is expected from the current two-venv design but could be made less confusing by splitting service tests into their own file.

## Current Worktree Note

Before this report, tracked files were clean and these untracked files already existed:

```text
videolink2_resolver.py
videolink_assessment.md
```

This assessment was added as a new file and did not modify those artifacts.
