from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, File, Header
import httpx
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.supabase import Supabase, eq, storage_object_path

router = APIRouter(tags=["projects"])
MIME_BY_EXT = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm"}
TUS_CHUNK_BYTES = 6 * 1024 * 1024
TUS_VERSION = "1.0.0"


def _tus_metadata(**items: str) -> str:
    return ",".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in items.items())


def _upload_asset(db: Supabase, project_id: uuid.UUID, asset_id: uuid.UUID) -> dict[str, Any]:
    rows = db.select("media_assets", {"select": "*", "project_id": eq(str(project_id)), "id": eq(str(asset_id)), "limit": "1"})
    if not rows:
        raise HTTPException(404, detail={"code": "asset_not_found", "message": "Upload was not found."})
    if rows[0]["status"] != "pending":
        raise HTTPException(409, detail={"code": "upload_not_pending", "message": "Upload is no longer pending."})
    _project(db, project_id)
    return rows[0]


@router.post("/projects/{project_id}/source-video/uploads", status_code=201)
def begin_resumable_upload(
    request: Request,
    project_id: uuid.UUID,
    upload_length: int = Header(alias="Upload-Length", ge=1),
    filename: str = Header(alias="X-File-Name", min_length=1, max_length=255),
    content_type: str = Header(alias="Content-Type"),
    idempotency_key: str | None = Header(default=None, alias="Upload-Idempotency-Key", max_length=200),
) -> Response:
    """Create a private TUS 1.0 session; its privileged remote URL stays in Postgres."""
    db, cfg = Supabase(), get_settings()
    _project(db, project_id)
    if upload_length > cfg.max_video_upload_bytes:
        raise HTTPException(413, detail={"code": "video_too_large", "message": "Video exceeds the configured upload limit."})
    safe_name = Path(unquote(filename)).name
    ext = Path(safe_name).suffix.lower()
    mime = MIME_BY_EXT.get(ext)
    if not mime or content_type not in {mime, "application/octet-stream"}:
        raise HTTPException(415, detail={"code": "unsupported_video", "message": "Use MP4, MOV, MKV, or WebM with a matching media type."})
    if idempotency_key is not None:
        if not idempotency_key.strip():
            raise HTTPException(422, detail={"code": "invalid_idempotency_key", "message": "Upload-Idempotency-Key cannot be blank."})
        matches = db.select("media_assets", {"select": "id,status,size_bytes,original_filename,mime_type,upload_session_url", "project_id": eq(str(project_id)), "upload_idempotency_key": eq(idempotency_key), "limit": "1"})
        if matches:
            prior = matches[0]
            if prior["status"] != "pending" or prior["size_bytes"] != upload_length or prior["original_filename"] != safe_name or prior["mime_type"] != mime or not prior.get("upload_session_url"):
                raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "This upload key is already used by a different or completed upload."})
            prior_url = str(request.url_for("resumable_upload_status", project_id=str(project_id), asset_id=prior["id"]))
            return Response(status_code=201, headers={"Location": prior_url, "Tus-Resumable": TUS_VERSION, "Tus-Version": TUS_VERSION, "Tus-Extension": "creation"})
    asset_id = uuid.uuid4()
    object_path = f"{project_id}/source-video/{asset_id}/{uuid.uuid4()}{ext}"
    db.insert("media_assets", {"id": str(asset_id), "project_id": str(project_id), "storage_path": object_path, "original_filename": safe_name, "mime_type": mime, "size_bytes": upload_length, "upload_idempotency_key": idempotency_key, "status": "pending", "write_lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()})
    api = urlparse(cfg.supabase_url)
    tus_host = (api.hostname or "").replace(".supabase.co", ".storage.supabase.co")
    tus_base = f"{api.scheme}://{tus_host}/storage/v1/upload/resumable"
    try:
        remote = db.tus_request("POST", tus_base, headers={
            "Tus-Resumable": TUS_VERSION,
            "Upload-Length": str(upload_length),
            "Upload-Metadata": _tus_metadata(bucketName=cfg.media_bucket, objectName=object_path, contentType=mime),
            "x-upsert": "false",
            "Cache-Control": "no-store",
        }, timeout=30)
        location = remote.headers.get("Location")
        if not location:
            raise HTTPException(502, detail={"code": "upload_session_missing", "message": "Storage did not create an upload session."})
        if not location.startswith("https://"):
            location = tus_base.rstrip("/") + "/" + location.lstrip("/")
        db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"upload_session_url": location, "upload_expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(), "write_lease_expires_at": None})
    except Exception:
        if 'location' in locals() and location:
            try:
                db.tus_request("DELETE", location, headers={"Tus-Resumable": TUS_VERSION})
            except HTTPException:
                pass
        db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "failed", "write_lease_expires_at": None, "failure_code": "upload_session_failed", "failure_message": "Storage could not start a resumable upload."})
        raise
    return Response(status_code=201, headers={"Location": str(request.url_for("resumable_upload_status", project_id=str(project_id), asset_id=str(asset_id))), "Tus-Resumable": TUS_VERSION, "Tus-Version": TUS_VERSION, "Tus-Extension": "creation"})


