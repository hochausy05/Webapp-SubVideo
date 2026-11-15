import { useCallback, useEffect, useRef, useState, type CSSProperties, type DragEvent } from "react";

import {
  ApiError,
  cancelTranscription,
  cancelTranslation,
  getTranslation,
  saveTranslation,
  startTranslation,
  getJobDetails,
  getProject,
  getSourcePlayback,
  getTranscriptionState,
  getUploadConfig,
  listSegments,
  listJobs,
  startTranscription,
  uploadSourceVideo,
  type JobDetails,
  type PlaybackAsset,
  type Project,
  type ProjectJob,
  type TranscriptSegment,
  type TranscriptionState,
  type TranslationState,
  type UploadConfig,
} from "../api/projects";
import { Icon, type IconName } from "./Icon";

export type ProjectTab = "transcription" | "tts" | "export" | "logs";

const tabs: Array<{ id: ProjectTab; label: string; shortLabel: string; icon: IconName }> = [
  { id: "transcription", label: "Nhận diện & dịch", shortLabel: "Nhận diện", icon: "wave" },
  { id: "tts", label: "TTS & đồng bộ", shortLabel: "TTS & Sync", icon: "sync" },
  { id: "export", label: "Xuất video", shortLabel: "Xuất video", icon: "export" },
  { id: "logs", label: "Nhật ký", shortLabel: "Nhật ký", icon: "terminal" },
];

