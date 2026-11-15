import { useCallback, useEffect, useRef, useState } from "react";

import { getBackendHealth } from "./api/health";
import { ProjectLibrary } from "./components/ProjectLibrary";
import { Icon } from "./components/Icon";
import { ProjectWorkspace, type ProjectTab } from "./components/ProjectWorkspace";

type Route = { kind: "library" } | { kind: "project"; projectId: string; tab: ProjectTab };
type Connection = "checking" | "online" | "offline";

function currentRoute(): Route {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] !== "projects" || !parts[1]) return { kind: "library" };
  const possibleTab = parts[2] as ProjectTab | undefined;
  const tab: ProjectTab = ["transcription", "tts", "export", "logs"].includes(possibleTab ?? "") ? possibleTab as ProjectTab : "transcription";
  return { kind: "project", projectId: decodeURIComponent(parts[1]), tab };
}

function pathForProject(projectId: string, tab: ProjectTab): string {
  return `/projects/${encodeURIComponent(projectId)}/${tab}`;
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute);
  const [connection, setConnection] = useState<Connection>("checking");
  const dirtyRef = useRef(false);
  const routePathRef = useRef(window.location.pathname);

  const navigate = useCallback((path: string) => {
    if (window.location.pathname !== path) window.history.pushState({}, "", path);
    routePathRef.current = path;
    setRoute(currentRoute());
    window.scrollTo(0, 0);
  }, []);

  const checkConnection = useCallback(async () => {
    setConnection("checking");
    try {
      await getBackendHealth();
      setConnection("online");
    } catch {
      setConnection("offline");
    }
  }, []);

  useEffect(() => {
    void checkConnection();
  }, [checkConnection]);

  useEffect(() => {
    const handlePopState = () => {
      if (dirtyRef.current && !window.confirm('Bạn có bản dịch chưa lưu. Rời trang và bỏ thay đổi?')) {
        window.history.pushState({}, '', routePathRef.current);
        return;
      }
      dirtyRef.current = false;
      routePathRef.current = window.location.pathname;
      setRoute(currentRoute());
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  return (
    <div className={`app-frame${route.kind === "project" ? " app-frame--workspace" : ""}`}>
      {route.kind === "library" && <header className="app-topbar glass-surface">
        <button className="brand-lockup" type="button" onClick={() => navigate("/")} aria-label="AutoDub 2.0 — về thư viện dự án">
          <span className="brand-mark"><Icon name="brand" size={20} /></span>
          <span className="brand-name">AutoDub <span>2.0</span></span>
        </button>
        <div className="topbar-right">
          {connection === "offline" ? (
            <button className="backend-status backend-status--offline topbar-connection" type="button" onClick={() => void checkConnection()}><span />Backend chưa kết nối <Icon name="refresh" size={14} /></button>
          ) : (
            <div className={`backend-status${connection === "online" ? " backend-status--online" : " backend-status--checking"}`} role="status"><span />{connection === "online" ? "Backend hoạt động" : "Đang kết nối"}</div>
          )}
          <span className="topbar-divider" />
          <span className="local-badge"><span />LOCAL</span>
        </div>
      </header>}

      {route.kind === "library" ? (
        <ProjectLibrary onOpen={(projectId) => navigate(pathForProject(projectId, "transcription"))} backendOnline={connection === "online"} />
      ) : (
        <ProjectWorkspace
          key={route.projectId}
          projectId={route.projectId}
          tab={route.tab}
          onTabChange={(tab) => navigate(pathForProject(route.projectId, tab))}
          onBack={() => navigate("/")}
          onTranslationDirtyChange={(dirty) => { dirtyRef.current = dirty; }}
          backendOnline={connection === "online"}
          onRetryBackend={() => void checkConnection()}
        />
      )}

      {route.kind === "library" && <footer className="app-footer"><span>AutoDub 2.0</span><span>Local workspace · {connection === "online" ? "Dữ liệu đã kết nối" : "Đang chờ backend"}</span></footer>}
    </div>
  );
}