@router.head("/projects/{project_id}/source-video/uploads/{asset_id}")
def resumable_upload_status(project_id: uuid.UUID, asset_id: uuid.UUID) -> Response:
    db = Supabase()
    asset = _upload_asset(db, project_id, asset_id)
    remote = db.tus_request("HEAD", asset["upload_session_url"], headers={"Tus-Resumable": TUS_VERSION})
    offset = int(remote.headers.get("Upload-Offset", asset["upload_offset_bytes"]))
    db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"upload_offset_bytes": offset, "write_lease_expires_at": None})
    return Response(status_code=200, headers={"Tus-Resumable": TUS_VERSION, "Upload-Offset": str(offset), "Upload-Length": str(asset["size_bytes"]), "Cache-Control": "no-store"})


@router.patch("/projects/{project_id}/source-video/uploads/{asset_id}")
async def resumable_upload_chunk(project_id: uuid.UUID, asset_id: uuid.UUID, request: Request, upload_offset: int = Header(alias="Upload-Offset", ge=0), tus_resumable: str = Header(alias="Tus-Resumable"), content_type: str = Header(alias="Content-Type")) -> Response:
    if tus_resumable != TUS_VERSION or content_type != "application/offset+octet-stream":
        raise HTTPException(415, detail={"code": "invalid_tus_request", "message": "Expected TUS 1.0 and application/offset+octet-stream."})
    body = await request.body()
    if not body or len(body) > TUS_CHUNK_BYTES:
        raise HTTPException(413, detail={"code": "invalid_chunk_size", "message": "Each resumable chunk must be 1 to 6 MiB."})
    db = Supabase()
    asset = _upload_asset(db, project_id, asset_id)
    if upload_offset != asset["upload_offset_bytes"] or upload_offset + len(body) > asset["size_bytes"]:
        raise HTTPException(409, detail={"code": "upload_offset_mismatch", "message": "Upload offset does not match the saved session; query HEAD and resume from its offset."})
    lease = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending", "upload_offset_bytes": eq(str(upload_offset))}, {"write_lease_expires_at": lease})
    remote = db.tus_request("PATCH", asset["upload_session_url"], content=body, headers={"Tus-Resumable": TUS_VERSION, "Upload-Offset": str(upload_offset), "Content-Type": "application/offset+octet-stream"}, timeout=120)
    next_offset = int(remote.headers.get("Upload-Offset", upload_offset + len(body)))
    db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"upload_offset_bytes": next_offset, "write_lease_expires_at": None})
    return Response(status_code=204, headers={"Tus-Resumable": TUS_VERSION, "Upload-Offset": str(next_offset)})


