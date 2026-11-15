import { useCallback, useEffect, useState, type FormEvent } from "react";

import { createProject, deleteProject, listProjects, updateProject, type Project } from "../api/projects";
import { Icon } from "./Icon";
import { Modal } from "./Modal";

const PAGE_SIZE = 12;

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(value >= 100 * 1024 * 1024 ? 0 : 1)} MB`;
}

function formatDuration(durationUs: number | null | undefined): string {
  if (!durationUs) return "—";
  const seconds = Math.floor(durationUs / 1_000_000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}` : `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function formatUpdated(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Vừa cập nhật";
  return new Intl.DateTimeFormat("vi-VN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function ProjectLibrary({ onOpen, backendOnline }: { onOpen: (projectId: string) => void; backendOnline: boolean }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Project | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [busyProjectId, setBusyProjectId] = useState("");
  const [deletingProjectId, setDeletingProjectId] = useState("");

  const refresh = useCallback(async (pageOffset = offset) => {
    setLoading(true);
    setError("");
    try {
      const page = await listProjects(PAGE_SIZE, pageOffset);
      if (page.items.length === 0 && pageOffset > 0) {
        setOffset(Math.max(0, pageOffset - PAGE_SIZE));
        return;
      }
      setProjects(page.items);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Không tải được danh sách dự án.");
    } finally {
      setLoading(false);
    }
  }, [offset]);

  useEffect(() => { void refresh(offset); }, [offset, refresh]);

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const description = String(form.get("description") ?? "").trim();
    setIsCreating(true);
    setError("");
    try {
      const project = await createProject(name, description || undefined);
      setCreateOpen(false);
      onOpen(project.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Không tạo được dự án.");
    } finally {
      setIsCreating(false);
    }
  };

  const handleRename = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!renameTarget) return;
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    setBusyProjectId(renameTarget.id);
    setError("");
    try {
      await updateProject(renameTarget.id, { name });
      setRenameTarget(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Không đổi được tên dự án.");
    } finally {
      setBusyProjectId("");
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const project = deleteTarget;
    setDeletingProjectId(project.id);
    setDeleteTarget(null);
    setError("");
    setNotice("");
    try {
      const result = await deleteProject(project.id);
      if (result === "pending") {
        setNotice(`“${project.name}” đang chờ phiên upload hoặc worker kết thúc. Dự án vẫn được giữ lại; bạn có thể thử xóa lại sau.`);
      } else {
        setNotice(`Đã xóa “${project.name}” và dữ liệu Storage.`);
      }
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Chưa xóa được dự án. Dữ liệu vẫn được giữ để thử lại.");
      await refresh();
    } finally {
      setDeletingProjectId("");
    }
  };

  const hasMore = projects.length === PAGE_SIZE;

  return (
    <main className="library-page">
      <header className="library-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-dot" /> PROJECT WORKSPACE</div>
          <h1>Dự án của bạn</h1>
          <p>Quản lý video nguồn và tiếp tục công việc từ nơi bạn đã dừng lại.</p>
        </div>
        <button className="button button-primary create-project-button" type="button" onClick={() => { setError(""); setCreateOpen(true); }} disabled={!backendOnline}>
          <Icon name="plus" size={18} /> Tạo dự án
        </button>
      </header>

      <div className="library-toolbar">
        <div className="section-heading"><span>Dự án</span><span className="count-chip">{projects.length}{hasMore ? "+" : ""}</span></div>
        <button className="button button-quiet" type="button" onClick={() => void refresh()} disabled={loading}>
          <Icon name="refresh" size={16} className={loading ? "spin-icon" : ""} /> Làm mới
        </button>
      </div>

      {notice && <div className="notice-banner" role="status"><Icon name="check" size={17} /> <span>{notice}</span><button className="icon-button quiet-button" type="button" aria-label="Ẩn thông báo" onClick={() => setNotice("")}><Icon name="close" size={16} /></button></div>}
      {error && <div className="error-banner" role="alert"><Icon name="alert" size={17} /><span>{error}</span><button className="button button-quiet" type="button" onClick={() => void refresh()}>Thử lại</button></div>}

      {loading && projects.length === 0 ? (
        <div className="loading-grid" aria-label="Đang tải dự án">{Array.from({ length: 3 }, (_, i) => <div className="project-skeleton" key={i} />)}</div>
      ) : projects.length === 0 && !error ? (
        <section className="empty-library">
          <div className="empty-orbit"><Icon name="folder" size={28} /></div>
          <h2>Chưa có dự án nào</h2>
          <p>Tạo dự án đầu tiên bằng nút “Tạo dự án” ở phía trên, rồi thêm video nguồn để bắt đầu.</p>
        </section>
      ) : (
        <div className="project-grid">
          {projects.map((project) => (
            <article className={`project-card glass-surface${project.status === "deleting" ? " project-card--deleting" : ""}`} key={project.id}>
              <button className="project-cover" type="button" onClick={() => onOpen(project.id)} aria-label={`Mở dự án ${project.name}`}>
                <span className="cover-icon"><Icon name="video" size={24} /></span>
                <span className="cover-label">{project.source_video?.original_filename ? project.source_video.original_filename.split(".").pop()?.toUpperCase() : "SOURCE VIDEO"}</span>
              </button>
              <div className="project-card-body">
                <div className="project-card-title-row">
                  <button className="project-title-button" type="button" onClick={() => onOpen(project.id)}>{project.name}</button>
                  {project.status === "deleting" ? (
                    <span className={`status-chip ${project.deletion_state === "failed" ? "status-chip--danger" : "status-chip--warning"}`}>{project.deletion_state === "failed" ? "Cần thử lại" : "Đang xóa"}</span>
                  ) : (
                    <details className="project-menu">
                      <summary className="icon-button quiet-button" aria-label={`Thao tác với ${project.name}`} title={`Thao tác với ${project.name}`}><Icon name="dots" size={20} /></summary>
                      <div className="project-menu-popover">
                        <button type="button" onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setRenameTarget(project); }}><Icon name="edit" size={16} /> Đổi tên</button>
                        <button className="menu-danger" type="button" onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setDeleteTarget(project); }}><Icon name="trash" size={16} /> Xóa dự án</button>
                      </div>
                    </details>
                  )}
                </div>
                <div className="project-meta-line">
                  <span><Icon name="clock" size={14} /> {formatUpdated(project.updated_at)}</span>
                </div>
                {project.source_video?.status === "ready" ? (
                  <div className="asset-meta-line">
                    <span><Icon name="video" size={14} /> {formatDuration(project.source_video.duration_us)}</span>
                    <span className="meta-divider" />
                    <span>{formatBytes(project.source_video.size_bytes)}</span>
                    <span className="asset-ready"><Icon name="check" size={13} /> Video đã sẵn sàng</span>
                  </div>
                ) : project.status === "deleting" ? (
                  <div className="asset-warning">{project.deletion_error_message || "Đang chờ thao tác an toàn hoàn tất."}</div>
                ) : (
                  <div className="asset-empty"><span className="status-pip" /> Chưa có video nguồn</div>
                )}
                {project.status === "deleting" && (
                  <button className="button button-danger-outline retry-delete" type="button" disabled={deletingProjectId === project.id} onClick={() => setDeleteTarget(project)}>
                    <Icon name="refresh" size={15} /> {deletingProjectId === project.id ? "Đang thử lại…" : "Thử xóa lại"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="pagination-row">
        <span>{projects.length ? `${offset + 1}–${offset + projects.length} dự án` : ""}</span>
        <div className="pagination-controls">
          <button className="icon-button quiet-button" type="button" aria-label="Trang trước" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0 || loading}><Icon name="chevron-left" /></button>
          <button className="icon-button quiet-button" type="button" aria-label="Trang sau" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={!hasMore || loading}><Icon name="chevron-right" /></button>
        </div>
      </div>

      {createOpen && <Modal title="Tạo dự án" onClose={() => { if (!isCreating) setCreateOpen(false); }}>
        <form className="modal-form" onSubmit={(event) => void handleCreate(event)}>
          <p className="modal-copy">Đặt tên để tạo không gian làm việc mới. Bạn có thể tải video ngay sau đó.</p>
          <label className="field-label" htmlFor="new-project-name">Tên dự án</label>
          <input id="new-project-name" name="name" className="text-input" type="text" maxLength={200} placeholder="Ví dụ: Phim tài liệu tập 01" autoFocus required />
          <label className="field-label" htmlFor="new-project-description">Mô tả <span>Không bắt buộc</span></label>
          <textarea id="new-project-description" name="description" className="text-input text-area" maxLength={20000} rows={3} placeholder="Ghi chú ngắn về dự án" />
          <div className="modal-actions">
            <button className="button button-quiet" type="button" onClick={() => setCreateOpen(false)} disabled={isCreating}>Hủy</button>
            <button className="button button-primary" type="submit" disabled={isCreating}><Icon name={isCreating ? "spinner" : "plus"} className={isCreating ? "spin-icon" : ""} size={16} />{isCreating ? "Đang tạo…" : "Tạo dự án"}</button>
          </div>
        </form>
      </Modal>}

      {renameTarget && <Modal title="Đổi tên dự án" onClose={() => setRenameTarget(null)}>
        <form className="modal-form" onSubmit={(event) => void handleRename(event)}>
          <p className="modal-copy">Tên mới sẽ được lưu vào dự án của bạn.</p>
          <label className="field-label" htmlFor="rename-project-name">Tên dự án</label>
          <input id="rename-project-name" name="name" className="text-input" type="text" maxLength={200} defaultValue={renameTarget.name} autoFocus required />
          <div className="modal-actions">
            <button className="button button-quiet" type="button" onClick={() => setRenameTarget(null)} disabled={busyProjectId === renameTarget.id}>Hủy</button>
            <button className="button button-primary" type="submit" disabled={busyProjectId === renameTarget.id}>{busyProjectId === renameTarget.id ? "Đang lưu…" : "Lưu thay đổi"}</button>
          </div>
        </form>
      </Modal>}

      {deleteTarget && <Modal title="Xóa dự án?" onClose={() => setDeleteTarget(null)}>
        <div className="modal-form">
          <div className="confirm-icon"><Icon name="trash" size={20} /></div>
          <p className="modal-copy">Dự án <strong>“{deleteTarget.name}”</strong> và video trong Storage sẽ được xóa. Backend sẽ giữ dữ liệu nếu chưa dọn được tệp.</p>
          <div className="modal-actions">
            <button className="button button-quiet" type="button" onClick={() => setDeleteTarget(null)}>Giữ lại</button>
            <button className="button button-danger" type="button" onClick={() => void handleDelete()}><Icon name="trash" size={16} /> Xóa dự án</button>
          </div>
        </div>
      </Modal>}
    </main>
  );
}
