import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";

type Screen = "drop" | "queue" | "library" | "settings";
type FileStatus = "pending" | "converting" | "done" | "failed";
type BackendPref = "auto" | "mineru" | "paddleocr";

interface QueueFile {
  id: string;
  path: string;
  name: string;
  size: string;
  status: FileStatus;
  progress: number;
  backend: string; // 实际使用的后端(从日志解析:auto/local/mineru/paddle)
  pages: number;
  log: string[];
  epub?: string;
  error?: string;
}

interface LibraryBook {
  id: string;
  title: string;
  author: string;
  size: string;
  date: string;
  epub: string;
}

const statusColors: Record<FileStatus, string> = {
  pending: "text-[#6B6B6B] border-[#6B6B6B]",
  converting: "text-[#FF4D00] border-[#FF4D00]",
  done: "text-[#1A7A4A] border-[#1A7A4A]",
  failed: "text-[#CC1A1A] border-[#CC1A1A]",
};

const statusBg: Record<FileStatus, string> = {
  pending: "bg-[#6B6B6B]/10",
  converting: "bg-[#FF4D00]/10",
  done: "bg-[#1A7A4A]/10",
  failed: "bg-[#CC1A1A]/10",
};

const backendColors: Record<string, string> = {
  auto: "text-[#6B6B6B] border-[#6B6B6B]",
  local: "text-foreground border-border",
  mineru: "text-[#2255CC] border-[#2255CC]",
  paddle: "text-[#8833CC] border-[#8833CC]",
};

const backendLabel: Record<string, string> = {
  auto: "AUTO",
  local: "LOCAL",
  mineru: "MINERU",
  paddle: "PADDLE",
};

function parseBackendFromLine(line: string, fallback: string): string {
  if (/text\/pymupdf|manual\/pymupdf|\(pymupdf\)|pymupdf/.test(line)) return "local";
  if (/mineru/.test(line)) return "mineru";
  if (/paddleocr|paddle/.test(line)) return "paddle";
  if (/auto|detect/.test(line)) return "auto";
  return fallback;
}

function parsePagesFromLine(line: string, fallback: number): number {
  const m = line.match(/(\d+)\/(\d+)\s*页/);
  if (m) return parseInt(m[2], 10);
  return fallback;
}

function parseTitleAuthor(name: string): { title: string; author: string } {
  const stem = name.replace(/\.(pdf|md|markdown)$/i, "");
  const m = stem.match(/^(.+?)\s+-\s+(.+)$/);
  if (m) return { title: m[1].trim(), author: m[2].trim() };
  return { title: stem, author: "" };
}