@router.post("/projects/{project_id}/source-video/uploads/{asset_id}/complete")
def complete_resumable_upload(project_id: uuid.UUID, asset_id: uuid.UUID) -> dict[str, Any]:
    db, cfg = Supabase(), get_settings()
    asset = _upload_asset(db, project_id, asset_id)
    if asset["upload_offset_bytes"] != asset["size_bytes"]:
        raise HTTPException(409, detail={"code": "upload_incomplete", "message": "Upload is incomplete; resume its chunks before verification."})
    temp = tempfile.NamedTemporaryFile(prefix="autodub-verify-", suffix=Path(asset["original_filename"]).suffix, delete=False)
    digest, received = hashlib.sha256(), 0
    try:
        with httpx.stream("GET", cfg.supabase_url.rstrip("/") + f"/storage/v1/object/{asset['storage_bucket']}/{storage_object_path(asset['storage_path'])}", headers=db.headers, timeout=600) as download:
            if download.is_error:
                raise HTTPException(502, detail={"code": "stored_media_missing", "message": "Storage could not verify the completed upload."})
            for chunk in download.iter_bytes(1024 * 1024):
                received += len(chunk)
                if received > asset["size_bytes"]:
                    raise HTTPException(422, detail={"code": "stored_media_size_mismatch", "message": "Stored media size did not match the upload."})
                digest.update(chunk); temp.write(chunk)
        temp.close()
        if received != asset["size_bytes"]:
            raise HTTPException(422, detail={"code": "stored_media_size_mismatch", "message": "Stored media size did not match the upload."})
        probe = subprocess.run([cfg.ffprobe_path, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate", "-of", "json", temp.name], capture_output=True, text=True, timeout=cfg.ffprobe_timeout_seconds, check=True)
        metadata = json.loads(probe.stdout)
        duration_us = round(float(metadata.get("format", {}).get("duration", 0)) * 1_000_000)
        streams = metadata.get("streams", [])
        if not any(s.get("codec_type") == "video" for s in streams) or duration_us <= 0 or duration_us > cfg.max_video_duration_seconds * 1_000_000:
            raise ValueError("invalid media duration or video stream")
        safe_streams = [{k: s[k] for k in ("codec_type", "codec_name", "width", "height", "avg_frame_rate") if k in s} for s in streams]
        ready = db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "ready", "duration_us": duration_us, "sha256": digest.hexdigest(), "measured_at": datetime.now(timezone.utc).isoformat(), "upload_session_url": None, "write_lease_expires_at": None, "technical_metadata": {"container": metadata.get("format", {}).get("format_name"), "streams": safe_streams}})
        if not ready:
            raise HTTPException(409, detail={"code": "upload_not_pending", "message": "Upload state changed before final verification."})
        selected = db.request("POST", "/rest/v1/rpc/activate_source_video", json={"p_project_id": str(project_id), "p_asset_id": str(asset_id)}).json()
        return {"asset": ready, "project": selected}
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "failed", "upload_session_url": None, "write_lease_expires_at": None, "failure_code": "media_probe_failed", "failure_message": "FFprobe could not validate this media file."})
        try:
            db.delete_storage_paths(asset["storage_bucket"], [asset["storage_path"]])
        except HTTPException:
            db.patch("media_assets", {"id": eq(str(asset_id))}, {"status": "deletion_failed", "failure_code": "storage_cleanup_failed", "failure_message": "Invalid uploaded media remains in Storage; retry Project deletion."})
        raise HTTPException(422, detail={"code": "invalid_video", "message": "FFprobe could not validate a supported video within the configured duration limit."})
    finally:
        temp.close()
        try: os.unlink(temp.name)
        except OSError: pass


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    source_language: str = "und"
    target_language: str = "vi"


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate) -> dict[str, Any]:
    return Supabase().insert("projects", body.model_dump())


@router.get("/config")
def get_public_config() -> dict[str, Any]:
    """Expose only safe upload validation settings needed by the local browser UI."""
    cfg = get_settings()
    return {
        "max_video_upload_bytes": cfg.max_video_upload_bytes,
        "max_video_duration_seconds": cfg.max_video_duration_seconds,
        "supported_video_extensions": [".mp4", ".mov", ".mkv", ".webm"],
    }


