from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AutoDub 2.0 API"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    supabase_url: str = ""
    supabase_secret_key: str = ""
    media_bucket: str = "autodub-private-media"
    max_video_upload_bytes: int = 536870912
    max_video_duration_seconds: int = 7200
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: int = 60
    ffmpeg_path: str = "ffmpeg"
    asr_model: str = "small"
    asr_device: str = "cuda"
    asr_compute_type: str = "float16"
    asr_language: str = "zh"
    asr_beam_size: int = 5
    asr_cpu_fallback: bool = True
    asr_model_init_timeout_seconds: int = 600
    asr_first_segment_timeout_seconds: int = 120
    asr_inference_stall_timeout_seconds: int = 120
    asr_inference_timeout_seconds: int = 1800
    asr_vad_filter: bool = True
    asr_vad_min_silence_ms: int = 2000
    asr_timeline_tolerance_ms: int = 250
    asr_max_segments: int = 50000
    asr_worker_poll_seconds: float = 2.0
    asr_lease_seconds: int = 120
    asr_heartbeat_seconds: int = 30
    asr_model_cache_dir: Path = BACKEND_ROOT / ".cache" / "asr" / "models"
    asr_work_dir: Path = BACKEND_ROOT / ".cache" / "asr" / "jobs"

    @field_validator("backend_host")
    @classmethod
    def require_loopback_backend(cls, value: str) -> str:
        if value not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Backend must bind to a loopback address in local development")
        return value

    @field_validator("frontend_origin")
    @classmethod
    def require_local_frontend(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Frontend origin must be localhost in local development")
        return value

    @field_validator("max_video_upload_bytes")
    @classmethod
    def enforce_storage_upload_ceiling(cls, value: int) -> int:
        if value < 1 or value > 536870912:
            raise ValueError("MAX_VIDEO_UPLOAD_BYTES must be between 1 and the 512 MiB Storage ceiling")
        return value

    @field_validator("max_video_duration_seconds")
    @classmethod
    def enforce_video_duration_ceiling(cls, value: int) -> int:
        if value < 1 or value > 7200:
            raise ValueError("MAX_VIDEO_DURATION_SECONDS must be between 1 and the two-hour schema ceiling")
        return value

    @field_validator("asr_device")
    @classmethod
    def validate_asr_device(cls, value: str) -> str:
        if value not in {"cuda", "cpu"}:
            raise ValueError("ASR_DEVICE must be cuda or cpu; device fallback is never automatic")
        return value

    @field_validator("asr_beam_size")
    @classmethod
    def validate_asr_beam_size(cls, value: int) -> int:
        if value < 1 or value > 10:
            raise ValueError("ASR_BEAM_SIZE must be between 1 and 10")
        return value

    @field_validator("asr_worker_poll_seconds")
    @classmethod
    def validate_worker_poll_seconds(cls, value: float) -> float:
        if value < 0.5 or value > 30:
            raise ValueError("ASR_WORKER_POLL_SECONDS must be between 0.5 and 30")
        return value

    @field_validator("asr_lease_seconds")
    @classmethod
    def validate_asr_lease_seconds(cls, value: int) -> int:
        if value < 30 or value > 3600:
            raise ValueError("ASR_LEASE_SECONDS must be between 30 and 3600")
        return value

    @field_validator("asr_heartbeat_seconds")
    @classmethod
    def validate_asr_heartbeat_seconds(cls, value: int) -> int:
        if value < 5 or value > 300:
            raise ValueError("ASR_HEARTBEAT_SECONDS must be between 5 and 300")
        return value

    @model_validator(mode="after")
    def validate_asr_runtime(self) -> "Settings":
        allowed_compute_types = {
            "cpu": {"int8", "int8_float32", "float32"},
            "cuda": {"int8", "int8_float16", "float16", "float32"},
        }
        if self.asr_compute_type not in allowed_compute_types[self.asr_device]:
            raise ValueError("ASR_COMPUTE_TYPE is incompatible with the explicitly selected ASR_DEVICE")
        if self.asr_heartbeat_seconds * 2 >= self.asr_lease_seconds:
            raise ValueError("ASR_HEARTBEAT_SECONDS must be less than half of ASR_LEASE_SECONDS")
        if self.asr_vad_min_silence_ms < 100 or self.asr_vad_min_silence_ms > 10000:
            raise ValueError("ASR_VAD_MIN_SILENCE_MS must be between 100 and 10000")
        if self.asr_timeline_tolerance_ms < 0 or self.asr_timeline_tolerance_ms > 5000:
            raise ValueError("ASR_TIMELINE_TOLERANCE_MS must be between 0 and 5000")
        if self.asr_max_segments < 1 or self.asr_max_segments > 100000:
            raise ValueError("ASR_MAX_SEGMENTS must be between 1 and 100000")
        timeout_values = (
            self.asr_model_init_timeout_seconds,
            self.asr_first_segment_timeout_seconds,
            self.asr_inference_stall_timeout_seconds,
            self.asr_inference_timeout_seconds,
        )
        if any(value < 1 or value > 7200 for value in timeout_values):
            raise ValueError("ASR stage timeouts must be between 1 and 7200 seconds")
        return self

    @property
    def cors_origins(self) -> list[str]:
        origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            self.frontend_origin.rstrip("/"),
        ]
        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
