import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath, revealItemInDir } from "@tauri-apps/plugin-opener";

// ── I18N ─────────────────────────────────────────────────────────────────────

type Lang = "en" | "zh";

const T = {
  en: {
    nav: ["Import", "Queue", "Library", "Settings"],
    langToggle: "ZH",
    import: {
      section: "IMPORT",
      hint: "Drop files here",
      sub: "PDF · MARKDOWN · MD",
      browse: "Browse files",
      maxSize: "Cloud OCR: ≤200 pages / 200 MB per task",
      autoDetect: "Auto-detect text / scanned / hybrid · OCR optional",
      recent: "RECENT",
      recentAdd: "ADD →",
      preview: {
        title: "TYPE DETECTION",
        empty: "Add a file to preview detection",
        backend: "BACKEND",
        shards: "SHARDS",
        unknown: "Detecting…",
        txtDesc: "Text layer intact — local conversion",
        scnDesc: "Scanned pages — will route to cloud OCR",
        hybDesc: "Mixed — per-page routing (text + OCR)",
        mdDesc: "Markdown source — direct local build",
      },
    },
    queue: {
      section: "QUEUE",
      overall: "OVERALL PROGRESS",
      filesLabel: "FILES",
      done: "DONE",
      active: "ACTIVE",
      failed: "FAILED",
      pending: "PENDING",
      selectAll: "SELECT ALL",
      clearDone: "CLEAR FINISHED",
      retryFailed: "RETRY FAILED",
      cancelAll: "CANCEL ALL",
      activeCount: (n: number) => `${n} ACTIVE`,
      colFile: "FILE",
      colSize: "SIZE",
      colPages: "PAGES",
      colStatus: "STATUS",
      colBackend: "BACKEND",
      retry: "RETRY",
      requeue: "RE-QUEUE",
      cancel: "✕",
      warn: {
        msg: "Pseudo-text layer detected — conversion may be inaccurate without OCR.",
        ocr: "USE OCR",
        local: "CONTINUE LOCAL",
      },
      console: { title: "CONSOLE", tail: "TAIL", pause: "PAUSE" },
      emptyTitle: "No files in queue",
      emptyHint: "Go to IMPORT and add files",
    },
    library: {
      section: "LIBRARY",
      sort: "SORT",
      refresh: "REFRESH",
      openFolder: "OPEN OUTPUT FOLDER",
      sortDate: "DATE",
      sortTitle: "TITLE",
      sortSize: "SIZE",
      filterAll: "ALL",
      openEpub: "OPEN EPUB",
      reconvert: "RE-CONVERT",
      colCover: "COVER",
      colTitle: "TITLE / AUTHOR",
      colSize: "SIZE",
      colPages: "PAGES",
      colDate: "DATE",
      colActions: "ACTIONS",
      totalSize: "total",
      emptyTitle: "No converted books yet",
      emptyHint: "Converted EPUBs will appear here",
    },
    settings: {
      section: "SETTINGS",
      ocrBackend: "OCR BACKEND",
      credentials: "API CREDENTIALS",
      mineruLabel: "MinerU API Token",
      paddleLabel: "PaddleOCR Token",
      autoDesc: "Local for text PDFs · OCR for scanned",
      mineruDesc: "Cloud-based, best for complex layouts",
      paddleDesc: "Cloud vision model, alternative backend",
      outputDir: "OUTPUT DIRECTORY",
      browse: "BROWSE",
      cleaning: "CLEANING OPTIONS",
      cleanOpts: [
        ["Remove page numbers", "Strip standalone page-number lines"],
        ["Join broken lines", "Re-flow paragraphs split across pages"],
        ["Normalize bold fonts", "KaiTi / STZhongsong → semantic strong"],
        ["Embed inline images", "Keep images referenced from work/ output"],
      ] as [string, string][],
      appearance: "APPEARANCE",
      light: "Light",
      dark: "Dark",
      language: "LANGUAGE",
      langEn: "English",
      langZh: "简体中文",
      envCheck: "ENVIRONMENT CHECK",
      save: "SAVE SETTINGS",
      configured: "CONFIGURED",
      missing: "MISSING",
      show: "SHOW",
      active: "ACTIVE",
      darkReady: "DARK MODE READY",
    },
    status: {
      ready: "READY",
      backend: "Backend",
      pending: (n: number) => `${n} jobs pending`,
    },
  },
  zh: {
    nav: ["导入", "队列", "书库", "设置"],
    langToggle: "EN",
    import: {
      section: "导入",
      hint: "拖放文件到此处",
      sub: "PDF · MARKDOWN · MD",
      browse: "浏览文件",
      maxSize: "云端 OCR 单任务上限 200 页 / 200 MB",
      autoDetect: "自动检测：纯文字 / 扫描件 / 混合 · OCR 可选",
      recent: "最近文件",
      recentAdd: "添加 →",
      preview: {
        title: "类型检测",
        empty: "添加文件后预览检测结果",
        backend: "后端",
        shards: "分片",
        unknown: "检测中…",
        txtDesc: "文字层完整 — 本地转换",
        scnDesc: "扫描件 — 将路由至云端 OCR",
        hybDesc: "混合内容 — 按页路由（文字 + OCR）",
        mdDesc: "Markdown 源文件 — 直接本地构建",
      },
    },
    queue: {
      section: "队列",
      overall: "总体进度",
      filesLabel: "文件",
      done: "DONE",
      active: "ACTIVE",
      failed: "FAILED",
      pending: "PENDING",
      selectAll: "全选",
      clearDone: "清除已完成",
      retryFailed: "重试失败",
      cancelAll: "取消全部",
      activeCount: (n: number) => `${n} 进行中`,
      colFile: "文件",
      colSize: "大小",
      colPages: "页数",
      colStatus: "状态",
      colBackend: "后端",
      retry: "重试",
      requeue: "重新入队",
      cancel: "✕",
      warn: {
        msg: "检测到伪文字层 — 不使用 OCR 可能导致转换结果不准确。",
        ocr: "使用 OCR",
        local: "继续本地",
      },
      console: { title: "CONSOLE", tail: "TAIL", pause: "PAUSE" },
      emptyTitle: "队列为空",
      emptyHint: "前往「导入」添加文件",
    },
    library: {
      section: "书库",
      sort: "排序",
      refresh: "刷新",
      openFolder: "打开输出目录",
      sortDate: "日期",
      sortTitle: "标题",
      sortSize: "大小",
      filterAll: "全部",
      openEpub: "打开 EPUB",
      reconvert: "重新转换",
      colCover: "封面",
      colTitle: "标题 / 作者",
      colSize: "大小",
      colPages: "页数",
      colDate: "日期",
      colActions: "操作",
      totalSize: "合计",
      emptyTitle: "还没有转换完成的书籍",
      emptyHint: "转换完成的 EPUB 会显示在这里",
    },
    settings: {
      section: "设置",
      ocrBackend: "OCR 后端",
      credentials: "API 凭证",
      mineruLabel: "MinerU API 密钥",
      paddleLabel: "PaddleOCR 密钥",
      autoDesc: "文字 PDF 走本地 · 扫描件走 OCR",
      mineruDesc: "云端，适合复杂排版",
      paddleDesc: "云端视觉模型，备用后端",
      outputDir: "输出目录",
      browse: "浏览",
      cleaning: "清理选项",
      cleanOpts: [
        ["删除页码", "去除独立页码行"],
        ["合并断行", "重排跨页断行段落"],
        ["规范粗体", "楷体 / 中宋 → 语义 strong"],
        ["嵌入图片", "保留 work/ 输出中的图片"],
      ] as [string, string][],
      appearance: "外观",
      light: "浅色",
      dark: "深色",
      language: "语言",
      langEn: "English",
      langZh: "简体中文",
      envCheck: "环境检查",
      save: "保存设置",
      configured: "已配置",
      missing: "未配置",
      show: "显示",
      active: "ACTIVE",
      darkReady: "深色模式就绪",
    },
    status: {
      ready: "就绪",
      backend: "后端",
      pending: (n: number) => `${n} 个任务等待中`,
    },
  },
} as const;

// ── TYPES ─────────────────────────────────────────────────────────────────────

type Screen = "drop" | "queue" | "library" | "settings";
type FileStatus = "pending" | "converting" | "done" | "failed" | "cancelled";
type Backend = "Local" | "MinerU" | "PaddleOCR" | "Auto";
type FileType = "TXT" | "SCN" | "HYB" | "MD";
type StageState = "pending" | "active" | "done" | "failed";
type BackendPref = "auto" | "mineru" | "paddleocr";

interface StageNode {
  key: string;
  state: StageState;
  isOcr?: boolean;
}

interface QueueFile {
  id: string;
  path: string;
  name: string;
  size: string;
  status: FileStatus;
  progress: number;
  backend: Backend;
  pages: number;
  type?: FileType;
  stages: StageNode[];
  shards?: { current: number; total: number };
  lastLog?: string;
  log: string[];
  error?: string;
  warning?: boolean;
  epub?: string;
  date?: string;
}