@router.get("/projects")
def list_projects(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    db = Supabase()
    rows = db.select("projects", {"select": "*", "status": "in.(active,deleting)", "order": "updated_at.desc,id.desc", "limit": str(limit), "offset": str(offset)})
    asset_ids = [row["source_video_asset_id"] for row in rows if row.get("source_video_asset_id")]
    assets_by_id: dict[str, dict[str, Any]] = {}
    if asset_ids:
        assets = db.select("media_assets", {"select": "id,kind,status,original_filename,mime_type,size_bytes,duration_us,technical_metadata", "id": f"in.({','.join(asset_ids)})"})
        assets_by_id = {asset["id"]: asset for asset in assets}
    for row in rows:
        row["source_video"] = assets_by_id.get(row.get("source_video_asset_id"))
    return {"items": rows, "limit": limit, "offset": offset}


@router.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID) -> dict[str, Any]:
    db = Supabase()
    rows = db.select("projects", {"select": "*", "id": eq(str(project_id)), "limit": "1"})
    if not rows:
        raise HTTPException(404, detail={"code": "project_not_found", "message": "Project was not found."})
    project = rows[0]
    if project["source_video_asset_id"]:
        assets = db.select("media_assets", {"select": "id,kind,status,original_filename,mime_type,size_bytes,duration_us,sha256,technical_metadata,created_at", "id": eq(project["source_video_asset_id"]), "limit": "1"})
        project["source_video"] = assets[0] if assets else None
    else:
        project["source_video"] = None
    return project


@router.patch("/projects/{project_id}")
def update_project(project_id: uuid.UUID, body: ProjectUpdate) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, detail={"code": "empty_update", "message": "Provide a name or description to update."})
    if "name" in changes and changes["name"] is None:
        raise HTTPException(422, detail={"code": "invalid_name", "message": "Project name cannot be null."})
    row = Supabase().patch("projects", {"id": eq(str(project_id)), "status": "eq.active"}, changes)
    if row is None:
        raise HTTPException(404, detail={"code": "project_not_found_or_deleting", "message": "Active Project was not found."})
    return row


def _project(db: Supabase, project_id: uuid.UUID) -> dict[str, Any]:
    rows = db.select("projects", {"select": "id,status,active_transcription_job_id", "id": eq(str(project_id)), "limit": "1"})
    if not rows:
        raise HTTPException(404, detail={"code": "project_not_found", "message": "Project was not found."})
    if rows[0]["status"] != "active":
        raise HTTPException(409, detail={"code": "project_deleting", "message": "Project is deleting; new uploads are blocked."})
    return rows[0]


