"""Validate Gemini translation against an existing active transcript; never persist it."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from filelock import FileLock, Timeout

from app.services.gemini_translation import GeminiTranslator, TranslationError
from app.services.supabase import Supabase, eq
from app.services.translation_validation import SourceSegment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    args = parser.parse_args()
    env = {**dotenv_values(Path(__file__).resolve().parents[2] / ".env"), **os.environ}
    db = Supabase()
    projects = db.select("projects", {"select": "active_transcription_job_id,source_language", "id": eq(str(args.project_id)), "limit": "1"})
    if not projects or not projects[0].get("active_transcription_job_id"):
        print("No active transcript found", file=sys.stderr)
        return 2
    job_id = projects[0]["active_transcription_job_id"]
    rows = db.select("segments", {"select": "id,start_us,end_us,source_text", "project_id": eq(str(args.project_id)),
        "transcription_job_id": eq(job_id), "order": "position.asc", "limit": "501"})
    if not rows or len(rows) > 500:
        print("Transcript missing or exceeds the safe bulk limit", file=sys.stderr)
        return 2
    segments = [SourceSegment(id=UUID(row["id"]), start_us=row["start_us"], end_us=row["end_us"], text=row["source_text"]) for row in rows]
    model = env.get("GEMINI_MODEL") or "gemini-2.5-flash"
    translator = GeminiTranslator(env.get("GEMINI_API_KEY") or "", model)
    lock_path = Path(__file__).resolve().parents[2] / ".cache" / "gemini-translation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with FileLock(str(lock_path), timeout=0):
            units, usage = translator.translate(segments, "zh" if projects[0].get("source_language") in (None, "und") else projects[0]["source_language"])
    except Timeout:
        print("Another translation process is active", file=sys.stderr)
        return 2
    except TranslationError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), "requests": translator.usage.requests, "retries": translator.usage.retries}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"model": model, "source_segments": len(segments), "unit_count": len(units), "requests": usage.requests,
        "retries": usage.retries, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "elapsed_seconds": round(time.monotonic() - started, 2), "units": units}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
