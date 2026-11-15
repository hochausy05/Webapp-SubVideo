# AutoDub 2.0

**Status: Project management, ASR and two-pass Gemini translation are implemented with real Demo2 data; browser workflow and owner acceptance remain pending. TTS, sync and rendering are not implemented.**

AutoDub 2.0 is a fresh implementation plan for a local-first web application that translates source-video dialogue into concise Vietnamese narration, synthesizes offline audio, optimizes audio/video synchronization, and produces a verified MP4. The project intentionally begins from new source code rather than inheriting the previous AutoDub implementation.

## What this project intends to deliver

1. **Project and media management:** create/open projects; securely upload and inspect a video; preserve progress and recoverable processing jobs.
2. **Recognition:** transcribe source audio with a Whisper-family ASR implementation and preserve stable source segment identity and timestamps.
3. **Two-pass translation:** use Gemini to translate entire contextual thoughts, then edit for concise, natural Vietnamese speech; map every result safely back to source segments.
4. **Human editing:** allow multiple edits before saving; protect accepted user translations from automatic overwrites.
5. **Offline narration:** use Supertonic 3 for Vietnamese TTS; trim excess edge silence conservatively; measure the **actual final** audio and reuse matching artifacts.
6. **Joint AV-Sync:** optimize measured speech durations together with video timing, retain natural pacing, and expose actionable conflicts by segment without hiding global blockers.
7. **Verified output:** render MP4 from a validated immutable plan, probe the media, and make it available only after successful verification.

TTS through verified output are planned capabilities. Project management, private source-media handling, ASR and translation are implemented; user-facing acceptance remains separately tracked.

The standalone translation CLI sends one existing active transcript through Gemini in two bulk passes without persisting results. Configure `GEMINI_API_KEY` in `backend/.env`; `GEMINI_MODEL` is optional and defaults to `gemini-2.5-flash`. From `backend`, run `.\.venv\Scripts\python.exe -m app.services.translation_cli --project-id <project-uuid>`. It calls the provider API, outputs translation data to the terminal, and rejects transcripts beyond its provisional bulk limit. Review model output against the source: structured validation cannot prove meaning was preserved.

Run only one translation Worker for this deployment. A local lock and database claim guard limit concurrency, but distributed failure recovery has not been fully verified. The Worker uses a bounded subprocess and stops on provider quota/billing errors. It publishes a translation atomically only if its lease, source transcript and previous active run still match. Previously edited active translations are protected from automatic replacement. The Workspace editor batches changed `final_text` values with revisions; conflict preserves the draft. UI acceptance and long-transcript handling remain pending.

The Project Library contains the global header and footer. Opening a Project switches to a viewport-fitted workspace with a centered Back/four-tab navigation row, compact source-video details, and internally scrolling result or job-log panels. The workspace uses restrained Midnight Glass surfaces on a solid background and adapts to smaller screens.

## Documentation map — start here

| File | Language | Role |
|---|---|---|
| [`AGENTS.md`](AGENTS.md) | English | Mandatory AI entry point: reading order, task workflow, reporting and owner acceptance |
| [`RULES.md`](RULES.md) | English | Detailed engineering/product safety rules **and the only instructions for writing both history files** |
| [`TASK.md`](TASK.md) | English | Checkbox implementation queue, phase gates, explicit pending user acceptance |
| [`PLAN.md`](PLAN.md) | English | System design, six-phase detailed roadmap, open decisions, failure prevention, acceptance |
| [`README.md`](README.md) | English | This truthful repository overview and setup status |
| [`CHANGELOG.md`](CHANGELOG.md) | Vietnamese | Newest-first factual execution results after every agent implementation prompt |
| [`UPDATES.md`](UPDATES.md) | English | Newest-first versioned product features/fixes only; may initially be empty |

The instructions for `CHANGELOG.md` and `UPDATES.md` placement and formatting are in **`RULES.md`, sections R14–R15**. Those two files should contain only the title and real entries; prior entries must never be silently removed.

## Local development

From the project root on Windows, start all current services with one command:

```powershell
.\run.cmd
```