@router.post("/projects/{project_id}/source-video", status_code=201)
async def upload_source_video(project_id: uuid.UUID, file: UploadFile = File(...)) -> dict[str, Any]:
    """Stream a source file to private Storage; multipart uploads are bounded by configured limits."""
    db, cfg = Supabase(), get_settings()
    _project(db, project_id)
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    mime = MIME_BY_EXT.get(ext)
    if not mime or (file.content_type and file.content_type not in {mime, "application/octet-stream"}):
        raise HTTPException(415, detail={"code": "unsupported_video", "message": "Use an MP4, MOV, MKV, or WebM video with a matching media type."})
    asset_id = uuid.uuid4()
    object_path = f"{project_id}/source-video/{asset_id}/{uuid.uuid4()}{ext}"
    temp = tempfile.NamedTemporaryFile(prefix="autodub-upload-", suffix=ext, delete=False)
    size, digest = 0, hashlib.sha256()
    asset_created, asset_ready = False, False
    try:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > cfg.max_video_upload_bytes:
                raise HTTPException(413, detail={"code": "video_too_large", "message": "Video exceeds the configured upload limit."})
            digest.update(chunk)
            temp.write(chunk)
        temp.close()
        if size == 0:
            raise HTTPException(422, detail={"code": "empty_video", "message": "The uploaded file is empty."})
        db.insert("media_assets", {"id": str(asset_id), "project_id": str(project_id), "storage_path": object_path, "original_filename": filename[:255], "mime_type": mime, "size_bytes": size, "sha256": digest.hexdigest(), "status": "pending", "write_lease_expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()})
        asset_created = True
        with open(temp.name, "rb") as stream:
            db.request("POST", f"/storage/v1/object/{cfg.media_bucket}/{storage_object_path(object_path)}", content=stream, headers={"Content-Type": mime, "Content-Length": str(size), "x-upsert": "false"}, timeout=600)
        stored_digest, stored_size = hashlib.sha256(), 0
        with httpx.stream("GET", cfg.supabase_url.rstrip("/") + f"/storage/v1/object/{cfg.media_bucket}/{storage_object_path(object_path)}", headers=db.headers, timeout=600) as stored:
            if stored.is_error:
                raise HTTPException(502, detail={"code": "stored_media_missing", "message": "Storage could not verify the uploaded file."})
            for chunk in stored.iter_bytes(1024 * 1024):
                stored_size += len(chunk)
                stored_digest.update(chunk)
        if stored_size != size or stored_digest.hexdigest() != digest.hexdigest():
            raise HTTPException(422, detail={"code": "stored_media_mismatch", "message": "Stored file verification failed; retry the upload."})
        probe = subprocess.run([cfg.ffprobe_path, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate", "-of", "json", temp.name], capture_output=True, text=True, timeout=cfg.ffprobe_timeout_seconds, check=True)
        metadata = json.loads(probe.stdout)
        duration_us = round(float(metadata.get("format", {}).get("duration", 0)) * 1_000_000)
        streams = metadata.get("streams", [])
        if not any(s.get("codec_type") == "video" for s in streams) or duration_us <= 0 or duration_us > cfg.max_video_duration_seconds * 1_000_000:
            raise ValueError("unsupported or out-of-range video")
        safe_streams = [{k: s[k] for k in ("codec_type", "codec_name", "width", "height", "avg_frame_rate") if k in s} for s in streams]
        ready = db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "ready", "duration_us": duration_us, "measured_at": datetime.now(timezone.utc).isoformat(), "write_lease_expires_at": None, "technical_metadata": {"container": metadata.get("format", {}).get("format_name"), "streams": safe_streams}})
        if not ready:
            raise RuntimeError("asset state changed before verification")
        asset_ready = True
        selected = db.request("POST", "/rest/v1/rpc/activate_source_video", json={"p_project_id": str(project_id), "p_asset_id": str(asset_id)}).json()
        return {"asset": ready, "project": selected}
    except HTTPException:
        if asset_created and not asset_ready:
            db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "failed", "write_lease_expires_at": None, "failure_code": "upload_failed", "failure_message": "Upload did not complete."})
            try:
                db.delete_storage_paths(cfg.media_bucket, [object_path])
            except HTTPException:
                db.patch("media_assets", {"id": eq(str(asset_id))}, {"status": "deletion_failed", "failure_code": "storage_cleanup_failed", "failure_message": "Upload cleanup failed; retry Project deletion."})
        raise
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        if asset_created:
            db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "failed", "write_lease_expires_at": None, "failure_code": "media_probe_failed", "failure_message": "FFprobe could not validate this media file."})
            try:
                db.delete_storage_paths(cfg.media_bucket, [object_path])
            except HTTPException:
                db.patch("media_assets", {"id": eq(str(asset_id))}, {"status": "deletion_failed", "failure_code": "storage_cleanup_failed", "failure_message": "Invalid upload remains in private Storage; retry Project deletion."})
        raise HTTPException(422, detail={"code": "invalid_video", "message": "FFprobe could not validate a supported video within the configured duration limit."})
    except Exception:
        if asset_created and not asset_ready:
            db.patch("media_assets", {"id": eq(str(asset_id)), "status": "eq.pending"}, {"status": "failed", "write_lease_expires_at": None, "failure_code": "upload_failed", "failure_message": "Storage upload or verification failed."})
            try:
                db.delete_storage_paths(cfg.media_bucket, [object_path])
            except HTTPException:
                db.patch("media_assets", {"id": eq(str(asset_id))}, {"status": "deletion_failed", "failure_code": "storage_cleanup_failed", "failure_message": "Upload cleanup failed; retry Project deletion."})
        raise
    finally:
        await file.close()
        try:
            temp.close()
            os.unlink(temp.name)
        except OSError:
            pass


