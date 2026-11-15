from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download

from app.core.config import Settings
from app.asr.runtime import prepare_windows_cuda_dll_search_path
from app.services.asr import (
    TranscriptionFailure,
    _extract_audio,
    _load_model,
    _probe_source,
    transcribe_audio,
)


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated Faster-Whisper transcription.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--compute-type", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad-filter", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    parser.add_argument("--vad-min-silence-ms", type=int, default=2000)
    parser.add_argument("--max-segments", type=int, default=50000)
    parser.add_argument("--timeline-tolerance-ms", type=int, default=250)
    return parser.parse_args()


def main() -> int:
    args = _args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    source = args.input.resolve(strict=True)
    audio = args.work_dir / "source-audio.wav"
    settings = Settings(
        _env_file=None,
        asr_model=args.model,
        asr_device=args.device,
        asr_compute_type=args.compute_type,
        asr_beam_size=args.beam_size,
        asr_vad_filter=args.vad_filter,
        ffmpeg_path=args.ffmpeg_path,
        ffprobe_path=args.ffprobe_path,
        asr_vad_min_silence_ms=args.vad_min_silence_ms,
        asr_max_segments=args.max_segments,
        asr_timeline_tolerance_ms=args.timeline_tolerance_ms,
    )
    timings: dict[str, float] = {}
    try:
        _emit({"event": "stage", "stage": "audio_probe"})
        started = time.monotonic()
        audio_info = _probe_source(source, settings)
        timings["audio_probe_seconds"] = time.monotonic() - started

        _emit({"event": "stage", "stage": "audio_extraction"})
        started = time.monotonic()
        _extract_audio(source, audio, settings, lambda: False, audio_info.stream_index)
        timings["audio_extraction_seconds"] = time.monotonic() - started

        model_candidate = Path(args.model)
        if model_candidate.is_dir():
            model_location = str(model_candidate.resolve())
            timings["model_download_seconds"] = 0.0
        else:
            repo_id = args.model if "/" in args.model else f"Systran/faster-whisper-{args.model}"
            _emit({"event": "stage", "stage": "model_download"})
            started = time.monotonic()
            try:
                model_location = snapshot_download(repo_id=repo_id, cache_dir=str(settings.asr_model_cache_dir))
            except Exception as exc:
                raise TranscriptionFailure(
                    "asr_model_download_failed",
                    "Không tải được model Faster-Whisper. Kiểm tra kết nối tải model và dung lượng đĩa rồi thử lại.",
                ) from exc
            timings["model_download_seconds"] = time.monotonic() - started

        _emit({"event": "stage", "stage": "model_initialization"})
        started = time.monotonic()
        dll_directories = prepare_windows_cuda_dll_search_path() if args.device == "cuda" else []
        _emit({"event": "runtime", "registered_cuda_dll_directories": dll_directories})
        model, _ = _load_model(settings, model_location)
        timings["model_initialization_seconds"] = time.monotonic() - started

        source_sha256 = _sha256(source)
        job_input = {
            "source_asset_id": "standalone",
            "source_sha256": source_sha256,
            "source_duration_us": audio_info.duration_us,
            "timeline_tolerance_us": settings.asr_timeline_tolerance_ms * 1000,
            "model": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
            "language": args.language,
            "beam_size": args.beam_size,
            "vad_filter": args.vad_filter,
            "vad_min_silence_ms": settings.asr_vad_min_silence_ms,
        }
        _emit({"event": "stage", "stage": "inference"})
        inference_started = time.monotonic()
        first_segment_seconds: float | None = None

        def report_progress(message: str) -> None:
            nonlocal first_segment_seconds
            match = re.search(r"(\d+) Segment", message)
            count = int(match.group(1)) if match else None
            if count == 1 and first_segment_seconds is None:
                first_segment_seconds = time.monotonic() - inference_started
            _emit({
                "event": "progress",
                "stage": "inference",
                "segment_count": count,
                "first_segment_seconds": first_segment_seconds if count == 1 else None,
            })

        segments, metadata = transcribe_audio(
            model,
            audio,
            audio_info,
            job_input,
            settings,
            lambda: False,
            report_progress,
        )
        timings["inference_seconds"] = time.monotonic() - inference_started
        metadata["measured_source_duration_us"] = audio_info.duration_us
        _emit({"event": "stage", "stage": "segment_generation"})
        for segment in segments:
            _emit({"event": "segment", **segment})
        metadata["inference_timings"] = {
            **timings,
            "first_segment_seconds": first_segment_seconds,
        }
        _emit({
            "event": "complete",
            "segment_count": len(segments),
            "detected_language": metadata.get("detected_language"),
            "language_probability": metadata.get("language_probability"),
            "model": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
            "source_duration_us": audio_info.duration_us,
            "audio_timeline_offset_us": audio_info.timeline_offset_us,
            "first_segment_seconds": first_segment_seconds,
            "timings": timings,
            "result_metadata": metadata,
        })
        return 0
    except Exception as exc:
        if isinstance(exc, TranscriptionFailure):
            code, message = exc.code, exc.message
        else:
            code, message = "asr_unexpected_error", "Unexpected Faster-Whisper process failure."
        cause = exc.__cause__
        if cause is not None:
            diagnostic = " ".join(str(cause).split())[:500]
            diagnostic = re.sub(r"https?://\S+", "[URL redacted]", diagnostic, flags=re.IGNORECASE)
            diagnostic = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", diagnostic)
            diagnostic = re.sub(r"(?i)\b(?:sb_(?:secret|publishable)_|service[_ -]?role[_ -]?key)\S*", "[key redacted]", diagnostic)
            diagnostic = re.sub(r"(?i)\b(authorization|\w*key|secret|password|token)\s*[:=]\s*\S+", r"\1=[redacted]", diagnostic)
            logging.getLogger("autodub.asr.cli").error(
                "ASR failed code=%s cause_type=%s cause=%s", code, type(cause).__name__, diagnostic
            )
        else:
            logging.getLogger("autodub.asr.cli").error("ASR failed code=%s message=%s", code, message)
        _emit({"event": "error", "code": code, "message": message})
        return 1
    finally:
        audio.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(main())