The launcher starts FastAPI, Vite, the ASR Worker and the Translation Worker, checks health/RPC readiness and supervises their processes. Press Ctrl+C to stop them. In some Windows batch/PTY hosts, a `Terminate batch job (Y/N)?` prompt may appear after the services have stopped; answer `Y`. Existing processes on ports 8000 or 5173, or existing Workers, must be stopped before launch. The launcher does not install dependencies. For initial setup, install the pinned backend requirements into `backend/.venv`, run `npm ci` in `frontend`, and configure `backend/.env` as described below. The queue warning threshold is configurable as `VITE_WORKER_QUEUE_WARN_SECONDS` in the root `.env` (default 60 seconds).

Open `http://127.0.0.1:5173` for the Midnight Glass Project Library. Create, reopen, rename, and request deletion of Projects; open a stable `/projects/{id}/{tab}` URL with tabs Nhận diện & dịch, TTS & đồng bộ, Xuất video, and Nhật ký. Upload source video by clicking anywhere in the upload area or dropping a file there; choosing a valid file starts resumable TUS automatically, shows byte progress, resumes from the saved offset after retry, and refreshes server-side FFprobe metadata and private playback when complete. The detail page currently provides playback and metadata for an uploaded source; it has no replace-video action. Processing tabs intentionally show that later pipeline stages are not available yet. Refreshing preserves persisted Projects/media and the current route. The API base URL defaults to `http://127.0.0.1:8000`; set `VITE_API_BASE_URL` in the root `.env` to use another local address. To create a local environment file, run `Copy-Item .env.example .env` from the project root and edit only the values needed for local development. Keep `.env` out of Git.

After a verified source video is ready, choose **Nhận diện bằng Whisper** in the Project's transcription tab. The UI shows the queued/running status, worker stage, errors and saved source Segments; it polls for updates and supports cancellation, retry as a new Job, and clicking a Segment to seek the source video. Prior successful transcript revisions remain stored and active until a replacement Job completes validation. The unified launcher starts the worker independently of FastAPI; no processing result is fabricated.

The Worker controller runs one Faster-Whisper CLI subprocess per attempt. The subprocess sends JSON Lines events on stdout and keeps model/runtime diagnostics on stderr; Supabase and Gemini credentials are not passed to it. The controller independently maintains the Job lease, enforces model initialization, first-Segment, inference-stall and overall inference timeouts, and terminates the subprocess tree on timeout, cancellation or lease loss. CUDA failures are diagnosed before an optional fresh CPU `int8` subprocess is started. A successful result records the actual device/compute type and fallback reason; Segments are persisted and activated by the existing atomic completion RPC.

For a real standalone recognition without creating or changing a Supabase Job, run this from `backend`:

```powershell
.\.venv\Scripts\python.exe -m app.asr.cli --input '.\path\to\video.mp4' --model small --device cpu --compute-type int8 --language zh --work-dir '.\.cache\asr\cli' --vad-filter
```

The CLI writes JSON Lines stage, progress, Segment and completion records to stdout; Python/model logs go to stderr. It does not persist a transcript. Model artifacts are cached under `.cache/asr/models`; the temporary extracted WAV is removed when the CLI exits.

An empty queue is normal and the Worker waits at the configured poll interval. Temporary Supabase network/timeout, HTTP 408/425/429, serialization/deadlock, and HTTP 5xx claim errors are logged with a bounded exponential backoff and jitter (up to 60 seconds). Non-retryable RPC/schema/logic errors log the RPC, provider HTTP status, PostgREST code and sanitized message, then stop the Worker with a non-zero exit; fix the reported database/configuration issue before restarting it. Logs redact URLs and common credential/token formats.

The frontend reads `VITE_API_BASE_URL` from the root `.env`. The backend reads `BACKEND_HOST`, `BACKEND_PORT`, `FRONTEND_ORIGIN`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `MEDIA_BUCKET`, video limits and FFmpeg/FFprobe/ASR settings from `backend/.env`. The server binds only to loopback and allows localhost frontend origins. The Supabase Secret Key stays in the trusted backend; no Supabase key is read by the frontend. The root `.env.example` contains empty credential placeholders and proposed video defaults (MP4/MOV/MKV/WebM, 512 MiB, 2 hours). D03 remains open: app limits may be lowered in configuration; the current migration and bucket enforce 512 MiB/two-hour ceilings until revised.

