from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Iterator

from app.core.config import Settings


class TranscriptionFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TranscriptionCancelled(Exception):
    pass


@dataclass(frozen=True)
class AudioInfo:
    stream_index: int
    duration_us: int
    timeline_offset_us: int


def _seconds_to_us(value: Any) -> int:
    try:
        return int((Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TranscriptionFailure("transcript_validation_failed", "FFprobe or Faster-Whisper returned an invalid timestamp.") from exc


def _probe_source(path: Path, settings: Settings) -> AudioInfo:
    command = [
        settings.ffprobe_path, "-v", "error", "-show_entries",
        "format=duration,start_time:stream=index,codec_type,start_time,duration",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=settings.ffprobe_timeout_seconds)
        probe = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise TranscriptionFailure("ffprobe_failed", "FFprobe could not inspect the verified source video.") from exc

    streams = probe.get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if audio is None:
        raise TranscriptionFailure("no_audio_stream", "Video nguồn không có audio stream để nhận diện.")
    if video is None:
        raise TranscriptionFailure("source_video_invalid", "Video nguồn không còn chứa video stream hợp lệ.")

    try:
        duration_us = _seconds_to_us(probe["format"]["duration"])
        format_start = Decimal(str(probe.get("format", {}).get("start_time", "0")))
        audio_start = Decimal(str(audio.get("start_time", format_start)))
        video_start = Decimal(str(video.get("start_time", format_start)))
        offset_us = int(((audio_start - video_start) * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        stream_index = int(audio["index"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise TranscriptionFailure("ffprobe_failed", "FFprobe returned incomplete stream timing metadata.") from exc
    if duration_us < 1:
        raise TranscriptionFailure("source_video_invalid", "FFprobe reported an invalid source duration.")
    return AudioInfo(stream_index, duration_us, offset_us)


def _extract_audio(source: Path, destination: Path, settings: Settings, cancelled: Callable[[], bool], stream_index: int) -> None:
    command = [
        settings.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", f"0:{stream_index}", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", str(destination),
    ]
    try:
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr)
            while process.poll() is None:
                if cancelled():
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise TranscriptionCancelled()
                time.sleep(0.5)
            if process.returncode != 0:
                stderr.seek(0, 2)
                stderr.seek(max(0, stderr.tell() - 8192))
                diagnostic = stderr.read(8192).decode("utf-8", errors="replace")
                code = "no_audio_stream" if "matches no streams" in diagnostic.lower() else "ffmpeg_failed"
                message = "FFmpeg không trích xuất được audio từ video nguồn." if code == "ffmpeg_failed" else "Video nguồn không có audio stream để nhận diện."
                raise TranscriptionFailure(code, message)
    except TranscriptionCancelled:
        raise
    except TranscriptionFailure:
        raise
    except OSError as exc:
        raise TranscriptionFailure("ffmpeg_unavailable", "Không chạy được FFmpeg. Kiểm tra FFmpeg và FFmpeg_PATH trong cấu hình backend.") from exc
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise TranscriptionFailure("ffmpeg_failed", "FFmpeg không tạo được audio PCM hợp lệ.")


def _load_model(settings: Settings, model_location: str | None = None):
    try:
        import ctranslate2
    except Exception as exc:
        raise TranscriptionFailure("asr_runtime_missing", "CTranslate2 chưa được cài đúng trong backend/.venv.") from exc

    try:
        supported_types = ctranslate2.get_supported_compute_types(settings.asr_device)
        if settings.asr_compute_type not in supported_types:
            raise TranscriptionFailure(
                "asr_compute_type_unsupported",
                f"CTranslate2 không hỗ trợ compute type {settings.asr_compute_type} trên {settings.asr_device}; hãy chọn cấu hình tương thích rõ ràng.",
            )
        if settings.asr_device == "cuda" and ctranslate2.get_cuda_device_count() < 1:
            raise TranscriptionFailure(
                "asr_cuda_unavailable",
                "CTranslate2 chưa tìm thấy CUDA GPU hoặc runtime DLL; worker có thể chuyển sang CPU int8 nếu ASR_CPU_FALLBACK được bật.",
            )
    except TranscriptionFailure:
        raise
    except Exception as exc:
        raise TranscriptionFailure(
            "asr_cuda_unavailable" if settings.asr_device == "cuda" else "asr_runtime_unavailable",
            "Không khởi tạo được runtime CTranslate2 theo thiết bị đã cấu hình.",
        ) from exc

    try:
        from faster_whisper import WhisperModel

        settings.asr_model_cache_dir.mkdir(parents=True, exist_ok=True)
        model = WhisperModel(
            model_location or settings.asr_model,
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
            download_root=str(settings.asr_model_cache_dir),
        )
    except Exception as exc:
        message = str(exc).lower()
        if settings.asr_device == "cuda" and any(library in message for library in ("cublas64_12.dll", "cudnn", "cublas")):
            raise TranscriptionFailure(
                "asr_cuda_runtime_missing",
                "Thiếu thư viện CUDA/cuBLAS/cuDNN tương thích trên Windows.",
            ) from exc
        message = "Không tải hoặc khởi tạo được model Faster-Whisper. Kiểm tra kết nối tải model, dung lượng đĩa và compute type đã chọn."
        error = TranscriptionFailure("asr_model_load_failed", message)
        error.__cause__ = exc
        raise error
    return model, ctranslate2


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def transcribe_audio(
    model: Any,
    audio_path: Path,
    audio_info: AudioInfo,
    job_input: dict[str, Any],
    settings: Settings,
    cancelled: Callable[[], bool],
    progress: Callable[[str], None],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    language = job_input.get("language")
    if language == "auto":
        language = None
    vad_parameters = {"min_silence_duration_ms": settings.asr_vad_min_silence_ms} if settings.asr_vad_filter else None
    try:
        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=int(job_input["beam_size"]),
            vad_filter=bool(job_input["vad_filter"]),
            vad_parameters=vad_parameters,
            word_timestamps=False,
        )
        output: list[dict[str, Any]] = []
        duplicate_positions: list[list[int]] = []
        previous_text = ""
        previous_position: int | None = None
        previous_start = -1
        for segment in segments:  # Faster-Whisper runs inference while its result generator is consumed.
            if cancelled():
                raise TranscriptionCancelled()
            text = str(segment.text).strip()
            if not text or len(text) > 20000:
                raise TranscriptionFailure("transcript_validation_failed", "Faster-Whisper trả về Segment rỗng hoặc vượt giới hạn văn bản.")
            start_us = _seconds_to_us(segment.start) + audio_info.timeline_offset_us
            end_us = _seconds_to_us(segment.end) + audio_info.timeline_offset_us
            if start_us < 0 or end_us <= start_us or end_us > audio_info.duration_us + settings.asr_timeline_tolerance_ms * 1000:
                raise TranscriptionFailure("transcript_validation_failed", "Một Segment có timestamp nằm ngoài timeline video nguồn.")
            if start_us < previous_start:
                raise TranscriptionFailure("transcript_validation_failed", "Thứ tự timestamp Segment từ model không tăng theo timeline nguồn.")
            position = len(output)
            normalized = _normalize_text(text)
            if normalized and normalized == previous_text and previous_position is not None:
                duplicate_positions.append([previous_position, position])
            output.append({
                "position": position,
                "start_us": start_us,
                "end_us": end_us,
                "source_text": text,
                "speaker_label": None,
            })
            previous_text = normalized
            previous_position = position
            previous_start = start_us
            if position == 0 or (position + 1) % 25 == 0:
                progress(f"Đang nhận diện · đã thu được {position + 1} Segment")
            if len(output) > settings.asr_max_segments:
                raise TranscriptionFailure("transcript_segment_limit", "Transcript vượt giới hạn Segment an toàn của cấu hình.")
    except TranscriptionCancelled:
        raise
    except TranscriptionFailure:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if "out of memory" in message or "cuda_error_out_of_memory" in message:
            raise TranscriptionFailure(
                "asr_cuda_out_of_memory",
                "GPU hết VRAM khi nhận diện. Worker có thể thử lại bằng CPU int8 nếu ASR_CPU_FALLBACK được bật.",
            ) from exc
        if any(library in message for library in ("cublas64_12.dll", "cudnn", "cublas")):
            raise TranscriptionFailure(
                "asr_cuda_runtime_missing",
                "Thiếu thư viện CUDA/cuBLAS/cuDNN tương thích trên Windows. Worker sẽ thử CPU int8 nếu ASR_CPU_FALLBACK được bật; nếu không, hãy cài CUDA 12 và cuDNN 9 phù hợp với CTranslate2.",
            ) from exc
        code = "asr_cuda_inference_failed" if settings.asr_device == "cuda" else "asr_inference_failed"
        raise TranscriptionFailure(code, "Faster-Whisper gặp lỗi trong khi nhận diện audio.") from exc

    if not output:
        raise TranscriptionFailure("no_speech_detected", "Không phát hiện được lời thoại. Transcript cũ (nếu có) vẫn được giữ nguyên.")
    progress(f"Đã nhận diện {len(output)} Segment · đang kiểm tra và lưu kết quả")
    result_metadata = {
        "schema_version": 1,
        "model": job_input["model"],
        "compute_type": job_input["compute_type"],
        "device": job_input["device"],
        "configured_language": job_input["language"],
        "detected_language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "faster_whisper_version": _package_version("faster-whisper"),
        "ctranslate2_version": _package_version("ctranslate2"),
        "source_asset_id": job_input["source_asset_id"],
        "source_sha256": job_input["source_sha256"],
        "source_duration_us": job_input["source_duration_us"],
        "audio_timeline_offset_us": audio_info.timeline_offset_us,
        "audio_sample_rate_hz": 16000,
        "audio_channels": 1,
        "beam_size": job_input["beam_size"],
        "vad_filter": job_input["vad_filter"],
        "vad_min_silence_ms": job_input["vad_min_silence_ms"],
        "segment_count": len(output),
        "suspicious_adjacent_duplicate_positions": duplicate_positions[:100],
    }
    return output, result_metadata