interface ConsoleLine {
  ts: string;
  level: "INFO" | "WARN" | "ERROR";
  text: string;
}

interface LibraryBook {
  id: string;
  title: string;
  author: string;
  size: string;
  pages: number;
  date: string;
  type: FileType;
  backend: Backend;
  epub: string;
  path: string;
}

interface EnvState {
  pandoc: { status: string; version: string | null; path: string | null };
  engine: { status: string; version: string | null; path: string | null };
  mineru_configured: boolean;
  paddle_configured: boolean;
}

const STAGE_KEYS = ["DETECT", "EXTRACT", "CLEAN", "BUILD"];

// ── LOG PARSERS(对齐 CLI 真实输出)────────────────────────────────────────────

function parseTypeFromLine(line: string, name: string): FileType | undefined {
  if (/\.md$/i.test(name)) return "MD";
  const m = line.match(/type=(\w+)/);
  if (m) {
    const t = m[1].toLowerCase();
    if (t === "text") return "TXT";
    if (t === "scanned") return "SCN";
    if (t === "hybrid") return "HYB";
  }
  if (/\[hybrid\]|页级路由|hybrid/i.test(line)) return "HYB";
  if (/扫描页|scanned/i.test(line)) return "SCN";
  return undefined;
}

function updateStagesFromLine(line: string, current: StageNode[]): StageNode[] {
  const s = current.map(n => ({ ...n }));
  const mark = (key: string, state: StageState, isOcr?: boolean) => {
    for (let i = 0; i < s.length; i++) {
      if (s[i].key === key) s[i] = { ...s[i], state, isOcr: isOcr ?? s[i].isOcr };
    }
  };
  if (/\[detect\]|type=|\[hybrid\]|文字层|文字页/.test(line)) {
    mark("DETECT", "done");
    const ex = s.find(n => n.key === "EXTRACT");
    if (ex && ex.state === "pending") mark("EXTRACT", "active");
    return s;
  }
  if (/\[mineru\]|\[paddleocr\]|\[OCR\]|ocr/i.test(line)) {
    mark("EXTRACT", "active", true);
    return s;
  }
  if (/pymupdf4llm|本地提取|local parser|提取|pymupdf/.test(line)) {
    mark("EXTRACT", "active", false);
    return s;
  }
  if (/清理|cleaner|\[CLEAN\]|断行|页码/.test(line)) {
    mark("CLEAN", "active");
    return s;
  }
  if (/pandoc|\[BUILD\]|生成 EPUB|打包|\.epub/.test(line)) {
    mark("BUILD", "active");
    return s;
  }
  return s;
}

function parseShardsFromLine(line: string): { current: number; total: number } | undefined {
  const m = line.match(/自动分片\s*(\d+)\s*段/);
  if (m) return { current: 1, total: parseInt(m[1], 10) };
  const s = line.match(/shard\s*(\d+)\s*\/\s*(\d+)/i);
  if (s) return { current: parseInt(s[1], 10), total: parseInt(s[2], 10) };
  return undefined;
}

function parseBackendFromLine(line: string, fallback: Backend): Backend {
  if (/text\/pymupdf|manual\/pymupdf|\(pymupdf\)|pymupdf/.test(line)) return "Local";
  if (/mineru/.test(line)) return "MinerU";
  if (/paddleocr|paddle/.test(line)) return "PaddleOCR";
  return fallback;
}

function parsePagesFromLine(line: string, fallback: number): number {
  const m = line.match(/(\d+)\/(\d+)\s*页/);
  if (m) return parseInt(m[2], 10);
  return fallback;
}

function parseWarningFromLine(line: string): boolean {
  return /伪文字层|疑似|乱码|garbage/i.test(line);
}

function parseTitleAuthor(name: string): { title: string; author: string } {
  const stem = name.replace(/\.(pdf|md|markdown)$/i, "");
  const m = stem.match(/^(.+?)\s+-\s+(.+)$/);
  if (m) return { title: m[1].trim(), author: m[2].trim() };
  return { title: stem, author: "" };
}

const freshStages = (): StageNode[] => STAGE_KEYS.map(k => ({ key: k, state: "pending" as StageState }));

// ── SHARED COMPONENTS ─────────────────────────────────────────────────────────

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

function SectionHeader({ label, lang, children }: { label: string; lang: Lang; children?: ReactNode }) {
  return (
    <div className="flex items-center gap-6 mb-6">
      <h2 className={`text-[11px] text-[var(--muted-foreground)] shrink-0 uppercase ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.18em]"}`}>
        {label}
      </h2>
      <ThinRule className="flex-1" />
      {children}
      <CrosshairMark className="text-[var(--border)]" />
    </div>
  );
}

function TypeBadge({ type }: { type?: FileType }) {
  if (!type) {
    return (
      <span className="text-[8px] font-mono leading-none px-1.5 py-0.5 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] shrink-0">
        ?
      </span>
    );
  }
  if (type === "HYB") {
    return (
      <span className="inline-flex overflow-hidden text-[8px] font-mono leading-none shrink-0" aria-label="HYB">
        <span className="px-1 py-0.5 bg-[#1A3A7A] text-[#89B4FF] border border-[#2255CC]">HY</span>
        <span className="px-1 py-0.5 bg-[var(--muted)] text-[var(--muted-foreground)] border border-l-0 border-[var(--border)]">B</span>
      </span>
    );
  }
  const styles: Record<Exclude<FileType, "HYB">, string> = {
    TXT: "border-[var(--border)] text-[var(--muted-foreground)]",
    SCN: "border-[#2255CC] text-[#2255CC] bg-[#2255CC]/8",
    MD: "border-[#5533AA] text-[#5533AA] bg-[#5533AA]/8",
  };
  return (
    <span className={`text-[8px] font-mono leading-none px-1.5 py-0.5 border shrink-0 ${styles[type]}`}>
      {type}
    </span>
  );
}

const STATUS_COLORS: Record<FileStatus, string> = {
  pending: "text-[#6B6B6B] border-[#6B6B6B] bg-[#6B6B6B]/8",
  converting: "text-[#FF4D00] border-[#FF4D00] bg-[#FF4D00]/8",
  done: "text-[#1A7A4A] border-[#1A7A4A] bg-[#1A7A4A]/8",
  failed: "text-[#CC1A1A] border-[#CC1A1A] bg-[#CC1A1A]/8",
  cancelled: "text-[#8A6A00] border-[#8A6A00] bg-[#8A6A00]/8",
};

function StatusChip({ status }: { status: FileStatus }) {
  const label = status === "converting" ? "CONVERTING" : status.toUpperCase();
  return (
    <span className={`font-mono text-[8px] tracking-[0.1em] px-1.5 py-0.5 border ${STATUS_COLORS[status]}`}>
      {label}
    </span>
  );
}

const BACKEND_STYLES: Record<Backend, string> = {
  Local: "border-[var(--border)] text-[var(--muted-foreground)]",
  MinerU: "border-[#2255CC] text-[#2255CC] bg-[#2255CC]/8",
  PaddleOCR: "border-[#6633AA] text-[#6633AA] bg-[#6633AA]/8",
  Auto: "border-[#FF4D00] text-[#FF4D00] bg-[#FF4D00]/8",
};

function BackendBadge({ backend }: { backend: Backend }) {
  const label = backend === "PaddleOCR" ? "PADDLE" : backend.toUpperCase();
  return (
    <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border shrink-0 ${BACKEND_STYLES[backend]}`}>
      {label}
    </span>
  );
}

function StageStepper({ stages, shards }: { stages: StageNode[]; shards?: { current: number; total: number } }) {
  return (
    <div className="flex items-center gap-0">
      {stages.map((stage, i) => {
        const isActive = stage.state === "active";
        const isDone = stage.state === "done";
        const isFailed = stage.state === "failed";
        const lineColor = isDone ? "bg-[#1A7A4A]" : isActive ? "bg-[#FF4D00]" : "bg-[var(--border)]";
        const nodeColor = isDone
          ? "border-[#1A7A4A] bg-[#1A7A4A] text-white"
          : isActive
          ? "border-[#FF4D00] bg-[#FF4D00]/12 text-[#FF4D00] stage-active"
          : isFailed
          ? "border-[#CC1A1A] bg-[#CC1A1A]/12 text-[#CC1A1A]"
          : "border-[var(--border)] text-[var(--muted-foreground)]";
        const labelColor = isDone ? "text-[#1A7A4A]" : isActive ? "text-[#FF4D00]" : isFailed ? "text-[#CC1A1A]" : "text-[var(--muted-foreground)]";

        return (
          <div key={stage.key} className="flex items-center">
            {i > 0 && <div className={`w-6 h-px ${lineColor}`} />}
            <div className="flex flex-col items-center gap-0.5">
              <div className={`w-4 h-4 border flex items-center justify-center text-[7px] font-mono ${nodeColor}`}>
                {isDone ? "✓" : isFailed ? "✕" : i + 1}
              </div>
              <span className={`text-[7px] font-mono tracking-[0.04em] ${labelColor}`}>
                {stage.isOcr ? "OCR" : stage.key}
              </span>
            </div>
          </div>
        );
      })}
      {shards && (
        <span className="ml-3 font-mono text-[8px] px-1.5 py-0.5 border border-[#FF4D00]/50 text-[#FF4D00] tracking-[0.04em]">
          {shards.current}/{shards.total} SHARDS
        </span>
      )}
    </div>
  );
}

function Toggle({ checked, onChange, label, description, lang }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  description?: string;
  lang: Lang;
}) {
  return (
    <label className="flex items-start gap-4 cursor-pointer group">
      <button
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative mt-0.5 w-8 h-4 border transition-colors focus:outline-none focus:ring-1 focus:ring-[#FF4D00] focus:ring-offset-1 focus:ring-offset-[var(--background)] shrink-0
          ${checked ? "bg-[#FF4D00] border-[#FF4D00]" : "bg-transparent border-[var(--border)]"}`}
      >
        <span className={`absolute top-0.5 w-3 h-3 bg-white transition-transform ${checked ? "translate-x-4" : "translate-x-0.5"}`} />
      </button>
      <div>
        <div className={`text-[12px] font-medium text-[var(--foreground)] ${lang === "zh" ? "cjk-label" : ""}`}>{label}</div>
        {description && (
          <div className={`text-[10px] text-[var(--muted-foreground)] mt-0.5 ${lang === "zh" ? "cjk-label font-mono" : "font-mono"}`}>{description}</div>
        )}
      </div>
    </label>
  );
}