@router.get("/projects/{project_id}/source-video")
def source_video(project_id: uuid.UUID) -> dict[str, Any]:
    project = get_project(project_id)
    asset_id = project.get("source_video_asset_id")
    if not asset_id:
        raise HTTPException(404, detail={"code": "source_video_missing", "message": "Project has no verified source video."})
    db, cfg = Supabase(), get_settings()
    asset = db.select("media_assets", {"select": "*", "id": eq(asset_id), "limit": "1"})[0]
    signed = db.request("POST", f"/storage/v1/object/sign/{asset['storage_bucket']}/{storage_object_path(asset['storage_path'])}", json={"expiresIn": 900}).json()
    signed_url = signed.get("signedURL") or signed.get("signedUrl")
    if not signed_url:
        raise HTTPException(502, detail={"code": "playback_url_unavailable", "message": "Storage could not create private playback access."})
    if signed_url.startswith("https://"):
        playback_url = signed_url
    else:
        storage_path = signed_url if signed_url.startswith("/storage/v1/") else "/storage/v1" + (signed_url if signed_url.startswith("/") else "/" + signed_url)
        playback_url = cfg.supabase_url.rstrip("/") + storage_path
    return {"asset": {k: v for k, v in asset.items() if k not in {"upload_session_url", "upload_expires_at", "write_lease_expires_at"}}, "playback_url": playback_url, "expires_in": 900}


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    db = Supabase(); _project(db, project_id)
    items = db.select("jobs", {"select": "*", "project_id": eq(str(project_id)), "order": "created_at.desc,id.desc", "limit": str(limit), "offset": str(offset)})
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    db = Supabase(); _project(db, project_id)
    rows = db.select("jobs", {"select": "*", "project_id": eq(str(project_id)), "id": eq(str(job_id)), "limit": "1"})
    if not rows: raise HTTPException(404, detail={"code": "job_not_found", "message": "Job was not found."})
    rows[0]["attempts"] = db.select("job_attempts", {"select": "*", "job_id": eq(str(job_id)), "order": "attempt_number.asc"})
    return rows[0]