function bytesText(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes >= 100 * 1024 * 1024 ? 0 : 1)} MB`;
}

function durationText(durationUs: number | null): string {
  if (!durationUs) return "Chưa đo";
  const total = Math.floor(durationUs / 1_000_000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}` : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function timeText(value: string): string {
  return new Intl.DateTimeFormat("vi-VN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function displayUploadError(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : "Tải video chưa hoàn tất.";
  const translated: Record<string, string> = {
    unsupported_video: "Định dạng chưa được hỗ trợ. Hãy chọn tệp MP4, MOV, MKV hoặc WebM.",
    video_too_large: "Video vượt quá dung lượng cho phép theo cấu hình hiện tại.",
    invalid_video: "Không đọc được video hoặc video vượt quá thời lượng cho phép. Hãy kiểm tra tệp rồi thử lại.",
    upload_incomplete: "Video chưa tải đủ. Hãy tiếp tục tải trước khi xác minh.",
    upload_offset_mismatch: "Phiên tải đã thay đổi. Hãy thử lại cùng tệp để tiếp tục từ vị trí đã lưu.",
    upload_session_rejected: "Phiên tải đã hết hạn hoặc bị gián đoạn. Hãy thử lại cùng tệp.",
    backend_unavailable: "Không kết nối được backend. Kiểm tra FastAPI rồi thử lại.",
    storage_cleanup_failed: "Video đã lỗi nhưng Storage chưa dọn được. Hãy thử xóa dự án để dọn lại.",
    idempotency_key_reused: "Tệp này đã được tải hoặc phiên cũ không còn dùng được. Hãy làm mới Project rồi kiểm tra video hiện tại.",
  };
  return translated[error.code] ?? error.message;
}

function UploadBox({ projectId, config, disabled, onUploaded }: { projectId: string; config: UploadConfig | null; disabled: boolean; onUploaded: () => Promise<void> }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const uploadLockRef = useRef(false);
  const [file, setFile] = useState<File | null>(null);
  const [hovering, setHovering] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadedBytes, setUploadedBytes] = useState(0);
  const [error, setError] = useState("");
  const [retryable, setRetryable] = useState(false);
  const [needsProjectRefresh, setNeedsProjectRefresh] = useState(false);
  const [validationMessage, setValidationMessage] = useState("");

  useEffect(() => () => abortRef.current?.abort(), []);

  const startUpload = async (candidate: File) => {
    if (!config || disabled || uploadLockRef.current) return;
    uploadLockRef.current = true;
    setFile(candidate);
    setUploadedBytes(0);
    setUploading(true);
    setError("");
    setRetryable(false);
    setNeedsProjectRefresh(false);
    setValidationMessage("");
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await uploadSourceVideo(projectId, candidate, setUploadedBytes, controller.signal);
      setValidationMessage("Video đã được lưu và xác minh.");
      setFile(null);
      setUploadedBytes(0);
      if (inputRef.current) inputRef.current.value = "";
      await onUploaded();
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") {
        setError("Đã dừng tải. Bạn có thể tiếp tục cùng tệp từ phần đã lưu.");
        setRetryable(true);
      } else {
        const isAlreadyComplete = reason instanceof ApiError && reason.code === "idempotency_key_reused";
        const canResume = reason instanceof ApiError && ["upload_incomplete", "upload_offset_mismatch", "upload_session_rejected", "upload_interrupted", "backend_unavailable"].includes(reason.code);
        setError(displayUploadError(reason));
        setRetryable(canResume);
        setNeedsProjectRefresh(isAlreadyComplete);
      }
    } finally {
      setUploading(false);
      abortRef.current = null;
      uploadLockRef.current = false;
    }
  };

  const chooseFile = (candidate?: File) => {
    if (!candidate || disabled || uploadLockRef.current) return;
    setError("");
    setRetryable(false);
    setNeedsProjectRefresh(false);
    setValidationMessage("");
    setFile(null);
    setUploadedBytes(0);
    if (!config) {
      setError("Chưa tải được giới hạn video từ backend. Hãy thử lại sau.");
      return;
    }
    const extension = candidate.name.slice(candidate.name.lastIndexOf(".")).toLowerCase();
    if (!config.supported_video_extensions.includes(extension)) {
      setError("Định dạng chưa được hỗ trợ. Chọn MP4, MOV, MKV hoặc WebM.");
      return;
    }
    if (candidate.size === 0) {
      setError("Tệp video trống. Hãy chọn tệp khác.");
      return;
    }
    if (candidate.size > config.max_video_upload_bytes) {
      setError(`Video vượt giới hạn ${bytesText(config.max_video_upload_bytes)} của cấu hình hiện tại.`);
      return;
    }
    setFile(candidate);
    setUploadedBytes(0);
    void startUpload(candidate);
  };

  const onDrop = (event: DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    setHovering(false);
    if (!disabled && !uploadLockRef.current) chooseFile(event.dataTransfer.files[0]);
  };

  const progress = file && file.size > 0 ? Math.min(100, Math.round((uploadedBytes / file.size) * 100)) : 0;

  return (
    <div className="upload-section">
      <button
        type="button"
        className={`drop-zone${hovering ? " drop-zone--active" : ""}${disabled ? " drop-zone--disabled" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragEnter={(event) => { event.preventDefault(); setHovering(true); }}
        onDragOver={(event) => { event.preventDefault(); setHovering(true); }}
        onDragLeave={(event) => { if (event.currentTarget === event.target) setHovering(false); }}
        onDrop={onDrop}
        disabled={disabled || uploading}
        aria-label="Chọn video nguồn hoặc kéo thả video để tải lên"
      >
        <span className="upload-icon"><Icon name="upload" size={21} /></span>
        <strong>{hovering ? "Thả video để bắt đầu tải" : "Thêm video nguồn"}</strong>
        <span>Nhấn vào đây để chọn tệp hoặc kéo thả video</span>
        <small>Tải lên tự động · MP4 · MOV · MKV · WebM · tối đa {config ? bytesText(config.max_video_upload_bytes) : "đang tải giới hạn…"}</small>
      </button>
      <input ref={inputRef} className="file-input" type="file" accept={config?.supported_video_extensions.join(",") ?? ".mp4,.mov,.mkv,.webm"} onChange={(event) => { const candidate = event.currentTarget.files?.[0]; event.currentTarget.value = ""; chooseFile(candidate); }} disabled={disabled || uploading} aria-label="Chọn video nguồn" />

      {file && (
        <div className="selected-file-card">
          <div className="selected-file-icon"><Icon name="video" size={18} /></div>
          <div className="selected-file-copy"><strong title={file.name}>{file.name}</strong><span>{bytesText(file.size)} · {file.name.split(".").pop()?.toUpperCase()}</span></div>
          {!uploading && <button className="icon-button quiet-button" type="button" aria-label="Bỏ tệp đã chọn" onClick={() => { setFile(null); setUploadedBytes(0); if (inputRef.current) inputRef.current.value = ""; }}><Icon name="close" size={16} /></button>}
        </div>
      )}

      {uploading && file && (
        <div className="upload-progress" aria-live="polite">
          <div className="progress-heading"><span>Đang tải video</span><strong>{progress}%</strong></div>
          <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
          <div className="progress-subtext"><span>{bytesText(uploadedBytes)} / {bytesText(file.size)} · tải theo từng khối có thể tiếp tục</span><button className="button button-quiet upload-stop" type="button" onClick={() => abortRef.current?.abort()}>Dừng tải</button></div>
        </div>
      )}

      {validationMessage && <div className={`upload-feedback${error ? " upload-feedback--error" : ""}`} role={error ? "alert" : "status"}>{!error && <Icon name="check" size={15} />}{error || validationMessage}</div>}
      {!validationMessage && error && <div className="upload-feedback upload-feedback--error" role="alert"><Icon name="alert" size={15} />{error}</div>}
      {file && !uploading && error && retryable && <button className="button button-quiet upload-retry" type="button" onClick={() => void startUpload(file)} disabled={disabled || !config}><Icon name="refresh" size={16} />Tiếp tục tải cùng tệp</button>}
      {file && !uploading && error && needsProjectRefresh && <button className="button button-quiet upload-retry" type="button" onClick={() => void onUploaded()}><Icon name="refresh" size={16} />Làm mới thông tin video</button>}
    </div>
  );
}

function JobLog({ projectId }: { projectId: string }) {
  const [jobs, setJobs] = useState<ProjectJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [details, setDetails] = useState<JobDetails | null>(null);

  useEffect(() => {
    let active = true;
    void listJobs(projectId).then((result) => { if (active) setJobs(result.items); }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Không tải được Job.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId]);

  const selectJob = async (jobId: string) => {
    if (selectedJobId === jobId) { setSelectedJobId(""); setDetails(null); return; }
    setSelectedJobId(jobId);
    try { setDetails(await getJobDetails(projectId, jobId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Không tải được lịch sử lần chạy."); }
  };

  if (loading) return <div className="loading-inline"><span className="spinner" /> Đang tải nhật ký Job…</div>;
  if (error) return <div className="error-banner" role="alert"><Icon name="alert" size={17} />{error}</div>;
  if (jobs.length === 0) return <EmptyState title="Chưa có Job nền" description="Nhật ký sẽ xuất hiện khi một Job thực sự được tạo." />;

  return (
    <div className="job-list">
      {jobs.map((job) => (
        <article className="job-entry" key={job.id}>
          <button className="job-row" type="button" onClick={() => void selectJob(job.id)} aria-expanded={selectedJobId === job.id}>
            <span className={`job-status-dot job-status-dot--${job.status}`} />
            <span className="job-row-main"><strong>{job.kind.replaceAll("_", " ")}</strong><span>{job.error_message || job.progress_message || "Không có thông báo chi tiết"}</span></span>
            <span className={`status-chip status-chip--${job.status}`}>{job.status}</span>
            <span className="job-row-time">{timeText(job.created_at)}</span>
          </button>
          {selectedJobId === job.id && details && (
            <div className="attempt-list">
              <div className="attempt-heading"><strong>Lịch sử lần chạy</strong><span>{details.attempts.length} lần</span></div>
              {details.attempts.length === 0 ? <span className="muted-copy">Job chưa được worker nhận.</span> : details.attempts.map((attempt) => (
                <div className="attempt-row" key={attempt.id}>
                  <span>#{attempt.attempt_number}</span><span className={`status-chip status-chip--${attempt.status}`}>{attempt.status}</span><span>{attempt.worker_id}</span><span>{attempt.failure_code || attempt.error_message || timeText(attempt.started_at)}</span>
                </div>
              ))}
            </div>
          )}
        </article>
      ))}
    </div>
  );
}

function EmptyState({ title, description }: { title: string; description: string }) {
  return <section className="workspace-empty"><h3>{title}</h3><p>{description}</p></section>;
}

function segmentTime(us: number): string {
  const totalMs = Math.floor(us / 1000);
  const minutes = Math.floor(totalMs / 60000);
  const seconds = Math.floor((totalMs % 60000) / 1000);
  const millis = totalMs % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function TranslationEditor({ projectId, transcriptAvailable, onSeek, onDirtyChange }: { projectId: string; transcriptAvailable: boolean; onSeek: (us: number) => void; onDirtyChange: (dirty: boolean) => void }) {
  const [state, setState] = useState<TranslationState | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const dirty = state?.units.filter((unit) => drafts[unit.id] !== undefined && drafts[unit.id] !== unit.final_text) ?? [];
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty.length > 0;
  useEffect(() => { onDirtyChange(dirty.length > 0); return () => onDirtyChange(false); }, [dirty.length, onDirtyChange]);
  const refresh = useCallback(async () => {
    const next = await getTranslation(projectId);
    setState((previous) => dirtyRef.current && previous ? { ...next, run: previous.run, units: previous.units, stale: next.stale || previous.run?.id !== next.run?.id } : next);
  }, [projectId]);
  useEffect(() => { void refresh().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Không tải được bản dịch.')); }, [refresh]);
  useEffect(() => {
    const status = state?.latest_job?.status;
    if (status !== 'queued' && status !== 'running') return;
    const timer = window.setInterval(() => { void refresh().catch(() => undefined); }, 3000);
    return () => window.clearInterval(timer);
  }, [state?.latest_job?.status, refresh]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirtyRef.current) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, []);
  const current = state?.latest_job && ['queued', 'running'].includes(state.latest_job.status) ? state.latest_job : null;
  const configuredWait = Number(import.meta.env.VITE_WORKER_QUEUE_WARN_SECONDS ?? 60);
  const queueWarningSeconds = Number.isFinite(configuredWait) && configuredWait > 0 ? configuredWait : 60;
  const queuedTooLong = current?.status === 'queued' && Date.now() - Date.parse(current.created_at) >= queueWarningSeconds * 1000;
  const runAction = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(''); setMessage('');
    try { await action(); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Yêu cầu thất bại.'); } finally { setBusy(false); }
  };
  const save = async () => {
    if (!state?.run || !dirty.length) return;
    setBusy(true); setError(''); setMessage('');
    try {
      await saveTranslation(projectId, state.run.id, dirty.map((unit) => ({ unit_id: unit.id, revision: unit.revision, final_text: drafts[unit.id] })));
      const next = await getTranslation(projectId);
      setState(next); setDrafts({}); setMessage('Đã lưu thay đổi.');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Không lưu được thay đổi.'); } finally { setBusy(false); }
  };
  return <section className="translation-editor">
    <div className="transcript-heading"><strong>Bản dịch tiếng Việt</strong><span>{dirty.length ? `${dirty.length} thay đổi chưa lưu` : 'Đối chiếu và chỉnh sửa'}</span></div>
    <div className="transcription-actions">
      {current ? <button type="button" className="button button-quiet" disabled={busy} onClick={() => void runAction(() => cancelTranslation(projectId, current.id))}>Hủy dịch</button> :
        <button type="button" className="button button-primary" disabled={!transcriptAvailable || busy || dirty.length > 0} onClick={() => {
          if (state?.run && !window.confirm('Tạo bản dịch mới? Bản đang hoạt động được giữ cho đến khi bản mới hoàn tất.')) return;
          void runAction(() => startTranslation(projectId));
        }}>Dịch bằng Gemini</button>}
      {state?.run && <button type="button" className="button button-quiet" disabled={!dirty.length || busy || state.stale} onClick={() => void save()}>{busy ? 'Đang xử lý…' : `Lưu thay đổi${dirty.length ? ` (${dirty.length})` : ''}`}</button>}
    </div>
    {current && <div className="transcription-progress" role="status"><span className="spinner" />{current.status === 'queued' ? 'Đang chờ Worker' : (current.progress_message || 'Đang xử lý bản dịch')}</div>}
    {queuedTooLong && <p className="translation-review" role="status">Job đang chờ lâu. Hãy kiểm tra AutoDub đang chạy bằng .\run.cmd và Translation Worker đã kết nối.</p>}
    {state?.stale && <p className="transcription-error">Transcript nguồn đã thay đổi. Bản dịch cũ được giữ để đối chiếu.</p>}
    {state?.latest_job?.status === 'failed' && <div className="transcription-error" role="alert">{state.latest_job.error_message || 'Dịch chưa hoàn tất.'} · {state.latest_job.error_code}</div>}
    {error && <div className="error-banner" role="alert">{error} {dirty.length > 0 && <button type="button" className="text-link" onClick={() => { if (window.confirm('Tải bản mới và bỏ nội dung chưa lưu?')) { setDrafts({}); dirtyRef.current = false; void refresh(); } }}>Tải lại dữ liệu</button>}</div>}
    {message && <p role="status">{message}</p>}
    {state?.units.map((unit) => <article className="translation-unit" key={unit.id}>
      <button type="button" className="translation-source" onClick={() => onSeek(unit.start_us)}><time>{segmentTime(unit.start_us)} – {segmentTime(unit.end_us)}</time><span>{unit.source_text}</span></button>
      {unit.needs_review && <p className="translation-review">Cần xem lại: {unit.review_reason || 'Kiểm tra nghĩa và ngữ cảnh.'}</p>}
      <textarea aria-label={`Bản dịch ${unit.position + 1}`} value={drafts[unit.id] ?? unit.final_text} onChange={(event) => setDrafts((previous) => ({ ...previous, [unit.id]: event.target.value }))} />
    </article>)}
  </section>;
}

function TranscriptPanel({ projectId, activeJobId, sourceReady, onSeek, onTranslationSeek, onDirtyChange }: { projectId: string; activeJobId: string | null; sourceReady: boolean; onSeek: (timeUs: number) => void; onTranslationSeek: (timeUs: number) => void; onDirtyChange: (dirty: boolean) => void }) {
  const [state, setState] = useState<TranscriptionState>({ active_job: null, latest_job: null });
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadingSegments, setLoadingSegments] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const next = await getTranscriptionState(projectId);
    setState(next);
    const completedId = next.active_job?.id ?? activeJobId;
    if (completedId) {
      const page = await listSegments(projectId, completedId, 500, 0);
      setSegments(page.items);
      setHasMore(page.items.length === page.limit);
    } else {
      setSegments([]);
      setHasMore(false);
    }
  }, [activeJobId, projectId]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const next = await getTranscriptionState(projectId);
        if (!alive) return;
        setState(next);
        const completedId = next.active_job?.id ?? activeJobId;
        if (completedId) {
          const page = await listSegments(projectId, completedId, 500, 0);
          if (alive) { setSegments(page.items); setHasMore(page.items.length === page.limit); }
        } else if (alive) { setSegments([]); setHasMore(false); }
      } catch (reason) {
        if (alive) setError(reason instanceof Error ? reason.message : "Không tải được trạng thái nhận diện.");
      }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 4000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [activeJobId, projectId]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Không thể cập nhật Job nhận diện."); }
    finally { setBusy(false); }
  };

  const latest = state.latest_job;
  const current = latest && ["queued", "running"].includes(latest.status) ? latest : null;
  const asrQueueThreshold = Number(import.meta.env.VITE_WORKER_QUEUE_WARN_SECONDS ?? 60);
  const asrQueuedTooLong = current?.status === 'queued' && Date.now() - Date.parse(current.created_at) >= (Number.isFinite(asrQueueThreshold) && asrQueueThreshold > 0 ? asrQueueThreshold : 60) * 1000;
  const terminal = latest && ["failed", "cancelled"].includes(latest.status);
  const canStart = sourceReady && !current;

  const loadMore = async () => {
    if (!state.active_job && !activeJobId) return;
    setLoadingSegments(true);
    try {
      const page = await listSegments(projectId, state.active_job?.id ?? activeJobId ?? undefined, 500, segments.length);
      setSegments((prior) => [...prior, ...page.items]);
      setHasMore(page.items.length === page.limit);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Không tải thêm được Segment."); }
    finally { setLoadingSegments(false); }
  };

  return (
    <div className="transcript-surface">
      <div className="transcription-controls">
        <div><div className="eyebrow">WHISPER · BẢN GHI NGUỒN</div><h2>Bản nhận diện</h2></div>
        <div className="transcription-actions">
          {current ? <>
            <span className={`status-chip status-chip--${current.status}`}>{current.status === "queued" ? "Đang chờ Worker" : "Đang chạy"}</span>
            <button type="button" className="button button-quiet" disabled={busy} onClick={() => void run(() => cancelTranscription(projectId, current.id))}>Hủy nhận diện</button>
          </> : <button type="button" className="button button-primary" disabled={!canStart || busy} onClick={() => void run(() => startTranscription(projectId))}>
            <Icon name={terminal ? "refresh" : "wave"} size={16} />{terminal ? "Thử nhận diện lại" : "Nhận diện bằng Whisper"}
          </button>}
        </div>
      </div>
      {current && <div className="transcription-progress" role="status"><span className="spinner" />{current.status === 'queued' ? 'Đang chờ Worker' : current.progress_message}</div>}
      {asrQueuedTooLong && <p className="translation-review" role="status">Job đang chờ lâu. Hãy kiểm tra AutoDub đang chạy bằng .\run.cmd và ASR Worker đã kết nối.</p>}
      {current && segments.length > 0 && <p className="muted-copy">Đang giữ bản nhận diện đang hoạt động; chỉ chuyển sang bản mới sau khi Job hoàn tất kiểm tra.</p>}
      {error && <div className="error-banner" role="alert"><Icon name="alert" size={17} />{error}<button type="button" className="text-link" onClick={() => void run(refresh)}>Làm mới</button></div>}
      {!sourceReady && <EmptyState title="Cần video nguồn đã xác minh" description="Tải video lên và đợi xác minh xong trước khi tạo transcript." />}
      {latest?.status === "failed" && <div className="transcription-error" role="alert"><strong>Nhận diện chưa hoàn tất</strong><span>{latest.error_message || latest.progress_message || "Worker không thể hoàn thành Job."}{latest.error_code ? ` · ${latest.error_code}` : ""}</span></div>}
      {latest?.status === "cancelled" && !current && <p className="muted-copy">Job nhận diện gần nhất đã được hủy. Có thể tạo một lần chạy mới.</p>}
      {!current && latest?.status === "succeeded" && segments.length === 0 && <p className="muted-copy">Transcript đã hoàn tất nhưng chưa có Segment để hiển thị.</p>}
      {segments.length > 0 && <>
        <div className="transcript-heading"><strong>{segments.length} Segment nguồn</strong><span>Nhấn vào dòng để tua video</span></div>
        <ol className="transcript-segments">
          {segments.map((segment) => <li key={segment.id}>
            <button type="button" className="transcript-segment" onClick={() => onSeek(segment.start_us)} aria-label={`Tua video đến ${segmentTime(segment.start_us)}`}>
              <time>{segmentTime(segment.start_us)} – {segmentTime(segment.end_us)}</time><span>{segment.source_text}</span>
            </button>
          </li>)}
        </ol>
        {hasMore && <button type="button" className="button button-quiet transcript-load-more" disabled={loadingSegments} onClick={() => void loadMore()}>{loadingSegments ? "Đang tải…" : "Tải Segment tiếp theo"}</button>}
      </>}
      {!current && !latest && sourceReady && <EmptyState title="Chưa có bản nhận diện" description="Tạo Job để bắt đầu nhận diện tiếng nói bằng Faster-Whisper." />}
      {!sourceReady && current && <p className="muted-copy">Job hiện có: {current.status}</p>}
      <TranslationEditor projectId={projectId} transcriptAvailable={Boolean(state.active_job || activeJobId)} onSeek={onTranslationSeek} onDirtyChange={onDirtyChange} />
    </div>
  );
}

function FeaturePlaceholder({ tab }: { tab: Exclude<ProjectTab, "transcription" | "logs"> }) {
  const tts = tab === "tts";
  return (
    <div className="feature-page">
      <EmptyState
        title={tts ? "TTS & đồng bộ chưa triển khai" : "Chưa có video đầu ra"}
        description={tts ? "Supertonic và AV-Sync sẽ được bổ sung sau." : "Render video sẽ được bổ sung sau."}
      />
    </div>
  );
}

export function ProjectWorkspace({ projectId, tab, onTabChange, onBack, onTranslationDirtyChange, backendOnline, onRetryBackend }: { projectId: string; tab: ProjectTab; onTabChange: (tab: ProjectTab) => void; onBack: () => void; onTranslationDirtyChange: (dirty: boolean) => void; backendOnline: boolean; onRetryBackend: () => void }) {
  const [project, setProject] = useState<Project | null>(null);
  const [translationDirty, setTranslationDirty] = useState(false);
  useEffect(() => { onTranslationDirtyChange(translationDirty); }, [translationDirty, onTranslationDirtyChange]);
  const [playback, setPlayback] = useState<PlaybackAsset | null>(null);
  const [config, setConfig] = useState<UploadConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [playbackError, setPlaybackError] = useState("");
  const [refreshingPlayback, setRefreshingPlayback] = useState(false);
  const playerRefreshCount = useRef(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoDimensions, setVideoDimensions] = useState<{ assetId: string; width: number; height: number } | null>(null);
  const videoStream = project?.source_video?.technical_metadata?.streams?.find((stream) => stream.codec_type === "video");
  const streamWidth = videoStream?.width;
  const streamHeight = videoStream?.height;
  const dimensions = videoDimensions?.assetId === project?.source_video_asset_id ? videoDimensions : null;
  const aspectRatio = dimensions ? dimensions.width / dimensions.height
    : typeof streamWidth === "number" && Number.isFinite(streamWidth) && streamWidth > 0 && typeof streamHeight === "number" && Number.isFinite(streamHeight) && streamHeight > 0
      ? streamWidth / streamHeight : 16 / 9;
  const videoFrameStyle = { "--video-ratio": aspectRatio } as CSSProperties;

  const loadProject = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const value = await getProject(projectId);
      setProject(value);
      if (value.source_video_asset_id && value.source_video?.status === "ready") {
        const source = await getSourcePlayback(projectId);
        setPlayback(source);
        setPlaybackError("");
        playerRefreshCount.current = 0;
      } else {
        setPlayback(null);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Không tải được dự án.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadProject();
    void getUploadConfig().then(setConfig).catch(() => setConfig(null));
  }, [loadProject]);

  const refreshPlayback = async () => {
    if (refreshingPlayback) return;
    setRefreshingPlayback(true);
    try {
      const next = await getSourcePlayback(projectId);
      setPlayback(next);
      setPlaybackError("");
    } catch (reason) {
      setPlaybackError(reason instanceof Error ? reason.message : "Không lấy được quyền phát video mới.");
    } finally {
      setRefreshingPlayback(false);
    }
  };

  const handleVideoError = () => {
    if (playerRefreshCount.current === 0) {
      playerRefreshCount.current = 1;
      void refreshPlayback();
    } else {
      setPlaybackError("Trình duyệt không phát được định dạng video này. Hãy thử lấy URL phát mới hoặc mở tệp bằng trình phát khác.");
    }
  };

  const seekVideo = (timeUs: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = timeUs / 1_000_000;
    void video.play().catch(() => undefined);
  };

  const uploadAndReload = async () => {
    await loadProject();
  };

  const currentTab = tabs.find((item) => item.id === tab) ?? tabs[0];

  return (
    <main className="workspace-page">
      <nav className="workspace-navigation" aria-label="Điều hướng dự án">
        <button className="workspace-back" type="button" onClick={() => { if (!translationDirty || window.confirm('Bạn có bản dịch chưa lưu. Rời trang và bỏ thay đổi?')) onBack(); }}><Icon name="arrow-left" size={17} /> Dự án</button>
        <div className="workspace-tabs glass-surface" role="group" aria-label="Các trang chức năng của dự án">
        {tabs.map((item) => (
          <button className={`workspace-tab${tab === item.id ? " workspace-tab--active" : ""}`} type="button" key={item.id} aria-current={tab === item.id ? "page" : undefined} onClick={() => { if (tab === item.id || !translationDirty || window.confirm('Bạn có bản dịch chưa lưu. Chuyển tab và bỏ thay đổi?')) onTabChange(item.id); }}>
            <Icon name={item.icon} size={17} /><span className="tab-full-label">{item.label}</span><span className="tab-short-label">{item.shortLabel}</span>
          </button>
        ))}
        </div>
        <div className="workspace-navigation-end">
          {!backendOnline && <button className="workspace-offline" type="button" onClick={onRetryBackend}><span />Backend chưa kết nối · Thử lại</button>}
        </div>
      </nav>

      {error && <div className="workspace-alert" role="alert"><Icon name="alert" size={18} /><span>{error}</span><button className="button button-quiet" type="button" onClick={() => void loadProject()}>Tải lại</button></div>}

      {loading && !project ? <div className="workspace-loading"><span className="spinner" /> Đang mở Project…</div> : project && (
        <div className={`workspace-layout${tab === "logs" ? " workspace-layout--logs" : ""}`}>
          <aside className="source-column">
            <section className="source-card glass-surface">
              <div className="source-card-header"><div><div className="eyebrow">PROJECT · VIDEO NGUỒN</div><h2 title={project.name}>{project.name}</h2></div></div>
              {playback?.playback_url ? (
                <>
                  <div className="video-frame" style={videoFrameStyle}>
                  <video ref={videoRef} key={playback.playback_url} src={playback.playback_url} controls playsInline preload="metadata" onLoadedMetadata={(event) => { const video = event.currentTarget; if (project.source_video_asset_id && video.videoWidth > 0 && video.videoHeight > 0) setVideoDimensions({ assetId: project.source_video_asset_id, width: video.videoWidth, height: video.videoHeight }); }} onError={handleVideoError} aria-label={`Video nguồn ${project.source_video?.original_filename ?? ""}`} />
                  </div>
                  {playbackError && <div className="player-error" role="alert"><Icon name="alert" size={15} /><span>{playbackError}</span><button className="text-link" type="button" onClick={() => void refreshPlayback()} disabled={refreshingPlayback}>{refreshingPlayback ? "Đang làm mới…" : "Lấy URL mới"}</button></div>}
                  <div className="video-file-meta">
                    <div className="file-name-row"><strong title={project.source_video?.original_filename}>{project.source_video?.original_filename}</strong></div>
                    <dl className="video-metadata-grid">
                      <div><dt>Thời lượng</dt><dd>{durationText(project.source_video?.duration_us ?? null)}</dd></div>
                      <div><dt>Dung lượng</dt><dd>{project.source_video ? bytesText(project.source_video.size_bytes) : "—"}</dd></div>
                      <div><dt>Định dạng</dt><dd>{project.source_video?.original_filename.split(".").pop()?.toUpperCase()}</dd></div>
                      <div><dt>Trạng thái</dt><dd className="text-success">Sẵn sàng</dd></div>
                    </dl>
                  </div>
                </>
              ) : project.status === "deleting" ? (
                <EmptyState title="Project đang được dọn" description="Quay lại danh sách để kiểm tra trạng thái xóa." />
              ) : (
                <UploadBox projectId={projectId} config={config} disabled={!backendOnline} onUploaded={uploadAndReload} />
              )}
            </section>
          </aside>

          <section className="workspace-content glass-surface" aria-label={currentTab.label}>
            <div className="workspace-result-content">
            {tab === "transcription" && <TranscriptPanel projectId={projectId} activeJobId={project.active_transcription_job_id} sourceReady={project.source_video?.status === "ready"} onSeek={seekVideo} onTranslationSeek={(timeUs) => { if (videoRef.current) videoRef.current.currentTime = timeUs / 1_000_000; }} onDirtyChange={setTranslationDirty} />}

            {tab === "tts" && <FeaturePlaceholder tab="tts" />}
            {tab === "export" && <FeaturePlaceholder tab="export" />}
            {tab === "logs" && <div className="logs-surface"><JobLog projectId={projectId} /></div>}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
