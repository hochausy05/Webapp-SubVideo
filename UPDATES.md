# AutoDub 2.0 — Product Updates

## v0.5.2 — 2026-11-15
- Added a Windows `run.cmd` supervisor that starts and monitors the API, frontend, ASR Worker and Translation Worker together, with startup checks, bounded Worker restarts and queued-Job warnings. Terminal shutdown behavior remains under review. (CORE-RUN-01)

## v0.5.1 — 2026-11-15
- Fixed the workspace source player to follow the video's display aspect ratio, including portrait media, while keeping the full video visible within a bounded frame. Browser acceptance remains pending. (UI-VIDEO-RATIO-01)

## v0.5.0 — 2026-11-15
- Rebuilt ASR around a standalone Faster-Whisper JSON Lines process with bounded timeouts, cancellation-safe process cleanup, CUDA diagnostics and one optional CPU `int8` fallback. Verified a real 21-second Chinese video through the API and Supabase; the active transcript contains 11 persisted Segments. Owner transcript and browser/seek acceptance remain pending. (ASR-REBUILD-02)
- Changed new transcription defaults to the `small` model and configurable `zh` source language; result provenance records the actual device and fallback reason. (ASR-REBUILD-02)

## v0.4.1 — 2026-11-15
- Fixed transcription Job claiming so persisted attempt counts and `job_attempts.attempt_number` stay aligned; expired leases close their previous attempt before retry, cancellation, or terminal failure. The migration is applied and the previously queued user Job remains eligible. (ASR-FIX-01)
- Worker claim failures now log sanitized Supabase/PostgREST diagnostics, retry only transient network/429/5xx errors with bounded exponential backoff and jitter, and stop on non-retryable RPC errors. Real video acceptance remains pending. (ASR-FIX-01)

## v0.4.0 — 2026-11-15
- Added a separate Faster-Whisper worker that claims durable transcription Jobs, verifies private source media, records attempt status, and stores validated source Segments atomically before activating a transcript revision. (ASR-01)
- Added Project transcription controls for starting, monitoring, cancelling and retrying Jobs, reviewing paginated source Segments, and seeking the source video from a Segment. Actual model inference on owner media and workflow acceptance remain pending. (ASR-01)

## v0.3.2 — 2026-11-15
- Finalized the Project workspace shell with a centered Back and four-tab navigation row; the global header and footer remain in the Project Library. (UI-FINAL-01)
- Consolidated source-video details, added internally scrolling result/log panels, and simplified the Midnight Glass surfaces on a solid background with responsive layouts. Browser visual review and owner acceptance remain pending. (UI-FINAL-01)

## v0.3.1 — 2026-11-15
- Made the complete source upload area clickable and start resumable uploads immediately after file selection or drop; retained same-file retry and added protection against duplicate sessions. (UI-REFINE-01)
- Increased Project actions and media metadata readability, removed the unsupported replace-video control without modifying stored media, and kept a single primary create action. (UI-REFINE-01)
- Improved desktop width and card density at 2K breakpoints while preserving the Midnight Glass palette. Browser visual review and owner acceptance remain pending. (UI-REFINE-01)

## v0.3.0 — 2026-11-15
- Added the Midnight Glass Project Library with create, reopen, rename, delete, and retryable deletion flows plus stable four-tab Project URLs. (UI-CORE-01)
- Added resumable source-video upload with byte-based progress, persisted media metadata, and private playback access in the Project workspace. (UI-CORE-01)
- Processing tabs clearly indicate that ASR, translation, TTS, synchronization, and rendering are not implemented; this release is awaiting owner workflow acceptance. (UI-CORE-01)

## v0.2.0 — 2026-11-15
- Added persistent Projects and verified private source-video uploads with resumable TUS support, FFprobe metadata, short-lived playback access, and retryable Storage cleanup. (DB-CORE-01)
- Added durable Job/attempt and source Segment schema/read foundations without starting processing workers or fabricating transcript results. (DB-CORE-01)
- Phase 02 implementation is available for owner verification; this entry does not record user acceptance.
