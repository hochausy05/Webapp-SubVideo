export interface ApiErrorShape {
  error?: { code?: string; message?: string };
}

export interface MediaAsset {
  id: string;
  kind: "source_video";
  status: "pending" | "ready" | "failed" | "deletion_pending" | "deletion_failed";
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  duration_us: number | null;
  technical_metadata?: { container?: string; streams?: Array<Record<string, unknown>> };
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "deleting";
  source_language: string;
  target_language: string;
  source_video_asset_id: string | null;
  active_transcription_job_id: string | null;
  deletion_state: "none" | "pending" | "failed";
  deletion_error_message?: string | null;
  created_at: string;
  updated_at: string;
  source_video: MediaAsset | null;
}

export interface ProjectPage {
  items: Project[];
  limit: number;
  offset: number;
}

export interface UploadConfig {
  max_video_upload_bytes: number;
  max_video_duration_seconds: number;
  supported_video_extensions: string[];
}

export interface ProjectJob {
  id: string;
  project_id: string;
  kind: string;
  status: string;
  progress: number;
  progress_message: string | null;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobAttempt {
  id: string;
  attempt_number: number;
  status: string;
  worker_id: string;
  started_at: string;
  ended_at: string | null;
  failure_code: string | null;
  error_message: string | null;
}

export interface JobDetails extends ProjectJob {
  attempts: JobAttempt[];
}

export interface TranscriptSegment {
  id: string;
  position: number;
  start_us: number;
  end_us: number;
  source_text: string;
  speaker_label: string | null;
}

export interface TranslationUnit {
  id: string; position: number; start_us: number; end_us: number; source_text: string;
  source_segment_ids: string[]; final_text: string; pass1_text: string; pass2_text: string;
  needs_review: boolean; review_reason: string; revision: number;
}
export interface TranslationState {
  run: { id: string } | null; units: TranslationUnit[]; latest_job: ProjectJob | null; stale: boolean;
}
export function getTranslation(projectId: string): Promise<TranslationState> {
  return request(`/projects/${projectId}/translations`);
}
export function startTranslation(projectId: string): Promise<{ job: ProjectJob; reused: boolean }> {
  return request(`/projects/${projectId}/translations`, { method: 'POST' });
}
export function cancelTranslation(projectId: string, jobId: string): Promise<{ ok: boolean }> {
  return request(`/projects/${projectId}/translations/${jobId}/cancel`, { method: 'POST' });
}
export function saveTranslation(projectId: string, runId: string, edits: Array<{ unit_id: string; revision: number; final_text: string }>): Promise<{ updated: number }> {
  return request(`/projects/${projectId}/translations`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: runId, edits }) });
}

export interface TranscriptionState {
  active_job: ProjectJob | null;
  latest_job: ProjectJob | null;
}

export interface PlaybackAsset {
  asset: MediaAsset;
  playback_url: string;
  expires_in: number;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code = "request_failed") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");
const API_ROOT = `${API_BASE_URL}/api/v1`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
      signal: init?.signal ?? AbortSignal.timeout(30000),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("Không thể kết nối FastAPI. Hãy kiểm tra backend rồi thử lại.", 0, "backend_unavailable");
  }

  if (response.status === 204) return undefined as T;
  if (response.status === 202) return { deletion_state: "pending" } as T;
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload as ApiErrorShape | null;
    throw new ApiError(
      detail?.error?.message ?? `Yêu cầu thất bại (HTTP ${response.status}).`,
      response.status,
      detail?.error?.code ?? "request_failed",
    );
  }
  return payload as T;
}

export function getUploadConfig(): Promise<UploadConfig> {
  return request<UploadConfig>("/config");
}

export function listProjects(limit = 12, offset = 0): Promise<ProjectPage> {
  return request<ProjectPage>(`/projects?limit=${limit}&offset=${offset}`);
}

export function getProject(projectId: string): Promise<Project> {
  return request<Project>(`/projects/${encodeURIComponent(projectId)}`);
}

export function createProject(name: string, description?: string): Promise<Project> {
  return request<Project>("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim(), description: description?.trim() || null }),
  });
}