ASR defaults for new Jobs are Faster-Whisper `small`, language `zh`, CUDA `float16`, beam size 5 and VAD enabled. `D03` remains open, so the language is a configurable default rather than a locked language policy. On this Windows host, CTranslate2 detects the GPU but CUDA inference fails because `cublas64_12.dll` is unavailable; with the default `ASR_CPU_FALLBACK=true`, the Worker ends that CUDA subprocess and retries once in a new CPU `int8` subprocess. The actual device and fallback reason are recorded with the result. To force CPU directly, set `ASR_DEVICE=cpu` and `ASR_COMPUTE_TYPE=int8` in `backend/.env`; set `ASR_CPU_FALLBACK=false` to disable fallback. Timeout settings are `ASR_MODEL_INIT_TIMEOUT_SECONDS`, `ASR_FIRST_SEGMENT_TIMEOUT_SECONDS`, `ASR_INFERENCE_STALL_TIMEOUT_SECONDS` and `ASR_INFERENCE_TIMEOUT_SECONDS`. ASR models are cached under `backend/.cache/asr/models`; per-attempt source and scratch files under `backend/.cache/asr/jobs` are cleaned after completion.

If you need to create local files, copy `.env.example` to the root `.env` for Vite and to `backend/.env` for FastAPI only when the destination does not already exist. Keep the configured `backend/.env` private.

### Phase 02 API

The API is versioned under `/api/v1` and persists through the Supabase Data API/Storage API. `GET /config` exposes only public upload limits and supported extensions. `GET /projects` includes verified source-video metadata when present and includes Projects pending deletion so cleanup can be retried. The API provides:

- `POST /projects`, `GET /projects?limit=20&offset=0`, `GET /projects/{id}`, `PATCH /projects/{id}`
- `POST /projects/{id}/source-video/uploads` starts a TUS 1.0 resumable session. Send `Upload-Length`, `X-File-Name`, and the matching `Content-Type`; optionally send `Upload-Idempotency-Key` to recover the same pending session on a retried create call. PATCH its returned `Location` in chunks up to 6 MiB with `Tus-Resumable`, `Upload-Offset`, and `application/offset+octet-stream`. HEAD that URL to recover the saved offset, then `POST {Location}/complete` to verify the stored bytes and FFprobe metadata.
- `POST /projects/{id}/source-video` accepts a bounded multipart upload for short/simple workflows.
- `GET /projects/{id}/source-video` returns verified metadata and a private playback URL that expires after 15 minutes.
- `DELETE /projects/{id}` requests cleanup. It returns `204` when Storage cleanup and row deletion finish, `202` while an upload/worker lease remains active, and an actionable error if Storage cleanup fails. Retry a pending/failed deletion.
- `POST /projects/{id}/transcriptions` creates or reuses one active transcription Job; `GET /projects/{id}/transcriptions` reads latest and active revision status; `POST /projects/{id}/transcriptions/{job_id}/cancel` requests safe cancellation.
- `GET /projects/{id}/jobs`, `GET /projects/{id}/jobs/{job_id}`, and paginated `GET /projects/{id}/segments?transcription_job_id=...` read persisted Job attempts and successful source Segments. Segment writes and activation occur atomically only after result validation; no transcript rows are fabricated.

Errors use `{"error":{"code":"...","message":"..."}}`. The current API intentionally has no user authentication because the app is loopback-only for one local user; do not expose the backend to a network.

## Current implementation stack

| Component | Proposed implementation | Status |
|---|---|---|
| Web interface | React 19, TypeScript 7, Vite 8 | Midnight Glass Project Library and four-tab Project workspace; later processing workflows deferred |
| HTTP API | Python 3.11, FastAPI, Pydantic Settings, HTTPX | Health, Project/media APIs, Job/Segment read APIs |
| Worker | One separate local Python process | Faster-Whisper transcription worker with leases, heartbeat, cancellation and persisted Job attempts |
| Metadata/access | Supabase Postgres with RLS and private media Storage | Five-table Phase 02 schema applied to `autodub` |
| ASR | faster-whisper `small`, CUDA `float16` with one optional CPU `int8` fallback | Real 21-second Chinese video processed and 11 Segments persisted; this host fell back because cuBLAS is missing; owner transcript/browser acceptance pending |
| Translation | Gemini two-pass contextual prompts, revisioned outputs | Intended; not connected |
| Vietnamese speech | Supertonic 3 offline, local model | Intended; not installed |
| AV-Sync | Canonical timing planner and strict render validation | Planned; design constraints not fully agreed |
| Export | FFmpeg + FFprobe with verified MP4 publication | Intended; not implemented |

