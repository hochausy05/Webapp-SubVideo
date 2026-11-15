export type IconName =
  | "brand"
  | "folder"
  | "plus"
  | "video"
  | "clock"
  | "dots"
  | "edit"
  | "trash"
  | "arrow-left"
  | "chevron-left"
  | "chevron-right"
  | "upload"
  | "play"
  | "wave"
  | "sync"
  | "export"
  | "terminal"
  | "check"
  | "alert"
  | "close"
  | "refresh"
  | "spinner";

const paths: Record<IconName, string[]> = {
  brand: ["M3 12h3l3-7 5 14 3-7h4"],
  folder: ["M3 7.5A1.5 1.5 0 0 1 4.5 6H10l2 2h7.5A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"],
  plus: ["M12 5v14", "M5 12h14"],
  video: ["M4 6.5A1.5 1.5 0 0 1 5.5 5h9A1.5 1.5 0 0 1 16 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 17.5z", "m16 10 4-2v8l-4-2"],
  clock: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z", "M12 6v6l4 2"],
  dots: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  edit: ["m15 5 4 4", "M4 20l4.5-1 10.8-10.8a2.1 2.1 0 0 0-3-3L5.5 16z"],
  trash: ["M4 7h16", "M10 11v6", "M14 11v6", "m5 7 1 13h12l1-13", "M9 7V4h6v3"],
  "arrow-left": ["M19 12H5", "m12 19-7-7 7-7"],
  "chevron-left": ["m15 18-6-6 6-6"],
  "chevron-right": ["m9 18 6-6-6-6"],
  upload: ["M12 16V4", "m7 9 5-5 5 5", "M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"],
  play: ["m8 5 11 7-11 7z"],
  wave: ["M3 12h2l2-6 4 12 4-12 3 6h3"],
  sync: ["M20 7v5h-5", "M5 9a8 8 0 0 1 13-2", "M4 17v-5h5", "M19 15a8 8 0 0 1-13 2"],
  export: ["M12 3v12", "m7 10 5 5 5-5", "M5 21h14"],
  terminal: ["m4 17 6-5-6-5", "M12 19h8"],
  check: ["m5 12 4 4L19 6"],
  alert: ["M12 9v4", "M12 17h.01", "M10.3 4.4 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3l-7.8-13.6a2 2 0 0 0-3.4 0Z"],
  close: ["M18 6 6 18", "m6 6 12 12"],
  refresh: ["M20 7v5h-5", "M4 17v-5h5", "M5.7 9A7 7 0 0 1 18.4 7L20 12", "M4 12l1.6 5A7 7 0 0 0 18.3 15"],
  spinner: ["M20 12a8 8 0 1 1-8-8"],
};

export function Icon({ name, size = 20, className = "" }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths[name].map((path, index) => <path d={path} key={`${name}-${index}`} />)}
    </svg>
  );
}