@router.get("/projects/{project_id}/segments")
def list_segments(project_id: uuid.UUID, transcription_job_id: uuid.UUID | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    db = Supabase(); project = _project(db, project_id)
    job_id = str(transcription_job_id) if transcription_job_id else project.get("active_transcription_job_id")
    if not job_id: return {"items": [], "limit": limit, "offset": offset, "transcription_job_id": None}
    jobs = db.select("jobs", {"select": "id,status,kind", "project_id": eq(str(project_id)), "id": eq(job_id), "limit": "1"})
    if not jobs or jobs[0]["kind"] != "transcription":
        raise HTTPException(404, detail={"code": "transcription_not_found", "message": "Không tìm thấy lần nhận diện này trong Project."})
    if jobs[0]["status"] != "succeeded":
        return {"items": [], "limit": limit, "offset": offset, "transcription_job_id": job_id}
    items = db.select("segments", {"select": "*", "project_id": eq(str(project_id)), "transcription_job_id": eq(job_id), "order": "position.asc", "limit": str(limit), "offset": str(offset)})
    return {"items": items, "limit": limit, "offset": offset, "transcription_job_id": job_id}


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID) -> Response:
    db = Supabase()
    rows = db.select("projects", {"select": "id,status", "id": eq(str(project_id)), "limit": "1"})
    if not rows: return Response(status_code=204)
    db.patch("projects", {"id": eq(str(project_id)), "status": "eq.active"}, {"status": "deleting", "deletion_requested_at": datetime.now(timezone.utc).isoformat()})
    assets = db.select("media_assets", {"select": "id,storage_bucket,storage_path,status,write_lease_expires_at", "project_id": eq(str(project_id))})
    active_writes = [a for a in assets if a.get("write_lease_expires_at") and datetime.fromisoformat(a["write_lease_expires_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)]
    active_jobs = db.select("jobs", {"select": "id", "project_id": eq(str(project_id)), "status": "eq.running", "lease_expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}"})
    # Even an expired TTS lease may still be finishing an interrupted Storage
    # request. Keep the project and manifests until that Job is terminal.
    active_tts = db.select("jobs", {"select":"id,lease_expires_at", "project_id":eq(str(project_id)), "kind":"eq.tts", "status":"eq.running", "limit":"1"})
    if active_tts:
        raise HTTPException(409, detail={"code":"tts_cleanup_waiting", "message":"TTS Job chưa dừng hoàn toàn. Hãy chờ Worker dừng, sau đó thử xóa lại; nếu vẫn kẹt, dừng launcher để kiểm tra an toàn."})
    if active_writes or active_jobs or active_tts:
        return Response(status_code=202, content=b'{"deletion_state":"pending","message":"An upload or worker lease is active; retry deletion after it expires."}', media_type="application/json")
    cfg = get_settings()
    failures = []
    # Every generated Storage path is recorded before upload, including failed
    # and partial runs, so project deletion can clean them without guessing.
    offset = 0
    while True:
        runs = db.select("tts_runs", {"select":"id", "project_id":eq(str(project_id)), "order":"id.asc", "limit":"100", "offset":str(offset)})
        for run in runs:
            asset_offset = 0
            while True:
                tts_assets = db.select("tts_artifacts", {"select":"storage_path,raw_storage_path", "run_id":eq(run["id"]), "order":"id.asc", "limit":"100", "offset":str(asset_offset)})
                if tts_assets:
                    paths = [path for a in tts_assets for path in (a["storage_path"],a.get("raw_storage_path")) if path]
                    try: db.delete_storage_paths("autodub-private-tts", paths)
                    except HTTPException: failures.append(run["id"])
                if len(tts_assets) < 100: break
                asset_offset += len(tts_assets)
        if len(runs) < 100: break
        offset += len(runs)
    if failures:
        db.patch("projects", {"id": eq(str(project_id))}, {"deletion_state": "failed", "deletion_error_code": "storage_cleanup_failed", "deletion_error_message": "TTS Storage cleanup failed; retry Project deletion."})
        raise HTTPException(502, detail={"code":"storage_cleanup_failed", "message":"Không dọn được audio TTS; dữ liệu Project được giữ để thử lại."})
    for asset in assets:
        db.patch("media_assets", {"id": eq(asset["id"]), "status": f"in.({asset['status']},deletion_failed)"}, {"status": "deletion_pending"})
        try:
            db.delete_storage_paths(asset["storage_bucket"], [asset["storage_path"]])
        except HTTPException:
            failures.append(asset["id"])
            db.patch("media_assets", {"id": eq(asset["id"])}, {"status": "deletion_failed", "failure_code": "storage_cleanup_failed", "failure_message": "Storage cleanup failed; retry Project deletion."})
    if failures:
        db.patch("projects", {"id": eq(str(project_id))}, {"deletion_state": "failed", "deletion_error_code": "storage_cleanup_failed", "deletion_error_message": "One or more private files could not be removed; retry deletion."})
        raise HTTPException(502, detail={"code": "storage_cleanup_failed", "message": "Some files could not be removed. Project data is retained; retry deletion."})
    db.delete("projects", {"id": eq(str(project_id))})
    return Response(status_code=204)