function CoverPlaceholder({ title }: { title: string }) {
  const words = title.split(" ").filter(w => !["the", "a", "an", "and", "of", "in"].includes(w.toLowerCase()));
  const initials = words.slice(0, 2).map(w => w[0]?.toUpperCase() ?? "").join("") || "EP";
  const hues = [220, 170, 0, 280, 30, 200];
  const hue = hues[(title.charCodeAt(0) || 65) % hues.length];
  return (
    <div
      className="w-10 h-14 flex items-center justify-center border border-[var(--border)] relative overflow-hidden shrink-0"
      style={{ backgroundColor: `hsl(${hue}, 28%, 22%)` }}
    >
      <span className="font-mono text-[11px] font-bold text-white/75 z-10 select-none">{initials}</span>
      <div className="absolute bottom-0 right-0 w-5 h-5 opacity-25" style={{ backgroundColor: `hsl(${hue}, 50%, 55%)` }} />
      <div className="absolute top-0 left-0 w-3 h-3 border-r border-b border-white/10" />
    </div>
  );
}

function WarningBanner({ lang, onDismiss, onUseOcr }: { lang: Lang; onDismiss: () => void; onUseOcr: () => void }) {
  const t = T[lang].queue.warn;
  return (
    <div className="flex items-center gap-3 px-4 py-2 border-b border-[#A07000] bg-[#A07000]/8 shrink-0">
      <svg width="14" height="13" viewBox="0 0 14 13" className="text-[#C89000] shrink-0" fill="none">
        <path d="M7 1.5 L12.5 11.5 L1.5 11.5 Z" stroke="currentColor" strokeWidth="1" />
        <line x1="7" y1="5.5" x2="7" y2="9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="square" />
        <rect x="6.4" y="10.2" width="1.2" height="1.2" fill="currentColor" />
      </svg>
      <span className={`text-[10px] text-[#A07000] flex-1 ${lang === "zh" ? "cjk-label" : "font-mono"}`}>{t.msg}</span>
      <button onClick={onUseOcr} className="px-2.5 py-1 bg-[#FF4D00] text-white text-[9px] font-mono tracking-[0.1em] hover:bg-[#E04400] transition-colors">
        {t.ocr}
      </button>
      <button onClick={onDismiss} className="px-2.5 py-1 border border-[#A07000]/50 text-[#A07000] text-[9px] font-mono tracking-[0.1em] hover:border-[#C89000] transition-colors">
        {t.local}
      </button>
      <button onClick={onDismiss} className="ml-1 text-[#A07000] hover:text-[#C89000] transition-colors p-1" aria-label="Dismiss">
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
          <line x1="1" y1="1" x2="7" y2="7" stroke="currentColor" strokeWidth="1.2" />
          <line x1="7" y1="1" x2="1" y2="7" stroke="currentColor" strokeWidth="1.2" />
        </svg>
      </button>
    </div>
  );
}