export function updateProject(projectId: string, changes: { name?: string; description?: string | null }): Promise<Project> {
  return request<Project>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export async function deleteProject(projectId: string): Promise<"deleted" | "pending"> {
  const result = await request<{ deletion_state?: string }>(`/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
  return result?.deletion_state === "pending" ? "pending" : "deleted";
}

export function getSourcePlayback(projectId: string): Promise<PlaybackAsset> {
  return request<PlaybackAsset>(`/projects/${encodeURIComponent(projectId)}/source-video`);
}

export function listJobs(projectId: string): Promise<{ items: ProjectJob[]; limit: number; offset: number }> {
  return request(`/projects/${encodeURIComponent(projectId)}/jobs?limit=100&offset=0`);
}

export function getJobDetails(projectId: string, jobId: string): Promise<JobDetails> {
  return request(`/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}`);
}

export function getTranscriptionState(projectId: string): Promise<TranscriptionState> {
  return request(`/projects/${encodeURIComponent(projectId)}/transcriptions`);
}

export function startTranscription(projectId: string): Promise<{ job: ProjectJob; reused: boolean }> {
  return request(`/projects/${encodeURIComponent(projectId)}/transcriptions`, { method: "POST" });
}

export function cancelTranscription(projectId: string, jobId: string): Promise<{ job: ProjectJob; cancellation_requested?: boolean }> {
  return request(`/projects/${encodeURIComponent(projectId)}/transcriptions/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export function listSegments(projectId: string, jobId?: string, limit = 500, offset = 0): Promise<{ items: TranscriptSegment[]; limit: number; offset: number; transcription_job_id: string | null }> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (jobId) query.set("transcription_job_id", jobId);
  return request(`/projects/${encodeURIComponent(projectId)}/segments?${query}`);
}

function extensionOf(filename: string): string {
  return filename.slice(filename.lastIndexOf(".")).toLowerCase();
}

function fileMime(file: File): string {
  if (file.type) return file.type;
  return ({ ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm" } as Record<string, string>)[extensionOf(file.name)] ?? "application/octet-stream";
}

async function stableUploadKey(projectId: string, file: File): Promise<string> {
  const source = new TextEncoder().encode(`${projectId}:${file.name}:${file.size}:${file.lastModified}`);
  const digest = await crypto.subtle.digest("SHA-256", source);
  return `web-${Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function tusRequest(url: string, method: "HEAD" | "PATCH", offset: number, chunk?: Blob, signal?: AbortSignal): Promise<XMLHttpRequest> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.setRequestHeader("Tus-Resumable", "1.0.0");
    if (method === "PATCH") {
      xhr.setRequestHeader("Upload-Offset", String(offset));
      xhr.setRequestHeader("Content-Type", "application/offset+octet-stream");
    }
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort, { once: true });
    xhr.onload = () => {
      signal?.removeEventListener("abort", onAbort);
      if (method === "HEAD" ? xhr.status === 200 : xhr.status === 204) resolve(xhr);
      else reject(parseXhrError(xhr));
    };
    xhr.onerror = () => {
      signal?.removeEventListener("abort", onAbort);
      reject(new ApiError("Kết nối bị ngắt trong lúc tải. Hãy thử tải lại cùng tệp để tiếp tục.", 0, "upload_interrupted"));
    };
    xhr.onabort = () => {
      signal?.removeEventListener("abort", onAbort);
      reject(new DOMException("Upload cancelled", "AbortError"));
    };
    xhr.send(chunk ?? null);
  });
}

function parseXhrError(xhr: XMLHttpRequest): ApiError {
  try {
    const payload = JSON.parse(xhr.responseText) as ApiErrorShape;
    return new ApiError(payload.error?.message ?? `Tải lên thất bại (HTTP ${xhr.status}).`, xhr.status, payload.error?.code);
  } catch {
    return new ApiError(`Tải lên thất bại (HTTP ${xhr.status}).`, xhr.status);
  }
}

export async function uploadSourceVideo(
  projectId: string,
  file: File,
  onProgress: (bytesUploaded: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  const idempotencyKey = await stableUploadKey(projectId, file);
  // The API returns a local proxy URL in Location; use it as the durable session handle.
  let startResponse: Response;
  try {
    startResponse = await fetch(`${API_ROOT}/projects/${encodeURIComponent(projectId)}/source-video/uploads`, {
      method: "POST",
      headers: {
        "Upload-Length": String(file.size),
        "X-File-Name": encodeURIComponent(file.name),
        "Content-Type": fileMime(file),
        "Upload-Idempotency-Key": idempotencyKey,
      },
      signal: signal ?? AbortSignal.timeout(30000),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("Không thể kết nối phiên tải. Hãy kiểm tra backend và thử lại.", 0, "backend_unavailable");
  }
  if (!startResponse.ok) {
    const payload = await startResponse.json().catch(() => null) as ApiErrorShape | null;
    throw new ApiError(payload?.error?.message ?? `Không thể bắt đầu phiên tải (HTTP ${startResponse.status}).`, startResponse.status, payload?.error?.code);
  }
  const location = startResponse.headers.get("Location");
  if (!location) throw new ApiError("Backend không trả về phiên tải có thể tiếp tục.", 502, "upload_session_missing");

  const sessionUrl = new URL(location, API_BASE_URL).toString();
  const head = await tusRequest(sessionUrl, "HEAD", 0, undefined, signal);
  let offset = Number(head.getResponseHeader("Upload-Offset") ?? 0);
  const chunkSize = 6 * 1024 * 1024;
  onProgress(offset);

  while (offset < file.size) {
    if (signal?.aborted) throw new DOMException("Upload cancelled", "AbortError");
    const end = Math.min(offset + chunkSize, file.size);
    const chunk = file.slice(offset, end);
    await new Promise<void>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PATCH", sessionUrl);
      xhr.setRequestHeader("Tus-Resumable", "1.0.0");
      xhr.setRequestHeader("Upload-Offset", String(offset));
      xhr.setRequestHeader("Content-Type", "application/offset+octet-stream");
      const onAbort = () => xhr.abort();
      signal?.addEventListener("abort", onAbort, { once: true });
      xhr.upload.onprogress = (event) => onProgress(offset + Math.min(event.loaded, chunk.size));
      xhr.onload = () => {
        signal?.removeEventListener("abort", onAbort);
        if (xhr.status !== 204) {
          reject(parseXhrError(xhr));
          return;
        }
        offset = Number(xhr.getResponseHeader("Upload-Offset") ?? end);
        onProgress(offset);
        resolve();
      };
      xhr.onerror = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new ApiError("Kết nối bị ngắt trong lúc tải. Hãy thử lại cùng tệp để tiếp tục.", 0, "upload_interrupted"));
      };
      xhr.onabort = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new DOMException("Upload cancelled", "AbortError"));
      };
      xhr.send(chunk);
    });
  }

  const assetId = new URL(sessionUrl).pathname.split("/").filter(Boolean).at(-1);
  if (!assetId) throw new ApiError("Phiên tải không chứa mã Asset hợp lệ.", 502, "invalid_upload_session");
  await request(`/projects/${encodeURIComponent(projectId)}/source-video/uploads/${encodeURIComponent(assetId)}/complete`, { method: "POST", signal });
}