function CrosshairMark({ size = 12, className = "" }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 12 12" className={className} aria-hidden="true">
      <line x1="6" y1="0" x2="6" y2="12" stroke="currentColor" strokeWidth="1" />
      <line x1="0" y1="6" x2="12" y2="6" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

function ThinRule({ vertical = false, className = "" }: { vertical?: boolean; className?: string }) {
  return (
    <div
      className={`bg-[var(--border)] ${vertical ? "w-px self-stretch" : "h-px w-full"} ${className}`}
      role="separator"
    />
  );
}

function StatusChip({ status }: { status: FileStatus }) {
  const label = status === "converting" ? "CONVERTING" : status.toUpperCase();
  return (
    <span
      className={`font-mono text-[9px] tracking-[0.12em] px-1.5 py-0.5 border ${statusColors[status]} ${statusBg[status]} uppercase`}
    >
      {label}
    </span>
  );
}

function BackendBadge({ backend }: { backend: string }) {
  const key = backendLabel[backend] ? backend : "auto";
  return (
    <span
      className={`font-mono text-[9px] tracking-[0.1em] px-1.5 py-0.5 border ${backendColors[key]} bg-transparent uppercase`}
    >
      {backendLabel[key]}
    </span>
  );
}

// ── SCREEN 1: DROP ZONE ─────────────────────────────────────────────────────

function DropZoneScreen({ onPick, recent }: { onPick: () => void; recent: QueueFile[] }) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const onDropFiles = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    // Tauri WebView 拖放文件路径受限,统一走原生对话框
    onPick();
  };

  return (
    <div className="flex-1 flex flex-col overflow-auto">
      {/* Section header */}
      <div className="px-10 pt-8 pb-6">
        <div className="flex items-baseline gap-6">
          <h2 className="text-[11px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
            IMPORT
          </h2>
          <ThinRule className="flex-1" />
          <CrosshairMark className="text-[var(--border)]" />
        </div>
      </div>

      {/* Drop zone centered */}
      <div className="flex-1 flex flex-col items-center justify-center px-10">
        <div
          className={`relative w-[440px] h-[360px] border transition-colors duration-150 flex flex-col items-center justify-center gap-6 cursor-pointer
            ${dragging
              ? "border-[#FF4D00] bg-[#FF4D00]/5"
              : "border-dashed border-[var(--border)] hover:border-[var(--foreground)]/40"
            }`}
          style={{ borderWidth: "1.5px", borderStyle: dragging ? "solid" : "dashed" }}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDropFiles}
          onClick={onPick}
          role="button"
          tabIndex={0}
          aria-label="Drop files here or click to browse"
        >
          <input ref={inputRef} type="file" className="hidden" accept=".pdf,.md,.markdown" multiple />

          {/* Corner marks */}
          <CrosshairMark size={10} className="absolute top-3 left-3 text-[var(--muted-foreground)]" />
          <CrosshairMark size={10} className="absolute top-3 right-3 text-[var(--muted-foreground)]" />
          <CrosshairMark size={10} className="absolute bottom-3 left-3 text-[var(--muted-foreground)]" />
          <CrosshairMark size={10} className="absolute bottom-3 right-3 text-[var(--muted-foreground)]" />

          {/* Large circle document icon */}
          <div className={`relative transition-colors ${dragging ? "text-[#FF4D00]" : "text-[var(--foreground)]"}`}>
            <svg width="96" height="96" viewBox="0 0 96 96" fill="none" aria-hidden="true">
              <circle cx="48" cy="48" r="47" stroke="currentColor" strokeWidth="1" />
              <rect x="28" y="22" width="32" height="40" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none" />
              <line x1="34" y1="32" x2="54" y2="32" stroke="currentColor" strokeWidth="1.5" />
              <line x1="34" y1="38" x2="54" y2="38" stroke="currentColor" strokeWidth="1.5" />
              <line x1="34" y1="44" x2="46" y2="44" stroke="currentColor" strokeWidth="1.5" />
              <path d="M48 56 L48 74 M40 66 L48 74 L56 66" stroke={dragging ? "#FF4D00" : "currentColor"} strokeWidth="1.5" strokeLinecap="square" />
            </svg>
          </div>

          <div className="text-center space-y-2">
            <p className="text-[15px] font-medium tracking-tight text-[var(--foreground)]">
              Drop files here
            </p>
            <p className="text-[11px] font-mono tracking-[0.08em] text-[var(--muted-foreground)] uppercase">
              PDF · MARKDOWN · MD
            </p>
          </div>

          <button
            onClick={(e) => { e.stopPropagation(); onPick(); }}
            className="mt-2 px-6 py-2.5 bg-[#FF4D00] text-white text-[11px] font-mono tracking-[0.12em] uppercase hover:bg-[#E04400] transition-colors focus:outline-none focus:ring-2 focus:ring-[#FF4D00] focus:ring-offset-2 focus:ring-offset-[var(--background)]"
          >
            Browse files
          </button>
        </div>

        <p className="mt-4 text-[10px] font-mono tracking-[0.1em] text-[var(--muted-foreground)] uppercase">
          Auto-detect text / scanned / hybrid · OCR optional
        </p>
      </div>

      {/* Recent files */}
      {recent.length > 0 && (
        <div className="px-10 pb-8">
          <div className="flex items-baseline gap-4 mb-4">
            <span className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
              RECENT
            </span>
            <ThinRule className="flex-1" />
          </div>
          <div className="space-y-0 border border-[var(--border)]">
            {recent.map((file, i) => (
              <div key={file.id}>
                {i > 0 && <ThinRule />}
                <div className="flex items-center px-4 py-3 hover:bg-[var(--secondary)] transition-colors group">
                  <svg width="14" height="16" viewBox="0 0 14 16" className="text-[var(--muted-foreground)] mr-3 shrink-0" fill="none">
                    <rect x="0.5" y="0.5" width="11" height="15" rx="0.5" stroke="currentColor" strokeWidth="1" />
                    <path d="M9 0.5 L13.5 5" stroke="currentColor" strokeWidth="1" />
                    <path d="M9 0.5 L9 5 L13.5 5" stroke="currentColor" strokeWidth="1" fill="none" />
                  </svg>
                  <span className="flex-1 text-[12px] tracking-tight text-[var(--foreground)] truncate">{file.name}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] mr-6">{file.size}</span>
                  <StatusChip status={file.status} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── SCREEN 2: CONVERSION QUEUE ───────────────────────────────────────────────

function QueueScreen({ files, onCancel }: { files: QueueFile[]; onCancel: (id: string) => void }) {
  const done = files.filter(f => f.status === "done").length;
  const total = files.length;
  const overallPct = total === 0
    ? 0
    : Math.round(files.reduce((sum, f) => sum + f.progress, 0) / (total * 100) * 100);

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header with big progress */}
      <div className="px-10 pt-8 pb-0">
        <div className="flex items-baseline gap-6 mb-6">
          <h2 className="text-[11px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
            QUEUE
          </h2>
          <ThinRule className="flex-1" />
          <CrosshairMark className="text-[var(--border)]" />
        </div>

        {/* Overall progress */}
        <div className="grid grid-cols-[1fr_auto] gap-8 items-end mb-6 border border-[var(--border)] px-6 py-5">
          <div>
            <div className="text-[10px] font-mono tracking-[0.14em] text-[var(--muted-foreground)] uppercase mb-3">
              OVERALL PROGRESS — {done}/{total} FILES
            </div>
            <div className="h-px w-full bg-[var(--border)] relative overflow-hidden">
              <div
                className="absolute inset-y-0 left-0 bg-[#FF4D00] transition-all duration-700"
                style={{ width: `${overallPct}%`, height: "2px", top: "-0.5px" }}
              />
            </div>
          </div>
          <div className="text-right">
            <span className="font-mono text-[56px] font-300 leading-none text-[var(--foreground)] tabular-nums tracking-tighter">
              {overallPct}
            </span>
            <span className="font-mono text-[20px] text-[var(--muted-foreground)] ml-1">%</span>
          </div>
        </div>
      </div>

      {/* File list */}
      <div className="flex-1 overflow-auto px-10 pb-8">
        {total === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center space-y-3">
            <CrosshairMark className="text-[var(--border)]" />
            <p className="text-[12px] text-[var(--muted-foreground)]">No files in queue</p>
            <p className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">
              Go to IMPORT and add files
            </p>
          </div>
        ) : (
          <div className="border border-[var(--border)]">
            {/* Column headers */}
            <div className="grid grid-cols-[2fr_80px_90px_90px_120px_44px] gap-4 px-4 py-2 border-b border-[var(--border)] bg-[var(--muted)]">
              {["FILE", "SIZE", "PAGES", "STATUS", "BACKEND", ""].map((h, i) => (
                <span key={i} className="font-mono text-[9px] tracking-[0.14em] text-[var(--muted-foreground)] uppercase">
                  {h}
                </span>
              ))}
            </div>

            {files.map((file, i) => (
              <div key={file.id}>
                {i > 0 && <ThinRule />}
                <div className="grid grid-cols-[2fr_80px_90px_90px_120px_44px] gap-4 px-4 py-3 items-center hover:bg-[var(--secondary)] transition-colors">
                  <div className="min-w-0">
                    <div className="text-[12px] font-medium truncate text-[var(--foreground)]">
                      {file.name}
                    </div>
                    {/* Per-file progress bar */}
                    <div className="mt-1.5 h-px bg-[var(--border)] relative overflow-visible">
                      <div
                        className={`absolute top-0 left-0 h-px transition-all duration-700 ${
                          file.status === "failed" ? "bg-[#CC1A1A]" :
                          file.status === "done" ? "bg-[#1A7A4A]" : "bg-[#FF4D00]"
                        }`}
                        style={{ width: `${file.progress}%` }}
                      />
                    </div>
                    {file.status === "converting" && file.log.length > 0 && (
                      <div className="mt-1 font-mono text-[9px] text-[var(--muted-foreground)] truncate">
                        {file.log[file.log.length - 1]}
                      </div>
                    )}
                    {file.error && (
                      <div className="mt-1 font-mono text-[9px] text-[#CC1A1A] truncate">
                        {file.error}
                      </div>
                    )}
                  </div>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{file.size}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{file.pages || "—"}</span>
                  <div><StatusChip status={file.status} /></div>
                  <div><BackendBadge backend={file.backend} /></div>
                  <div className="flex justify-end">
                    {(file.status === "pending" || file.status === "converting") && (
                      <button
                        onClick={() => onCancel(file.id)}
                        className="w-6 h-6 flex items-center justify-center text-[var(--muted-foreground)] hover:text-[#CC1A1A] transition-colors"
                        aria-label="Cancel"
                      >
                        <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                          <line x1="1" y1="1" x2="9" y2="9" stroke="currentColor" strokeWidth="1.5" />
                          <line x1="9" y1="1" x2="1" y2="9" stroke="currentColor" strokeWidth="1.5" />
                        </svg>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Legend */}
        {total > 0 && (
          <div className="mt-5 flex items-center gap-6">
            {(["pending", "converting", "done", "failed"] as FileStatus[]).map(s => (
              <div key={s} className="flex items-center gap-2">
                <StatusChip status={s} />
              </div>
            ))}
            <ThinRule className="flex-1" />
            <span className="font-mono text-[9px] tracking-[0.12em] text-[var(--muted-foreground)] uppercase">
              {files.filter(f => f.status === "converting").length} ACTIVE
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── SCREEN 3: LIBRARY ────────────────────────────────────────────────────────

function LibraryScreen({ books, outputDir }: { books: LibraryBook[]; outputDir: string }) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const openFolder = async (epub: string) => {
    try {
      await revealItemInDir(epub);
    } catch (e) {
      console.error("reveal failed", e);
    }
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-10 pt-8 pb-6">
        <div className="flex items-baseline gap-6 mb-1">
          <h2 className="text-[11px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
            LIBRARY
          </h2>
          <ThinRule className="flex-1" />
          <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">
            {books.length} EPUB
          </span>
          <CrosshairMark className="text-[var(--border)]" />
        </div>
        <p className="font-mono text-[10px] tracking-[0.08em] text-[var(--muted-foreground)] uppercase mt-3">
          Converted output — from this session
        </p>
      </div>

      <div className="flex-1 overflow-auto px-10 pb-8">
        {books.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center space-y-3">
            <CrosshairMark className="text-[var(--border)]" />
            <p className="text-[12px] text-[var(--muted-foreground)]">No converted books yet</p>
            <p className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">
              Converted EPUBs will appear here
            </p>
          </div>
        ) : (
          <>
            {/* Column headers */}
            <div className="grid grid-cols-[1fr_100px_100px_180px] gap-4 px-4 py-2 border border-[var(--border)] bg-[var(--muted)] mb-0 border-b-0">
              {["TITLE / AUTHOR", "SIZE", "DATE", "ACTIONS"].map(h => (
                <span key={h} className="font-mono text-[9px] tracking-[0.14em] text-[var(--muted-foreground)] uppercase">{h}</span>
              ))}
            </div>

            <div className="border border-[var(--border)]">
              {books.map((book, i) => (
                <div key={book.id}>
                  {i > 0 && <ThinRule />}
                  <div
                    className="grid grid-cols-[1fr_100px_100px_180px] gap-4 px-4 py-3 items-center hover:bg-[var(--secondary)] transition-colors cursor-default"
                    onMouseEnter={() => setHoveredId(book.id)}
                    onMouseLeave={() => setHoveredId(null)}
                  >
                    {/* Title/author */}
                    <div className="min-w-0">
                      <div className="text-[12px] font-medium text-[var(--foreground)] truncate">{book.title}</div>
                      {book.author && (
                        <div className="text-[10px] font-mono text-[var(--muted-foreground)] truncate mt-0.5">{book.author}</div>
                      )}
                    </div>

                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{book.size}</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{book.date}</span>

                    {/* Actions */}
                    <div className={`flex items-center gap-3 transition-opacity ${hoveredId === book.id ? "opacity-100" : "opacity-0"}`}>
                      <button
                        onClick={() => openFolder(book.epub)}
                        className="text-[10px] font-mono tracking-[0.1em] text-[var(--foreground)] uppercase hover:text-[#FF4D00] transition-colors"
                      >
                        OPEN FOLDER
                      </button>
                      <div className="w-px h-3 bg-[var(--border)]" />
                      <button
                        onClick={() => revealItemInDir(book.epub)}
                        className="text-[10px] font-mono tracking-[0.1em] text-[var(--foreground)] uppercase hover:text-[#FF4D00] transition-colors"
                      >
                        REVEAL
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}

        {/* Storage summary */}
        <div className="mt-6 flex items-center gap-4 border border-[var(--border)] px-5 py-3">
          <CrosshairMark size={10} className="text-[var(--muted-foreground)]" />
          <span className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">
            Output directory
          </span>
          <span className="font-mono text-[11px] text-[var(--foreground)] truncate">{outputDir}</span>
        </div>
      </div>
    </div>
  );
}

// ── SCREEN 4: SETTINGS ────────────────────────────────────────────────────────

interface ToggleProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  description?: string;
}

function Toggle({ checked, onChange, label, description }: ToggleProps) {
  return (
    <label className="flex items-start gap-4 cursor-pointer group">
      <div className="mt-0.5">
        <button
          role="switch"
          aria-checked={checked}
          onClick={() => onChange(!checked)}
          className={`relative w-8 h-4 border transition-colors focus:outline-none focus:ring-1 focus:ring-[#FF4D00] focus:ring-offset-1 focus:ring-offset-[var(--background)]
            ${checked ? "bg-[#FF4D00] border-[#FF4D00]" : "bg-transparent border-[var(--border)]"}`}
        >
          <span
            className={`absolute top-0.5 w-3 h-3 bg-white transition-transform
              ${checked ? "translate-x-4" : "translate-x-0.5"}`}
          />
        </button>
      </div>
      <div>
        <div className="text-[12px] font-medium text-[var(--foreground)] group-hover:text-[var(--foreground)]">{label}</div>
        {description && (
          <div className="text-[10px] font-mono text-[var(--muted-foreground)] mt-0.5">{description}</div>
        )}
      </div>
    </label>
  );
}

function SettingsScreen({
  darkMode,
  setDarkMode,
  backendPref,
  setBackendPref,
  outputDir,
  setOutputDir,
}: {
  darkMode: boolean;
  setDarkMode: (v: boolean) => void;
  backendPref: BackendPref;
  setBackendPref: (v: BackendPref) => void;
  outputDir: string;
  setOutputDir: (v: string) => void;
}) {
  const [minerUToken, setMinerUToken] = useState("");
  const [paddleToken, setPaddleToken] = useState("");
  const [removePageNums, setRemovePageNums] = useState(true);
  const [joinLines, setJoinLines] = useState(true);
  const [boldFonts, setBoldFonts] = useState(false);
  const [embedImages, setEmbedImages] = useState(true);
  const [cliPath, setCliPath] = useState("(auto-detect)");

  const browseDir = async () => {
    const sel = await open({ directory: true });
    if (sel) setOutputDir(sel);
  };

  const save = () => {
    localStorage.setItem("pdf2epub.backend", backendPref);
    localStorage.setItem("pdf2epub.outputDir", outputDir);
    // 注意:Token 不落库 — CLI 从项目根 apikey.json 读取(见 README)
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-10 pt-8 pb-6">
        <div className="flex items-baseline gap-6">
          <h2 className="text-[11px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase">
            SETTINGS
          </h2>
          <ThinRule className="flex-1" />
          <CrosshairMark className="text-[var(--border)]" />
        </div>
      </div>

      <div className="flex-1 overflow-auto px-10 pb-8">
        <div className="grid grid-cols-2 gap-8 max-w-[900px]">

          {/* Left column */}
          <div className="space-y-8">
            {/* OCR Backend */}
            <section>
              <div className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase mb-4">
                OCR BACKEND
              </div>
              <div className="border border-[var(--border)]">
                {([
                  { key: "auto" as BackendPref, label: "Auto Detect", desc: "Local for text PDFs · OCR for scanned" },
                  { key: "mineru" as BackendPref, label: "MinerU", desc: "Cloud-based, best for complex layouts" },
                  { key: "paddleocr" as BackendPref, label: "PaddleOCR-VL", desc: "Cloud vision model, alternative backend" },
                ]).map((b, i) => (
                  <div key={b.key}>
                    {i > 0 && <ThinRule />}
                    <button
                      onClick={() => setBackendPref(b.key)}
                      className={`w-full flex items-center gap-4 px-4 py-4 text-left transition-colors hover:bg-[var(--secondary)]
                        ${backendPref === b.key ? "bg-[var(--secondary)]" : ""}`}
                    >
                      <div className={`w-3 h-3 border flex items-center justify-center transition-colors
                        ${backendPref === b.key ? "border-[#FF4D00]" : "border-[var(--border)]"}`}>
                        {backendPref === b.key && (
                          <div className="w-1.5 h-1.5 bg-[#FF4D00]" />
                        )}
                      </div>
                      <div>
                        <div className={`text-[12px] font-medium ${backendPref === b.key ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"}`}>
                          {b.label}
                        </div>
                        <div className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5">
                          {b.desc}
                        </div>
                      </div>
                      {backendPref === b.key && (
                        <span className="ml-auto font-mono text-[9px] tracking-[0.12em] text-[#FF4D00] uppercase">
                          ACTIVE
                        </span>
                      )}
                    </button>
                  </div>
                ))}
              </div>
            </section>

            {/* API Tokens */}
            <section>
              <div className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase mb-4">
                API CREDENTIALS
              </div>
              <div className="space-y-4">
                <div>
                  <label className="block font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1.5">
                    MinerU API Token
                  </label>
                  <input
                    type="password"
                    value={minerUToken}
                    onChange={e => setMinerUToken(e.target.value)}
                    placeholder="Read from apikey.json · optional"
                    className="w-full px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                  />
                </div>
                <div>
                  <label className="block font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1.5">
                    PaddleOCR Token
                  </label>
                  <input
                    type="password"
                    value={paddleToken}
                    onChange={e => setPaddleToken(e.target.value)}
                    placeholder="Read from apikey.json · optional"
                    className="w-full px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                  />
                </div>
                <p className="font-mono text-[9px] leading-relaxed text-[var(--muted-foreground)]">
                  Tokens are read from apikey.json in the project root and never stored by the app.
                </p>
              </div>
            </section>
          </div>

          {/* Right column */}
          <div className="space-y-8">
            {/* Output directory */}
            <section>
              <div className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase mb-4">
                OUTPUT DIRECTORY
              </div>
              <div className="flex gap-0">
                <input
                  type="text"
                  value={outputDir}
                  onChange={e => setOutputDir(e.target.value)}
                  className="flex-1 px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors"
                />
                <button
                  onClick={browseDir}
                  className="px-4 border border-l-0 border-[var(--border)] bg-[var(--secondary)] text-[10px] font-mono tracking-[0.1em] text-[var(--foreground)] hover:bg-[var(--muted)] transition-colors uppercase whitespace-nowrap"
                >
                  BROWSE
                </button>
              </div>
              <div className="mt-3">
                <label className="block font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1.5">
                  Converter CLI Path
                </label>
                <input
                  type="text"
                  value={cliPath}
                  onChange={e => setCliPath(e.target.value)}
                  placeholder="ebook-converter (auto-detect from project .venv)"
                  className="w-full px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                />
              </div>
            </section>

            {/* Cleaning options */}
            <section>
              <div className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase mb-4">
                CLEANING OPTIONS
              </div>
              <div className="space-y-5">
                <Toggle
                  checked={removePageNums}
                  onChange={setRemovePageNums}
                  label="Remove page numbers"
                  description="Strip standalone page-number lines"
                />
                <Toggle
                  checked={joinLines}
                  onChange={setJoinLines}
                  label="Join broken lines"
                  description="Re-flow paragraphs split across pages"
                />
                <Toggle
                  checked={boldFonts}
                  onChange={setBoldFonts}
                  label="Mark emphasis fonts as bold"
                  description="KaiTi / STZhongsong → semantic strong"
                />
                <Toggle
                  checked={embedImages}
                  onChange={setEmbedImages}
                  label="Embed images"
                  description="Keep images referenced from work/ output"
                />
              </div>
            </section>

            {/* Theme */}
            <section>
              <div className="text-[10px] font-mono tracking-[0.18em] text-[var(--muted-foreground)] uppercase mb-4">
                APPEARANCE
              </div>
              <div className="border border-[var(--border)]">
                {[{ label: "Light", val: false }, { label: "Dark", val: true }].map(({ label, val }, i) => (
                  <div key={label}>
                    {i > 0 && <ThinRule />}
                    <button
                      onClick={() => setDarkMode(val)}
                      className={`w-full flex items-center gap-4 px-4 py-3 text-left transition-colors hover:bg-[var(--secondary)]
                        ${darkMode === val ? "bg-[var(--secondary)]" : ""}`}
                    >
                      <div className={`w-3 h-3 border flex items-center justify-center
                        ${darkMode === val ? "border-[#FF4D00]" : "border-[var(--border)]"}`}>
                        {darkMode === val && <div className="w-1.5 h-1.5 bg-[#FF4D00]" />}
                      </div>
                      <span className={`text-[12px] font-medium ${darkMode === val ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"}`}>
                        {label}
                      </span>
                    </button>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </div>

        {/* Save */}
        <div className="mt-10 flex items-center gap-6 max-w-[900px]">
          <ThinRule className="flex-1" />
          <button
            onClick={save}
            className="px-8 py-2.5 bg-[#FF4D00] text-white text-[10px] font-mono tracking-[0.14em] uppercase hover:bg-[#E04400] transition-colors focus:outline-none focus:ring-2 focus:ring-[#FF4D00] focus:ring-offset-2 focus:ring-offset-[var(--background)]"
          >
            SAVE SETTINGS
          </button>
        </div>
      </div>
    </div>
  );
}

// ── APP SHELL ─────────────────────────────────────────────────────────────────

const NAV_ITEMS: { screen: Screen; label: string; shortcut: string }[] = [
  { screen: "drop", label: "Import", shortcut: "01" },
  { screen: "queue", label: "Queue", shortcut: "02" },
  { screen: "library", label: "Library", shortcut: "03" },
  { screen: "settings", label: "Settings", shortcut: "04" },
];

export default function App() {
  const [screen, setScreen] = useState<Screen>("drop");
  const [darkMode, setDarkMode] = useState(false);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [backendPref, setBackendPref] = useState<BackendPref>(
    () => (localStorage.getItem("pdf2epub.backend") as BackendPref) || "auto",
  );
  const [outputDir, setOutputDir] = useState<string>(
    () => localStorage.getItem("pdf2epub.outputDir") || "output",
  );
  const canceled = useRef<Set<string>>(new Set());

  // 监听 CLI 进度事件
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    listen<{ file: string; line: string }>("conv://progress", (e) => {
      const { file, line } = e.payload;
      setFiles(prev =>
        prev.map(f =>
          f.path === file && f.status === "converting"
            ? {
                ...f,
                progress: Math.min(95, f.progress + 6),
                pages: parsePagesFromLine(line, f.pages),
                backend: parseBackendFromLine(line, f.backend),
                log: [...f.log.slice(-40), line],
              }
            : f,
        ),
      );
    }).then((un) => { unlisten = un; });
    return () => { unlisten?.(); };
  }, []);

  const convertOne = useCallback(async (f: QueueFile) => {
    if (canceled.current.has(f.id)) return;
    setFiles(prev => prev.map(x => x.path === f.path ? { ...x, status: "converting" as FileStatus, progress: 5, log: [] } : x));
    try {
      const res = await invoke<{ success: boolean; epub: string | null; error: string | null }>("convert_file", {
        filePath: f.path,
        outputDir,
        backend: backendPref === "auto" ? null : backendPref,
        retries: 1,
      });
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.path === f.path ? {
        ...x,
        status: res.success ? "done" as FileStatus : "failed" as FileStatus,
        progress: res.success ? 100 : 60,
        epub: res.epub ?? undefined,
        error: res.error ?? undefined,
      } : x));
    } catch (err) {
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.path === f.path ? { ...x, status: "failed" as FileStatus, error: String(err) } : x));
    }
  }, [outputDir, backendPref]);

  const addFiles = useCallback(async (paths: string[]) => {
    const newFiles: QueueFile[] = paths.map((p, i) => ({
      id: `${Date.now()}-${i}`,
      path: p,
      name: p.split(/[\\/]/).pop() || p,
      size: "—",
      status: "pending" as FileStatus,
      progress: 0,
      backend: "auto",
      pages: 0,
      log: [],
    }));
    setFiles(prev => [...prev, ...newFiles]);
    // 串行转换
    for (const f of newFiles) {
      await convertOne(f);
    }
  }, [convertOne]);

  const pickFiles = useCallback(async () => {
    const sel = await open({
      multiple: true,
      filters: [{ name: "PDF / Markdown", extensions: ["pdf", "md", "markdown"] }],
    });
    if (sel) {
      const paths = Array.isArray(sel) ? sel : [sel];
      await addFiles(paths);
      setScreen("queue");
    }
  }, [addFiles]);

  const cancelFile = useCallback((id: string) => {
    canceled.current.add(id);
    setFiles(prev => prev.map(f => f.id === id ? { ...f, status: "failed" as FileStatus, error: "Canceled" } : f));
  }, []);

  const handleDarkMode = (v: boolean) => {
    setDarkMode(v);
    document.documentElement.classList.toggle("dark", v);
  };

  const books: LibraryBook[] = files
    .filter(f => f.status === "done" && f.epub)
    .map(f => {
      const { title, author } = parseTitleAuthor(f.name);
      return {
        id: f.id,
        title,
        author,
        size: "—",
        date: new Date().toISOString().slice(0, 10),
        epub: f.epub!,
      };
    });

  const pendingCount = files.filter(f => f.status === "pending" || f.status === "converting").length;

  return (
    <div className={`w-full h-full flex flex-col bg-[var(--background)] text-[var(--foreground)] overflow-hidden select-none ${darkMode ? "dark" : ""}`}>
      {/* Titlebar */}
      <div className="shrink-0 h-11 border-b border-[var(--border)] flex items-center px-6 gap-0">
        {/* Window controls placeholder */}
        <div className="flex items-center gap-1.5 mr-6">
          {["", "", ""].map((_, i) => (
            <div key={i} className="w-3 h-3 rounded-full border border-[var(--border)] bg-[var(--muted)]" />
          ))}
        </div>
        <ThinRule vertical className="mr-6" />

        {/* App name */}
        <div className="flex items-center gap-2 mr-10">
          <div className="w-5 h-5 bg-[#FF4D00] flex items-center justify-center">
            <svg width="10" height="11" viewBox="0 0 10 11" fill="none" aria-hidden="true">
              <path d="M1 1 L7 1 L9 3 L9 10 L1 10 Z" stroke="white" strokeWidth="1" fill="none" />
              <path d="M7 1 L7 3 L9 3" stroke="white" strokeWidth="1" fill="none" />
              <path d="M3 5 L5 7 L7 5" stroke="white" strokeWidth="1" strokeLinecap="square" />
              <line x1="5" y1="3.5" x2="5" y2="7" stroke="white" strokeWidth="1" />
            </svg>
          </div>
          <span className="font-mono text-[11px] font-medium tracking-[0.06em] text-[var(--foreground)]">
            pdf2epub
          </span>
          <span className="font-mono text-[9px] text-[var(--muted-foreground)] tracking-[0.06em] ml-1">
            v0.9.4
          </span>
        </div>

        {/* Nav */}
        <nav className="flex items-stretch h-full gap-0" role="navigation" aria-label="Main navigation">
          {NAV_ITEMS.map(({ screen: s, label, shortcut }) => (
            <button
              key={s}
              onClick={() => setScreen(s)}
              className={`relative flex items-center gap-2.5 px-5 h-full font-mono text-[10px] tracking-[0.1em] uppercase transition-colors focus:outline-none
                ${screen === s
                  ? "text-[var(--foreground)] after:absolute after:bottom-0 after:left-0 after:right-0 after:h-px after:bg-[#FF4D00]"
                  : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                }`}
            >
              <span className="text-[#FF4D00] text-[8px]">{shortcut}</span>
              {label}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-4">
          <CrosshairMark size={10} className="text-[var(--border)]" />
          <span className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase tabular-nums">
            {new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" })}
          </span>
        </div>
      </div>

      {/* Main content */}
      <main className="flex-1 flex overflow-hidden">
        <div className="flex-1 flex flex-col overflow-hidden">
          {screen === "drop" && <DropZoneScreen onPick={pickFiles} recent={files.slice(-5).reverse()} />}
          {screen === "queue" && <QueueScreen files={files} onCancel={cancelFile} />}
          {screen === "library" && <LibraryScreen books={books} outputDir={outputDir} />}
          {screen === "settings" && (
            <SettingsScreen
              darkMode={darkMode}
              setDarkMode={handleDarkMode}
              backendPref={backendPref}
              setBackendPref={setBackendPref}
              outputDir={outputDir}
              setOutputDir={setOutputDir}
            />
          )}
        </div>
      </main>

      {/* Status bar */}
      <div className="shrink-0 h-6 border-t border-[var(--border)] flex items-center px-6 gap-6">
        <CrosshairMark size={8} className="text-[var(--border)]" />
        <span className="font-mono text-[9px] tracking-[0.12em] text-[var(--muted-foreground)] uppercase">
          {pendingCount > 0 ? "WORKING" : "READY"}
        </span>
        <ThinRule vertical />
        <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">
          Backend: {backendPref.toUpperCase()}
        </span>
        <ThinRule vertical />
        <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">
          {pendingCount} job{pendingCount === 1 ? "" : "s"} pending
        </span>
        <div className="ml-auto flex items-center gap-4">
          <span className="font-mono text-[9px] text-[var(--muted-foreground)] truncate max-w-[300px]">
            {outputDir}
          </span>
          <CrosshairMark size={8} className="text-[var(--border)]" />
        </div>
      </div>
    </div>
  );
}