Do not treat model revision, rate ranges, video-speed caps, cloud deployment, auth mode, or source-audio mixing as already approved. See the decisions in `PLAN.md`.

## Current architecture

```text
frontend/src/       Route shell, Project Library/workspace components, transcription controls, typed API client, icons and styles
backend/app/        FastAPI entry point, loopback settings, Project/media/Job/Segment APIs, ASR service, worker and Supabase client
backend/.venv/      Existing local Python 3.11 environment (Git-ignored)
supabase/migrations/ Versioned five-table schema, Storage bucket, RLS/grants and lifecycle/ASR RPCs
```

The backend does not load AI models at startup. It uses the Supabase Secret Key for server-side Data API and private Storage operations. The frontend only uses variables prefixed with `VITE_`; service credentials remain server-side.

The primary data chain is **source video → source transcript revisions → translation revisions → measured audio artifacts → immutable sync plans → verified renders**. A translation edit should invalidate only dependent artifacts, not restart unrelated work. Job completion, plan validity and verified output are independent states.

## Quality goals

- **Translation:** faithful meaning and narrative continuity, concise Vietnamese dubbing, no duplicated/missing fragments, manual edits preserved.
- **TTS:** offline and reproducible, natural Vietnamese, no clipped phonemes from silence removal, accurate post-processing duration.
- **Sync:** no unauthorized spoken overlap/truncation; minimal necessary audio-speed and video-speed changes; no persisted Decimal/float mismatches; clear segment versus plan errors.
- **Rendering:** final MP4 has both expected streams and trustworthy playback timing, not merely a successful subprocess exit code.
- **UX:** compact and direct, useful error summaries, actionable segment cards that scroll to the matching transcript item, no unexplained disabled actions or wasteful horizontal tables.
- **Operations:** no leaked secrets, versioned data, repeatable retries, durable state across refresh, economical and owner-approved provider usage.

## Safety and environment

The initial target is a Windows development machine with constrained RAM/VRAM, so avoid unbounded GPU concurrency and full-video reruns after small changes. Supabase/Gemini require network access; Supertonic inference should remain offline after approved model setup. Model licensing and current dependency compatibility must be checked at installation time.

Keep media and models out of Git, keep frontend credentials public-safe, and never expose Supabase Secret or Gemini credentials in the browser. The application tables have no `anon`/`authenticated` policies or grants; the private bucket is accessed only by the loopback FastAPI backend. Do not perform destructive remote actions or deploy paid workloads without instruction.

## Development and acceptance workflow

1. Pick a checked/unchecked task in `TASK.md` and the corresponding phase in `PLAN.md`.
2. Establish the user-visible outcome and protect all related revisions/IDs.
3. Implement a small, focused change and verify it with relevant technical checks, not gratuitous test infrastructure.
4. Record work in Vietnamese in `CHANGELOG.md` **after each execution prompt**, inserting it above prior entries without deleting any history.
5. Add an English version entry to `UPDATES.md` only when an actual noteworthy product feature/fix is implemented sufficiently to justify that release.
6. Discuss/inspect actual browser, audio or MP4 behavior with the owner. User acceptance remains pending until explicitly granted; build success is not product acceptance.

## Known risks from the previous tool

The fresh design explicitly targets: fragmented translation context and repeated words; lengthy unnatural translations; provider TTS timeouts; duplicated audio generation; silence-trimming damage; mismatched duration measurements; 1-microsecond persisted-rate disagreement between planner and renderer; HTTP errors that hide valid analysis; global plan errors mislabeled as segment defects; overlong video retiming; hard-to-navigate Analyze tables; and a Render job reported complete without a verified MP4.

Treat each as a failure mode to prevent, **not** evidence that the new project already solved it.

## References and source of decisions

The detailed six-phase project blueprint was agreed during planning; the project's operative technical rules, dependencies and decisions are recorded in this folder. Historical AutoDub repository files are not included and must not be inferred to exist. Runtime versions are recorded in the frontend lockfile and `backend/requirements.txt`.