function ConsolePanel({ expanded, setExpanded, lang, lines }: {
  expanded: boolean;
  setExpanded: (v: boolean) => void;
  lang: Lang;
  lines: ConsoleLine[];
}) {
  const t = T[lang].queue.console;
  const [paused, setPaused] = useState(false);

  return (
    <div
      className="border-t border-[var(--border)] shrink-0 flex flex-col overflow-hidden transition-[height] duration-200"
      style={{ height: expanded ? 240 : 28 }}
    >
      {/* Handle bar */}
      <div
        className="h-7 flex items-center px-4 gap-3 cursor-pointer hover:bg-[var(--secondary)] transition-colors shrink-0"
        onClick={() => setExpanded(!expanded)}
      >
        <CrosshairMark size={8} className="text-[var(--muted-foreground)]" />
        <span className="font-mono text-[9px] tracking-[0.14em] text-[var(--muted-foreground)] uppercase">{t.title}</span>
        <ThinRule className="flex-1" />
        {expanded && (
          <>
            <button
              onClick={e => { e.stopPropagation(); setPaused(!paused); }}
              className={`font-mono text-[9px] tracking-[0.1em] uppercase transition-colors ${paused ? "text-[#FF4D00]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
            >
              {paused ? t.tail : t.pause}
            </button>
            <ThinRule vertical />
          </>
        )}
        <svg width="8" height="5" viewBox="0 0 8 5" className={`text-[var(--muted-foreground)] transition-transform ${expanded ? "rotate-180" : ""}`} fill="none">
          <path d="M1 1 L4 4 L7 1" stroke="currentColor" strokeWidth="1" />
        </svg>
      </div>

      {/* Log content */}
      {expanded && (
        <div className="flex-1 overflow-auto px-4 py-2 space-y-0.5">
          {lines.length === 0 && (
            <div className="font-mono text-[9px] text-[var(--muted-foreground)]">— no output yet —</div>
          )}
          {(paused ? lines.slice(-80) : lines).map((line, i) => (
            <div key={i} className="flex gap-3 leading-5">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums shrink-0 w-14">{line.ts}</span>
              <span className={`font-mono text-[9px] shrink-0 w-9 ${line.level === "ERROR" ? "text-[#CC1A1A]" : line.level === "WARN" ? "text-[#C89000]" : "text-[var(--muted-foreground)]"}`}>
                {line.level}
              </span>
              <span className={`font-mono text-[9px] ${line.level === "ERROR" ? "text-[#CC1A1A]" : "text-[var(--foreground)]"}`}>{line.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── SCREEN 1: DROP ZONE ───────────────────────────────────────────────────────

function DropZoneScreen({ lang, recent, preview, onPick }: {
  lang: Lang;
  recent: QueueFile[];
  preview: QueueFile | null;
  onPick: () => void;
}) {
  const t = T[lang].import;
  const [dragging, setDragging] = useState(false);

  const previewDesc = preview
    ? preview.type === "TXT" ? t.preview.txtDesc
      : preview.type === "SCN" ? t.preview.scnDesc
      : preview.type === "HYB" ? t.preview.hybDesc
      : preview.type === "MD" ? t.preview.mdDesc
      : t.preview.unknown
    : null;

  return (
    <div className="flex-1 flex flex-col overflow-auto">
      <div className="px-10 pt-8 pb-6">
        <SectionHeader label={t.section} lang={lang} />
      </div>

      {/* Two-column: drop zone left, preview right */}
      <div className="flex-1 flex gap-0 px-10 pb-6 min-h-0">
        {/* Left: drop zone */}
        <div className="flex-1 flex flex-col items-center justify-center">
          <div
            className={`relative w-[420px] h-[340px] flex flex-col items-center justify-center gap-5 cursor-pointer transition-colors
              ${dragging ? "border border-[#FF4D00] bg-[#FF4D00]/5" : "border border-dashed border-[var(--border)] hover:border-[var(--foreground)]/40"}`}
            style={{ borderWidth: "1.5px" }}
            onDragOver={e => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); onPick(); }}
            onClick={onPick}
            role="button"
            tabIndex={0}
            aria-label={t.hint}
          >
            <CrosshairMark size={10} className="absolute top-3 left-3 text-[var(--muted-foreground)]" />
            <CrosshairMark size={10} className="absolute top-3 right-3 text-[var(--muted-foreground)]" />
            <CrosshairMark size={10} className="absolute bottom-3 left-3 text-[var(--muted-foreground)]" />
            <CrosshairMark size={10} className="absolute bottom-3 right-3 text-[var(--muted-foreground)]" />

            <div className={`transition-colors ${dragging ? "text-[#FF4D00]" : "text-[var(--foreground)]"}`}>
              <svg width="88" height="88" viewBox="0 0 96 96" fill="none">
                <circle cx="48" cy="48" r="47" stroke="currentColor" strokeWidth="1" />
                <rect x="28" y="20" width="32" height="40" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none" />
                <line x1="34" y1="30" x2="54" y2="30" stroke="currentColor" strokeWidth="1.5" />
                <line x1="34" y1="36" x2="54" y2="36" stroke="currentColor" strokeWidth="1.5" />
                <line x1="34" y1="42" x2="46" y2="42" stroke="currentColor" strokeWidth="1.5" />
                <path d="M48 54 L48 74 M40 66 L48 74 L56 66" stroke={dragging ? "#FF4D00" : "currentColor"} strokeWidth="1.5" strokeLinecap="square" />
              </svg>
            </div>

            <div className="text-center space-y-1.5">
              <p className={`text-[15px] font-medium text-[var(--foreground)] ${lang === "zh" ? "cjk-label" : ""}`}>{t.hint}</p>
              <p className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">{t.sub}</p>
              <p className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label" : "font-mono tracking-[0.06em]"}`}>{t.autoDetect}</p>
            </div>

            <button
              onClick={e => { e.stopPropagation(); onPick(); }}
              className="px-6 py-2.5 bg-[#FF4D00] text-white text-[10px] font-mono tracking-[0.12em] uppercase hover:bg-[#E04400] transition-colors"
            >
              {t.browse}
            </button>
          </div>
          <p className={`mt-3 text-[9px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label" : "font-mono tracking-[0.08em] uppercase"}`}>
            {t.maxSize}
          </p>
        </div>

        {/* Right: type detection preview */}
        <div className="w-[280px] border-l border-[var(--border)] pl-6 flex flex-col gap-4">
          <div className={`text-[10px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.14em] uppercase"}`}>
            {t.preview.title}
          </div>

          {/* Preview card */}
          {preview ? (
            <div className="border border-[var(--border)] p-4 space-y-4">
              <div className="flex items-center gap-2">
                <TypeBadge type={preview.type} />
                <span className="font-mono text-[10px] text-[var(--foreground)] truncate">{preview.name}</span>
              </div>
              <p className={`text-[10px] text-[var(--muted-foreground)] leading-relaxed ${lang === "zh" ? "cjk-label" : "font-mono"}`}>
                {previewDesc ?? t.preview.unknown}
              </p>
              <ThinRule />
              <div className="space-y-2">
                <div className="flex justify-between items-center">
                  <span className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">{t.preview.backend}</span>
                  <BackendBadge backend={preview.backend} />
                </div>
                <div className="flex justify-between items-center">
                  <span className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase">{t.preview.shards}</span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">
                    {preview.shards ? `${preview.shards.total}` : "—"}
                  </span>
                </div>
              </div>
            </div>
          ) : (
            <div className="border border-dashed border-[var(--border)] p-4">
              <p className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label" : "font-mono"}`}>{t.preview.empty}</p>
            </div>
          )}

          {/* Type legend */}
          <div className="space-y-2">
            {(["TXT", "SCN", "HYB", "MD"] as FileType[]).map(tp => (
              <div key={tp} className="flex items-start gap-2">
                <TypeBadge type={tp} />
                <span className={`text-[9px] text-[var(--muted-foreground)] leading-tight mt-0.5 ${lang === "zh" ? "cjk-label" : "font-mono"}`}>
                  {tp === "TXT" ? t.preview.txtDesc : tp === "SCN" ? t.preview.scnDesc : tp === "HYB" ? t.preview.hybDesc : t.preview.mdDesc}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Recent files */}
      {recent.length > 0 && (
        <div className="px-10 pb-8">
          <div className="flex items-center gap-4 mb-3">
            <span className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.18em] uppercase"}`}>
              {t.recent}
            </span>
            <ThinRule className="flex-1" />
          </div>
          <div className="border border-[var(--border)]">
            {recent.map((file, i) => (
              <div key={file.id}>
                {i > 0 && <ThinRule />}
                <div className="flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--secondary)] transition-colors group">
                  <TypeBadge type={file.type} />
                  <span className="flex-1 text-[12px] text-[var(--foreground)] truncate">{file.name}</span>
                  <StatusChip status={file.status} />
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] w-14 text-right">{file.size}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] w-28 text-right">{file.date ?? ""}</span>
                  <button
                    onClick={() => onPick()}
                    className="ml-2 opacity-0 group-hover:opacity-100 font-mono text-[9px] tracking-[0.1em] text-[#FF4D00] uppercase transition-opacity"
                  >
                    {t.recentAdd}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── SCREEN 2: QUEUE ───────────────────────────────────────────────────────────

function QueueScreen({ lang, files, consoleLines, consoleExpanded, setConsoleExpanded, onCancel, onRetry, onClearDone, onRetryFailed, onCancelAll, onUseOcr }: {
  lang: Lang;
  files: QueueFile[];
  consoleLines: ConsoleLine[];
  consoleExpanded: boolean;
  setConsoleExpanded: (v: boolean) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onClearDone: () => void;
  onRetryFailed: () => void;
  onCancelAll: () => void;
  onUseOcr: (id: string) => void;
}) {
  const t = T[lang].queue;
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [showBanner, setShowBanner] = useState(true);

  const done = files.filter(f => f.status === "done").length;
  const active = files.filter(f => f.status === "converting").length;
  const failed = files.filter(f => f.status === "failed").length;
  const pending = files.filter(f => f.status === "pending").length;
  const total = files.length;
  const overallPct = total === 0
    ? 0
    : Math.round(files.reduce((s, f) => s + f.progress, 0) / (total * 100) * 100);

  const toggleAll = () => {
    setSelectedIds(prev => prev.size === total ? new Set() : new Set(files.map(f => f.id)));
  };
  const toggleRow = (id: string) => {
    setExpandedRows(prev => { const s = new Set(prev); if (s.has(id)) { s.delete(id); } else { s.add(id); } return s; });
  };

  const warnFile = files.find(f => f.warning && (f.status === "converting" || f.status === "pending"));

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Warning banner */}
      {showBanner && warnFile && (
        <WarningBanner
          lang={lang}
          onDismiss={() => { setShowBanner(false); }}
          onUseOcr={() => { setShowBanner(false); onUseOcr(warnFile.id); }}
        />
      )}

      <div className="px-10 pt-6 pb-0 shrink-0">
        <SectionHeader label={t.section} lang={lang} />

        {/* Overall progress block */}
        <div className="flex items-center gap-0 border border-[var(--border)] mb-4">
          <div className="flex-1 px-5 py-4">
            <div className={`text-[10px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.14em] uppercase"}`}>
              {t.overall} — {total} {t.filesLabel}
            </div>
            <div className="h-0.5 w-full bg-[var(--border)] relative overflow-visible mb-3">
              <div className="absolute inset-y-0 left-0 bg-[#FF4D00] transition-all duration-700" style={{ width: `${overallPct}%` }} />
              {active > 0 && (
                <div
                  className="absolute top-1/2 -translate-y-1/2 w-1.5 h-1.5 bg-[#FF4D00] progress-tick"
                  style={{ left: `calc(${overallPct}% - 3px)` }}
                />
              )}
            </div>
            <div className="flex gap-4">
              {[{ label: t.done, val: done, color: "#1A7A4A" }, { label: t.active, val: active, color: "#FF4D00" }, { label: t.failed, val: failed, color: "#CC1A1A" }, { label: t.pending, val: pending, color: "#6B6B6B" }].map(({ label, val, color }) => (
                <div key={label} className="flex items-center gap-1.5">
                  <span className="font-mono text-[11px] font-medium tabular-nums" style={{ color }}>{val}</span>
                  <span className="font-mono text-[9px] tracking-[0.08em] text-[var(--muted-foreground)]">{label}</span>
                </div>
              ))}
            </div>
          </div>
          <ThinRule vertical />
          <div className="px-6 text-right shrink-0">
            <span className="font-mono text-[52px] font-light leading-none text-[var(--foreground)] tabular-nums tracking-tighter">{overallPct}</span>
            <span className="font-mono text-[18px] text-[var(--muted-foreground)]">%</span>
          </div>
        </div>

        {/* Toolbar */}
        <div className="flex items-center gap-1 mb-3 border border-[var(--border)] px-3 py-1.5 bg-[var(--muted)]">
          {[
            { label: t.selectAll, action: toggleAll, disabled: total === 0 },
            { label: t.clearDone, action: onClearDone, disabled: done === 0 },
            { label: t.retryFailed, action: onRetryFailed, disabled: failed === 0 },
            { label: t.cancelAll, action: onCancelAll, disabled: active + pending === 0 },
          ].map(({ label, action, disabled }, i) => (
            <span key={label} className="flex items-center gap-1">
              {i > 0 && <ThinRule vertical className="mx-1" />}
              <button
                onClick={action}
                disabled={disabled}
                className={`font-mono text-[9px] tracking-[0.1em] uppercase transition-colors ${lang === "zh" ? "cjk-label font-medium" : ""} ${disabled ? "text-[var(--muted-foreground)]/40 cursor-default" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
              >
                {label}
              </button>
            </span>
          ))}
          <div className="flex-1" />
          <span className="font-mono text-[9px] tracking-[0.1em] text-[#FF4D00]">{t.activeCount(active)}</span>
        </div>

        {/* Column headers */}
        <div className="grid grid-cols-[1fr_70px_64px_108px_100px_80px] gap-3 px-3 py-1.5 border border-b-0 border-[var(--border)] bg-[var(--muted)]">
          {[t.colFile, t.colSize, t.colPages, t.colStatus, t.colBackend, ""].map((h, i) => (
            <span key={i} className="font-mono text-[8px] tracking-[0.12em] text-[var(--muted-foreground)] uppercase">{h}</span>
          ))}
        </div>
      </div>

      {/* File rows */}
      <div className="flex-1 overflow-auto px-10 pb-2">
        {total === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center space-y-3">
            <CrosshairMark className="text-[var(--border)]" />
            <p className="text-[12px] text-[var(--muted-foreground)]">{t.emptyTitle}</p>
            <p className={`font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase ${lang === "zh" ? "cjk-label" : ""}`}>
              {t.emptyHint}
            </p>
          </div>
        ) : (
          <div className="border border-[var(--border)]">
            {files.map((file, i) => {
              const isExpanded = expandedRows.has(file.id);
              const isSelected = selectedIds.has(file.id);
              return (
                <div key={file.id} className={isSelected ? "bg-[#FF4D00]/4" : ""}>
                  {i > 0 && <ThinRule />}
                  {/* Line 1 */}
                  <div
                    className="grid grid-cols-[1fr_70px_64px_108px_100px_80px] gap-3 px-3 py-2 items-center hover:bg-[var(--secondary)] transition-colors cursor-pointer"
                    onClick={() => toggleRow(file.id)}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <TypeBadge type={file.type} />
                      <span className="text-[11px] font-medium text-[var(--foreground)] truncate">{file.name}</span>
                    </div>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{file.size}</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{file.pages || "—"}</span>
                    <StatusChip status={file.status} />
                    <BackendBadge backend={file.backend} />
                    <div className="flex justify-end">
                      {(file.status === "pending" || file.status === "converting") && (
                        <button
                          onClick={e => { e.stopPropagation(); onCancel(file.id); }}
                          className="font-mono text-[9px] text-[var(--muted-foreground)] hover:text-[#CC1A1A] transition-colors"
                          aria-label="Cancel"
                        >
                          {t.cancel}
                        </button>
                      )}
                      {file.status === "failed" && (
                        <button
                          onClick={e => { e.stopPropagation(); onRetry(file.id); }}
                          className="font-mono text-[9px] tracking-[0.08em] text-[#FF4D00] uppercase hover:text-[#CC4400] transition-colors"
                        >
                          {t.retry}
                        </button>
                      )}
                      {file.status === "done" && (
                        <button
                          onClick={e => { e.stopPropagation(); onRetry(file.id); }}
                          className="font-mono text-[9px] tracking-[0.08em] text-[var(--muted-foreground)] uppercase hover:text-[var(--foreground)] transition-colors"
                        >
                          {t.requeue}
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Line 2: stepper + log (always visible, collapses extended log) */}
                  <div className="px-3 pb-2 -mt-1">
                    {/* Progress hairline */}
                    <div className="relative h-0.5 w-full bg-[var(--border)] mb-2 overflow-visible">
                      <div
                        className={`absolute inset-y-0 left-0 transition-all duration-700 ${file.status === "failed" || file.status === "cancelled" ? "bg-[#CC1A1A]" : file.status === "done" ? "bg-[#1A7A4A]" : "bg-[#FF4D00]"}`}
                        style={{ width: `${file.progress}%` }}
                      />
                      {file.status === "converting" && (
                        <div
                          className="absolute top-1/2 -translate-y-1/2 w-1.5 h-1.5 bg-[#FF4D00] progress-tick"
                          style={{ left: `calc(${file.progress}% - 3px)` }}
                        />
                      )}
                    </div>

                    <div className="flex items-center gap-4">
                      <StageStepper stages={file.stages} shards={file.shards} />
                      <div className="flex-1 min-w-0">
                        {isExpanded ? (
                          file.log.length > 0 ? (
                            <pre className="font-mono text-[9px] leading-relaxed text-[var(--muted-foreground)] whitespace-pre-wrap border border-[var(--border)] bg-[var(--muted)] p-2 max-h-36 overflow-auto">
                              {file.log.join("\n")}
                            </pre>
                          ) : (
                            <span className="font-mono text-[9px] text-[var(--muted-foreground)]">— no output yet —</span>
                          )
                        ) : file.error ? (
                          <span className="font-mono text-[9px] text-[#CC1A1A] truncate block">{file.error}</span>
                        ) : file.lastLog ? (
                          <span className="font-mono text-[9px] text-[var(--muted-foreground)] truncate block">{file.lastLog}</span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Legend */}
        {total > 0 && (
          <div className="mt-4 flex items-center gap-4 flex-wrap">
            {(["pending", "converting", "done", "failed", "cancelled"] as FileStatus[]).map(s => (
              <StatusChip key={s} status={s} />
            ))}
            <ThinRule className="flex-1" />
            <span className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)]">
              {active} ACTIVE · {failed} FAILED
            </span>
          </div>
        )}
      </div>

      {/* Console */}
      <ConsolePanel expanded={consoleExpanded} setExpanded={setConsoleExpanded} lang={lang} lines={consoleLines} />
    </div>
  );
}

// ── SCREEN 3: LIBRARY ─────────────────────────────────────────────────────────

type SortKey = "date" | "title" | "size";

function LibraryScreen({ lang, books, outputDir, onRefresh, onOpenFolder, onOpenEpub, onReconvert }: {
  lang: Lang;
  books: LibraryBook[];
  outputDir: string;
  onRefresh: () => void;
  onOpenFolder: (epub: string) => void;
  onOpenEpub: (epub: string) => void;
  onReconvert: (path: string) => void;
}) {
  const t = T[lang].library;
  const [sort, setSort] = useState<SortKey>("date");
  const [backendFilter, setBackendFilter] = useState<Backend | "All">("All");
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const sorted = [...books]
    .filter(b => backendFilter === "All" || b.backend === backendFilter)
    .sort((a, b) => {
      if (sort === "date") return (b.date || "").localeCompare(a.date || "");
      if (sort === "title") return a.title.localeCompare(b.title);
      if (sort === "size") return parseFloat(b.size) - parseFloat(a.size);
      return 0;
    });

  const backends: (Backend | "All")[] = ["All", "Local", "MinerU", "PaddleOCR"];

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-10 pt-8 pb-4 shrink-0">
        <SectionHeader label={t.section} lang={lang}>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums mr-2">{books.length} EPUB</span>
        </SectionHeader>

        {/* Controls row */}
        <div className="flex items-center gap-4 mb-4">
          {/* Sort */}
          <div className="flex items-center gap-1 border border-[var(--border)]">
            <span className={`text-[9px] text-[var(--muted-foreground)] px-2 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em]"}`}>{t.sort}</span>
            <ThinRule vertical />
            {([["date", t.sortDate], ["title", t.sortTitle], ["size", t.sortSize]] as [SortKey, string][]).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setSort(key)}
                className={`px-2 py-1 font-mono text-[9px] tracking-[0.08em] uppercase transition-colors ${sort === key ? "text-[#FF4D00]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Backend filter */}
          <div className="flex items-center gap-0 border border-[var(--border)]">
            {backends.map((b, i) => (
              <button
                key={b}
                onClick={() => setBackendFilter(b)}
                className={`px-2 py-1 font-mono text-[9px] tracking-[0.08em] uppercase transition-colors ${i > 0 ? "border-l border-[var(--border)]" : ""}
                  ${backendFilter === b ? "text-[#FF4D00] bg-[#FF4D00]/8" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
              >
                {b === "All" ? t.filterAll : b === "PaddleOCR" ? "PADDLE" : b.toUpperCase()}
              </button>
            ))}
          </div>

          <div className="flex-1" />

          <button onClick={onRefresh} className={`font-mono text-[9px] tracking-[0.1em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk-label" : ""}`}>
            {t.refresh}
          </button>
          <ThinRule vertical />
          <button onClick={() => onOpenFolder(books[0]?.epub ?? "")} className={`font-mono text-[9px] tracking-[0.1em] uppercase text-[var(--foreground)] hover:text-[#FF4D00] transition-colors ${lang === "zh" ? "cjk-label" : ""}`}>
            {t.openFolder}
          </button>
        </div>

        {/* Column headers */}
        <div className="grid grid-cols-[44px_1fr_80px_72px_96px_164px] gap-4 px-4 py-2 border border-b-0 border-[var(--border)] bg-[var(--muted)]">
          {[t.colCover, t.colTitle, t.colSize, t.colPages, t.colDate, t.colActions].map((h, i) => (
            <span key={i} className={`text-[9px] text-[var(--muted-foreground)] uppercase ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.12em]"}`}>{h}</span>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-auto px-10 pb-4">
        {sorted.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center space-y-3">
            <CrosshairMark className="text-[var(--border)]" />
            <p className="text-[12px] text-[var(--muted-foreground)]">{t.emptyTitle}</p>
            <p className={`font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase ${lang === "zh" ? "cjk-label" : ""}`}>
              {t.emptyHint}
            </p>
          </div>
        ) : (
          <div className="border border-[var(--border)]">
            {sorted.map((book, i) => (
              <div key={book.id}>
                {i > 0 && <ThinRule />}
                <div
                  className="grid grid-cols-[44px_1fr_80px_72px_96px_164px] gap-4 px-4 py-3 items-center hover:bg-[var(--secondary)] transition-colors"
                  onMouseEnter={() => setHoveredId(book.id)}
                  onMouseLeave={() => setHoveredId(null)}
                >
                  <CoverPlaceholder title={book.title} />
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                      <TypeBadge type={book.type} />
                      <span className="text-[12px] font-medium text-[var(--foreground)] truncate">{book.title}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <BackendBadge backend={book.backend} />
                      <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate">{book.author}</span>
                    </div>
                  </div>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{book.size}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{book.pages || "—"}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{(book.date || "").slice(5)}</span>
                  <div className={`flex items-center gap-2 transition-opacity ${hoveredId === book.id ? "opacity-100" : "opacity-0"}`}>
                    <button onClick={() => onOpenEpub(book.epub)} className={`text-[9px] text-[var(--foreground)] uppercase hover:text-[#FF4D00] transition-colors ${lang === "zh" ? "cjk-label font-mono" : "font-mono tracking-[0.08em]"}`}>
                      {t.openEpub}
                    </button>
                    <div className="w-px h-3 bg-[var(--border)]" />
                    <button onClick={() => onReconvert(book.path)} className={`text-[9px] text-[var(--muted-foreground)] uppercase hover:text-[#FF4D00] transition-colors ${lang === "zh" ? "cjk-label font-mono" : "font-mono tracking-[0.08em]"}`}>
                      {t.reconvert}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Footer */}
        {books.length > 0 && (
          <div className="mt-5 flex items-center gap-4 border border-[var(--border)] px-5 py-3">
            <CrosshairMark size={10} className="text-[var(--muted-foreground)]" />
            <span className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em] uppercase"}`}>
              {lang === "zh" ? "输出目录" : "Output directory"}
            </span>
            <span className="font-mono text-[11px] text-[var(--foreground)] truncate">{outputDir}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── SCREEN 4: SETTINGS ────────────────────────────────────────────────────────

function SettingsScreen({ lang, setLang, darkMode, setDarkMode, backendPref, setBackendPref, outputDir, setOutputDir, cliPath, setCliPath, env, onSave }: {
  lang: Lang;
  setLang: (l: Lang) => void;
  darkMode: boolean;
  setDarkMode: (v: boolean) => void;
  backendPref: BackendPref;
  setBackendPref: (v: BackendPref) => void;
  outputDir: string;
  setOutputDir: (v: string) => void;
  cliPath: string;
  setCliPath: (v: string) => void;
  env: EnvState | null;
  onSave: () => void;
}) {
  const t = T[lang].settings;
  const [mineruToken, setMinerUToken] = useState("");
  const [paddleToken, setPaddleToken] = useState("");
  const [cleanOpts, setCleanOpts] = useState([true, true, false, true]);
  const [showMineruToken, setShowMineruToken] = useState(false);

  const envItems = [
    { name: "Pandoc", status: env?.pandoc.status ?? "missing", version: env?.pandoc.version ?? null, path: env?.pandoc.path ?? null },
    { name: "Converter engine", status: env?.engine.status ?? "missing", version: env?.engine.version ?? null, path: env?.engine.path ?? null },
  ];

  const browseDir = async () => {
    const sel = await open({ directory: true });
    if (sel) setOutputDir(String(sel));
  };

  const ConfiguredChip = ({ ok }: { ok: boolean }) => (
    <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border ${ok ? "border-[#1A7A4A] text-[#1A7A4A] bg-[#1A7A4A]/8" : "border-[#A07000] text-[#A07000] bg-[#A07000]/8"}`}>
      {ok ? t.configured : t.missing}
    </span>
  );

  const RadioOpt = ({ label, active, onClick, desc }: { label: string; active: boolean; onClick: () => void; desc: string }) => (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[var(--secondary)] ${active ? "bg-[var(--secondary)]" : ""}`}
    >
      <div className={`w-3 h-3 border flex items-center justify-center shrink-0 ${active ? "border-[#FF4D00]" : "border-[var(--border)]"}`}>
        {active && <div className="w-1.5 h-1.5 bg-[#FF4D00]" />}
      </div>
      <div className="flex-1 text-left">
        <div className={`text-[12px] font-medium ${active ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"} ${lang === "zh" ? "cjk-label" : ""}`}>{label}</div>
        <div className={`text-[10px] text-[var(--muted-foreground)] mt-0.5 ${lang === "zh" ? "cjk-label" : "font-mono"}`}>{desc}</div>
      </div>
      {active && <span className="font-mono text-[8px] tracking-[0.1em] text-[#FF4D00]">{t.active}</span>}
    </button>
  );

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-10 pt-8 pb-6 shrink-0">
        <SectionHeader label={t.section} lang={lang} />
      </div>

      <div className="flex-1 overflow-auto px-10 pb-8">
        <div className="grid grid-cols-2 gap-8 max-w-[880px]">
          {/* Left column */}
          <div className="space-y-7">
            {/* OCR Backend */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.ocrBackend}
              </div>
              <div className="border border-[var(--border)]">
                <RadioOpt
                  label="Auto Detect"
                  active={backendPref === "auto"}
                  onClick={() => setBackendPref("auto")}
                  desc={t.autoDesc}
                />
                <ThinRule />
                <RadioOpt
                  label="MinerU"
                  active={backendPref === "mineru"}
                  onClick={() => setBackendPref("mineru")}
                  desc={t.mineruDesc}
                />
                <ThinRule />
                <RadioOpt
                  label="PaddleOCR-VL"
                  active={backendPref === "paddleocr"}
                  onClick={() => setBackendPref("paddleocr")}
                  desc={t.paddleDesc}
                />
              </div>
            </section>

            {/* API Credentials */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.credentials}
              </div>
              <div className="space-y-4">
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em] uppercase"}`}>
                      {t.mineruLabel}
                    </label>
                    <ConfiguredChip ok={env?.mineru_configured ?? false} />
                  </div>
                  <div className="flex">
                    <input
                      type={showMineruToken ? "text" : "password"}
                      value={mineruToken}
                      onChange={e => setMinerUToken(e.target.value)}
                      placeholder="Read from apikey.json · optional"
                      className="flex-1 px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                    />
                    <button
                      onClick={() => setShowMineruToken(!showMineruToken)}
                      className="px-2.5 border border-l-0 border-[var(--border)] font-mono text-[9px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:border-[var(--foreground)] transition-colors uppercase"
                    >
                      {t.show}
                    </button>
                  </div>
                </div>
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em] uppercase"}`}>
                      {t.paddleLabel}
                    </label>
                    <ConfiguredChip ok={env?.paddle_configured ?? false} />
                  </div>
                  <input
                    type="password"
                    value={paddleToken}
                    onChange={e => setPaddleToken(e.target.value)}
                    placeholder="Read from apikey.json · optional"
                    className="w-full px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                  />
                </div>
                <p className="font-mono text-[9px] leading-relaxed text-[var(--muted-foreground)]">
                  {lang === "zh"
                    ? "凭证从项目根 apikey.json 读取,应用不保存。"
                    : "Tokens are read from apikey.json and never stored by the app."}
                </p>
              </div>
            </section>

            {/* Environment check */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.envCheck}
              </div>
              <div className="border border-[var(--border)]">
                {envItems.map((item, i) => (
                  <div key={item.name}>
                    {i > 0 && <ThinRule />}
                    <div className="flex items-center gap-3 px-3 py-2">
                      <div className={`w-1.5 h-1.5 shrink-0 ${item.status === "ok" ? "bg-[#1A7A4A]" : "bg-[#CC1A1A]"}`} />
                      <span className="font-mono text-[10px] text-[var(--foreground)] flex-1">{item.name}</span>
                      {item.version && (
                        <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">{item.version}</span>
                      )}
                      <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border ${item.status === "ok" ? "border-[#1A7A4A] text-[#1A7A4A]" : "border-[#CC1A1A] text-[#CC1A1A]"}`}>
                        {item.status === "ok" ? "OK" : "MISSING"}
                      </span>
                    </div>
                    {item.path && (
                      <div className="px-3 pb-1.5">
                        <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{item.path}</span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>
          </div>

          {/* Right column */}
          <div className="space-y-7">
            {/* Output directory */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.outputDir}
              </div>
              <div className="flex">
                <input
                  type="text"
                  value={outputDir}
                  onChange={e => setOutputDir(e.target.value)}
                  className="flex-1 px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors"
                />
                <button onClick={browseDir} className={`px-3 border border-l-0 border-[var(--border)] bg-[var(--secondary)] text-[9px] text-[var(--foreground)] hover:bg-[var(--muted)] transition-colors whitespace-nowrap ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em] uppercase"}`}>
                  {t.browse}
                </button>
              </div>
              <div className="mt-3">
                <label className={`block text-[10px] text-[var(--muted-foreground)] mb-1.5 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.1em] uppercase"}`}>
                  {lang === "zh" ? "转换引擎 CLI 路径" : "Converter CLI Path"}
                </label>
                <input
                  type="text"
                  value={cliPath}
                  onChange={e => setCliPath(e.target.value)}
                  placeholder={lang === "zh" ? "ebook-converter(留空 = 自动探测)" : "ebook-converter (empty = auto-detect)"}
                  className="w-full px-3 py-2 border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] font-mono text-[11px] focus:outline-none focus:border-[#FF4D00] transition-colors placeholder:text-[var(--muted-foreground)]/50"
                />
              </div>
            </section>

            {/* Cleaning options */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-4 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.cleaning}
              </div>
              <div className="space-y-4">
                {t.cleanOpts.map(([label, desc], i) => (
                  <Toggle
                    key={i}
                    checked={cleanOpts[i]}
                    onChange={v => setCleanOpts(prev => prev.map((c, j) => j === i ? v : c))}
                    label={label}
                    description={desc}
                    lang={lang}
                  />
                ))}
              </div>
            </section>

            {/* Appearance */}
            <section>
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.appearance}
              </div>
              <div className="border border-[var(--border)] mb-4">
                {[{ label: t.light, val: false }, { label: t.dark, val: true }].map(({ label, val }, i) => (
                  <div key={label}>
                    {i > 0 && <ThinRule />}
                    <button
                      onClick={() => setDarkMode(val)}
                      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-[var(--secondary)] ${darkMode === val ? "bg-[var(--secondary)]" : ""}`}
                    >
                      <div className={`w-3 h-3 border flex items-center justify-center shrink-0 ${darkMode === val ? "border-[#FF4D00]" : "border-[var(--border)]"}`}>
                        {darkMode === val && <div className="w-1.5 h-1.5 bg-[#FF4D00]" />}
                      </div>
                      <span className={`text-[12px] font-medium flex-1 text-left ${darkMode === val ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"} ${lang === "zh" ? "cjk-label" : ""}`}>
                        {label}
                      </span>
                      {val && (
                        <span className={`font-mono text-[8px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label" : ""}`}>{t.darkReady}</span>
                      )}
                    </button>
                  </div>
                ))}
              </div>

              {/* Language */}
              <div className={`text-[10px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.16em] uppercase"}`}>
                {t.language}
              </div>
              <div className="border border-[var(--border)]">
                {([["en", t.langEn], ["zh", t.langZh]] as [Lang, string][]).map(([l, label], i) => (
                  <div key={l}>
                    {i > 0 && <ThinRule />}
                    <button
                      onClick={() => setLang(l)}
                      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-[var(--secondary)] ${lang === l ? "bg-[var(--secondary)]" : ""}`}
                    >
                      <div className={`w-3 h-3 border flex items-center justify-center shrink-0 ${lang === l ? "border-[#FF4D00]" : "border-[var(--border)]"}`}>
                        {lang === l && <div className="w-1.5 h-1.5 bg-[#FF4D00]" />}
                      </div>
                      <span className={`text-[12px] font-medium ${lang === l ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"}`}>{label}</span>
                      <span className="font-mono text-[8px] text-[var(--muted-foreground)] ml-auto">{l.toUpperCase()}</span>
                    </button>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </div>

        {/* Save */}
        <div className="mt-8 flex items-center gap-6 max-w-[880px]">
          <ThinRule className="flex-1" />
          <button onClick={onSave} className={`px-8 py-2.5 bg-[#FF4D00] text-white text-[10px] hover:bg-[#E04400] transition-colors focus:outline-none focus:ring-2 focus:ring-[#FF4D00] focus:ring-offset-2 focus:ring-offset-[var(--background)] ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.14em] uppercase"}`}>
            {t.save}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── APP SHELL ─────────────────────────────────────────────────────────────────

const NAV_SCREENS: Screen[] = ["drop", "queue", "library", "settings"];
const APP_VERSION = "v0.2.0";

export default function App() {
  const [screen, setScreen] = useState<Screen>("drop");
  const [darkMode, setDarkMode] = useState<boolean>(() => localStorage.getItem("pdf2epub.dark") === "1");
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("pdf2epub.lang") as Lang) || "en");
  const [consoleExpanded, setConsoleExpanded] = useState(false);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [consoleLines, setConsoleLines] = useState<ConsoleLine[]>([]);
  const [env, setEnv] = useState<EnvState | null>(null);
  const [backendPref, setBackendPref] = useState<BackendPref>(
    () => (localStorage.getItem("pdf2epub.backend") as BackendPref) || "auto",
  );
  const [outputDir, setOutputDir] = useState<string>(
    () => localStorage.getItem("pdf2epub.outputDir") || "output",
  );
  const [cliPath, setCliPath] = useState<string>(
    () => localStorage.getItem("pdf2epub.cliPath") || "",
  );
  const canceled = useRef<Set<string>>(new Set());

  // 监听 CLI 进度事件
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    listen<{ file: string; line: string }>("conv://progress", (e) => {
      const { file, line } = e.payload;
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: /error|失败/i.test(line) ? "ERROR" : /warn|伪文字层|疑似/i.test(line) ? "WARN" : "INFO",
        text: line,
      }]);
      setFiles(prev => prev.map(f => {
        if (f.path !== file || f.status !== "converting") return f;
        const shards = parseShardsFromLine(line) ?? f.shards;
        const type = f.type === "MD" ? f.type : parseTypeFromLine(line, f.name) ?? f.type;
        const backend = parseBackendFromLine(line, f.backend);
        const pages = parsePagesFromLine(line, f.pages);
        const progress = shards && shards.total > 1
          ? Math.max(f.progress, 20 + ((shards.current - 1) / shards.total) * 55)
          : Math.min(95, f.progress + 5);
        return {
          ...f,
          progress,
          pages,
          backend,
          type,
          stages: updateStagesFromLine(line, f.stages),
          shards,
          warning: f.warning || parseWarningFromLine(line),
          log: [...f.log.slice(-100), line],
          lastLog: line,
        };
      }));
    }).then(un => { unlisten = un; });
    return () => { unlisten?.(); };
  }, []);

  // 主题/语言初始化
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);
  useEffect(() => {
    document.documentElement.classList.toggle("lang-zh", lang === "zh");
  }, [lang]);

  // 设置页打开时刷新环境检查
  useEffect(() => {
    if (screen !== "settings") return;
    invoke<EnvState>("check_env").then(setEnv).catch(() => setEnv(null));
  }, [screen]);

  const convertOne = useCallback(async (f: QueueFile, backendOverride?: string) => {
    if (canceled.current.has(f.id)) return;
    canceled.current.delete(f.id);
    setFiles(prev => prev.map(x => x.id === f.id ? {
      ...x,
      status: "converting",
      progress: 5,
      error: undefined,
      warning: false,
      log: [],
      lastLog: undefined,
      stages: freshStages(),
    } : x));
    try {
      const res = await invoke<{ success: boolean; epub: string | null; error: string | null }>("convert_file", {
        filePath: f.path,
        outputDir,
        backend: backendOverride ?? (backendPref === "auto" ? null : backendPref),
        retries: 1,
        cliPath: cliPath || null,
      });
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status: res.success ? "done" : "failed",
        progress: res.success ? 100 : 60,
        epub: res.epub ?? undefined,
        error: res.error ?? undefined,
        date: res.success ? new Date().toISOString().slice(0, 10) : x.date,
        stages: res.success
          ? x.stages.map(s => ({ ...s, state: "done" as StageState }))
          : x.stages.map(s => s.state === "active" ? { ...s, state: "failed" as StageState } : s),
        lastLog: res.error ?? undefined,
      } : x));
    } catch (err) {
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status: "failed",
        progress: 60,
        error: String(err),
      } : x));
    }
  }, [outputDir, backendPref, cliPath]);

  const addFiles = useCallback(async (paths: string[]) => {
    const newFiles: QueueFile[] = paths.map((p, i) => {
      const name = p.split(/[\\/]/).pop() || p;
      return {
        id: `${Date.now()}-${i}`,
        path: p,
        name,
        size: "—",
        status: "pending" as FileStatus,
        progress: 0,
        backend: "Auto" as Backend,
        pages: 0,
        type: /\.md$/i.test(name) ? "MD" as FileType : undefined,
        stages: freshStages(),
        log: [],
      };
    });
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
    setFiles(prev => prev.map(f => f.id === id ? {
      ...f,
      status: "cancelled" as FileStatus,
      error: undefined,
    } : f));
  }, []);

  const retryFile = useCallback((id: string) => {
    const f = files.find(x => x.id === id);
    if (f) void convertOne(f);
  }, [files, convertOne]);

  const retryFailed = useCallback(async () => {
    for (const f of files) {
      if (f.status === "failed") await convertOne(f);
    }
  }, [files, convertOne]);

  const clearDone = useCallback(() => {
    setFiles(prev => prev.filter(f => f.status !== "done"));
  }, []);

  const cancelAll = useCallback(() => {
    setFiles(prev => prev.map(f =>
      (f.status === "pending" || f.status === "converting") ? { ...f, status: "cancelled" as FileStatus } : f,
    ));
    for (const f of files) {
      if (f.status === "pending" || f.status === "converting") canceled.current.add(f.id);
    }
  }, [files]);

  const useOcrFor = useCallback((id: string) => {
    const f = files.find(x => x.id === id);
    if (f) void convertOne(f, "mineru");
  }, [files, convertOne]);

  const handleDarkMode = (v: boolean) => {
    setDarkMode(v);
    localStorage.setItem("pdf2epub.dark", v ? "1" : "0");
  };

  const handleLang = (l: Lang) => {
    setLang(l);
    localStorage.setItem("pdf2epub.lang", l);
  };

  const saveSettings = useCallback(async () => {
    localStorage.setItem("pdf2epub.backend", backendPref);
    localStorage.setItem("pdf2epub.outputDir", outputDir);
    localStorage.setItem("pdf2epub.cliPath", cliPath);
    try {
      await invoke("set_cli_path", { path: cliPath || null });
    } catch { /* 忽略 */ }
    try {
      const e = await invoke<EnvState>("check_env");
      setEnv(e);
    } catch { /* 忽略 */ }
  }, [backendPref, outputDir, cliPath]);

  const books: LibraryBook[] = files
    .filter(f => f.status === "done" && f.epub)
    .map(f => {
      const { title, author } = parseTitleAuthor(f.name);
      return {
        id: f.id,
        title,
        author,
        size: f.size,
        pages: f.pages,
        date: f.date ?? new Date().toISOString().slice(0, 10),
        type: f.type ?? "TXT",
        backend: f.backend,
        epub: f.epub!,
        path: f.path,
      };
    });

  const openEpub = useCallback(async (epub: string) => {
    try { await openPath(epub); } catch (e) { console.error("open epub failed", e); }
  }, []);
  const openFolder = useCallback(async (epub: string) => {
    try { await revealItemInDir(epub); } catch (e) { console.error("reveal failed", e); }
  }, []);

  const convertingCount = files.filter(f => f.status === "converting").length;
  const activeFile = files.find(f => f.status === "converting");
  const activeStage = activeFile?.stages.find(s => s.state === "active");
  const pendingCount = files.filter(f => f.status === "pending").length;
  const previewFile = files.length > 0 ? files[files.length - 1] : null;

  const t = T[lang];

  return (
    <div className={`w-full h-full flex flex-col overflow-hidden select-none ${darkMode ? "dark" : ""} ${lang === "zh" ? "lang-zh" : ""}`}
      style={{ background: "var(--background)", color: "var(--foreground)" }}>

      {/* Titlebar */}
      <div className="shrink-0 h-11 border-b border-[var(--border)] flex items-center px-5 gap-0" style={{ background: "var(--card)" }}>
        {/* Window controls */}
        <div className="flex items-center gap-1.5 mr-5">
          {[0, 1, 2].map(i => <div key={i} className="w-3 h-3 rounded-full border border-[var(--border)] bg-[var(--muted)]" />)}
        </div>
        <ThinRule vertical className="mr-5" />

        {/* Logo */}
        <div className="flex items-center gap-2 mr-8">
          <div className="w-5 h-5 bg-[#FF4D00] flex items-center justify-center shrink-0">
            <svg width="10" height="11" viewBox="0 0 10 11" fill="none" aria-hidden="true">
              <path d="M1 1 L7 1 L9 3 L9 10 L1 10 Z" stroke="white" strokeWidth="1" fill="none" />
              <path d="M7 1 L7 3 L9 3" stroke="white" strokeWidth="1" fill="none" />
              <path d="M3 5 L5 7 L7 5 M5 3.5 L5 7" stroke="white" strokeWidth="1" strokeLinecap="square" />
            </svg>
          </div>
          <span className="font-mono text-[11px] font-medium tracking-[0.06em]">pdf2epub</span>
          <span className="font-mono text-[8px] text-[var(--muted-foreground)] ml-1">{APP_VERSION}</span>
        </div>

        {/* Nav */}
        <nav className="flex items-stretch h-full" aria-label="Main navigation">
          {NAV_SCREENS.map((s, idx) => {
            const label = t.nav[idx];
            const isActive = screen === s;
            const showBubble = s === "queue" && convertingCount > 0;
            return (
              <button
                key={s}
                onClick={() => setScreen(s)}
                className={`relative flex items-center gap-2 px-5 h-full font-mono text-[10px] tracking-[0.1em] uppercase transition-colors focus:outline-none
                  ${isActive ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
              >
                {isActive && (
                  <div className="absolute bottom-0 left-0 right-0 h-px bg-[#FF4D00]" />
                )}
                <span className="text-[#FF4D00] text-[8px] font-mono">0{idx + 1}</span>
                <span className={lang === "zh" ? "cjk-label" : ""}>{label}</span>
                {showBubble && (
                  <span className="w-4 h-4 rounded-full bg-[#FF4D00] text-white font-mono text-[8px] flex items-center justify-center leading-none">
                    {convertingCount}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {/* ZH|EN toggle */}
          <button
            onClick={() => handleLang(lang === "en" ? "zh" : "en")}
            className="font-mono text-[9px] tracking-[0.12em] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors border border-[var(--border)] px-2 py-0.5 hover:border-[var(--foreground)]"
          >
            {t.langToggle}
          </button>
          <CrosshairMark size={8} className="text-[var(--border)]" />
          <span className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] tabular-nums">
            {new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" })}
          </span>
        </div>
      </div>

      {/* Main content */}
      <main className="flex-1 flex overflow-hidden min-h-0">
        <div className="flex-1 flex flex-col overflow-hidden">
          {screen === "drop" && (
            <DropZoneScreen lang={lang} recent={files.slice(-5).reverse()} preview={previewFile} onPick={pickFiles} />
          )}
          {screen === "queue" && (
            <QueueScreen
              lang={lang}
              files={files}
              consoleLines={consoleLines}
              consoleExpanded={consoleExpanded}
              setConsoleExpanded={setConsoleExpanded}
              onCancel={cancelFile}
              onRetry={retryFile}
              onClearDone={clearDone}
              onRetryFailed={() => void retryFailed()}
              onCancelAll={cancelAll}
              onUseOcr={useOcrFor}
            />
          )}
          {screen === "library" && (
            <LibraryScreen
              lang={lang}
              books={books}
              outputDir={outputDir}
              onRefresh={() => setFiles(prev => [...prev])}
              onOpenFolder={openFolder}
              onOpenEpub={openEpub}
              onReconvert={(p) => {
                const f = files.find(x => x.path === p);
                if (f) void convertOne(f);
              }}
            />
          )}
          {screen === "settings" && (
            <SettingsScreen
              lang={lang}
              setLang={handleLang}
              darkMode={darkMode}
              setDarkMode={handleDarkMode}
              backendPref={backendPref}
              setBackendPref={setBackendPref}
              outputDir={outputDir}
              setOutputDir={setOutputDir}
              cliPath={cliPath}
              setCliPath={setCliPath}
              env={env}
              onSave={() => void saveSettings()}
            />
          )}
        </div>
      </main>

      {/* Status bar */}
      <div className="shrink-0 h-6 border-t border-[var(--border)] flex items-center px-5 gap-4" style={{ background: "var(--card)" }}>
        <CrosshairMark size={8} className="text-[var(--border)]" />
        <span className={`text-[9px] text-[var(--muted-foreground)] uppercase ${lang === "zh" ? "cjk-label font-medium" : "font-mono tracking-[0.12em]"}`}>
          {activeFile ? (activeStage?.isOcr ? "OCR" : activeStage?.key ?? "CONVERTING") : t.status.ready}
        </span>
        <ThinRule vertical />
        <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
          {t.status.backend}: {backendPref.toUpperCase()}
        </span>
        {activeFile && (
          <>
            <ThinRule vertical />
            <span className="font-mono text-[9px] text-[#FF4D00] truncate max-w-[220px]">{activeFile.name}</span>
          </>
        )}
        <ThinRule vertical />
        <span className={`text-[9px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk-label" : "font-mono"}`}>
          {t.status.pending(pendingCount)}
        </span>
        <div className="ml-auto flex items-center gap-3">
          <span className="font-mono text-[9px] text-[var(--muted-foreground)] truncate max-w-[300px]">{outputDir}</span>
          <CrosshairMark size={8} className="text-[var(--border)]" />
        </div>
      </div>
    </div>
  );
}
