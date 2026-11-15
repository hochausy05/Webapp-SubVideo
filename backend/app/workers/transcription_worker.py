from __future__ import annotations

import json
import logging
import os
import queue
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.config import get_settings
from app.services.asr import (
    TranscriptionCancelled,
    TranscriptionFailure,
)
from app.services.supabase import Supabase, SupabaseRequestError, eq


logger = logging.getLogger("autodub.asr.worker")


_ASR_STAGE_LABELS = {
    "audio_probe": "Đang kiểm tra metadata video",
    "audio_extraction": "Đang trích xuất audio mono 16 kHz",
    "model_download": "Đang tải model nhận diện",
    "model_initialization": "Đang khởi tạo model Faster-Whisper",
    "inference": "Đang nhận diện lời thoại",
    "segment_generation": "Đang kiểm tra Segments nhận diện",
}


def _format_elapsed(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _claim_retry_delay(failure_count: int, base_seconds: float) -> float:
    exponential = min(60.0, max(1.0, base_seconds) * (2 ** min(max(0, failure_count - 1), 6)))
    return round(min(60.0, exponential * random.uniform(0.8, 1.2)), 2)


class LeaseKeeper:
    def __init__(self, db: Supabase, job: dict[str, Any], worker_id: str) -> None:
        self.db = db
        self.job = job
        self.worker_id = worker_id
        self.settings = get_settings()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.lost_event = threading.Event()
        self.started_at = time.monotonic()
        self.stage_message = "Đang xử lý nhận diện"
        self.stage_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"lease-{job['id'][:8]}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def should_cancel(self) -> bool:
        return self.cancel_event.is_set()

    def set_stage(self, message: str) -> None:
        with self.stage_lock:
            self.stage_message = message
        self._write_progress()

    def _write_progress(self) -> None:
        with self.stage_lock:
            message = self.stage_message
        message = f"{message} · {_format_elapsed(int(time.monotonic() - self.started_at))}"
        try:
            self.db.patch(
                "jobs",
                {"id": eq(self.job["id"]), "project_id": eq(self.job["project_id"]), "status": "eq.running", "worker_id": eq(self.worker_id)},
                {"progress_message": message[:500]},
            )
        except HTTPException:
            self._observe_state()

    def _observe_state(self) -> None:
        try:
            jobs = self.db.select(
                "jobs",
                {"select": "id,status,worker_id,cancel_requested_at", "id": eq(self.job["id"]), "project_id": eq(self.job["project_id"]), "limit": "1"},
            )
            if not jobs or jobs[0]["status"] != "running" or jobs[0].get("worker_id") != self.worker_id:
                self.lost_event.set()
                return
            if jobs[0].get("cancel_requested_at"):
                self.cancel_event.set()
                return
            projects = self.db.select("projects", {"select": "status", "id": eq(self.job["project_id"]), "limit": "1"})
            if not projects or projects[0]["status"] != "active":
                self.cancel_event.set()
        except HTTPException:
            # A transient read failure is not evidence that the lease was lost.
            return

    def _run(self) -> None:
        while not self.stop_event.wait(self.settings.asr_heartbeat_seconds):
            try:
                updated = self.db.patch(
                    "jobs",
                    {
                        "id": eq(self.job["id"]), "project_id": eq(self.job["project_id"]),
                        "status": "eq.running", "worker_id": eq(self.worker_id), "cancel_requested_at": "is.null",
                    },
                    {
                        "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=self.settings.asr_lease_seconds)).isoformat(),
                    },
                )
            except HTTPException:
                self._observe_state()
                continue
            if not updated:
                self._observe_state()
            elif not self.cancel_event.is_set():
                self._write_progress()


class TranscriptionWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Supabase()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"

    def _claim(self) -> dict[str, Any] | None:
        response = self.db.request(
            "POST",
            "/rest/v1/rpc/claim_next_job",
            json={
                "p_worker_id": self.worker_id,
                "p_lease_seconds": self.settings.asr_lease_seconds,
                "p_kind": "transcription",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or (rows and (not isinstance(rows[0], dict) or rows[0].get('kind') != 'transcription')):
            raise ValueError('Invalid transcription claim response')
        return rows[0] if rows else None

    def run_forever(self) -> None:
        logger.info("ASR worker online worker=%s device=%s model=%s", self.worker_id, self.settings.asr_device, self.settings.asr_model)
        claim_failures = 0
        readiness_reported = False
        while True:
            try:
                job = self._claim()
            except SupabaseRequestError as exc:
                claim_failures += 1
                if not exc.retryable:
                    logger.error(
                        "job claim failed rpc=claim_next_job http_status=%s error_code=%s error_message=%r decision=stop_worker retry_seconds=0",
                        exc.provider_http_status or exc.status_code,
                        exc.provider_code or "unknown",
                        exc.provider_message,
                    )
                    raise SystemExit(2) from None
                delay = _claim_retry_delay(claim_failures, self.settings.asr_worker_poll_seconds)
                logger.warning(
                    "job claim failed rpc=claim_next_job http_status=%s error_code=%s error_message=%r decision=retry retry_seconds=%.2f",
                    exc.provider_http_status or exc.status_code,
                    exc.provider_code or "network_error",
                    exc.provider_message,
                    delay,
                )
                time.sleep(delay)
                continue
            except Exception as exc:
                logger.error(
                    "job claim failed rpc=claim_next_job http_status=unknown error_code=%s error_message=%r decision=stop_worker retry_seconds=0",
                    type(exc).__name__,
                    "Unexpected RPC response or local claim failure.",
                )
                raise SystemExit(2) from None
            claim_failures = 0
            if not readiness_reported:
                logger.info("RUNTIME_READY kind=transcription rpc=claim_next_job supabase=connected")
                readiness_reported = True
            if job is None:
                time.sleep(self.settings.asr_worker_poll_seconds)
                continue
            logger.info("claimed transcription job=%s attempt=%s", job["id"], job["attempt_count"])
            self._process(job)

    def _finish(self, job: dict[str, Any], worker_id: str, status: str, code: str | None = None, message: str | None = None) -> None:
        self.db.request(
            "POST",
            "/rest/v1/rpc/finish_transcription_job",
            json={
                "p_project_id": job["project_id"], "p_job_id": job["id"], "p_worker_id": worker_id,
                "p_status": status, "p_error_code": code, "p_error_message": message,
            },
            timeout=60,
        )

    def _load_source_asset(self, job: dict[str, Any]) -> dict[str, Any]:
        input_metadata = job.get("input_metadata") or {}
        asset_id = input_metadata.get("source_asset_id")
        if not asset_id:
            raise TranscriptionFailure("job_input_invalid", "Transcription Job không có tham chiếu Asset nguồn hợp lệ.")
        rows = self.db.select(
            "media_assets",
            {
                "select": "id,project_id,status,storage_bucket,storage_path,size_bytes,duration_us,sha256",
                "id": eq(asset_id), "project_id": eq(job["project_id"]), "limit": "1",
            },
        )
        if not rows or rows[0]["status"] != "ready":
            raise TranscriptionFailure("source_asset_unavailable", "Video nguồn đã bị xóa hoặc không còn ở trạng thái sẵn sàng.")
        asset = rows[0]
        if asset.get("sha256") != input_metadata.get("source_sha256") or asset.get("duration_us") != input_metadata.get("source_duration_us"):
            raise TranscriptionFailure("source_asset_changed", "Metadata checksum hoặc thời lượng video đã thay đổi từ lúc tạo Job.")
        return asset

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _run_asr_subprocess(
        self,
        source_path: Path,
        job: dict[str, Any],
        keeper: LeaseKeeper,
        device: str,
        compute_type: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        inputs = job["input_metadata"]
        command = [
            sys.executable,
            "-m",
            "app.asr.cli",
            "--input",
            str(source_path),
            "--model",
            str(inputs["model"]),
            "--device",
            device,
            "--compute-type",
            compute_type,
            "--language",
            str(inputs.get("language") or "auto"),
            "--beam-size",
            str(int(inputs["beam_size"])),
            "--work-dir",
            str(source_path.parent),
            "--ffmpeg-path",
            self.settings.ffmpeg_path,
            "--ffprobe-path",
            self.settings.ffprobe_path,
            "--vad-min-silence-ms",
            str(int(inputs.get("vad_min_silence_ms", self.settings.asr_vad_min_silence_ms))),
            "--max-segments",
            str(self.settings.asr_max_segments),
            "--timeline-tolerance-ms",
            str(self.settings.asr_timeline_tolerance_ms),
        ]
        command.append("--vad-filter" if inputs["vad_filter"] else "--no-vad-filter")
        safe_env_names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        process_env = {
            key: value for key, value in os.environ.items()
            if key.upper() in safe_env_names or key.upper().startswith("CUDA_PATH")
        }
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        messages: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=128)
        stderr_lines: deque[str] = deque(maxlen=40)

        def read_stdout() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    messages.put(("line", line))
            finally:
                messages.put(("eof", None))

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip())

        stdout_thread = threading.Thread(target=read_stdout, name="asr-stdout", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name="asr-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        started = time.monotonic()
        model_stage_started: float | None = None
        inference_started: float | None = None
        first_segment_at: float | None = None
        last_progress_at: float | None = None
        last_persisted_progress_at: float | None = None
        eof = False
        segments: list[dict[str, Any]] = []
        result_metadata: dict[str, Any] | None = None
        child_error: TranscriptionFailure | None = None
        try:
            while not eof or process.poll() is None:
                now = time.monotonic()
                if keeper.should_cancel():
                    self._terminate_process_tree(process)
                    raise TranscriptionCancelled()
                if keeper.lost_event.is_set():
                    self._terminate_process_tree(process)
                    raise TranscriptionFailure("lease_lost", "Worker lease đã được chuyển sang lần chạy khác; kết quả cũ không được ghi.")

                if model_stage_started is not None and inference_started is None and now - model_stage_started > self.settings.asr_model_init_timeout_seconds:
                    self._terminate_process_tree(process)
                    raise TranscriptionFailure("asr_timeout", "Tải hoặc khởi tạo model vượt thời gian cho phép; thử lại hoặc kiểm tra kết nối và model cache.")
                if inference_started is not None:
                    if now - inference_started > self.settings.asr_inference_timeout_seconds:
                        self._terminate_process_tree(process)
                        raise TranscriptionFailure("asr_timeout", "Nhận diện vượt thời gian tối đa; tiến trình inference đã được dừng an toàn.")
                    if first_segment_at is None and now - inference_started > self.settings.asr_first_segment_timeout_seconds:
                        self._terminate_process_tree(process)
                        raise TranscriptionFailure("asr_timeout", "Không nhận được Segment đầu tiên trước thời hạn; tiến trình inference đã được dừng an toàn.")
                    if first_segment_at is not None and last_progress_at is not None and now - last_progress_at > self.settings.asr_inference_stall_timeout_seconds:
                        self._terminate_process_tree(process)
                        raise TranscriptionFailure("asr_timeout", "Inference không tạo tiến triển Segment trong thời hạn; tiến trình đã được dừng an toàn.")

                try:
                    kind, line = messages.get(timeout=0.4)
                except queue.Empty:
                    continue
                if kind == "eof":
                    eof = True
                    continue
                try:
                    event = json.loads(line or "")
                except json.JSONDecodeError as exc:
                    self._terminate_process_tree(process)
                    raise TranscriptionFailure("asr_protocol_error", "Tiến trình ASR trả về dữ liệu không hợp lệ.") from exc
                event_type = event.get("event")
                now = time.monotonic()
                if event_type == "stage":
                    stage = event.get("stage")
                    if stage in {"model_download", "model_initialization"} and model_stage_started is None:
                        model_stage_started = now
                    if stage == "inference":
                        inference_started = now
                        last_progress_at = now
                    keeper.set_stage(_ASR_STAGE_LABELS.get(stage, "Đang xử lý ASR"))
                elif event_type == "progress":
                    count = event.get("segment_count")
                    if isinstance(count, int):
                        last_progress_at = now
                        if count == 1 and first_segment_at is None:
                            first_segment_at = now
                        if count == 1 or last_persisted_progress_at is None or now - last_persisted_progress_at >= 5:
                            keeper.set_stage(f"Đang nhận diện lời thoại · đã nhận {count} Segment")
                            last_persisted_progress_at = now
                elif event_type == "segment":
                    segments.append({key: event[key] for key in ("position", "start_us", "end_us", "source_text", "speaker_label")})
                elif event_type == "complete":
                    result_metadata = event.get("result_metadata")
                    if not isinstance(result_metadata, dict) or event.get("segment_count") != len(segments):
                        child_error = TranscriptionFailure("asr_protocol_error", "Số Segment trong kết quả ASR không khớp.")
                elif event_type == "error":
                    code = str(event.get("code", "asr_subprocess_failed"))[:80]
                    message = str(event.get("message", "Faster-Whisper process failed."))[:500]
                    child_error = TranscriptionFailure(code, message)

            process.wait(timeout=5)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            diagnostic = next((line for line in reversed(stderr_lines) if "ASR failed code=" in line), None)
            if diagnostic:
                logger.warning("ASR subprocess diagnostic job=%s detail=%s", job["id"], diagnostic[:700])
            if child_error is not None:
                raise child_error
            if process.returncode != 0 or result_metadata is None:
                raise TranscriptionFailure("asr_subprocess_failed", "Tiến trình Faster-Whisper kết thúc không có kết quả hợp lệ.")
            if not segments or any(segment.get("position") != index for index, segment in enumerate(segments)):
                raise TranscriptionFailure("transcript_validation_failed", "Thứ tự Segment từ tiến trình ASR không hợp lệ.")
            logger.info(
                "ASR subprocess finished job=%s device=%s compute=%s segments=%s timings=%s elapsed_seconds=%.2f",
                job["id"], device, compute_type, len(segments), result_metadata.get("inference_timings"), time.monotonic() - started,
            )
            result_metadata["device"] = device
            result_metadata["compute_type"] = compute_type
            result_metadata["source_asset_id"] = inputs["source_asset_id"]
            result_metadata["source_sha256"] = inputs["source_sha256"]
            result_metadata["device_fallback"] = None
            return segments, result_metadata
        finally:
            if process.poll() is None:
                self._terminate_process_tree(process)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)

    def _run_asr_with_fallback(
        self,
        source_path: Path,
        job: dict[str, Any],
        keeper: LeaseKeeper,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        inputs = job["input_metadata"]
        device = str(inputs.get("device", self.settings.asr_device))
        compute_type = str(inputs.get("compute_type", self.settings.asr_compute_type))
        try:
            return self._run_asr_subprocess(source_path, job, keeper, device, compute_type)
        except TranscriptionFailure as exc:
            cuda_failure = device == "cuda" and exc.code.startswith("asr_cuda_")
            timeout_fallback = device == "cuda" and exc.code == "asr_timeout"
            if not self.settings.asr_cpu_fallback or not (cuda_failure or timeout_fallback):
                raise
            if keeper.should_cancel() or keeper.lost_event.is_set():
                raise
            logger.warning("CUDA ASR attempt failed job=%s code=%s; restarting in CPU int8 subprocess", job["id"], exc.code)
            keeper.set_stage("CUDA không khả dụng · đang chuyển sang CPU int8")
            segments, metadata = self._run_asr_subprocess(source_path, job, keeper, "cpu", "int8")
            metadata["device_fallback"] = {"from": "cuda", "to": "cpu", "reason": exc.code}
            return segments, metadata

    def _process(self, job: dict[str, Any]) -> None:
        started = time.monotonic()
        worker_id = job["worker_id"]
        keeper = LeaseKeeper(self.db, job, worker_id)
        keeper.start()
        error: TranscriptionFailure | None = None
        final_status = "failed"
        try:
            keeper.set_stage("Đang chuẩn bị video nguồn")
            asset = self._load_source_asset(job)
            self.settings.asr_work_dir.mkdir(parents=True, exist_ok=True)
            job_dir = self.settings.asr_work_dir / f"autodub-asr-{job['id']}-{job['attempt_count']}"
            job_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
            source_path = job_dir / "source-video.bin"
            audio_path = job_dir / "source-audio.wav"
            try:
                keeper.set_stage("Đang tải video nguồn từ Storage")
                received, checksum = self.db.download_storage_object(
                    asset["storage_bucket"], asset["storage_path"], source_path, int(asset["size_bytes"])
                )
                if received != int(asset["size_bytes"]) or checksum != asset["sha256"]:
                    raise TranscriptionFailure("source_checksum_mismatch", "Checksum video tải từ Storage không trùng Asset đã xác minh.")
                keeper._observe_state()
                if keeper.should_cancel():
                    raise TranscriptionCancelled()

                segments, result_metadata = self._run_asr_with_fallback(source_path, job, keeper)
                tolerance_us = int(job["input_metadata"].get("timeline_tolerance_us", self.settings.asr_timeline_tolerance_ms * 1000))
                if int(result_metadata.get("measured_source_duration_us", 0)) > int(job["input_metadata"]["source_duration_us"]) + tolerance_us:
                    raise TranscriptionFailure("source_duration_changed", "Thời lượng video không còn khớp metadata đã xác minh.")
                if keeper.should_cancel():
                    raise TranscriptionCancelled()
                if keeper.lost_event.is_set():
                    raise TranscriptionFailure("lease_lost", "Worker lease đã được chuyển sang lần chạy khác; kết quả cũ không được ghi.")

                keeper.set_stage(f"Đang lưu {len(segments)} Segment đã kiểm tra")
                persistence_started = time.monotonic()
                self.db.request(
                    "POST",
                    "/rest/v1/rpc/complete_transcription_job",
                    json={
                        "p_project_id": job["project_id"],
                        "p_job_id": job["id"],
                        "p_worker_id": worker_id,
                        "p_source_asset_id": asset["id"],
                        "p_segments": segments,
                        "p_result_metadata": result_metadata,
                    },
                    timeout=300,
                )
                logger.info("database persistence completed job=%s seconds=%.3f", job["id"], time.monotonic() - persistence_started)
                logger.info("completed transcription job=%s segments=%s elapsed=%s", job["id"], len(segments), _format_elapsed(int(time.monotonic() - started)))
                return
            finally:
                # These paths are generated inside the worker-owned cache root.
                for path in (audio_path, source_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    job_dir.rmdir()
                except OSError:
                    pass
        except TranscriptionCancelled:
            final_status = "cancelled"
            logger.info("cancelled transcription job=%s", job["id"])
        except TranscriptionFailure as exc:
            error = exc
            if exc.code == "lease_lost":
                keeper.lost_event.set()
                logger.warning("abandoned stale transcription result job=%s", job["id"])
            else:
                logger.warning("transcription failed job=%s code=%s", job["id"], exc.code)
        except HTTPException as exc:
            if keeper.should_cancel():
                final_status = "cancelled"
                logger.info("cancelled transcription job=%s", job["id"])
            else:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                provider_code = detail.get("code")
                if provider_code == "source_storage_unavailable":
                    error = TranscriptionFailure("source_download_failed", "Không tải được video nguồn từ Storage riêng tư.")
                elif provider_code == "source_size_mismatch":
                    error = TranscriptionFailure("source_checksum_mismatch", "Kích thước video tải từ Storage không trùng Asset đã xác minh.")
                else:
                    error = TranscriptionFailure("database_persist_failed", "Không lưu được trạng thái hoặc kết quả nhận diện vào Supabase.")
                logger.warning("transcription request failed job=%s http_status=%s", job["id"], exc.status_code)
        except Exception:
            error = TranscriptionFailure("worker_internal_error", "Worker gặp lỗi nội bộ khi xử lý Job.")
            logger.error("unexpected transcription failure job=%s", job["id"])
        finally:
            keeper._observe_state()
            keeper.stop()

        if keeper.lost_event.is_set():
            return
        if keeper.should_cancel():
            final_status = "cancelled"
        try:
            self._finish(
                job, worker_id, final_status,
                error.code if error and final_status == "failed" else None,
                error.message if error and final_status == "failed" else None,
            )
        except HTTPException as exc:
            logger.warning("could not finalize transcription job=%s http_status=%s; lease recovery will handle it", job["id"], exc.status_code)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if hasattr(signal, 'SIGBREAK'):
        def stop_on_break(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, stop_on_break)
    try:
        TranscriptionWorker().run_forever()
    except KeyboardInterrupt:
        logger.info('ASR Worker stopped')


if __name__ == "__main__":
    main()
