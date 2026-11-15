from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.core.config import get_settings
from app.services.supabase import Supabase, eq


router = APIRouter()


def _project(db: Supabase, project_id: uuid.UUID) -> dict[str, Any]:
    rows = db.select(
        "projects",
        {"select": "id,status,source_video_asset_id,source_language,active_transcription_job_id", "id": eq(str(project_id)), "limit": "1"},
    )
    if not rows:
        raise HTTPException(404, detail={"code": "project_not_found", "message": "Project không tồn tại."})
    return rows[0]


def _job(db: Supabase, project_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    rows = db.select(
        "jobs",
        {"select": "*", "project_id": eq(str(project_id)), "id": eq(str(job_id)), "kind": "eq.transcription", "limit": "1"},
    )
    if not rows:
        raise HTTPException(404, detail={"code": "transcription_not_found", "message": "Không tìm thấy lần nhận diện này."})
    return rows[0]


def _active_job(db: Supabase, project_id: uuid.UUID) -> dict[str, Any] | None:
    rows = db.select(
        "jobs",
        {
            "select": "*", "project_id": eq(str(project_id)), "kind": "eq.transcription",
            "status": "in.(queued,running)", "order": "created_at.desc,id.desc", "limit": "1",
        },
    )
    return rows[0] if rows else None


def _transcription_input(project: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    cfg = get_settings()
    language = cfg.asr_language
    if language == "auto" and project.get("source_language") not in (None, "und"):
        language = project["source_language"]
    return {
        "schema_version": 1,
        "source_asset_id": asset["id"],
        "source_sha256": asset["sha256"],
        "source_duration_us": asset["duration_us"],
        "source_language": project.get("source_language", "und"),
        "model": cfg.asr_model,
        "device": cfg.asr_device,
        "compute_type": cfg.asr_compute_type,
        "language": language,
        "beam_size": cfg.asr_beam_size,
        "vad_filter": cfg.asr_vad_filter,
        "vad_min_silence_ms": cfg.asr_vad_min_silence_ms,
        "timeline_tolerance_us": cfg.asr_timeline_tolerance_ms * 1000,
    }


@router.post("/projects/{project_id}/transcriptions", status_code=202)
def start_transcription(project_id: uuid.UUID) -> dict[str, Any]:
    db = Supabase()
    project = _project(db, project_id)
    if project["status"] != "active":
        raise HTTPException(409, detail={"code": "project_deleting", "message": "Project đang được xóa; không thể bắt đầu nhận diện."})
    asset_id = project.get("source_video_asset_id")
    if not asset_id:
        raise HTTPException(409, detail={"code": "source_video_missing", "message": "Hãy tải video nguồn lên trước khi nhận diện."})
    assets = db.select(
        "media_assets",
        {"select": "id,project_id,status,storage_bucket,storage_path,size_bytes,duration_us,sha256", "id": eq(asset_id), "project_id": eq(str(project_id)), "limit": "1"},
    )
    if not assets or assets[0]["status"] != "ready" or not assets[0].get("sha256") or not assets[0].get("duration_us"):
        raise HTTPException(409, detail={"code": "source_video_unverified", "message": "Video nguồn chưa được xác minh đầy đủ; hãy làm mới Project rồi thử lại."})

    active = _active_job(db, project_id)
    if active:
        return {"job": active, "reused": True}

    input_metadata = _transcription_input(project, assets[0])
    fingerprint = hashlib.sha256(json.dumps(input_metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    idempotency_key = f"transcription:{asset_id}:{fingerprint}"
    row = {
        "project_id": str(project_id),
        "kind": "transcription",
        "status": "queued",
        "progress": 0,
        "progress_message": "Đang chờ Worker nhận Job",
        "input_fingerprint": fingerprint,
        "idempotency_key": idempotency_key,
        "input_metadata": input_metadata,
    }
    try:
        created = db.insert("jobs", row)
        return {"job": created, "reused": False}
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # The partial unique index is the race-safe authority; reread its winner.
        active = _active_job(db, project_id)
        if active:
            return {"job": active, "reused": True}
        raise


@router.get("/projects/{project_id}/transcriptions")
def transcription_status(project_id: uuid.UUID) -> dict[str, Any]:
    db = Supabase()
    project = _project(db, project_id)
    latest_rows = db.select(
        "jobs",
        {"select": "*", "project_id": eq(str(project_id)), "kind": "eq.transcription", "order": "created_at.desc,id.desc", "limit": "1"},
    )
    active_job = _job(db, project_id, uuid.UUID(project["active_transcription_job_id"])) if project.get("active_transcription_job_id") else None
    return {"active_job": active_job, "latest_job": latest_rows[0] if latest_rows else None}


@router.post("/projects/{project_id}/transcriptions/{job_id}/cancel")
def cancel_transcription(project_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    db = Supabase()
    project = _project(db, project_id)
    job = _job(db, project_id, job_id)
    if job["status"] in {"succeeded", "failed", "cancelled"}:
        return {"job": job}
    if job["status"] == "queued":
        result = db.request(
            "POST", "/rest/v1/rpc/cancel_queued_transcription_job",
            json={"p_project_id": str(project_id), "p_job_id": str(job_id)},
        ).json()
        return {"job": result or _job(db, project_id, job_id)}

    if project["status"] != "active":
        # Project deletion blocks ordinary Job updates. The active worker will observe
        # the Project state on its lease heartbeat and finalize this Job as cancelled.
        return {"job": job, "cancellation_requested": True}
    updated = db.patch(
        "jobs",
        {"id": eq(str(job_id)), "project_id": eq(str(project_id)), "status": "eq.running", "cancel_requested_at": "is.null"},
        {"cancel_requested_at": datetime.now(timezone.utc).isoformat(), "progress_message": "Đang dừng nhận diện an toàn"},
    )
    return {"job": updated or _job(db, project_id, job_id), "cancellation_requested": True}
