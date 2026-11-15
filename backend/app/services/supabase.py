from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote
from pathlib import Path

import httpx
from urllib.parse import urlparse
from fastapi import HTTPException

from app.core.config import get_settings


class SupabaseRequestError(HTTPException):
    """A sanitized provider error for logs while keeping API responses generic."""

    def __init__(
        self,
        *,
        http_status: int,
        provider_http_status: int | None,
        provider_code: str | None,
        provider_message: str,
        retryable: bool,
    ) -> None:
        super().__init__(
            status_code=http_status,
            detail={"code": "supabase_unavailable" if retryable else "supabase_request_failed", "message": "Supabase could not complete the operation."},
        )
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.retryable = retryable


def _safe_provider_message(value: Any) -> str:
    message = " ".join(str(value or "Supabase returned no diagnostic message.").split())
    message = re.sub(r"https?://\S+", "[URL redacted]", message, flags=re.IGNORECASE)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]+", "[token redacted]", message)
    message = re.sub(r"(?i)\b(?:sb_(?:secret|publishable)_|sbp_|service[_ -]?role[_ -]?key)\S*", "[key redacted]", message)
    message = re.sub(r"(?i)\b(authorization|\w*key|secret|password|token)\s*[:=]\s*\S+", r"\1=[redacted]", message)
    return message[:500]


def _provider_error(response: httpx.Response) -> SupabaseRequestError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    raw_code = payload.get("code")
    provider_code = str(raw_code)[:40] if isinstance(raw_code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(raw_code)) else None
    message = _safe_provider_message(payload.get("message"))
    status = response.status_code
    retryable = status in {408, 425, 429} or status >= 500 or provider_code in {"40001", "40P01"}
    return SupabaseRequestError(
        http_status=502 if status >= 500 else status,
        provider_http_status=status,
        provider_code=provider_code,
        provider_message=message,
        retryable=retryable,
    )


class Supabase:
    def __init__(self) -> None:
        cfg = get_settings()
        if not cfg.supabase_url or not cfg.supabase_secret_key:
            raise HTTPException(503, detail={"code": "storage_not_configured", "message": "Backend Supabase configuration is missing."})
        self.root = cfg.supabase_url.rstrip("/")
        self.headers = {"apikey": cfg.supabase_secret_key, "Authorization": f"Bearer {cfg.supabase_secret_key}"}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = httpx.request(method, self.root + path, headers={**self.headers, **kwargs.pop("headers", {})}, timeout=kwargs.pop("timeout", 60), **kwargs)
        except httpx.HTTPError as exc:
            raise SupabaseRequestError(
                http_status=502,
                provider_http_status=None,
                provider_code=type(exc).__name__,
                provider_message="Supabase request failed because of a network error or timeout.",
                retryable=True,
            ) from exc
        if response.is_error:
            # Keep API responses generic; a sanitized diagnostic remains available to trusted worker logs.
            raise _provider_error(response)
        return response

    def tus_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        parsed = urlparse(url)
        allowed_host = urlparse(self.root).hostname or ""
        if not parsed.scheme == "https" or not (parsed.hostname == allowed_host or parsed.hostname == allowed_host.replace(".supabase.co", ".storage.supabase.co")):
            raise HTTPException(502, detail={"code": "invalid_upload_session", "message": "The resumable upload session is invalid."})
        try:
            response = httpx.request(method, url, headers={**self.headers, **kwargs.pop("headers", {})}, timeout=kwargs.pop("timeout", 60), **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(502, detail={"code": "upload_session_unavailable", "message": "The resumable upload session could not be reached; retry the request."}) from exc
        if response.is_error:
            raise HTTPException(409 if response.status_code in (409, 410) else 502, detail={"code": "upload_session_rejected", "message": "Storage rejected the resumable upload operation."})
        return response

    def select(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self.request("GET", f"/rest/v1/{table}", params=params)
        return response.json()

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        response = self.request("POST", f"/rest/v1/{table}", json=row, headers={"Prefer": "return=representation"})
        rows = response.json()
        return rows[0]

    def patch(self, table: str, params: dict[str, str], changes: dict[str, Any]) -> dict[str, Any] | None:
        response = self.request("PATCH", f"/rest/v1/{table}", params=params, json=changes, headers={"Prefer": "return=representation"})
        rows = response.json()
        return rows[0] if rows else None

    def delete(self, table: str, params: dict[str, str]) -> None:
        self.request("DELETE", f"/rest/v1/{table}", params=params)

    def delete_storage_paths(self, bucket: str, paths: list[str]) -> None:
        try:
            self.request("DELETE", f"/storage/v1/object/{bucket}", json={"prefixes": paths})
        except HTTPException as exc:
            if exc.status_code != 404:
                raise

    def download_storage_object(self, bucket: str, path: str, destination: Path, expected_size: int) -> tuple[int, str]:
        """Stream a trusted, database-sourced private object to disk without buffering it in memory."""
        digest = hashlib.sha256()
        received = 0
        url = f"{self.root}/storage/v1/object/{quote(bucket, safe='')}/{storage_object_path(path)}"
        try:
            with httpx.stream(
                "GET",
                url,
                headers=self.headers,
                timeout=httpx.Timeout(connect=20, read=90, write=60, pool=20),
            ) as response:
                if response.is_error:
                    raise HTTPException(502, detail={"code": "source_storage_unavailable", "message": "Supabase Storage could not provide the verified source video."})
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        received += len(chunk)
                        if received > expected_size:
                            raise HTTPException(422, detail={"code": "source_size_mismatch", "message": "The stored video size no longer matches its verified metadata."})
                        digest.update(chunk)
                        output.write(chunk)
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(502, detail={"code": "source_storage_unavailable", "message": "Supabase Storage could not provide the verified source video."}) from exc
        if received != expected_size:
            raise HTTPException(422, detail={"code": "source_size_mismatch", "message": "The stored video size no longer matches its verified metadata."})
        return received, digest.hexdigest()


def eq(value: str) -> str:
    return f"eq.{value}"


def storage_object_path(path: str) -> str:
    return quote(path, safe="/")
