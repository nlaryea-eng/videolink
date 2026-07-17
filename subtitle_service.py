#!/usr/bin/env python3
"""Local subtitle backend selection and status service for VideoLink.

The service supports macOS and Linux. It does not install system packages or
download ASR models at import/startup time. Backends load models only when
transcription is requested.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import asyncio
import json
import os
import platform
import queue
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from resolver_core import app_support_dir

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from pydantic import BaseModel, Field
except ModuleNotFoundError as error:  # pragma: no cover - setup script fixes this.
    raise SystemExit(
        "Subtitle service dependencies are missing. Run scripts/setup-venv.sh first."
    ) from error


APP_NAME = "VideoLink Subtitle Service"
DEFAULT_PORT = int(os.environ.get("VIDEOLINK_SUBTITLE_PORT", "8765"))
DEFAULT_LIVE_MODEL = os.environ.get("VIDEOLINK_LIVE_MODEL", "small").strip().lower()
APP_SUPPORT_DIR = app_support_dir()
SUBTITLE_OUTPUT_DIR = Path(os.environ.get(
    "VIDEOLINK_SUBTITLE_DIR",
    str(APP_SUPPORT_DIR / "subtitles"),
)).expanduser()
SAMPLE_RATE = 16000
BYTES_PER_PCM_SECOND = SAMPLE_RATE * 2
DEFAULT_CHUNK_SECONDS = float(os.environ.get("VIDEOLINK_CHUNK_SECONDS", "2.5"))
IINA_SEEK_THRESHOLD_SECONDS = float(os.environ.get("VIDEOLINK_SEEK_THRESHOLD_SECONDS", "8"))
DEFAULT_IINA_PREROLL_SECONDS = float(os.environ.get("VIDEOLINK_SUBTITLE_PREROLL_SECONDS", "0.75"))
IINA_BEHIND_RESYNC_SECONDS = float(os.environ.get("VIDEOLINK_BEHIND_RESYNC_SECONDS", "3.0"))
SUPPORTED_MODELS = ("tiny", "base", "small", "medium")
SUPPORTED_TASKS = {"transcribe", "translate"}
MLX_MODEL_REPO_CANDIDATES = {
    "tiny": ("mlx-community/whisper-tiny", "mlx-community/whisper-tiny-mlx"),
    "base": ("mlx-community/whisper-base-mlx", "mlx-community/whisper-base"),
    "small": ("mlx-community/whisper-small-mlx", "mlx-community/whisper-small"),
    "medium": ("mlx-community/whisper-medium-mlx", "mlx-community/whisper-medium"),
}

if DEFAULT_LIVE_MODEL not in SUPPORTED_MODELS:
    DEFAULT_LIVE_MODEL = "small"


@dataclass
class TranscriptionSegment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    text: str
    segments: list[TranscriptionSegment] = field(default_factory=list)
    language: str | None = None
    duration_seconds: float | None = None


@dataclass
class LanguageDetectionResult:
    language: str | None
    confidence: float | None = None


@dataclass
class BackendInfo:
    name: str
    device: str
    model: str
    acceleration_active: bool
    available: bool = True
    fallback_reason: str | None = None
    detail: str | None = None


class ASRBackend(Protocol):
    name: str

    def is_available(self) -> bool:
        ...

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        ...

    def detect_language(
        self,
        audio_pcm: bytes,
        sample_rate: int,
    ) -> LanguageDetectionResult:
        ...

    def close(self) -> None:
        ...


class BackendUnavailable(RuntimeError):
    """Raised when a backend is selected but cannot execute a request."""


def module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in {"arm64", "aarch64"}


def write_pcm16_wav(audio_pcm: bytes, sample_rate: int) -> str:
    temp = tempfile.NamedTemporaryFile(prefix="videolink-asr-", suffix=".wav", delete=False)
    temp_path = temp.name
    temp.close()
    with wave.open(temp_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio_pcm)
    return temp_path


def segments_from_plain_text(text: str) -> list[TranscriptionSegment]:
    return [TranscriptionSegment(start=0.0, end=0.0, text=text)] if text else []


class MLXWhisperBackend:
    name = "mlx-whisper"

    def __init__(self, model: str = DEFAULT_LIVE_MODEL) -> None:
        self.model = normalize_model(model)
        self._module: Any | None = None

    def is_available(self) -> bool:
        if not is_apple_silicon() or not module_available("mlx_whisper"):
            return False
        try:
            importlib.import_module("mlx_whisper")
            return True
        except Exception:
            return False

    def _load_module(self) -> Any:
        if not self.is_available():
            raise BackendUnavailable("MLX Whisper is unavailable on this machine.")
        if self._module is None:
            self._module = importlib.import_module("mlx_whisper")
        return self._module

    def model_repos(self) -> tuple[str, ...]:
        override = os.environ.get("VIDEOLINK_MLX_MODEL_REPO")
        if override:
            return (override,)
        return MLX_MODEL_REPO_CANDIDATES.get(self.model, (f"mlx-community/whisper-{self.model}-mlx",))

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        module = self._load_module()
        wav_path = write_pcm16_wav(audio_pcm, sample_rate)
        errors: list[str] = []
        try:
            result = None
            for model_repo in self.model_repos():
                try:
                    kwargs = {
                        "path_or_hf_repo": model_repo,
                        "language": language,
                        "task": task,
                    }
                    sig = inspect.signature(module.transcribe)
                    if "condition_on_previous_text" in sig.parameters:
                        kwargs["condition_on_previous_text"] = False
                    if "temperature" in sig.parameters:
                        kwargs["temperature"] = 0.0

                    result = module.transcribe(wav_path, **kwargs)
                    break
                except Exception as error:  # noqa: BLE001 - backend boundary.
                    errors.append(f"{model_repo}: {error}")
            if result is None:
                raise BackendUnavailable("MLX Whisper model load failed. " + " | ".join(errors))
        finally:
            safe_unlink(wav_path)

        text = str(result.get("text") or "").strip()
        segments = [
            TranscriptionSegment(
                start=float(segment.get("start") or 0.0),
                end=float(segment.get("end") or 0.0),
                text=str(segment.get("text") or "").strip(),
            )
            for segment in result.get("segments") or []
            if isinstance(segment, dict)
        ]
        return TranscriptionResult(
            text=text,
            segments=segments or segments_from_plain_text(text),
            language=result.get("language") or language,
        )

    def detect_language(self, audio_pcm: bytes, sample_rate: int) -> LanguageDetectionResult:
        result = self.transcribe_chunk(audio_pcm, sample_rate, None, "transcribe")
        return LanguageDetectionResult(language=result.language, confidence=None)

    def close(self) -> None:
        self._module = None


class WhisperCppBackend:
    name = "whisper.cpp"

    def __init__(self, model: str = DEFAULT_LIVE_MODEL) -> None:
        self.model = normalize_model(model)
        self._model: Any | None = None

    def is_available(self) -> bool:
        return self._pywhispercpp_available() or self._cli_available()

    def _pywhispercpp_available(self) -> bool:
        return module_available("pywhispercpp")

    def _cli_available(self) -> bool:
        return bool(whisper_cpp_command() and whisper_cpp_model_path())

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        if self._pywhispercpp_available():
            return self._transcribe_with_pywhispercpp(audio_pcm, sample_rate, language, task)
        if self._cli_available():
            return self._transcribe_with_cli(audio_pcm, sample_rate, language, task)
        raise BackendUnavailable("whisper.cpp is not configured.")

    def _transcribe_with_pywhispercpp(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        del task
        model_module = importlib.import_module("pywhispercpp.model")
        if self._model is None:
            self._model = model_module.Model(self.model)
        wav_path = write_pcm16_wav(audio_pcm, sample_rate)
        try:
            raw_segments = self._model.transcribe(wav_path, language=language)
        finally:
            safe_unlink(wav_path)

        segments: list[TranscriptionSegment] = []
        for segment in raw_segments or []:
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptionSegment(
                    start=float(getattr(segment, "t0", 0.0) or 0.0) / 100.0,
                    end=float(getattr(segment, "t1", 0.0) or 0.0) / 100.0,
                    text=text,
                )
            )
        text = " ".join(segment.text for segment in segments).strip()
        return TranscriptionResult(text=text, segments=segments, language=language)

    def _transcribe_with_cli(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        wav_path = write_pcm16_wav(audio_pcm, sample_rate)
        output_prefix = tempfile.NamedTemporaryFile(prefix="videolink-whispercpp-", delete=False)
        output_base = output_prefix.name
        output_prefix.close()
        safe_unlink(output_base)
        command = whisper_cpp_command() or []
        model_path = whisper_cpp_model_path()
        if not command or not model_path:
            raise BackendUnavailable("WHISPER_CPP_COMMAND and WHISPER_CPP_MODEL are required.")
        args = command + ["-m", model_path, "-f", wav_path, "-otxt", "-of", output_base]
        if language:
            args += ["-l", language]
        if task == "translate":
            args.append("-tr")
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)
            text_path = f"{output_base}.txt"
            text = open(text_path, encoding="utf-8").read().strip() if os.path.exists(text_path) else ""
        finally:
            safe_unlink(wav_path)
            safe_unlink(f"{output_base}.txt")
        return TranscriptionResult(text=text, segments=segments_from_plain_text(text), language=language)

    def detect_language(self, audio_pcm: bytes, sample_rate: int) -> LanguageDetectionResult:
        result = self.transcribe_chunk(audio_pcm, sample_rate, None, "transcribe")
        return LanguageDetectionResult(language=result.language, confidence=None)

    def close(self) -> None:
        self._model = None


class FasterWhisperBackend:
    name = "faster-whisper"

    def __init__(self, model: str = DEFAULT_LIVE_MODEL) -> None:
        self.model = normalize_model(model)
        self._model: Any | None = None

    def is_available(self) -> bool:
        return module_available("faster_whisper")

    def _load_model(self) -> Any:
        if not self.is_available():
            raise BackendUnavailable("faster-whisper is unavailable.")
        if self._model is None:
            module = importlib.import_module("faster_whisper")
            self._model = module.WhisperModel(self.model, device="cpu", compute_type="int8")
        return self._model

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        model = self._load_model()
        wav_path = write_pcm16_wav(audio_pcm, sample_rate)
        try:
            raw_segments, info = model.transcribe(wav_path, language=language, task=task)
            segments = [
                TranscriptionSegment(
                    start=float(segment.start or 0.0),
                    end=float(segment.end or 0.0),
                    text=str(segment.text or "").strip(),
                )
                for segment in raw_segments
                if str(segment.text or "").strip()
            ]
        finally:
            safe_unlink(wav_path)
        text = " ".join(segment.text for segment in segments).strip()
        return TranscriptionResult(
            text=text,
            segments=segments,
            language=getattr(info, "language", None) or language,
            duration_seconds=getattr(info, "duration", None),
        )

    def detect_language(self, audio_pcm: bytes, sample_rate: int) -> LanguageDetectionResult:
        result = self.transcribe_chunk(audio_pcm, sample_rate, None, "transcribe")
        return LanguageDetectionResult(language=result.language, confidence=None)

    def close(self) -> None:
        self._model = None


class WhisperXAlignmentBackend:
    name = "whisperx"

    def __init__(self, model: str = "medium") -> None:
        self.model = normalize_model(model)

    def is_available(self) -> bool:
        return module_available("whisperx")

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        del audio_pcm, sample_rate, language, task
        raise BackendUnavailable("WhisperX is optional refinement, not the live ASR backend.")

    def detect_language(self, audio_pcm: bytes, sample_rate: int) -> LanguageDetectionResult:
        del audio_pcm, sample_rate
        raise BackendUnavailable("WhisperX language detection is not used in the live path.")

    def close(self) -> None:
        return None


class MockASRBackend:
    name = "mock"

    def __init__(self, model: str = "mock") -> None:
        self.model = model

    def is_available(self) -> bool:
        return (
            os.environ.get("VIDEOLINK_ENABLE_MOCK_ASR") == "1"
            or os.environ.get("VIDEOLINK_ASR_BACKEND", "").strip().lower() == "mock"
        )

    def transcribe_chunk(
        self,
        audio_pcm: bytes,
        sample_rate: int,
        language: str | None,
        task: str,
    ) -> TranscriptionResult:
        del audio_pcm, sample_rate, task
        text = os.environ.get("VIDEOLINK_MOCK_TRANSCRIPT", "mock subtitle")
        return TranscriptionResult(text=text, segments=segments_from_plain_text(text), language=language)

    def detect_language(self, audio_pcm: bytes, sample_rate: int) -> LanguageDetectionResult:
        del audio_pcm, sample_rate
        return LanguageDetectionResult(language="en", confidence=1.0)

    def close(self) -> None:
        return None


def normalize_model(model: str | None) -> str:
    normalized = (model or DEFAULT_LIVE_MODEL).strip().lower()
    if normalized not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model {model!r}. Use one of: {', '.join(SUPPORTED_MODELS)}")
    return normalized


def whisper_cpp_command() -> list[str] | None:
    configured = os.environ.get("WHISPER_CPP_COMMAND")
    if configured:
        return shlex.split(configured)
    for candidate in ("whisper-cli", "whisper-cpp"):
        path = shutil.which(candidate)
        if path:
            return [path]
    return None


def whisper_cpp_model_path() -> str | None:
    model_path = os.environ.get("WHISPER_CPP_MODEL")
    if model_path and os.path.exists(os.path.expanduser(model_path)):
        return os.path.expanduser(model_path)
    return None


def backend_info(
    backend: ASRBackend,
    *,
    fallback_reason: str | None = None,
    available: bool = True,
    detail: str | None = None,
) -> BackendInfo:
    model = getattr(backend, "model", DEFAULT_LIVE_MODEL)
    if isinstance(backend, MLXWhisperBackend):
        return BackendInfo(
            name=backend.name,
            device="apple_silicon",
            model=model,
            acceleration_active=available and is_apple_silicon(),
            available=available,
            fallback_reason=fallback_reason,
            detail=detail,
        )
    if isinstance(backend, WhisperCppBackend):
        return BackendInfo(
            name=backend.name,
            device="metal" if os.environ.get("WHISPER_CPP_METAL") == "1" else "cpu_or_metal",
            model=model,
            acceleration_active=available and os.environ.get("WHISPER_CPP_METAL") == "1",
            available=available,
            fallback_reason=fallback_reason,
            detail=detail,
        )
    if isinstance(backend, FasterWhisperBackend):
        return BackendInfo(
            name=backend.name,
            device="cpu",
            model=model,
            acceleration_active=False,
            available=available,
            fallback_reason=fallback_reason,
            detail=detail,
        )
    if isinstance(backend, WhisperXAlignmentBackend):
        return BackendInfo(
            name=backend.name,
            device="offline_refinement",
            model=model,
            acceleration_active=False,
            available=available,
            fallback_reason=fallback_reason,
            detail=detail,
        )
    return BackendInfo(
        name=backend.name,
        device="test",
        model=model,
        acceleration_active=False,
        available=available,
        fallback_reason=fallback_reason,
        detail=detail,
    )


def candidate_live_backends(model: str = DEFAULT_LIVE_MODEL) -> list[ASRBackend]:
    forced_backend = os.environ.get("VIDEOLINK_ASR_BACKEND", "").strip().lower()
    if forced_backend:
        forced = {
            "mlx": MLXWhisperBackend(model),
            "mlx-whisper": MLXWhisperBackend(model),
            "whispercpp": WhisperCppBackend(model),
            "whisper.cpp": WhisperCppBackend(model),
            "faster-whisper": FasterWhisperBackend(model),
            "faster_whisper": FasterWhisperBackend(model),
            "mock": MockASRBackend(),
        }.get(forced_backend)
        return [forced] if forced is not None else []

    candidates: list[ASRBackend] = []
    if is_apple_silicon():
        candidates.append(MLXWhisperBackend(model))
    candidates.extend([WhisperCppBackend(model), FasterWhisperBackend(model), MockASRBackend()])
    return candidates


def select_live_backend(model: str = DEFAULT_LIVE_MODEL) -> tuple[ASRBackend | None, BackendInfo, list[BackendInfo]]:
    fallback_reasons: list[str] = []
    checked: list[BackendInfo] = []
    for backend in candidate_live_backends(model):
        available = backend.is_available()
        detail = None if available else f"{backend.name} backend was unavailable"
        checked.append(backend_info(backend, available=available, detail=detail))
        if available:
            return (
                backend,
                backend_info(
                    backend,
                    fallback_reason="; ".join(fallback_reasons) or None,
                    available=True,
                ),
                checked,
            )
        fallback_reasons.append(detail or f"{backend.name} backend was unavailable")
    return (
        None,
        BackendInfo(
            name="none",
            device="unavailable",
            model=model,
            acceleration_active=False,
            available=False,
            fallback_reason="; ".join(fallback_reasons) or None,
            detail="No live ASR backend is available.",
        ),
        checked,
    )


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def is_remote_media_source(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"}


def srt_timestamp(seconds: float) -> str:
    clamped = max(0.0, seconds)
    millis = int(round(clamped * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clean_caption_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def default_srt_path(session_id: str) -> str:
    return str(SUBTITLE_OUTPUT_DIR / f"{session_id}.srt")


def write_srt(captions: list["CaptionCue"], srt_path: str) -> None:
    path = Path(srt_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call: a fixed "<path>.tmp" name collides when two threads
    # (e.g. asr_worker_thread finishing a chunk and a seek restart) write the
    # same session's SRT concurrently - whichever os.replace() runs second
    # then fails with FileNotFoundError because the first already renamed
    # its tmp file away, silently killing whichever thread hit it.
    tmp_path = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        for cue in captions:
            handle.write(f"{cue.index}\n")
            handle.write(f"{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n")
            handle.write(f"{cue.text}\n\n")
    os.replace(tmp_path, path)


def safe_unlink_if_exists(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def iina_ipc_request(
    ipc_path: str | None,
    command: list[Any],
    *,
    timeout_seconds: float = 1.0,
) -> dict[str, Any] | None:
    if not ipc_path:
        return None
    path = os.path.expanduser(ipc_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(path)
        client.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        if not data:
            return {"error": "empty response"}
        return json.loads(data.decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as error:
        return {"error": str(error)}
    finally:
        client.close()


def iina_command_succeeded(response: dict[str, Any] | None) -> bool:
    return bool(response and response.get("error") == "success")


def add_iina_subtitle_track(ipc_path: str | None, srt_path: str) -> dict[str, Any] | None:
    if not ipc_path or not srt_path:
        return {"error": "missing ipc path or srt path"}
    return iina_ipc_request(ipc_path, ["sub-add", srt_path, "select", "Live captions"])


def reload_iina_subtitle_track(ipc_path: str | None) -> dict[str, Any] | None:
    if not ipc_path:
        return {"error": "missing ipc path"}
    return iina_ipc_request(ipc_path, ["sub-reload"])




def iina_time_position(ipc_path: str | None) -> float | None:
    response = iina_ipc_request(ipc_path, ["get_property", "time-pos"], timeout_seconds=0.5)
    if not iina_command_succeeded(response):
        return None
    value = response.get("data")
    return float(value) if isinstance(value, (int, float)) else None


def ffprobe_media(source: str, timeout_seconds: int = 20) -> dict[str, Any]:
    if not shutil.which("ffprobe"):
        raise BackendUnavailable("ffprobe is not available on PATH.")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        source,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        message = (error.stderr or error.stdout or "ffprobe failed").strip()
        raise BackendUnavailable(f"ffprobe could not inspect media: {message}") from error
    except subprocess.TimeoutExpired as error:
        raise BackendUnavailable("ffprobe timed out while inspecting media.") from error
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise BackendUnavailable("ffprobe returned invalid JSON.") from error


def ffmpeg_pcm_command(
    source: str,
    *,
    reconnect: bool = True,
    start_seconds: float = 0.0,
) -> list[str]:
    if not shutil.which("ffmpeg"):
        raise BackendUnavailable("ffmpeg is not available on PATH.")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error"]
    if reconnect and is_remote_media_source(source):
        command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if start_seconds > 0:
        command += ["-ss", f"{start_seconds:.3f}"]
    command += [
        "-i",
        source,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    return command


import math
import struct

class StartSubtitleRequest(BaseModel):
    media_url: str = Field(..., min_length=1)
    mode: str = "live"
    language: str | None = None
    task: str = "transcribe"
    model: str = DEFAULT_LIVE_MODEL
    automatic_model_fallback: bool = False
    probe_media: bool = True
    iina_ipc_path: str | None = None
    srt_path: str | None = None
    start_offset_seconds: float = Field(default=0.0, ge=0.0)
    chunk_seconds: float = Field(default=DEFAULT_CHUNK_SECONDS, ge=1.0, le=30.0)
    sync_to_iina: bool = True
    iina_preroll_seconds: float = Field(default=DEFAULT_IINA_PREROLL_SECONDS, ge=0.0, le=10.0)


class MediaProbeRequest(BaseModel):
    media_url: str = Field(..., min_length=1)


class SeekSubtitleRequest(BaseModel):
    position_seconds: float = Field(..., ge=0.0)


@dataclass
class CaptionCue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class AudioChunk:
    pcm: bytes
    start_seconds: float
    duration: float


def renumber_captions(captions: list[CaptionCue]) -> None:
    for index, cue in enumerate(captions, start=1):
        cue.index = index


@dataclass
class SubtitleSession:
    id: str
    request: StartSubtitleRequest
    backend: ASRBackend
    backend_info: BackendInfo
    srt_path: str
    mode: str = "live"
    state: str = "idle"
    model: str = DEFAULT_LIVE_MODEL
    detected_language: str | None = None
    language_candidate: str | None = None
    language_candidate_count: int = 0
    ipc_socket_path: str | None = None

    cancel_event: threading.Event = field(default_factory=threading.Event)
    event_queues: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    captions: list[CaptionCue] = field(default_factory=list)

    ffmpeg_process: subprocess.Popen[bytes] | None = None
    ffmpeg_thread: threading.Thread | None = None
    asr_task: threading.Thread | None = None
    pipeline_task: threading.Thread | None = None
    seek_watcher_task: threading.Thread | None = None
    audio_queue: queue.Queue[AudioChunk] = field(default_factory=lambda: queue.Queue(maxsize=50))

    start_time: float = field(default_factory=time.time)
    cursor_seconds: float = 0.0
    last_iina_position: float | None = None
    iina_failures_after_active: int = 0
    iina_subtitle_track_added: bool = False

    processed_until: float = 0.0
    latency_seconds: float | None = None
    message: str | None = None
    last_error: str | None = None
    updated_at: float = field(default_factory=time.time)

    last_transcription_text: str = ""
    iina_rendering_active: bool = False

    ipc_socket_expected: bool = True
    ipc_socket_detected: bool = False
    ipc_connected: bool = False
    ipc_last_success_at: float | None = None
    ipc_last_error: str | None = None
    first_rendered_caption_at: float | None = None
    accepted_caption_count: int = 0
    rejected_silent_count: int = 0
    rejected_low_confidence_count: int = 0
    rejected_repetition_count: int = 0
    queue_seconds: float = 0.0
    processing_speed_ratio: float | None = None
    dropped_stale_audio_seconds: float = 0.0
    last_error_code: str | None = None
    last_error_message: str | None = None
    render_queue: queue.Queue = field(default_factory=queue.Queue)
    renderer_task: threading.Thread | None = None

SESSION_LOCK = threading.RLock()
SESSIONS: dict[str, SubtitleSession] = {}

app = FastAPI(title=APP_NAME)

def clean_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}

def queue_event(session: SubtitleSession, event_type: str, **payload: Any) -> dict[str, Any]:
    event = {
        "type": event_type,
        "session_id": session.id,
        "timestamp": time.time(),
        **payload,
    }
    stale: list[queue.Queue[dict[str, Any]]] = []
    with SESSION_LOCK:
        queues = list(session.event_queues)
    for event_queue in queues:
        try:
            event_queue.put_nowait(event)
        except queue.Full:
            try:
                event_queue.get_nowait()
                event_queue.put_nowait(event)
            except queue.Empty:
                stale.append(event_queue)
    if stale:
        with SESSION_LOCK:
            session.event_queues = [q for q in session.event_queues if q not in stale]
    return event

def update_session_state(session: SubtitleSession, state: str | None = None, **updates: Any) -> None:
    with SESSION_LOCK:
        if state is not None:
            session.state = state
        session.updated_at = time.time()
        for key, value in updates.items():
            setattr(session, key, value)

def backend_display_name(info: BackendInfo) -> str:
    if info.name == "mlx-whisper":
        return "MLX Whisper on Apple Silicon"
    if info.name == "whisper.cpp":
        return "whisper.cpp"
    if info.name == "faster-whisper":
        return "faster-whisper CPU"
    if info.name == "mock":
        return "mock ASR"
    return info.name

def session_status_payload(session: SubtitleSession) -> dict[str, Any]:
    refinement = WhisperXAlignmentBackend()

    masked_url = session.request.media_url
    if "://" in masked_url:
        try:
            parsed = urlparse(masked_url)
            if parsed.password:
                masked_url = masked_url.replace(f":{parsed.password}@", ":***@")
        except Exception:
            pass

    return {
        "state": session.state,
        "backend": clean_mapping(asdict(session.backend_info)),
        "processed_until": session.processed_until,
        "latency_seconds": session.latency_seconds,
        "media_url": masked_url,
        "session_id": session.id,
        "srt_path": session.srt_path,
        "language": session.detected_language,
        "language_locked": session.detected_language is not None,
        "caption_count": len(session.captions),
        "caption_generation_active": len(session.captions) > 0,
        "iina_rendering_active": session.iina_rendering_active,
        "iina_ipc_path": session.ipc_socket_path,
        "ffmpeg_active": session.ffmpeg_process is not None and session.ffmpeg_process.poll() is None,
        "asr_active": session.asr_task is not None and session.asr_task.is_alive(),
        "live_asr_available": session.backend_info.available,
        "whisperx_refinement_available": refinement.is_available(),
        "message": session.message,
        "last_error": session.last_error,
        "events_url": f"/ws/captions/{session.id}",
        "updated_at": session.updated_at,
        "ipc_socket_expected": session.ipc_socket_expected,
        "ipc_socket_detected": session.ipc_socket_detected,
        "ipc_connected": session.ipc_connected,
        "ipc_last_success_at": session.ipc_last_success_at,
        "ipc_last_error": session.ipc_last_error,
        "first_rendered_caption_at": session.first_rendered_caption_at,
        "accepted_caption_count": session.accepted_caption_count,
        "rejected_silent_count": session.rejected_silent_count,
        "rejected_low_confidence_count": session.rejected_low_confidence_count,
        "rejected_repetition_count": session.rejected_repetition_count,
        "queue_seconds": session.queue_seconds,
        "processing_speed_ratio": session.processing_speed_ratio,
        "dropped_stale_audio_seconds": session.dropped_stale_audio_seconds,
        "last_error_code": session.last_error_code,
        "last_error_message": session.last_error_message,
    }


def service_status_payload() -> dict[str, Any]:
    with SESSION_LOCK:
        sessions = list(SESSIONS.values())
        session_ids = [session.id for session in sessions]

    active_latest = next(
        (session for session in reversed(sessions) if session.state in ACTIVE_SESSION_STATES),
        None,
    )
    if active_latest is not None:
        payload = session_status_payload(active_latest)
        payload["sessions"] = session_ids
        return payload

    _backend, info, _checked = select_live_backend(DEFAULT_LIVE_MODEL)
    refinement = WhisperXAlignmentBackend()
    return {
        "state": "idle",
        "backend": clean_mapping(asdict(info)),
        "processed_until": 0.0,
        "latency_seconds": None,
        "media_url": None,
        "session_id": None,
        "srt_path": None,
        "language": None,
        "language_locked": False,
        "caption_count": 0,
        "caption_generation_active": False,
        "iina_rendering_active": False,
        "iina_ipc_path": None,
        "ffmpeg_active": False,
        "asr_active": False,
        "live_asr_available": info.available,
        "whisperx_refinement_available": refinement.is_available(),
        "message": None,
        "last_error": None,
        "events_url": None,
        "updated_at": time.time(),
        "ipc_socket_expected": False,
        "ipc_socket_detected": False,
        "ipc_connected": False,
        "ipc_last_success_at": None,
        "ipc_last_error": None,
        "first_rendered_caption_at": None,
        "accepted_caption_count": 0,
        "rejected_silent_count": 0,
        "rejected_low_confidence_count": 0,
        "rejected_repetition_count": 0,
        "queue_seconds": 0.0,
        "processing_speed_ratio": None,
        "dropped_stale_audio_seconds": 0.0,
        "last_error_code": None,
        "last_error_message": None,
        "sessions": session_ids,
    }

def calculate_dbfs(pcm_data: bytes) -> float:
    if not pcm_data:
        return -float('inf')
    count = len(pcm_data) // 2
    if count == 0:
        return -float('inf')
    samples = struct.unpack(f"<{count}h", pcm_data)
    sum_squares = sum(s * s for s in samples)
    if sum_squares == 0:
        return -float('inf')
    rms = math.sqrt(sum_squares / count)
    return 20 * math.log10(rms / 32768.0)

def should_accept_transcription(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    lower_text = text.lower()
    # Basic hallucination checks
    hallucinations = {"thank you.", "thank you", "thanks for watching", "subscribe to my channel", "thanks.", "you"}
    if lower_text in hallucinations:
        return False

    words = lower_text.split()
    if len(words) >= 4:
        # Check for repetition, e.g. "no no no no"
        if len(set(words)) == 1:
            return False

    # Simple N-gram repetition check (e.g. 3-grams)
    if len(words) > 9:
        for i in range(len(words) - 6):
            if words[i:i+3] == words[i+3:i+6] == words[i+6:i+9]:
                return False

    return True

def remove_overlap(prev_text: str, new_text: str) -> str:
    prev_words = prev_text.strip().split()
    new_words = new_text.strip().split()
    if not prev_words or not new_words:
        return new_text

    # Check for overlapping suffixes and prefixes
    max_overlap = min(len(prev_words), len(new_words))
    for i in range(max_overlap, 0, -1):
        if prev_words[-i:] == new_words[:i]:
            return " ".join(new_words[i:])
    return new_text

def read_pcm_chunk(process: subprocess.Popen[bytes], chunk_bytes: int) -> bytes:
    if process.stdout is None:
        return b""
    parts: list[bytes] = []
    remaining = chunk_bytes
    while remaining > 0:
        data = process.stdout.read(min(remaining, 64 * 1024))
        if not data:
            break
        parts.append(data)
        remaining -= len(data)
    return b"".join(parts)

def terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)

def open_ffmpeg_pcm(session: SubtitleSession, start_seconds: float) -> subprocess.Popen[bytes]:
    command = ffmpeg_pcm_command(
        session.request.media_url,
        start_seconds=start_seconds,
    )
    queue_event(session, "ffmpeg_starting", command=command, start_seconds=start_seconds)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    session.ffmpeg_process = process
    update_session_state(
        session,
        "running",
        processed_until=start_seconds,
        message="Caption pipeline running; waiting for first ASR chunk.",
    )
    queue_event(session, "ffmpeg_started", pid=process.pid, start_seconds=start_seconds)
    return process

def ffmpeg_reader_thread(session: SubtitleSession, process: subprocess.Popen[bytes], chunk_bytes: int):
    cursor = session.cursor_seconds
    while not session.cancel_event.is_set() and process.poll() is None:
        pcm = read_pcm_chunk(process, chunk_bytes)
        if not pcm:
            break
        duration = len(pcm) / BYTES_PER_PCM_SECOND
        try:
            session.audio_queue.put(AudioChunk(pcm, cursor, duration), timeout=1.0)
        except queue.Full:
            # Queue backed up - drop this chunk rather than blocking ffmpeg's
            # stdout pipe indefinitely. Still advance the cursor so later
            # chunks' timestamps track real playback time instead of drifting
            # behind by the dropped duration.
            session.dropped_stale_audio_seconds += duration
        cursor += duration

def make_caption_cues(
    session: SubtitleSession,
    result: TranscriptionResult,
    chunk_start: float,
    chunk_duration: float,
) -> list[CaptionCue]:
    raw_segments = result.segments or segments_from_plain_text(result.text)
    cues: list[CaptionCue] = []
    for segment in raw_segments:
        text = clean_caption_text(segment.text)
        if not text:
            continue
        local_start = max(0.0, min(float(segment.start or 0.0), chunk_duration))
        local_end = max(local_start, min(float(segment.end or 0.0), chunk_duration))
        if local_end <= local_start:
            local_end = min(chunk_duration, local_start + max(1.5, min(4.0, chunk_duration)))
        if local_end <= local_start:
            local_end = chunk_duration
        cues.append(
            CaptionCue(
                index=len(session.captions) + len(cues) + 1,
                start=chunk_start + local_start,
                end=chunk_start + local_end,
                text=text,
            )
        )
    return cues

def process_asr_chunk(session: SubtitleSession, chunk: AudioChunk):
    dbfs = calculate_dbfs(chunk.pcm)
    if dbfs < -48.0:
        session.rejected_silent_count += 1
        return # silent_audio

    update_session_state(session, message="Transcribing audio chunk.")
    queue_event(session, "asr_chunk_started", offset_seconds=chunk.start_seconds)

    language = session.detected_language or session.request.language
    if language == "auto":
        language = None
    result = session.backend.transcribe_chunk(
        chunk.pcm,
        SAMPLE_RATE,
        language,
        session.request.task,
    )

    if session.detected_language is None and result.language:
        if session.language_candidate == result.language:
            session.language_candidate_count += 1
        else:
            session.language_candidate = result.language
            session.language_candidate_count = 1

        if session.language_candidate_count >= 2:
            session.detected_language = session.language_candidate
            queue_event(session, "language_detected", language=session.detected_language)

    if not should_accept_transcription(result.text):
        session.rejected_repetition_count += 1
        queue_event(session, "asr_chunk_rejected", text=result.text)
        return

    result.text = remove_overlap(session.last_transcription_text, result.text)
    if not result.text.strip():
        return

    session.last_transcription_text = result.text

    queue_event(
        session,
        "asr_chunk_finished",
        offset_seconds=chunk.start_seconds,
        language=result.language or session.detected_language,
        text=result.text,
    )

    cues = make_caption_cues(session, result, chunk.start_seconds, chunk.duration)
    if cues:
        session.accepted_caption_count += len(cues)
        session.captions.extend(cues)
        write_srt(session.captions, session.srt_path)

        for cue in cues:
            session.render_queue.put(cue)

        for cue in cues:
            queue_event(
                session,
                "caption",
                caption=asdict(cue),
                rendered_in_iina=session.ipc_connected,
                srt_path=session.srt_path,
            )

def asr_worker_thread(session: SubtitleSession, start_offset: float):
    latency_over_start = None
    processed_duration = 0.0
    asr_start_time = time.time()

    while not session.cancel_event.is_set():
        try:
            chunk = session.audio_queue.get(timeout=1.0)
        except queue.Empty:
            if session.ffmpeg_process and session.ffmpeg_process.poll() is not None:
                break
            continue

        now = time.time()
        media_elapsed = chunk.start_seconds - start_offset
        real_elapsed = now - session.start_time
        latency = max(0.0, real_elapsed - media_elapsed)

        processing_time = now - asr_start_time
        processing_speed_ratio = processed_duration / processing_time if processing_time > 0 else 1.0
        session.processing_speed_ratio = processing_speed_ratio

        if processing_speed_ratio < 1.0 or latency > 5.0:
            if latency_over_start is None:
                latency_over_start = now
            elif now - latency_over_start > 5.0:
                # drop stale queued audio
                while not session.audio_queue.empty():
                    try:
                        dropped = session.audio_queue.get_nowait()
                        session.dropped_stale_audio_seconds += dropped.duration
                    except queue.Empty:
                        break
                latency_over_start = None
                continue
        else:
            latency_over_start = None

        process_start = time.time()
        process_asr_chunk(session, chunk)
        process_end = time.time()

        processed_duration += chunk.duration
        session.cursor_seconds = chunk.start_seconds + chunk.duration
        session.processed_until = session.cursor_seconds
        session.latency_seconds = latency

        queue_event(
            session,
            "chunk_processed",
            processed_until=session.cursor_seconds,
            caption_count=len(session.captions),
            latency_seconds=latency,
        )

def renderer_thread(session: SubtitleSession) -> None:
    if not session.ipc_socket_path:
        return

    socket_path = os.path.expanduser(session.ipc_socket_path)

    found = False
    for _ in range(100):
        if session.cancel_event.is_set():
            return
        if os.path.exists(socket_path):
            found = True
            break
        time.sleep(0.1)

    if not found:
        session.last_error = "IINA IPC handshake failed"
        return

    session.ipc_socket_detected = True

    handshake_success = False
    for _ in range(100):
        if session.cancel_event.is_set():
            return
        response = iina_ipc_request(session.ipc_socket_path, ["get_property", "mpv-version"], timeout_seconds=0.5)
        if iina_command_succeeded(response):
            handshake_success = True
            break
        time.sleep(0.1)

    if not handshake_success:
        session.last_error = "IINA IPC handshake failed"
        return

    session.ipc_connected = True
    session.ipc_last_success_at = time.time()

    IPC_FAILURE_STOP_THRESHOLD = 3

    while not session.cancel_event.is_set():
        try:
            cue = session.render_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if not session.iina_subtitle_track_added:
            resp = add_iina_subtitle_track(session.ipc_socket_path, session.srt_path)
            if iina_command_succeeded(resp):
                session.iina_subtitle_track_added = True
        else:
            resp = reload_iina_subtitle_track(session.ipc_socket_path)

        if iina_command_succeeded(resp):
            session.iina_failures_after_active = 0
            if session.first_rendered_caption_at is None:
                session.first_rendered_caption_at = time.time()
            session.iina_rendering_active = True
            session.ipc_last_success_at = time.time()
            session.ipc_last_error = None
        else:
            session.ipc_last_error = str((resp or {}).get("error") or "subtitle reload failed")
            # A response with a "request_id" means mpv is alive and replied -
            # just with a command-level error (paused, buffering, no active
            # track yet, etc). Only a response with NO request_id means the
            # socket connection itself failed (see iina_ipc_request's except
            # branch), which is the actual "IINA is gone" signal.
            connection_lost = not resp or "request_id" not in resp
            if session.iina_rendering_active and connection_lost:
                # We were rendering fine and the socket is now unreachable
                # several times in a row - it almost certainly closed.
                # Without this, ffmpeg/ASR keep running against a dead player
                # forever, burning CPU and network for captions nobody can see.
                session.iina_failures_after_active += 1
                if session.iina_failures_after_active >= IPC_FAILURE_STOP_THRESHOLD:
                    session.ipc_connected = False
                    session.iina_rendering_active = False
                    update_session_state(
                        session,
                        "stopped",
                        message="IINA closed or the subtitle socket was lost; live captions stopped.",
                    )
                    queue_event(session, "iina_lost")
                    session.cancel_event.set()
                    return
            else:
                session.iina_failures_after_active = 0


def detect_seek_or_stop(session: SubtitleSession) -> float | None:
    """Return a new start offset if IINA's playback position has drifted away
    from what the pipeline is currently processing (a manual seek, or the
    pipeline falling behind live playback) - otherwise None.
    """
    position = iina_time_position(session.ipc_socket_path)
    if position is None:
        return None

    previous = session.last_iina_position
    session.last_iina_position = position

    if position > session.cursor_seconds + IINA_BEHIND_RESYNC_SECONDS:
        # Pipeline fell behind (ASR slower than real-time); catch up to live.
        return max(0.0, position - session.request.iina_preroll_seconds)
    if previous is None:
        return None
    if position < previous - IINA_SEEK_THRESHOLD_SECONDS:
        # Rewound.
        return max(0.0, position - session.request.iina_preroll_seconds)
    if position > previous + session.request.chunk_seconds + IINA_SEEK_THRESHOLD_SECONDS:
        # Jumped forward.
        return max(0.0, position - session.request.iina_preroll_seconds)
    return None


def seek_watcher_thread(session: SubtitleSession) -> None:
    if not session.ipc_socket_path:
        return
    while not session.cancel_event.is_set():
        time.sleep(1.5)
        if session.cancel_event.is_set():
            break
        if session.state not in ("starting", "running"):
            continue
        seek_to = detect_seek_or_stop(session)
        if seek_to is None or session.cancel_event.is_set():
            continue
        queue_event(session, "seek_detected", position_seconds=seek_to)
        perform_seek(session.id, seek_to)
        return  # a fresh pipeline_task (with its own watcher) has taken over


def resolve_initial_start_offset(session: SubtitleSession) -> float:
    requested = session.request.start_offset_seconds
    if not session.request.sync_to_iina or not session.ipc_socket_path:
        return requested
    position = iina_time_position(session.ipc_socket_path)
    if position is None:
        return requested
    synced = max(0.0, position - session.request.iina_preroll_seconds)
    if requested <= 0.0:
        return synced
    if abs(synced - requested) > 1.0:
        return synced
    return requested

def run_caption_pipeline(session: SubtitleSession) -> None:
    process: subprocess.Popen[bytes] | None = None
    try:
        start_offset = resolve_initial_start_offset(session)
        session.cursor_seconds = start_offset
        write_srt(session.captions, session.srt_path)
        queue_event(
            session,
            "session_starting",
            backend=clean_mapping(asdict(session.backend_info)),
            srt_path=session.srt_path,
            iina_ipc_path=session.ipc_socket_path,
            requested_start_offset=session.request.start_offset_seconds,
            resolved_start_offset=start_offset,
        )
        process = open_ffmpeg_pcm(session, start_offset)
        chunk_bytes = int(SAMPLE_RATE * 2 * session.request.chunk_seconds)

        session.ffmpeg_thread = threading.Thread(target=ffmpeg_reader_thread, args=(session, process, chunk_bytes), daemon=True)
        session.ffmpeg_thread.start()

        session.asr_task = threading.Thread(target=asr_worker_thread, args=(session, start_offset), daemon=True)
        session.asr_task.start()

        session.renderer_task = threading.Thread(target=renderer_thread, args=(session,), daemon=True)
        session.renderer_task.start()

        session.seek_watcher_task = threading.Thread(target=seek_watcher_thread, args=(session,), daemon=True)
        session.seek_watcher_task.start()

        while not session.cancel_event.is_set() and (session.asr_task.is_alive() or session.ffmpeg_thread.is_alive()):
            time.sleep(0.5)

        if session.cancel_event.is_set():
            if session.state != "stopped":
                # "stopped" means renderer_thread already recorded a specific
                # reason (e.g. IINA closed) - don't clobber it with a generic
                # message.
                update_session_state(session, "idle", message="Live subtitle processing stopped.")
            queue_event(session, "session_stopped")
        else:
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
            return_code = process.wait(timeout=2) if process.poll() is None else process.returncode
            if return_code not in {0, None}:
                raise BackendUnavailable(stderr or f"ffmpeg exited with status {return_code}")
            update_session_state(session, "completed", message="Subtitle processing completed.")
            queue_event(session, "session_completed", srt_path=session.srt_path)
    except Exception as error:
        update_session_state(session, "failed", last_error=str(error), message=f"Subtitle processing failed: {error}")
        queue_event(session, "session_failed", error=str(error))
    finally:
        session.cancel_event.set()
        terminate_process(process or session.ffmpeg_process)
        session.backend.close()

def stop_session(session: SubtitleSession | None) -> None:
    if session is None:
        return
    session.cancel_event.set()
    terminate_process(session.ffmpeg_process)
    queue_event(session, "stop_requested")

TERMINAL_SESSION_STATES = {"idle", "completed", "stopped", "failed"}
ACTIVE_SESSION_STATES = {"starting", "running"}
MAX_FINISHED_SESSIONS_KEPT = 3


def stop_other_active_sessions(keep_id: str | None = None) -> None:
    # The use case is always "one video, muted, captions on" - never more
    # than one live ASR pipeline at a time. Without this, a session that a
    # client fails to stop (wrong endpoint, crashed process, double-tapped
    # keybind) keeps its ffmpeg + loaded Whisper model running forever
    # alongside every new one, compounding memory/CPU/network use.
    with SESSION_LOCK:
        active = [s for sid, s in SESSIONS.items() if s.state in ACTIVE_SESSION_STATES and sid != keep_id]
    for session in active:
        stop_session(session)


def prune_finished_sessions() -> None:
    # SESSIONS is never otherwise cleared, so a long-running service would
    # accumulate one entry (plus its full caption list) per video watched.
    with SESSION_LOCK:
        finished_ids = [sid for sid, s in SESSIONS.items() if s.state in TERMINAL_SESSION_STATES]
        if len(finished_ids) <= MAX_FINISHED_SESSIONS_KEPT:
            return
        finished_ids.sort(key=lambda sid: SESSIONS[sid].start_time)
        for sid in finished_ids[:-MAX_FINISHED_SESSIONS_KEPT]:
            del SESSIONS[sid]


@app.post("/sessions")
def create_session(request: StartSubtitleRequest) -> dict[str, Any]:
    stop_other_active_sessions()
    prune_finished_sessions()
    session_id = str(uuid.uuid4())
    srt_path = os.path.expanduser(request.srt_path or default_srt_path(session_id))

    try:
        model = normalize_model(request.model)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    backend, info, _checked = select_live_backend(model)
    if backend is None:
        raise HTTPException(status_code=503, detail="No live ASR backend is available.")

    ipc_path = request.iina_ipc_path
    if ipc_path is None:
        short_id = session_id[:8]
        ipc_path = os.path.join(tempfile.gettempdir(), f"vl-{short_id}.sock")

    session = SubtitleSession(
        id=session_id,
        request=request,
        backend=backend,
        backend_info=info,
        srt_path=srt_path,
        mode=request.mode,
        model=model,
        ipc_socket_path=ipc_path
    )
    with SESSION_LOCK:
        SESSIONS[session_id] = session

    return {
        "id": session_id,
        "ipc_socket_path": session.ipc_socket_path,
        "status": session_status_payload(session)
    }

@app.post("/sessions/{session_id}/start")
def start_session(session_id: str) -> dict[str, Any]:
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.state in ("starting", "running"):
        return session_status_payload(session)

    stop_other_active_sessions(keep_id=session_id)
    session.cancel_event.clear()
    update_session_state(
        session,
        "starting",
        processed_until=session.request.start_offset_seconds,
        latency_seconds=None,
        message="Caption pipeline starting...",
    )

    session.pipeline_task = threading.Thread(
        target=run_caption_pipeline,
        args=(session,),
        name=f"videolink-subtitles-{session_id[:8]}",
        daemon=True,
    )
    session.pipeline_task.start()

    return session_status_payload(session)

@app.post("/sessions/{session_id}/stop")
def stop_session_endpoint(session_id: str) -> dict[str, Any]:
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    stop_session(session)
    return session_status_payload(session)

def perform_seek(session_id: str, position_seconds: float) -> dict[str, Any]:
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    stop_session(session)
    # run_caption_pipeline's own wait loop exits as soon as cancel_event is
    # set, without waiting for its worker threads - so joining only
    # pipeline_task lets this proceed to truncate/rewrite the SRT file while
    # asr_worker_thread (or the old renderer/seek-watcher) may still be
    # mid-chunk. Join every worker explicitly so the old generation is fully
    # stopped before anything below touches shared session state. perform_seek
    # can itself be called from seek_watcher_thread, so skip joining whichever
    # thread is currently running this code - Thread.join() on the calling
    # thread raises RuntimeError.
    current_thread = threading.current_thread()
    for worker in (session.pipeline_task, session.ffmpeg_thread, session.asr_task,
                   session.renderer_task, session.seek_watcher_task):
        if worker and worker is not current_thread and worker.is_alive():
            worker.join(timeout=5.0)

    session.request.start_offset_seconds = position_seconds
    session.cursor_seconds = position_seconds
    session.processed_until = position_seconds
    session.captions = [cue for cue in session.captions if cue.start < position_seconds]
    renumber_captions(session.captions)
    write_srt(session.captions, session.srt_path)
    if session.iina_subtitle_track_added:
        reload_iina_subtitle_track(session.ipc_socket_path)
    session.audio_queue = queue.Queue(maxsize=50)
    session.render_queue = queue.Queue()
    session.iina_subtitle_track_added = False
    session.iina_rendering_active = False
    session.first_rendered_caption_at = None
    session.last_iina_position = None
    session.cancel_event.clear()

    return start_session(session_id)


@app.post("/sessions/{session_id}/seek")
def seek_session(session_id: str, request: SeekSubtitleRequest) -> dict[str, Any]:
    return perform_seek(session_id, request.position_seconds)

@app.get("/sessions/{session_id}/status")
def get_session_status(session_id: str) -> dict[str, Any]:
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session_status_payload(session)

@app.get("/sessions/{session_id}/export.srt")
def export_srt(session_id: str) -> str:
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not os.path.exists(session.srt_path):
        raise HTTPException(status_code=404, detail="SRT file not found")

    with open(session.srt_path, "r", encoding="utf-8") as f:
        return f.read()

@app.websocket("/ws/captions/{session_id}")
async def ws_captions(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    with SESSION_LOCK:
        session = SESSIONS.get(session_id)
        if not session:
            await websocket.send_json({
                "type": "session_missing",
                "session_id": session_id,
                "timestamp": time.time(),
            })
            await websocket.close()
            return
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        session.event_queues.append(event_queue)

    await websocket.send_json({"type": "snapshot", "status": session_status_payload(session), "timestamp": time.time()})
    try:
        while True:
            event = await asyncio.to_thread(event_queue.get)
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        with SESSION_LOCK:
            if session is not None:
                session.event_queues = [q for q in session.event_queues if q is not event_queue]


@app.get("/status")
def status() -> dict[str, Any]:
    return service_status_payload()


@app.get("/health")
def health() -> dict[str, Any]:
    payload = service_status_payload()
    payload["status"] = "ok"
    return payload
