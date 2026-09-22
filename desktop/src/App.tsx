import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath, revealItemInDir } from "@tauri-apps/plugin-opener";
import { getCurrentWebview } from "@tauri-apps/api/webview";
// 引擎事件流(--json-events):状态与进度的唯一可信来源(见 src/events.ts)
import {
  initialEventState,
  freshStages,
  parseEventLine,
  progressOf,
  reduceEvent,
  type EventState,
  type Signatures,
  type StageNode,
  type StageState,
} from "./events";

// ── I18N ─────────────────────────────────────────────────────────────────────
//  技术 token(TXT/SCN/HYB/MD、AUTO/LOCAL/MINERU/PADDLE、工序名、状态 chip)
//  两种语言下都保持英文 —— 它们是转换器词汇,不是文案。

type Lang = "en" | "zh";

interface CleanItem {
  glyph: string;
  key: string;
  label: string;
  desc: string;
  advanced?: boolean;
}
interface CleanGroup {
  name: string;
  items: CleanItem[];
}

const T = {
  en: {
    screens: ["CONVERT", "LIBRARY", "SETTINGS"],
    langToggle: "ZH",
    brand: "pdf2epub",
    convert: {
      head: "CONVERT",
      active: (n: number) => `${n} ACTIVE`,
      drop: "Drop files here",
      dropSub: "PDF · MARKDOWN · MD",
      autoDetect: "Auto-detect text / scanned / hybrid · OCR optional",
      browse: "Browse files",
      addFiles: "+ ADD FILES",
      recent: "RECENT",
      preflight: "PREFLIGHT",
      preflightRun: "PREFLIGHT",
      preflightBusy: "DETECTING…",
      selectAll: "SELECT ALL",
      clearDone: "CLEAR FINISHED",
      retryFailed: "RETRY FAILED",
      cancelAll: "CANCEL ALL",
      activeCount: (n: number) => `${n} ACTIVE`,
      colFile: "FILE",
      colType: "TYPE",
      colSize: "SIZE",
      colPages: "PAGES",
      colBackend: "BACKEND",
      colStatus: "STATUS",
      colAction: "ACTION",
      retry: "RETRY",
      requeue: "RE-QUEUE",
      addToQueue: "ADD →",
      detection: {
        title: "TYPE DETECTION",
        empty: "Drop a file to preview",
        backend: "BACKEND",
        signatures: "SIGNATURES",
        cloud: "CLOUD PAGES",
        pages: "PAGES",
        txtDesc: "Text layer intact — local conversion",
        scnDesc: "Scanned pages — cloud OCR required",
        hybDesc: "Mixed — per-page routing",
        mdDesc: "Markdown — direct local build",
      },
      quota: (need: number, quota: number) => `This batch needs ${need} cloud pages; the press takes ${quota}. Split the run.`,
      quotaOk: (need: number, shards: number) => `${need} cloud pages · ${shards} signatures · within today's quota`,
      warn: {
        msg: (f: string) => `Pseudo-text layer detected in ${f}`,
        ocr: "USE OCR",
        local: "CONTINUE LOCAL",
      },
      composing: "COMPOSING ROOM  排字记录",
      tail: "TAIL",
      pause: "PAUSE",
      noOutput: "— no output yet —",
    },
    library: {
      head: "LIBRARY",
      books: (n: number) => `${n} EPUBs`,
      search: "Search title or author…",
      sort: "SORT",
      sortDate: "DATE",
      sortTitle: "TITLE",
      sortSize: "SIZE",
      filterAll: "ALL",
      refresh: "REFRESH",
      openFolder: "OPEN OUTPUT FOLDER",
      openEpub: "OPEN EPUB",
      reconvert: "RE-CONVERT",
      colTitle: "TITLE / AUTHOR",
      colSize: "SIZE",
      colPages: "PAGES",
      colDate: "DATE",
      empty: "No converted books yet",
      emptySub: "Converted EPUBs will appear here",
      emptyFiltered: "Nothing matches this filter",
      colophon: "OUTPUT DIRECTORY",
      totalLabel: "TOTAL",
      total: "total",
      newest: "NEWEST",
      loading: "READING EPUB METADATA…",
    },
    settings: {
      head: "SETTINGS",
      dirty: (n: number) => `${n} UNSAVED`,
      save: "SAVE SETTINGS",
      discard: "DISCARD",
      sections: ["CONVERTER", "OCR & CREDENTIALS", "校勘 CLEANING PIPELINE", "QUALITY & APPEARANCE"],
      outputDir: "Output directory",
      cliPath: "Converter CLI path",
      cliPlaceholder: "auto-detect",
      browse: "BROWSE",
      envCheck: "ENVIRONMENT CHECK",
      pandocMissing: "Pandoc is not installed. pdf2epub needs it to build EPUB files.",
      installCmd: "winget install pandoc",
      copy: "COPY",
      copied: "COPIED",
      backendLabel: "OCR backend for the next conversion",
      backends: [
        ["Auto detect", "Local for text PDFs · cloud OCR for scanned"],
        ["MinerU", "Cloud OCR — best for complex layouts"],
        ["PaddleOCR-VL", "Cloud vision model — alternative backend"],
      ] as [string, string][],
      mineruToken: "MinerU API Token",
      paddleToken: "PaddleOCR Token",
      tokenPlaceholder: "Not set · paste to save",
      tokenPlaceholderSet: "•••••••• configured · paste to replace",
      tokenPending: "UNSAVED",
      tokenSaved: "SAVED",
      show: "SHOW",
      hide: "HIDE",
      showHint: "Type a value first",
      clear: "CLEAR",
      configured: "CONFIGURED",
      missing: "MISSING",
      cleanNote: "Applies to the next conversion. Hover a row for its CLI key.",
      epubValidation: "Strict EPUB validation",
      epubValidationDesc: "Check the produced EPUB (images / math / footnotes / links / TOC / CSS) and fail the file on any error; off by default",
      themeLabel: "Theme",
      themeLight: "Light",
      themeDark: "Dark",
      langLabel: "Language",
      langEn: "English",
      langZh: "简体中文",
      engine: "Engine",
      pandoc: "Pandoc",
      cli: "Converter engine",
    },
    status: {
      ready: "READY",
      backend: "Backend",
      pending: (n: number) => `${n} pending`,
    },
  },
  zh: {
    screens: ["转换", "书目", "规范"],
    langToggle: "EN",
    brand: "pdf2epub",
    convert: {
      head: "转换",
      active: (n: number) => `${n} 进行中`,
      drop: "拖放文件到此处",
      dropSub: "PDF · MARKDOWN · MD",
      autoDetect: "自动检测：纯文字 / 扫描件 / 混合 · OCR 可选",
      browse: "浏览文件",
      addFiles: "＋ 添加文件",
      recent: "最近文件",
      preflight: "印前检查",
      preflightRun: "印前检查",
      preflightBusy: "检测中…",
      selectAll: "全选",
      clearDone: "清除已完成",
      retryFailed: "重试失败",
      cancelAll: "取消全部",
      activeCount: (n: number) => `${n} 进行中`,
      colFile: "文件",
      colType: "TYPE",
      colSize: "大小",
      colPages: "页数",
      colBackend: "BACKEND",
      colStatus: "STATUS",
      colAction: "操作",
      retry: "重试",
      requeue: "重新入队",
      addToQueue: "加入 →",
      detection: {
        title: "类型检测",
        empty: "拖入文件以预览",
        backend: "后端",
        signatures: "折帖",
        cloud: "云端页数",
        pages: "页数",
        txtDesc: "文字层完整 — 本地转换",
        scnDesc: "扫描件 — 需要云端 OCR",
        hybDesc: "混合内容 — 按页路由",
        mdDesc: "Markdown — 直接本地构建",
      },
      quota: (need: number, quota: number) => `本批需云端 ${need} 页,当日额度 ${quota} 页,建议分批。`,
      quotaOk: (need: number, shards: number) => `云端 ${need} 页 · ${shards} 帖 · 未超出当日额度`,
      warn: {
        msg: (f: string) => `${f} 中检测到伪文字层`,
        ocr: "使用 OCR",
        local: "继续本地",
      },
      composing: "COMPOSING ROOM  排字记录",
      tail: "TAIL",
      pause: "PAUSE",
      noOutput: "— 暂无输出 —",
    },
    library: {
      head: "书目",
      books: (n: number) => `${n} 本`,
      search: "搜索标题或作者…",
      sort: "排序",
      sortDate: "日期",
      sortTitle: "标题",
      sortSize: "大小",
      filterAll: "全部",
      refresh: "刷新",
      openFolder: "打开输出目录",
      openEpub: "打开 EPUB",
      reconvert: "重新转换",
      colTitle: "标题 / 作者",
      colSize: "大小",
      colPages: "页数",
      colDate: "日期",
      empty: "暂无转换结果",
      emptySub: "转换完成的 EPUB 将显示于此",
      emptyFiltered: "没有符合筛选的书",
      colophon: "输出目录",
      totalLabel: "总计",
      total: "合计",
      newest: "最新",
      loading: "读取书志…",
    },
    settings: {
      head: "规范",
      dirty: (n: number) => `${n} 项未保存`,
      save: "保存设置",
      discard: "放弃",
      sections: ["转换器", "OCR 与凭证", "校勘流水线", "质量与外观"],
      outputDir: "输出目录",
      cliPath: "转换器路径",
      cliPlaceholder: "留空 = 自动探测",
      browse: "浏览",
      envCheck: "环境检查",
      pandocMissing: "未安装 Pandoc。pdf2epub 需要它来构建 EPUB 文件。",
      installCmd: "winget install pandoc",
      copy: "复制",
      copied: "已复制",
      backendLabel: "下次转换使用的 OCR 后端",
      backends: [
        ["自动检测", "文字 PDF 走本地 · 扫描件走云端 OCR"],
        ["MinerU", "云端 OCR — 适合复杂排版"],
        ["PaddleOCR-VL", "云端视觉模型 — 备用后端"],
      ] as [string, string][],
      mineruToken: "MinerU API 密钥",
      paddleToken: "PaddleOCR 密钥",
      tokenPlaceholder: "未设置 · 粘贴后保存",
      tokenPlaceholderSet: "•••••••• 已配置 · 粘贴以替换",
      tokenPending: "待保存",
      tokenSaved: "已保存",
      show: "显示",
      hide: "隐藏",
      showHint: "先输入内容",
      clear: "清除",
      configured: "已配置",
      missing: "未配置",
      cleanNote: "改动作用于下一次转换;悬停某行可见其 CLI 键名。",
      epubValidation: "严格校验 EPUB",
      epubValidationDesc: "转换后校验结构(图片/公式/脚注/链接/目录/CSS),有失败即标记该文件失败;默认关闭",
      themeLabel: "外观",
      themeLight: "浅色",
      themeDark: "深色",
      langLabel: "语言",
      langEn: "English",
      langZh: "简体中文",
      engine: "引擎",
      pandoc: "Pandoc",
      cli: "转换引擎",
    },
    status: {
      ready: "就绪",
      backend: "后端",
      pending: (n: number) => `${n} 个等待`,
    },
  },
};

/** 校勘清单:三组九项,与 cleaner 的 CLEAN_KEYS 一一对应(顺序见 CLEAN_KEYS) */
const CLEAN_GROUPS: Record<Lang, CleanGroup[]> = {
  en: [
    {
      name: "TEXT STRUCTURE",
      items: [
        { glyph: "¶", key: "page_numbers", label: "Remove page numbers", desc: "Strip standalone page-number lines" },
        { glyph: "⌐", key: "running_heads", label: "Strip running heads", desc: "Drop short lines repeated across pages" },
        { glyph: "¬", key: "join_lines", label: "Join broken lines", desc: "Re-flow paragraphs split across pages" },
      ],
    },
    {
      name: "TYPOGRAPHY",
      items: [
        { glyph: "⌀", key: "ocr_spaces", label: "Fix OCR spaces", desc: "“Py Mu PDF” → “PyMuPDF” (Chinese lines only)" },
        { glyph: "⌂", key: "cjk_spaces", label: "Normalize CJK spaces", desc: "Remove stray spaces between CJK characters" },
        { glyph: "ⓑ", key: "bold", label: "Normalize bold fonts", desc: "KaiTi / STZhongsong → semantic strong", advanced: true },
      ],
    },
    {
      name: "HEADINGS & CONTENT",
      items: [
        { glyph: "⊗", key: "dup_headings", label: "Dedupe headings", desc: "Keep one of adjacent duplicate headings" },
        { glyph: "⌗", key: "headings", label: "Fix heading levels", desc: "Drop empty headings, flatten level jumps" },
        { glyph: "⊞", key: "images", label: "Verify image refs", desc: "Report image links missing from work/images" },
      ],
    },
  ],
  zh: [
    {
      name: "文字结构",
      items: [
        { glyph: "¶", key: "page_numbers", label: "删除页码", desc: "去除独立页码行" },
        { glyph: "⌐", key: "running_heads", label: "剔除页眉页脚", desc: "删除跨页重复出现的短行(书名/章节名)" },
        { glyph: "¬", key: "join_lines", label: "合并断行", desc: "重排跨页断行段落" },
      ],
    },
    {
      name: "字体排印",
      items: [
        { glyph: "⌀", key: "ocr_spaces", label: "合并 OCR 空格", desc: "「Py Mu PDF」→「PyMuPDF」(仅中文行)" },
        { glyph: "⌂", key: "cjk_spaces", label: "修正中文空格", desc: "去除汉字/数字之间的多余空格" },
        { glyph: "ⓑ", key: "bold", label: "规范粗体", desc: "楷体 / 中宋 → 语义 strong", advanced: true },
      ],
    },
    {
      name: "标题与内容",
      items: [
        { glyph: "⊗", key: "dup_headings", label: "重复标题去重", desc: "相邻同名标题只保留一条" },
        { glyph: "⌗", key: "headings", label: "标题层级修正", desc: "删除空标题,收敛层级跳跃" },
        { glyph: "⊞", key: "images", label: "校验图片引用", desc: "报告 work/images 中缺失的图片引用" },
      ],
    },
  ],
};

// ── TYPES ─────────────────────────────────────────────────────────────────────

type Screen = "convert" | "library" | "settings";
type FileStatus = "pending" | "converting" | "done" | "failed" | "cancelled";
type Backend = "Local" | "MinerU" | "PaddleOCR" | "Auto";
type FileType = "TXT" | "SCN" | "HYB" | "MD";
type BackendPref = "auto" | "mineru" | "paddleocr";

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
  signatures?: Signatures;
  cloudPages?: number;
  lastLog?: string;
  log: string[];
  warning?: boolean;
  error?: string;
  epub?: string;
  date?: string;
  /** 引擎事件流状态(--json-events);进度与阶段由它驱动 */
  engine?: EventState;
  /** 该文件出现过「非事件」日志 → 退化为旧正则解析(老版 CLI 兜底) */
  legacy?: boolean;
}

interface ConsoleLine {
  ts: string;
  level: "INFO" | "WARN" | "ERROR";
  text: string;
  /** 该行来自「老版 CLI + 正则兜底」路径(事件流缺失) */
  legacy?: boolean;
}

interface LibraryBook {
  id: string;
  title: string;
  author: string;
  size: string;
  pages: number;
  date: string;
  type?: FileType;
  backend?: Backend;
  epub: string;
  path?: string;
}

interface LibEntry {
  path: string;
  title: string;
  author: string;
  size: number;
  mtime: number;
  added_at: number;
  /** 转换时回写的元数据(旧记录可能是 null → 界面显示未知) */
  kind?: string | null;
  backend?: string | null;
  pages?: number | null;
}

interface EnvState {
  pandoc: { status: string; version: string | null; path: string | null };
  engine: { status: string; version: string | null; path: string | null };
  mineru_configured: boolean;
  paddle_configured: boolean;
  apikey_path: string | null;
}

/** CLI `--dry-run --json` 的单个文件计划(印前检查的原始数据) */
interface PreflightFile {
  name: string;
  path: string;
  kind: string;
  backend: string;
  pages: number;
  text_pages: number;
  ocr_pages: number;
  shards: number;
  needs_ocr: boolean;
  notes: string[];
}

interface PreflightReport {
  files: PreflightFile[];
  total_ocr_pages: number;
  total_shards: number;
  quota: number;
  over_quota: boolean;
}

interface RecentEntry {
  path: string;
  name: string;
  at: number;
}

const APP_VERSION = "v0.3.0";
const RECENT_KEY = "pdf2epub.recent";
const MAX_RECENT = 8;

//: 清理项开关顺序必须与 CLI 的 cleaner.CLEAN_KEYS 一致
const CLEAN_KEYS = [
  "page_numbers", "running_heads", "join_lines", "ocr_spaces", "cjk_spaces",
  "dup_headings", "headings", "bold", "images",
] as const;
const CLEAN_DEFAULTS = [true, true, true, true, true, true, true, false, true];

// ── LOG PARSERS(legacy 兜底:仅在事件流缺失时使用)────────────────────────────
//  正常路径下状态全部来自引擎事件流(src/events.ts)。下面这些正则只服务于
//  「新版前端 + 老版 CLI」的组合,命中即标记 legacy(控制台会显示徽标)。

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

function parseWarningFromLine(line: string): boolean {
  return /伪文字层|疑似|乱码|garbage/i.test(line);
}

function normPath(p: string): string {
  return p.replace(/\\/g, "/").toLowerCase();
}

function formatSize(bytes: number): string {
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function baseName(p: string): string {
  return p.split(/[\\/]/).pop() || p;
}

/** CLI 计划 → 界面徽标(预检结果落到队列行的映射) */
const KIND_TO_TYPE: Record<string, FileType> = {
  "pdf-text": "TXT",
  "pdf-scanned": "SCN",
  "pdf-hybrid": "HYB",
  markdown: "MD",
};

function backendFromPlan(b: string): Backend {
  if (/pymupdf|markdown/.test(b)) return "Local";
  if (/paddle/.test(b)) return "PaddleOCR";
  if (/mineru/.test(b)) return "MinerU";
  return "Auto";
}

function loadCleanOpts(): boolean[] {
  try {
    const raw = JSON.parse(localStorage.getItem("pdf2epub.clean") || "null");
    if (Array.isArray(raw) && raw.length === CLEAN_KEYS.length) return raw.map(Boolean);
  } catch { /* 忽略坏数据 */ }
  return [...CLEAN_DEFAULTS];
}

function loadRecent(): RecentEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    if (Array.isArray(raw)) return raw.filter(r => r && typeof r.path === "string").slice(0, MAX_RECENT);
  } catch { /* 忽略坏数据 */ }
  return [];
}

// ── SHARED PRIMITIVES ─────────────────────────────────────────────────────────

function Rule({ vertical = false, className = "" }: { vertical?: boolean; className?: string }) {
  return <div className={`bg-[var(--border)] ${vertical ? "w-px self-stretch" : "h-px w-full"} ${className}`} />;
}

function TypeBadge({ type }: { type?: FileType }) {
  if (!type) {
    return (
      <span className="font-mono text-[8px] leading-none px-1.5 py-0.5 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] shrink-0">
        ?
      </span>
    );
  }
  if (type === "HYB") {
    return (
      <span className="inline-flex overflow-hidden font-mono text-[8px] leading-none shrink-0">
        <span className="px-1 py-0.5 border border-[var(--info)] text-[var(--info)]">HY</span>
        <span className="px-1 py-0.5 border border-l-0 border-[var(--border)] text-[var(--muted-foreground)]">B</span>
      </span>
    );
  }
  const s: Record<Exclude<FileType, "HYB">, string> = {
    TXT: "border-[var(--border)] text-[var(--muted-foreground)]",
    SCN: "border-[var(--info)] text-[var(--info)]",
    MD: "border-[var(--paddle)] text-[var(--paddle)]",
  };
  return <span className={`font-mono text-[8px] leading-none px-1.5 py-0.5 border shrink-0 ${s[type]}`}>{type}</span>;
}

const STATUS_STYLE: Record<FileStatus, string> = {
  pending: "text-[var(--muted-foreground)] border-[var(--border)]",
  converting: "text-[var(--primary)] border-[var(--primary)]",
  done: "text-[var(--ok)] border-[var(--ok)]",
  failed: "text-[var(--danger)] border-[var(--danger)]",
  cancelled: "text-[var(--warn)] border-[var(--warn)]",
};

function StatusChip({ status }: { status: FileStatus }) {
  return (
    <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border ${STATUS_STYLE[status]}`}>
      {status === "converting" ? "CONVERTING" : status.toUpperCase()}
    </span>
  );
}

const BACKEND_STYLE: Record<Backend, string> = {
  Local: "border-[var(--border)] text-[var(--muted-foreground)]",
  MinerU: "border-[var(--info)] text-[var(--info)]",
  PaddleOCR: "border-[var(--paddle)] text-[var(--paddle)]",
  Auto: "border-[var(--primary)] text-[var(--primary)]",
};

function BackendBadge({ backend }: { backend?: Backend }) {
  if (!backend) {
    return (
      <span className="font-mono text-[8px] px-1.5 py-0.5 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] shrink-0">
        —
      </span>
    );
  }
  const label = backend === "PaddleOCR" ? "PADDLE" : backend.toUpperCase();
  return <span className={`font-mono text-[8px] tracking-[0.06em] px-1.5 py-0.5 border shrink-0 ${BACKEND_STYLE[backend]}`}>{label}</span>;
}

/** 工序:制版 → 检字 → 校勘 → 付印(云端 OCR 时标 OCR) */
function StageStepper({ stages, signatures, lang }: { stages: StageNode[]; signatures?: Signatures; lang: Lang }) {
  const CRAFT_ZH: Record<string, string> = {
    DETECT: "制版", EXTRACT: "检字", CLEAN: "校勘", BUILD: "付印", VERIFY: "核验",
  };
  return (
    <div className="flex items-center gap-0 flex-wrap">
      {stages.map((s, i) => {
        const isDone = s.state === "done";
        const isActive = s.state === "active";
        const isFailed = s.state === "failed";
        const nodeClass = isDone
          ? "border-[var(--ok)] bg-[var(--ok)] text-[var(--primary-foreground)]"
          : isActive
          ? "border-[var(--primary)] text-[var(--primary)] stage-breathe"
          : isFailed
          ? "border-[var(--danger)] text-[var(--danger)]"
          : "border-[var(--border)] text-[var(--muted-foreground)]";
        const lineColor = isDone ? "bg-[var(--ok)]" : isActive ? "bg-[var(--primary)]" : "bg-[var(--border)]";
        const labelColor = isDone ? "text-[var(--ok)]" : isActive ? "text-[var(--primary)]" : isFailed ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]";
        const nodeLabel = s.isOcr ? "OCR" : `${i + 1}`;
        const stageLabel = s.isOcr ? "OCR" : s.key;

        return (
          <span key={s.key} className="flex items-center">
            {i > 0 && <span className={`w-5 h-px ${lineColor} transition-all duration-300`} />}
            <span className="flex flex-col items-center gap-0.5">
              <span className={`w-4 h-4 border flex items-center justify-center font-mono text-[7px] transition-all ${nodeClass}`}>
                {isDone ? (
                  <svg width="8" height="6" viewBox="0 0 8 6" fill="none" className="check-draw">
                    <path d="M1 3 L3 5 L7 1" stroke="currentColor" strokeWidth="1.2" strokeDasharray="20" strokeDashoffset="0" strokeLinecap="square" />
                  </svg>
                ) : isFailed ? "✕" : nodeLabel}
              </span>
              <span className={`font-mono text-[7px] tracking-[0.04em] ${labelColor}`}>
                {lang === "zh" && !s.isOcr ? (CRAFT_ZH[s.key] ?? s.key) : stageLabel}
              </span>
            </span>
          </span>
        );
      })}
      {signatures && signatures.total > 1 && (
        <span className="ml-2 font-mono text-[8px] px-1.5 py-0.5 border border-[var(--primary)] text-[var(--primary)] shrink-0">
          {lang === "zh"
            ? `第 ${signatures.current} 帖 · 共 ${signatures.total} 帖`
            : `SIGNATURE ${signatures.current} OF ${signatures.total}`}
        </span>
      )}
    </div>
  );
}

/** 书脊色块封面(真实封面提取尚未实现 → 几何占位,不用照片) */
function SpineBlock({ title }: { title: string }) {
  const words = title.split(" ").filter(w => !["the", "a", "an", "and", "of"].includes(w.toLowerCase()));
  const initials = words.slice(0, 2).map(w => w[0]?.toUpperCase() ?? "").join("") || "EP";
  const hues = [200, 150, 30, 260, 0, 180];
  const hue = hues[(title.charCodeAt(0) || 65) % hues.length];
  return (
    <div
      className="w-10 h-14 flex items-center justify-center border border-[var(--border)] shrink-0 relative overflow-hidden"
      style={{ backgroundColor: `hsl(${hue}, 22%, 28%)` }}
    >
      <span className="font-serif text-[11px] font-bold text-white/80 z-10">{initials}</span>
      <div className="absolute bottom-0 left-0 right-0 h-px" style={{ backgroundColor: `hsl(${hue}, 40%, 45%)`, opacity: 0.5 }} />
    </div>
  );
}

/* 方角开关 — 40×22 轨,16×16 方滑块,整行可点,右侧 mono ON/OFF */
function Switch({ checked, onChange, label, description, disabled, lang }: {
  checked: boolean; onChange: (v: boolean) => void; label: string; description?: string; disabled?: boolean; lang: Lang;
}) {
  return (
    <div
      className={`flex items-center h-11 gap-4 hover:bg-[var(--secondary)] transition-colors cursor-pointer ${disabled ? "opacity-40 pointer-events-none" : ""}`}
      onClick={() => !disabled && onChange(!checked)}
    >
      <div className="flex-1 min-w-0">
        <div className={`text-[13px] text-[var(--foreground)] ${lang === "zh" ? "cjk" : ""}`}>{label}</div>
        {description && <div className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{description}</div>}
      </div>
      <span className={`font-mono text-[9px] tracking-[0.08em] shrink-0 ${checked ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>
        {checked ? "ON" : "OFF"}
      </span>
      <button
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        className={`relative shrink-0 w-[40px] h-[22px] border transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-[var(--ring)] focus:ring-offset-2 focus:ring-offset-[var(--background)]
          ${checked ? "bg-[var(--primary)] border-[var(--primary)]" : "bg-[var(--card)] border-[var(--foreground)]"}
          ${disabled ? "border-dashed" : ""}`}
      >
        <span className={`absolute top-[2px] w-4 h-4 transition-transform duration-200 ${checked ? "translate-x-[19px] bg-white" : "translate-x-[2px] border border-[var(--foreground)] bg-transparent"}`} />
      </button>
    </div>
  );
}

/* 分段控件 — 排序 / 主题 / 语言 / 后端筛选 */
function Segment<T extends string>({ options, value, onChange }: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex border-t border-b border-[var(--border)]">
      {options.map((opt, i) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`flex-1 py-2 font-mono text-[11px] tracking-[0.08em] uppercase transition-colors focus:outline-none
            ${i > 0 ? "border-l border-[var(--border)]" : ""}
            ${value === opt.value
              ? "text-[var(--primary)] border-b-2 border-b-[var(--primary)] -mb-px"
              : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

/* 带说明的单选行 */
function RadioRow({ label, desc, active, onClick, lang }: {
  label: string; desc: string; active: boolean; onClick: () => void; lang: Lang;
}) {
  return (
    <button
      onClick={onClick}
      role="radio"
      aria-checked={active}
      className={`w-full flex items-center gap-4 py-3 text-left transition-colors hover:bg-[var(--secondary)] ${active ? "bg-[var(--secondary)]" : ""}`}
    >
      <div className={`w-3 h-3 border flex items-center justify-center shrink-0 ${active ? "border-[var(--primary)]" : "border-[var(--border)]"}`}>
        {active && <div className="w-1.5 h-1.5 bg-[var(--primary)]" />}
      </div>
      <div className="flex-1">
        <div className={`text-[13px] text-[var(--foreground)] ${lang === "zh" ? "cjk" : ""}`}>{label}</div>
        <div className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{desc}</div>
      </div>
      {active && <span className="font-mono text-[8px] tracking-[0.1em] text-[var(--primary)]">ACTIVE</span>}
    </button>
  );
}

/* 排字记录(控制台):真实 conv://progress 日志 */
function ComposingRoom({ expanded, setExpanded, lang, lines }: {
  expanded: boolean; setExpanded: (v: boolean) => void; lang: Lang; lines: ConsoleLine[];
}) {
  const [paused, setPaused] = useState(false);
  const t = T[lang].convert;
  const shown = paused ? lines.slice(-80) : lines;
  return (
    <div className="border-t border-[var(--border)] shrink-0 flex flex-col overflow-hidden transition-[height] duration-[220ms] ease-[cubic-bezier(0.2,0,0,1)]"
      style={{ height: expanded ? 220 : 28 }}>
      <div
        className="h-7 flex items-center px-[64px] pr-[72px] gap-4 cursor-pointer hover:bg-[var(--secondary)] transition-colors shrink-0"
        onClick={() => setExpanded(!expanded)}
      >
        <span className={`font-mono text-[9px] tracking-[0.12em] text-[var(--muted-foreground)]`}>{t.composing}</span>
        <Rule className="flex-1" />
        {expanded && (
          <>
            <button onClick={e => { e.stopPropagation(); setPaused(!paused); }}
              className={`font-mono text-[9px] tracking-[0.1em] ${paused ? "text-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}>
              {paused ? t.tail : t.pause}
            </button>
            <Rule vertical />
          </>
        )}
        <svg width="8" height="5" viewBox="0 0 8 5" className={`text-[var(--muted-foreground)] transition-transform ${expanded ? "rotate-180" : ""}`} fill="none">
          <path d="M1 1 L4 4 L7 1" stroke="currentColor" strokeWidth="1" />
        </svg>
      </div>
      {expanded && (
        <div className="flex-1 overflow-auto composing-bg px-[64px] pr-[72px] py-3 space-y-0.5">
          {shown.length === 0 && <div className="font-mono text-[10px] text-[var(--muted-foreground)]">{t.noOutput}</div>}
          {shown.map((line, i) => (
            <div key={i} className="flex gap-4">
              <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums shrink-0 w-16">{line.ts}</span>
              <span className={`font-mono text-[10px] shrink-0 w-10 ${line.level === "ERROR" ? "text-[var(--danger)]" : line.level === "WARN" ? "text-[var(--warn)]" : "text-[var(--muted-foreground)]"}`}>
                {line.level}
              </span>
              <span className={`font-mono text-[10px] ${line.level === "ERROR" ? "text-[var(--danger)]" : "text-[var(--foreground)]"}`}>
                {line.text}
              </span>
              {line.legacy && (
                <span className="font-mono text-[9px] px-1 border border-[var(--warn)] text-[var(--warn)] shrink-0 self-center" title="状态由旧正则从日志文本猜测(新版 CLI 会发 JSON 事件)">
                  LEGACY
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* 伪文字层警告(单行可折叠) */
function WarningStrip({ file, lang, onUseOcr }: { file: QueueFile; lang: Lang; onUseOcr: (id: string) => void }) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  const t = T[lang].convert.warn;
  return (
    <div className="flex items-center gap-3 py-1.5 border-t border-[var(--warn)]">
      <svg width="12" height="11" viewBox="0 0 12 11" className="text-[var(--warn)] shrink-0" fill="none">
        <path d="M6 1.5 L11 10 L1 10 Z" stroke="currentColor" strokeWidth="1" />
        <line x1="6" y1="4.5" x2="6" y2="7.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="square" />
        <rect x="5.4" y="8.3" width="1.2" height="1.2" fill="currentColor" />
      </svg>
      <span className={`text-[11px] text-[var(--warn)] flex-1 ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.msg(file.name)}</span>
      <button onClick={() => { onUseOcr(file.id); setDismissed(true); }}
        className="font-mono text-[9px] tracking-[0.08em] text-[var(--primary)] border border-[var(--primary)] px-2 py-0.5 hover:bg-[var(--primary)] hover:text-[var(--primary-foreground)] transition-colors">
        {t.ocr}
      </button>
      <button onClick={() => setDismissed(true)}
        className="font-mono text-[9px] tracking-[0.08em] text-[var(--warn)] border border-[var(--warn)] px-2 py-0.5 transition-colors">
        {t.local}
      </button>
      <button onClick={() => setDismissed(true)} className="text-[var(--muted-foreground)] hover:text-[var(--foreground)] p-1 transition-colors" aria-label="Dismiss">
        <svg width="7" height="7" viewBox="0 0 7 7" fill="none"><line x1="1" y1="1" x2="6" y2="6" stroke="currentColor" strokeWidth="1" /><line x1="6" y1="1" x2="1" y2="6" stroke="currentColor" strokeWidth="1" /></svg>
      </button>
    </div>
  );
}

/* 章节头 — 通栏 1px 线 + 屏号屏名 + 右侧状态 */
function ChapterHead({ label, state, lang }: { label: string; state?: string; lang: Lang }) {
  return (
    <div className="shrink-0">
      <Rule />
      <div className="flex items-baseline justify-between py-3">
        <span className={`font-mono text-[11px] tracking-[0.08em] uppercase text-[var(--foreground)] ${lang === "zh" ? "cjk font-sans" : ""}`}>{label}</span>
        {state && <span className="font-mono text-[11px] tracking-[0.08em] text-[var(--muted-foreground)]">{state}</span>}
      </div>
      <Rule />
    </div>
  );
}

// ── CONVERT SCREEN ────────────────────────────────────────────────────────────

function ConvertScreen({
  lang, files, preview, preflightOf, batchNotice, dragging, recent, busyPreflight,
  composingExpanded, setComposingExpanded, consoleLines,
  onPick, onCancel, onRetry, onClearDone, onRetryFailed, onCancelAll, onUseOcr,
  onPreflight, onPreflightRecent, onAddRecent,
}: {
  lang: Lang;
  files: QueueFile[];
  preview: QueueFile | null;
  preflightOf: (path: string) => PreflightFile | undefined;
  batchNotice: PreflightReport | null;
  dragging: boolean;
  recent: RecentEntry[];
  busyPreflight: boolean;
  composingExpanded: boolean;
  setComposingExpanded: (v: boolean) => void;
  consoleLines: ConsoleLine[];
  onPick: () => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onClearDone: () => void;
  onRetryFailed: () => void;
  onCancelAll: () => void;
  onUseOcr: (id: string) => void;
  onPreflight: () => void;
  onPreflightRecent: (path: string) => void;
  onAddRecent: (path: string) => void;
}) {
  const t = T[lang].convert;
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const hasFiles = files.length > 0;
  const done = files.filter(f => f.status === "done").length;
  const active = files.filter(f => f.status === "converting").length;
  const failed = files.filter(f => f.status === "failed").length;
  const pending = files.filter(f => f.status === "pending").length;
  const total = files.length;
  const overallPct = total > 0 ? Math.round(files.reduce((s, f) => s + f.progress, 0) / (total * 100) * 100) : 0;

  const toggleRow = (id: string) => setExpandedRows(prev => { const s = new Set(prev); if (s.has(id)) { s.delete(id); } else { s.add(id); } return s; });
  const toggleSel = (id: string) => setSelected(prev => { const s = new Set(prev); if (s.has(id)) { s.delete(id); } else { s.add(id); } return s; });
  const selectAll = () => setSelected(prev => prev.size === total ? new Set() : new Set(files.map(f => f.id)));

  const previewPlan = preview ? preflightOf(preview.path) : undefined;
  const previewDesc = previewPlan
    ? previewPlan.kind === "pdf-text" ? t.detection.txtDesc
      : previewPlan.kind === "pdf-scanned" ? t.detection.scnDesc
      : previewPlan.kind === "pdf-hybrid" ? t.detection.hybDesc
      : previewPlan.kind === "markdown" ? t.detection.mdDesc
      : t.detection.empty
    : null;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-[64px] pr-[72px] pt-6 shrink-0">
        <ChapterHead label={`01 ${T[lang].screens[0]}`} state={active > 0 ? t.active(active) : undefined} lang={lang} />
      </div>

      {/* ── 空状态:拖放区 + 类型检测 + 最近文件 ── */}
      {!hasFiles && (
        <div className="flex-1 flex overflow-auto px-[64px] pr-[72px] py-6">
          <div className="flex flex-1 gap-8 min-h-0">
            <div className="flex-1 flex flex-col items-center min-h-[280px]">
              <div
                className={`relative w-full max-w-[420px] h-[300px] flex flex-col items-center justify-center gap-5 cursor-pointer transition-all duration-[140ms]
                  ${dragging ? "border border-[var(--primary)] bg-[var(--primary)]/4" : "border border-dashed border-[var(--border)] hover:border-[var(--muted-foreground)]"}`}
                onDragOver={e => e.preventDefault()}
                onClick={onPick}
                role="button"
                tabIndex={0}
                aria-label={t.drop}
                onKeyDown={e => { if (e.key === "Enter" || e.key === " ") onPick(); }}
              >
                {[["top-2 left-2", "-translate-x-px -translate-y-px"], ["top-2 right-2", "translate-x-px -translate-y-px"], ["bottom-2 left-2", "-translate-x-px translate-y-px"], ["bottom-2 right-2", "translate-x-px translate-y-px"]].map(([pos, offset], i) => (
                  <svg key={i} width="8" height="8" viewBox="0 0 8 8" className={`absolute ${pos} text-[var(--muted-foreground)] transition-transform duration-[140ms] ${dragging ? offset : ""}`} fill="none">
                    <line x1="4" y1="0" x2="4" y2="8" stroke="currentColor" strokeWidth="0.75" />
                    <line x1="0" y1="4" x2="8" y2="4" stroke="currentColor" strokeWidth="0.75" />
                  </svg>
                ))}
                <div className="text-center space-y-2">
                  <p className={`text-[14px] text-[var(--foreground)] ${lang === "zh" ? "cjk" : ""}`}>{t.drop}</p>
                  <p className="font-mono text-[11px] tracking-[0.08em] text-[var(--muted-foreground)]">{t.dropSub}</p>
                  <p className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono text-[11px]"}`}>{t.autoDetect}</p>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); onPick(); }}
                  className={`px-6 py-2 bg-[var(--primary)] text-[var(--primary-foreground)] font-mono text-[11px] tracking-[0.1em] uppercase hover:opacity-90 transition-opacity ${lang === "zh" ? "cjk font-sans" : ""}`}
                >
                  {t.browse}
                </button>
              </div>

              {/* 最近文件(跨会话持久化) */}
              {recent.length > 0 && (
                <div className="w-full max-w-[420px] mt-6">
                  <div className="flex items-center gap-4 mb-2">
                    <span className={`text-[11px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk font-medium" : "font-mono tracking-[0.12em] uppercase"}`}>{t.recent}</span>
                    <Rule className="flex-1" />
                  </div>
                  <Rule />
                  {recent.slice(0, 5).map((r, i) => (
                    <div key={i}>
                      <div className="flex items-center gap-3 py-2 hover:bg-[var(--secondary)] transition-colors group -mx-4 px-4">
                        <TypeBadge type={preflightOf(r.path) ? KIND_TO_TYPE[preflightOf(r.path)!.kind] : undefined} />
                        <span className="flex-1 font-mono text-[11px] text-[var(--foreground)] truncate">{r.name}</span>
                        <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{preflightOf(r.path)?.pages || "—"}</span>
                        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{new Date(r.at).toISOString().slice(5, 10)}</span>
                        <button onClick={() => onPreflightRecent(r.path)} className="opacity-0 group-hover:opacity-100 font-mono text-[9px] text-[var(--primary)] transition-opacity">
                          {t.preflightRun}
                        </button>
                        <button onClick={() => onAddRecent(r.path)} className="opacity-0 group-hover:opacity-100 font-mono text-[9px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-opacity">
                          {t.addToQueue}
                        </button>
                      </div>
                      <Rule />
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 类型检测 / 印前检查 */}
            <div className="w-[260px] shrink-0 border-l border-[var(--border)] pl-8 flex flex-col gap-4 pt-2">
              <span className={`text-[11px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk font-medium" : "font-mono tracking-[0.12em] uppercase"}`}>
                {t.preflight}
              </span>
              <Rule />
              {busyPreflight && <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{t.preflightBusy}</span>}
              {preview && previewPlan ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <TypeBadge type={KIND_TO_TYPE[previewPlan.kind]} />
                    <span className="font-mono text-[10px] text-[var(--foreground)] truncate">{previewPlan.name}</span>
                  </div>
                  <p className={`text-[12px] text-[var(--muted-foreground)] leading-relaxed ${lang === "zh" ? "cjk" : "font-mono"}`}>{previewDesc}</p>
                  <Rule />
                  <div className="space-y-2">
                    {[
                      [t.detection.backend, <BackendBadge key="b" backend={backendFromPlan(previewPlan.backend)} />],
                      [t.detection.pages, <span key="p" className="font-mono text-[11px] text-[var(--foreground)] tabular-nums">{previewPlan.pages}</span>],
                      [t.detection.signatures, <span key="s" className="font-mono text-[9px] text-[var(--primary)] border border-[var(--primary)] px-1.5 py-0.5 tabular-nums">{previewPlan.shards}</span>],
                      [t.detection.cloud, <span key="c" className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">{previewPlan.ocr_pages}</span>],
                    ].map(([label, value], i) => (
                      <div key={i} className="flex items-center justify-between">
                        <span className={`text-[11px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono tracking-[0.08em]"}`}>{label}</span>
                        {value}
                      </div>
                    ))}
                  </div>
                  {previewPlan.notes.length > 0 && (
                    <>
                      <Rule />
                      <ul className="space-y-1">
                        {previewPlan.notes.map((n, i) => (
                          <li key={i} className={`text-[11px] text-[var(--warn)] leading-snug ${lang === "zh" ? "cjk" : "font-mono"}`}>· {n}</li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              ) : (
                <span className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.detection.empty}</span>
              )}
              <Rule />
              <div className="space-y-2">
                {(["TXT", "SCN", "HYB", "MD"] as FileType[]).map(tp => (
                  <div key={tp} className="flex items-start gap-2">
                    <TypeBadge type={tp} />
                    <span className={`text-[11px] text-[var(--muted-foreground)] mt-0.5 leading-snug ${lang === "zh" ? "cjk" : "font-mono"}`}>
                      {tp === "TXT" ? t.detection.txtDesc : tp === "SCN" ? t.detection.scnDesc : tp === "HYB" ? t.detection.hybDesc : t.detection.mdDesc}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── 工作状态:添加条 + 总进度 + 工具条 + 表 ── */}
      {hasFiles && (
        <>
          <div className="px-[64px] pr-[72px] shrink-0">
            <div
              className={`flex items-center justify-center cursor-pointer transition-all duration-[140ms] border-t border-b border-dashed border-[var(--border)] hover:border-[var(--muted-foreground)]
                ${dragging ? "h-28 border-[var(--primary)] bg-[var(--primary)]/4" : "h-[40px]"}`}
              onClick={onPick}
              onDragOver={e => e.preventDefault()}
            >
              <span className={`font-mono text-[10px] tracking-[0.1em] uppercase text-[var(--muted-foreground)] ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.addFiles}</span>
            </div>
          </div>

          {/* 印前检查汇总(需要云端 OCR 时出现) */}
          {batchNotice && batchNotice.total_ocr_pages > 0 && (
            <div className="px-[64px] pr-[72px] shrink-0">
              <div className={`flex items-center gap-3 py-1.5 border-b ${batchNotice.over_quota ? "border-[var(--danger)]" : "border-[var(--border)]"}`}>
                <span className={`font-mono text-[9px] tracking-[0.1em] uppercase ${batchNotice.over_quota ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]"}`}>{t.preflight}</span>
                <span className={`text-[11px] flex-1 ${batchNotice.over_quota ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]"} ${lang === "zh" ? "cjk" : "font-mono"}`}>
                  {batchNotice.over_quota
                    ? t.quota(batchNotice.total_ocr_pages, batchNotice.quota)
                    : t.quotaOk(batchNotice.total_ocr_pages, batchNotice.total_shards)}
                </span>
              </div>
            </div>
          )}

          <div className="px-[64px] pr-[72px] pt-4 shrink-0">
            <div className="flex items-end gap-8 py-4 border-b border-[var(--border)]">
              <div className="flex-1">
                <div className="flex items-baseline gap-6 mb-3">
                  {[{ label: "DONE", val: done, color: "var(--ok)" }, { label: "ACTIVE", val: active, color: "var(--primary)" }, { label: "FAILED", val: failed, color: "var(--danger)" }, { label: "PENDING", val: pending, color: "var(--muted-foreground)" }].map(({ label, val, color }) => (
                    <div key={label} className="flex items-baseline gap-1.5">
                      <span className="font-mono text-[13px] font-medium tabular-nums" style={{ color }}>{val}</span>
                      <span className="font-mono text-[9px] tracking-[0.08em] text-[var(--muted-foreground)]">{label}</span>
                    </div>
                  ))}
                  <span className="font-mono text-[9px] text-[var(--muted-foreground)] ml-auto">{total} FILES</span>
                </div>
                <div className="relative h-px w-full bg-[var(--border)] overflow-visible">
                  <div className="absolute inset-y-0 left-0 bg-[var(--primary)] transition-all duration-700" style={{ width: `${overallPct}%`, height: "1px" }} />
                  {active > 0 && (
                    <div className="absolute top-1/2 -translate-y-1/2 w-1.5 h-1.5 bg-[var(--primary)] ink-pulse" style={{ left: `calc(${overallPct}% - 3px)` }} />
                  )}
                </div>
              </div>
              <span className="font-mono text-[56px] font-light leading-none tabular-nums tracking-tighter text-[var(--foreground)]">
                {overallPct}<span className="text-[20px] text-[var(--muted-foreground)]">%</span>
              </span>
            </div>

            {/* 工具条(有选中 → 上下文操作条) */}
            {selected.size > 0 ? (
              <div className="flex items-center gap-4 py-2 border-b border-[var(--border)]">
                <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--primary)]">{selected.size} SELECTED</span>
                <Rule vertical />
                <button onClick={() => { files.filter(f => selected.has(f.id)).forEach(f => onCancel(f.id)); setSelected(new Set()); }}
                  className="font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--danger)] transition-colors">{t.cancelAll}</button>
                <Rule vertical />
                <button onClick={() => { files.filter(f => selected.has(f.id)).forEach(f => onRetry(f.id)); setSelected(new Set()); }}
                  className="font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors">{t.retry}</button>
                <div className="flex-1" />
                <button onClick={() => setSelected(new Set())} className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]">✕</button>
              </div>
            ) : (
              <div className="flex items-center gap-4 py-2 border-b border-[var(--border)]">
                {[
                  { label: t.selectAll, action: selectAll },
                  { label: t.clearDone, action: onClearDone },
                  { label: t.retryFailed, action: onRetryFailed },
                  { label: t.cancelAll, action: onCancelAll },
                ].map(({ label, action }, i) => (
                  <span key={label} className="flex items-center gap-4">
                    {i > 0 && <Rule vertical />}
                    <button onClick={action} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>
                      {label}
                    </button>
                  </span>
                ))}
                <div className="flex-1" />
                <button onClick={onPreflight} className="font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--primary)] transition-colors">{t.preflight}</button>
                <Rule vertical />
                <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--primary)]">{t.activeCount(active)}</span>
              </div>
            )}

            <div className="grid grid-cols-[1fr_56px_64px_60px_96px_100px_72px] gap-3 py-1.5 border-b border-[var(--border)]">
              {["", t.colFile, t.colSize, t.colPages, t.colBackend, t.colStatus, t.colAction].map((h, i) => (
                <span key={i} className={`font-mono text-[9px] tracking-[0.1em] uppercase text-[var(--muted-foreground)] ${i >= 2 && i <= 3 ? "text-right" : ""}`}>{h}</span>
              ))}
            </div>
          </div>

          <div className="flex-1 overflow-auto px-[64px] pr-[72px] pb-2">
            {files.map((file, fi) => {
              const isExpanded = expandedRows.has(file.id);
              const isSelected = selected.has(file.id);
              return (
                <div key={file.id} className={`row-enter ${isSelected ? "bg-[var(--primary)]/4" : ""}`} style={{ animationDelay: `${Math.min(fi, 8) * 24}ms` }}>
                  <div
                    className="grid grid-cols-[1fr_56px_64px_60px_96px_100px_72px] gap-3 py-2 items-center hover:bg-[var(--secondary)] transition-colors cursor-pointer border-b border-[var(--border)]"
                    onClick={() => toggleRow(file.id)}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <button
                        onClick={e => { e.stopPropagation(); toggleSel(file.id); }}
                        aria-label="select"
                        className={`w-2.5 h-2.5 border shrink-0 transition-colors ${isSelected ? "bg-[var(--primary)] border-[var(--primary)]" : "border-[var(--border)] hover:border-[var(--foreground)]"}`}
                      />
                      <svg width="6" height="6" viewBox="0 0 6 6" className={`shrink-0 text-[var(--muted-foreground)] transition-transform ${isExpanded ? "rotate-90" : ""}`} fill="none">
                        <path d="M1 1 L5 3 L1 5" stroke="currentColor" strokeWidth="1" />
                      </svg>
                      <span className="font-mono text-[11px] text-[var(--foreground)] truncate">{file.name}</span>
                    </div>
                    <TypeBadge type={file.type} />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums text-right">{file.size}</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums text-right">{file.pages || "—"}</span>
                    <BackendBadge backend={file.backend} />
                    <StatusChip status={file.status} />
                    <div className="flex justify-end">
                      {(file.status === "pending" || file.status === "converting") && (
                        <button onClick={e => { e.stopPropagation(); onCancel(file.id); }} className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--danger)] transition-colors" aria-label="cancel">✕</button>
                      )}
                      {file.status === "failed" && (
                        <button onClick={e => { e.stopPropagation(); onRetry(file.id); }} className={`font-mono text-[9px] tracking-[0.06em] text-[var(--primary)] uppercase ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.retry}</button>
                      )}
                      {file.status === "done" && (
                        <button onClick={e => { e.stopPropagation(); onRetry(file.id); }} className={`font-mono text-[9px] tracking-[0.06em] text-[var(--muted-foreground)] uppercase hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.requeue}</button>
                      )}
                    </div>
                  </div>

                  <div className={`border-b border-[var(--border)] overflow-hidden transition-all duration-[180ms] ${isExpanded ? "" : "py-1"}`}>
                    <div className="relative h-px bg-[var(--border)] mx-0 my-1 overflow-visible">
                      <div
                        className={`absolute inset-y-0 left-0 transition-all duration-700 ${file.status === "failed" ? "bg-[var(--danger)]" : file.status === "cancelled" ? "bg-[var(--warn)]" : file.status === "done" ? "bg-[var(--ok)]" : "bg-[var(--primary)]"}`}
                        style={{ width: `${file.progress}%`, height: "1px" }}
                      />
                      {file.status === "converting" && (
                        <div className="absolute top-1/2 -translate-y-1/2 w-1 h-1 bg-[var(--primary)] ink-pulse" style={{ left: `calc(${file.progress}% - 2px)` }} />
                      )}
                    </div>
                    <div className="flex items-center gap-4 py-1.5">
                      <StageStepper stages={file.stages} signatures={file.signatures} lang={lang} />
                      <div className="flex-1 min-w-0">
                        {isExpanded && file.log.length > 0 ? (
                          <pre className="font-mono text-[10px] leading-relaxed text-[var(--muted-foreground)] whitespace-pre-wrap">{file.log.slice(-12).join("\n")}</pre>
                        ) : file.error ? (
                          <span className="font-mono text-[10px] text-[var(--danger)] truncate block">{file.error}</span>
                        ) : file.lastLog ? (
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate block">{file.lastLog}</span>
                        ) : null}
                      </div>
                    </div>
                    {file.warning && isExpanded && <WarningStrip file={file} lang={lang} onUseOcr={onUseOcr} />}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      <ComposingRoom expanded={composingExpanded} setExpanded={setComposingExpanded} lang={lang} lines={consoleLines} />
    </div>
  );
}

// ── LIBRARY SCREEN(书目)──────────────────────────────────────────────────────

type SortKey = "date" | "title" | "size";

function LibraryScreen({ lang, books, loading, outputDir, onRefresh, onOpenFolder, onOpenEpub, onReconvert }: {
  lang: Lang;
  books: LibraryBook[];
  loading: boolean;
  outputDir: string;
  onRefresh: () => void;
  onOpenFolder: (epub: string) => void;
  onOpenEpub: (epub: string) => void;
  onReconvert: (path: string) => void;
}) {
  const t = T[lang].library;
  const [sort, setSort] = useState<SortKey>("date");
  const [filter, setFilter] = useState<Backend | "All">("All");
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const filtered = books
    .filter(b => filter === "All" || b.backend === filter)
    .filter(b => !search || b.title.toLowerCase().includes(search.toLowerCase()) || b.author.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      if (sort === "date") return (b.date || "").localeCompare(a.date || "");
      if (sort === "title") return a.title.localeCompare(b.title);
      return parseFloat(b.size) - parseFloat(a.size);
    });

  const totalBytes = books.reduce((s, b) => s + parseFloat(b.size) * 1024 * 1024, 0);
  const totalLabel = books.length ? formatSize(totalBytes) : "—";
  const newest = books.reduce((m, b) => (b.date > m ? b.date : m), "");
  const countOf = (bd: Backend) => books.filter(b => b.backend === bd).length;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-[64px] pr-[72px] pt-6 shrink-0">
        <ChapterHead
          label={`02 ${T[lang].screens[1]}`}
          state={loading ? t.loading : t.books(books.length)}
          lang={lang}
        />
      </div>

      <div className="px-[64px] pr-[72px] py-3 flex items-center gap-4 shrink-0 border-b border-[var(--border)]">
        <div className="relative flex items-center border-b border-[var(--border)]">
          <span className="font-mono text-[11px] text-[var(--border)] mr-2">/</span>
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder={t.search}
            className={`bg-transparent text-[12px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none w-48 ${lang === "zh" ? "cjk" : "font-mono"}`}
          />
          {search && <span className="font-mono text-[10px] text-[var(--muted-foreground)] ml-2 tabular-nums">{filtered.length}</span>}
        </div>

        <div className="flex border border-[var(--border)]">
          {([["date", t.sortDate], ["title", t.sortTitle], ["size", t.sortSize]] as [SortKey, string][]).map(([k, label], i) => (
            <button
              key={k}
              onClick={() => setSort(k)}
              className={`px-3 py-1 font-mono text-[9px] tracking-[0.08em] uppercase transition-colors ${i > 0 ? "border-l border-[var(--border)]" : ""}
                ${sort === k ? "text-[var(--primary)] border-b-2 border-b-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="flex border border-[var(--border)]">
          {(["All", "Local", "MinerU", "PaddleOCR"] as (Backend | "All")[]).map((b, i) => (
            <button
              key={b}
              onClick={() => setFilter(b)}
              className={`px-2 py-1 font-mono text-[9px] tracking-[0.06em] uppercase transition-colors ${i > 0 ? "border-l border-[var(--border)]" : ""}
                ${filter === b ? "text-[var(--primary)] border-b-2 border-b-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
            >
              {b === "All" ? t.filterAll : b === "PaddleOCR" ? "PADDLE" : b.toUpperCase()}
            </button>
          ))}
        </div>

        <div className="flex-1" />
        <button onClick={onRefresh} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.refresh}</button>
        <Rule vertical />
        <button onClick={() => onOpenFolder(books[0]?.epub ?? "")} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.openFolder}</button>
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* 左侧书志汇总栏 */}
        <div className="w-[160px] shrink-0 border-r border-[var(--border)] px-[24px] py-6 overflow-auto">
          <div className="space-y-4">
            <div>
              <div className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1">{t.totalLabel}</div>
              <div className="font-mono text-[20px] tabular-nums text-[var(--foreground)]">{books.length}</div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{totalLabel}</div>
            </div>
            <Rule />
            <div className="space-y-2">
              {([["Local", countOf("Local"), "var(--muted-foreground)"], ["MinerU", countOf("MinerU"), "var(--info)"], ["PaddleOCR", countOf("PaddleOCR"), "var(--paddle)"]] as [Backend, number, string][]).map(([name, count, color]) => (
                <button
                  key={name}
                  onClick={() => setFilter(filter === name ? "All" : name)}
                  className={`w-full flex items-baseline justify-between hover:text-[var(--foreground)] transition-colors ${filter === name ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"}`}
                >
                  <span className="font-mono text-[9px] tracking-[0.06em]">{name === "PaddleOCR" ? "PADDLE" : name.toUpperCase()}</span>
                  <span className="font-mono text-[11px] tabular-nums" style={{ color }}>{count}</span>
                </button>
              ))}
            </div>
            <Rule />
            <div>
              <div className="font-mono text-[9px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1">{t.newest}</div>
              <div className="font-mono text-[10px] text-[var(--foreground)] tabular-nums">{newest ? newest.slice(5) : "—"}</div>
            </div>
          </div>
        </div>

        {/* 书目 */}
        <div className="flex-1 overflow-auto pl-8 pr-[72px] py-4">
          <div className="grid grid-cols-[44px_1fr_80px_64px_88px_140px] gap-4 pb-1.5 border-b border-[var(--border)]">
            {["", t.colTitle, t.colSize, t.colPages, t.colDate, ""].map((h, i) => (
              <span key={i} className="font-mono text-[9px] tracking-[0.1em] uppercase text-[var(--muted-foreground)]">{h}</span>
            ))}
          </div>

          {filtered.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <div className="w-16 h-20 border border-dashed border-[var(--border)] flex items-center justify-center">
                <svg width="12" height="12" viewBox="0 0 12 12" className="text-[var(--border)]" fill="none">
                  <line x1="6" y1="0" x2="6" y2="12" stroke="currentColor" strokeWidth="1" />
                  <line x1="0" y1="6" x2="12" y2="6" stroke="currentColor" strokeWidth="1" />
                </svg>
              </div>
              <p className={`text-[13px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : ""}`}>{books.length === 0 ? t.empty : t.emptyFiltered}</p>
              <p className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{books.length === 0 ? t.emptySub : ""}</p>
            </div>
          )}

          {filtered.map((book, i) => (
            <div
              key={book.id}
              className="grid grid-cols-[44px_1fr_80px_64px_88px_140px] gap-4 py-3 border-b border-[var(--border)] hover:bg-[var(--secondary)] transition-colors row-enter"
              style={{ animationDelay: `${Math.min(i, 8) * 24}ms` }}
              onMouseEnter={() => setHoveredId(book.id)}
              onMouseLeave={() => setHoveredId(null)}
              title={book.epub}
            >
              <SpineBlock title={book.title} />
              <div className="min-w-0 flex flex-col justify-center gap-0.5">
                <div className="flex items-baseline gap-1 overflow-hidden">
                  <span className="font-serif text-[14px] text-[var(--foreground)] shrink-0 truncate max-w-[180px]">{book.title}</span>
                  <span className="flex-1 border-b border-dotted border-[var(--border)] mb-[3px] min-w-[8px]" />
                  <span className={`text-[12px] text-[var(--muted-foreground)] shrink-0 truncate max-w-[120px] ${lang === "zh" ? "cjk" : ""}`}>{book.author || "—"}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <TypeBadge type={book.type} />
                  <BackendBadge backend={book.backend} />
                </div>
              </div>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{book.size}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{book.pages || "—"}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{(book.date || "").slice(5)}</span>
              <div className={`flex items-center gap-3 self-center transition-opacity duration-[140ms] ${hoveredId === book.id ? "opacity-100" : "opacity-0"}`}>
                <button onClick={() => onOpenEpub(book.epub)} className={`font-mono text-[9px] tracking-[0.06em] uppercase text-[var(--foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.openEpub}</button>
                {book.path && (
                  <>
                    <Rule vertical />
                    <button onClick={() => onReconvert(book.path!)} className={`font-mono text-[9px] tracking-[0.06em] uppercase text-[var(--muted-foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.reconvert}</button>
                  </>
                )}
              </div>
            </div>
          ))}

          {/* 版权页 */}
          <div className="mt-6 pt-3 border-t border-[var(--border)]">
            <div className="flex items-baseline gap-4">
              <span className={`text-[11px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono tracking-[0.08em] uppercase"}`}>{t.colophon}</span>
              <span className="font-mono text-[11px] text-[var(--foreground)] truncate">{outputDir}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] ml-auto tabular-nums">{totalLabel} {t.total}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── SETTINGS SCREEN(规范页)───────────────────────────────────────────────────

/** 设置快照:未保存项数 = 与快照的差异个数(不是「改了几次」),DISCARD 也用它回滚 */
interface SettingsSnapshot {
  backendPref: BackendPref;
  outputDir: string;
  cliPath: string;
  cleanOpts: boolean[];
  strictVerify: boolean;
}

/* 以下三个组件必须是模块级定义:定义在 SettingsScreen 内部会在每次渲染时产生
   新的组件类型,React 会卸载重建整棵子树 —— 输入框输一个字符就失焦 */
function SectionCard({ lang, letter, title, summary, children }: {
  lang: Lang; letter: string; title: string; summary: string; children: ReactNode;
}) {
  return (
    <div className="mb-0">
      <div className="flex items-baseline gap-4 py-3 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] tracking-[0.1em] text-[var(--muted-foreground)]">{letter}</span>
        <span className={`text-[20px] text-[var(--foreground)] flex-1 ${lang === "zh" ? "cjk-serif" : "font-serif"}`}>{title}</span>
        <span className="font-mono text-[11px] text-[var(--muted-foreground)]">{summary}</span>
      </div>
      <div className="py-4 space-y-0">{children}</div>
    </div>
  );
}

function FieldRow({ lang, label, children }: { lang: Lang; label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-4 py-2 border-b border-[var(--border)]">
      <span className={`text-[13px] text-[var(--muted-foreground)] w-40 shrink-0 ${lang === "zh" ? "cjk" : ""}`}>{label}</span>
      {children}
    </div>
  );
}

/** 校勘分组主开关:三态(全开 / 部分两色 / 全关) */
function GroupSwitch({ idxs, cleanOpts, onToggle }: {
  idxs: number[]; cleanOpts: boolean[]; onToggle: (on: boolean) => void;
}) {
  const on = idxs.filter(i => cleanOpts[i]).length;
  const allOn = on === idxs.length;
  const someOn = on > 0 && on < idxs.length;
  return (
    <div className="relative flex items-center gap-3 shrink-0">
      <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">{on}/{idxs.length}</span>
      <button
        role="switch"
        aria-checked={allOn}
        aria-label="toggle group"
        onClick={() => onToggle(!allOn)}
        className={`relative w-[40px] h-[22px] border transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--ring)] focus:ring-offset-2 focus:ring-offset-[var(--background)]
          ${allOn || someOn ? "border-[var(--primary)]" : "border-[var(--foreground)]"}`}
        style={someOn ? { background: `linear-gradient(to right, var(--primary) ${(on / idxs.length) * 100}%, var(--muted) ${(on / idxs.length) * 100}%)` } : {}}
      >
        {!someOn && <span className={`absolute inset-0 ${allOn ? "bg-[var(--primary)]" : "bg-[var(--card)]"}`} />}
        <span className={`absolute top-[2px] w-4 h-4 transition-transform z-10 ${allOn ? "translate-x-[19px] bg-white" : "translate-x-[2px] border border-[var(--foreground)] bg-[var(--card)]"}`} />
      </button>
    </div>
  );
}

/** 凭证行:只写不读(原值不回显,这是项目的凭证约定),SHOW 仅在已输入内容时可点 */
function TokenRow({ lang, label, configured, pending, value, onChange, onClear }: {
  lang: Lang; label: string; configured: boolean; pending: boolean; value: string;
  onChange: (v: string) => void; onClear: () => void;
}) {
  const t = T[lang].settings;
  const [show, setShow] = useState(false);
  const hasInput = value.length > 0;
  const chipClass = pending
    ? "border-[var(--warn)] text-[var(--warn)]"
    : configured
    ? "border-[var(--ok)] text-[var(--ok)]"
    : "border-[var(--muted-foreground)] text-[var(--muted-foreground)]";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{label}</span>
        <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border ${chipClass}`}>
          {pending ? t.tokenPending : configured ? t.configured : t.missing}
        </span>
      </div>
      <div className="flex items-center border-b border-[var(--border)]">
        <input
          type={show ? "text" : "password"}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={configured ? t.tokenPlaceholderSet : t.tokenPlaceholder}
          className="flex-1 bg-transparent font-mono text-[11px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none py-1"
        />
        <button
          onClick={() => setShow(!show)}
          disabled={!hasInput}
          title={hasInput ? undefined : t.showHint}
          className={`font-mono text-[9px] tracking-[0.08em] uppercase px-2 ${hasInput ? "text-[var(--muted-foreground)] hover:text-[var(--foreground)]" : "text-[var(--border)] cursor-not-allowed"}`}>
          {show ? t.hide : t.show}
        </button>
        <button onClick={onClear} className="font-mono text-[9px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--danger)] px-2">{t.clear}</button>
      </div>
    </div>
  );
}

function SettingsScreen({
  lang, setLang, darkMode, setDarkMode, backendPref, setBackendPref,
  outputDir, setOutputDir, cliPath, setCliPath, env,
  cleanOpts, setCleanOpts, strictVerify, setStrictVerify, onSave,
}: {
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
  cleanOpts: boolean[];
  setCleanOpts: (v: boolean[]) => void;
  strictVerify: boolean;
  setStrictVerify: (v: boolean) => void;
  onSave: () => void;
}) {
  const t = T[lang].settings;
  const groups = CLEAN_GROUPS[lang];
  const [mineruToken, setMinerUToken] = useState("");
  const [paddleToken, setPaddleToken] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);

  const snapRef = useRef<SettingsSnapshot>({
    backendPref, outputDir, cliPath, cleanOpts: [...cleanOpts], strictVerify,
  });
  const snap = snapRef.current;
  const snapshot = (): SettingsSnapshot => ({
    backendPref, outputDir, cliPath, cleanOpts: [...cleanOpts], strictVerify,
  });
  // 差异数:把开关拨回去就归零(不再是「操作次数」)
  const dirty =
    (backendPref !== snap.backendPref ? 1 : 0) +
    (outputDir !== snap.outputDir ? 1 : 0) +
    (cliPath !== snap.cliPath ? 1 : 0) +
    (strictVerify !== snap.strictVerify ? 1 : 0) +
    cleanOpts.reduce((n, v, i) => n + (v !== snap.cleanOpts[i] ? 1 : 0), 0);
  // 凭证是「写入式」字段:已输入但未保存也必须计入未保存,
  // 否则粘完密钥、别处没改动 → 未保存数为 0 → 底部保存栏根本不出现
  const tokenPending = (mineruToken.trim() ? 1 : 0) + (paddleToken.trim() ? 1 : 0);
  const unsaved = dirty + tokenPending;

  const backendIndex = backendPref === "auto" ? 0 : backendPref === "mineru" ? 1 : 2;
  const configuredMineru = env?.mineru_configured ?? false;
  const configuredPaddle = env?.paddle_configured ?? false;

  const discard = () => {
    setBackendPref(snap.backendPref);
    setOutputDir(snap.outputDir);
    setCliPath(snap.cliPath);
    setCleanOpts([...snap.cleanOpts]);
    setStrictVerify(snap.strictVerify);
    setMinerUToken("");
    setPaddleToken("");
    setNote(null);
    setSavedFlash(false);
  };

  // 凭证:输入框有值才写盘(空 = 不改动;删除用 CLEAR)。返回是否真的写了密钥。
  const saveCredentials = async (): Promise<boolean> => {
    const jobs: { service: string; token: string }[] = [];
    if (mineruToken.trim()) jobs.push({ service: "MinerU", token: mineruToken });
    if (paddleToken.trim()) jobs.push({ service: "PaddleOCR-VL", token: paddleToken });
    if (jobs.length === 0) return false;
    for (const j of jobs) await invoke<string>("save_apikey", { service: j.service, token: j.token });
    setMinerUToken("");
    setPaddleToken("");
    setNote(null);
    return true;
  };

  const clearCredential = async (service: string) => {
    try {
      await invoke<string>("save_apikey", { service, token: "" });
      setNote(null);
      onSave();
    } catch (e) {
      setNote(String(e));
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const wroteKeys = await saveCredentials();
      snapRef.current = snapshot();   // 保存成功 → 建立新快照(未保存数归零)
      onSave();                        // 落盘设置 + 刷新环境检查(凭证才会变成 CONFIGURED)
      if (wroteKeys) {
        setSavedFlash(true);
        window.setTimeout(() => setSavedFlash(false), 2500);
      }
    } catch (e) {
      setNote(String(e));
    } finally {
      setSaving(false);
    }
  };

  const browseDir = async () => {
    const sel = await open({ directory: true });
    if (sel) setOutputDir(String(sel));
  };

  const copyInstall = async () => {
    try {
      await navigator.clipboard.writeText(t.installCmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* 剪贴板不可用时忽略 */ }
  };

  const envRows = [
    { name: t.pandoc, status: env?.pandoc.status ?? "missing", version: env?.pandoc.version ?? null, path: env?.pandoc.path ?? null },
    { name: t.cli, status: env?.engine.status ?? "missing", version: env?.engine.version ?? null, path: env?.engine.path ?? null },
  ];

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="px-[64px] pr-[72px] pt-6 shrink-0">
        <ChapterHead label={`03 ${T[lang].screens[2]}`} state={unsaved > 0 ? t.dirty(unsaved) : undefined} lang={lang} />
      </div>

      <div className="flex-1 overflow-auto px-[64px] pr-[72px] pb-24">
        {/* A: 转换器 */}
        <SectionCard lang={lang} letter="A" title={t.sections[0]} summary="">
          <FieldRow lang={lang} label={t.outputDir}>
            <input
              type="text" value={outputDir} onChange={e => setOutputDir(e.target.value)}
              className="flex-1 bg-transparent border-b border-[var(--border)] font-mono text-[11px] text-[var(--foreground)] focus:outline-none focus:border-[var(--primary)] py-1"
            />
            <button onClick={browseDir} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] border border-[var(--border)] px-3 py-1 ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.browse}</button>
          </FieldRow>
          <FieldRow lang={lang} label={t.cliPath}>
            <input
              type="text" value={cliPath} onChange={e => setCliPath(e.target.value)}
              placeholder={t.cliPlaceholder}
              className="flex-1 bg-transparent border-b border-[var(--border)] font-mono text-[11px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none focus:border-[var(--primary)] py-1"
            />
          </FieldRow>

          <div className="mt-4">
            <div className="font-mono text-[9px] tracking-[0.12em] uppercase text-[var(--muted-foreground)] mb-2">{t.envCheck}</div>
            <Rule />
            {envRows.map(item => (
              <div key={item.name} className="flex items-center gap-3 py-2 border-b border-[var(--border)]">
                <div className={`w-1.5 h-1.5 shrink-0 ${item.status === "ok" ? "bg-[var(--ok)]" : "bg-[var(--danger)]"}`} />
                <span className="font-mono text-[11px] text-[var(--foreground)] flex-1">{item.name}</span>
                {item.version && <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{item.version}</span>}
                {item.path && <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate max-w-[280px]">{item.path}</span>}
                <span className={`font-mono text-[8px] tracking-[0.08em] px-1.5 py-0.5 border ${item.status === "ok" ? "border-[var(--ok)] text-[var(--ok)]" : "border-[var(--danger)] text-[var(--danger)]"}`}>
                  {item.status === "ok" ? "OK" : "MISSING"}
                </span>
              </div>
            ))}
            {env && env.pandoc.status !== "ok" && (
              <div className="my-2 border-l-[3px] border-[var(--danger)] pl-4 py-2">
                <p className={`text-[13px] text-[var(--foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.pandocMissing}</p>
                <div className="flex items-center gap-3">
                  <code className="font-mono text-[11px] text-[var(--foreground)] bg-[var(--secondary)] px-3 py-1">{t.installCmd}</code>
                  <button onClick={copyInstall} className="font-mono text-[9px] tracking-[0.08em] border border-[var(--border)] px-2 py-1 text-[var(--muted-foreground)] hover:text-[var(--foreground)]">
                    {copied ? t.copied : t.copy}
                  </button>
                </div>
              </div>
            )}
          </div>
        </SectionCard>
        <Rule />

        {/* B: OCR 与凭证 */}
        <SectionCard lang={lang} letter="B" title={t.sections[1]} summary="">
          <div className={`text-[12px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.backendLabel}</div>
          <div role="radiogroup">
            {t.backends.map(([label, desc], i) => (
              <RadioRow
                key={i}
                label={label}
                desc={desc}
                active={backendIndex === i}
                onClick={() => setBackendPref((i === 0 ? "auto" : i === 1 ? "mineru" : "paddleocr") as BackendPref)}
                lang={lang}
              />
            ))}
          </div>
          <div className="mt-4 space-y-3">
            <TokenRow
              lang={lang}
              label={t.mineruToken}
              configured={configuredMineru}
              pending={mineruToken.trim().length > 0}
              value={mineruToken}
              onChange={setMinerUToken}
              onClear={() => void clearCredential("MinerU")}
            />
            <TokenRow
              lang={lang}
              label={t.paddleToken}
              configured={configuredPaddle}
              pending={paddleToken.trim().length > 0}
              value={paddleToken}
              onChange={setPaddleToken}
              onClear={() => void clearCredential("PaddleOCR-VL")}
            />
            {savedFlash && (
              <p className={`text-[10px] text-[var(--ok)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.tokenSaved}</p>
            )}
            {note && <p className={`text-[10px] leading-relaxed text-[var(--danger)] break-all ${lang === "zh" ? "cjk" : "font-mono"}`}>{note}</p>}
          </div>
        </SectionCard>
        <Rule />

        {/* C: 校勘流水线 */}
        <SectionCard lang={lang} letter="C" title={t.sections[2]} summary={`${cleanOpts.filter(Boolean).length}/9 ON`}>
          {groups.map((group, gi) => {
            const idxs = group.items.map(it => CLEAN_KEYS.indexOf(it.key as typeof CLEAN_KEYS[number]));
            return (
              <div key={gi} className="mb-4">
                <div className="flex items-center gap-4 py-1.5 border-b border-[var(--border)]">
                  <span className={`text-[12px] text-[var(--muted-foreground)] flex-1 ${lang === "zh" ? "cjk font-medium" : "font-mono tracking-[0.08em]"}`}>{group.name}</span>
                  <GroupSwitch
                    idxs={idxs}
                    cleanOpts={cleanOpts}
                    onToggle={on => setCleanOpts(cleanOpts.map((v, i) => idxs.includes(i) ? on : v))}
                  />
                </div>
                {group.items.map(item => {
                  const idx = CLEAN_KEYS.indexOf(item.key as typeof CLEAN_KEYS[number]);
                  return (
                    <div key={item.key} title={item.key}
                      className="flex items-center h-11 gap-4 border-b border-[var(--border)] hover:bg-[var(--secondary)] transition-colors cursor-pointer"
                      onClick={() => setCleanOpts(cleanOpts.map((v, j) => j === idx ? !v : v))}>
                      <span className="font-mono text-[13px] text-[var(--muted-foreground)] w-5 shrink-0 text-center">{item.glyph}</span>
                      <div className="flex-1 min-w-0">
                        <div className={`text-[13px] text-[var(--foreground)] ${lang === "zh" ? "cjk" : ""}`}>
                          {item.label}
                          {item.advanced && (
                            <span className="ml-2 font-mono text-[8px] tracking-[0.08em] border border-[var(--muted-foreground)] text-[var(--muted-foreground)] px-1">ADVANCED</span>
                          )}
                        </div>
                        <div className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{item.desc}</div>
                      </div>
                      <span className={`font-mono text-[9px] shrink-0 ${cleanOpts[idx] ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>
                        {cleanOpts[idx] ? "ON" : "OFF"}
                      </span>
                      <button
                        role="switch"
                        aria-checked={cleanOpts[idx]}
                        aria-label={item.label}
                        className={`relative shrink-0 w-[40px] h-[22px] border transition-colors
                          ${cleanOpts[idx] ? "bg-[var(--primary)] border-[var(--primary)]" : "bg-[var(--card)] border-[var(--foreground)]"}`}
                      >
                        <span className={`absolute top-[2px] w-4 h-4 transition-transform ${cleanOpts[idx] ? "translate-x-[19px] bg-white" : "translate-x-[2px] border border-[var(--foreground)] bg-transparent"}`} />
                      </button>
                    </div>
                  );
                })}
              </div>
            );
          })}
          <p className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.cleanNote}</p>
        </SectionCard>
        <Rule />

        {/* D: 质量与外观 */}
        <SectionCard lang={lang} letter="D" title={t.sections[3]} summary="">
          <div className="border-b border-[var(--border)]">
            <Switch
              checked={strictVerify}
              onChange={setStrictVerify}
              label={t.epubValidation}
              description={t.epubValidationDesc}
              lang={lang}
            />
          </div>
          <div className="py-3 border-b border-[var(--border)]">
            <div className={`text-[13px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.themeLabel}</div>
            <Segment
              options={[{ value: "light", label: t.themeLight }, { value: "dark", label: t.themeDark }]}
              value={darkMode ? "dark" : "light"}
              onChange={v => setDarkMode(v === "dark")}
            />
          </div>
          <div className="py-3 border-b border-[var(--border)]">
            <div className={`text-[13px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.langLabel}</div>
            <Segment
              options={[{ value: "en", label: t.langEn }, { value: "zh", label: t.langZh }]}
              value={lang}
              onChange={v => setLang(v as Lang)}
            />
          </div>
        </SectionCard>
      </div>

      {unsaved > 0 && (
        <div className="shrink-0 border-t border-[var(--border)] flex items-center justify-end gap-4 px-[64px] pr-[72px] py-3 bg-[var(--card)]">
          <span className="font-mono text-[11px] text-[var(--muted-foreground)] flex-1">{t.dirty(unsaved)}</span>
          <button onClick={discard} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.discard}</button>
          <button onClick={() => void handleSave()} disabled={saving} className={`px-6 py-2 bg-[var(--primary)] text-[var(--primary-foreground)] font-mono text-[10px] tracking-[0.1em] uppercase hover:opacity-90 transition-opacity disabled:opacity-60 ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.save}</button>
        </div>
      )}
    </div>
  );
}

// ── APP SHELL ─────────────────────────────────────────────────────────────────

export default function App() {
  const [screen, setScreen] = useState<Screen>("convert");
  const [darkMode, setDarkMode] = useState<boolean>(() => localStorage.getItem("pdf2epub.dark") === "1");
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("pdf2epub.lang") as Lang) || "en");
  const [composingExpanded, setComposingExpanded] = useState(false);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [consoleLines, setConsoleLines] = useState<ConsoleLine[]>([]);
  const [env, setEnv] = useState<EnvState | null>(null);
  const [diskBooks, setDiskBooks] = useState<LibEntry[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recent, setRecent] = useState<RecentEntry[]>(loadRecent);
  const [preflights, setPreflights] = useState<Record<string, PreflightFile>>({});
  const [batchNotice, setBatchNotice] = useState<PreflightReport | null>(null);
  const [busyPreflight, setBusyPreflight] = useState(false);
  const [backendPref, setBackendPref] = useState<BackendPref>(
    () => (localStorage.getItem("pdf2epub.backend") as BackendPref) || "auto",
  );
  const [outputDir, setOutputDir] = useState<string>(
    () => localStorage.getItem("pdf2epub.outputDir") || "output",
  );
  const [cliPath, setCliPath] = useState<string>(
    () => localStorage.getItem("pdf2epub.cliPath") || "",
  );
  const [cleanOpts, setCleanOpts] = useState<boolean[]>(loadCleanOpts);
  const [strictVerify, setStrictVerify] = useState<boolean>(
    () => localStorage.getItem("pdf2epub.strict") === "1",
  );
  const canceled = useRef<Set<string>>(new Set());
  // 队列最新快照:转换结束回写书库元数据时取用(不依赖 setState 的异步性)
  const filesRef = useRef<QueueFile[]>([]);
  useEffect(() => { filesRef.current = files; }, [files]);
  const cleanDisable = useMemo(() => CLEAN_KEYS.filter((_, i) => !cleanOpts[i]), [cleanOpts]);

  // ── CLI 进度事件 ──
  //  stdout 的 JSON 事件(--json-events)是状态/进度的唯一来源;stderr 的人类日志
  //  进控制台。只有拿不到事件(老版 CLI)时才退回正则解析,并给该文件打 legacy 标记。
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    // 事件流是否见过该文件(未见事件却收到日志 = 老版 CLI,退化正则并标注)
    const seenEvent = new Set<string>();
    const logLines = new Map<string, number>();
    listen<{ file: string; line: string; channel?: string }>("conv://progress", e => {
      const { file, line } = e.payload;
      const ev = parseEventLine(line);
      if (ev) {
        seenEvent.add(file);
      } else {
        const count = (logLines.get(file) ?? 0) + 1;
        logLines.set(file, count);
        const legacy = !seenEvent.has(file) && count >= 3;
        const ts = new Date().toTimeString().slice(0, 8);
        setConsoleLines(prev => [...prev.slice(-300), {
          ts,
          level: /error|失败/i.test(line) ? "ERROR" : /warn|伪文字层|疑似/i.test(line) ? "WARN" : "INFO",
          text: line,
          legacy,
        }]);
      }
      setFiles(prev => prev.map(f => {
        if (f.path !== file || f.status !== "converting") return f;
        if (ev) {
          const engine = reduceEvent(f.engine ?? initialEventState(), ev);
          return {
            ...f,
            engine,
            progress: progressOf(engine),
            pages: engine.pages ?? f.pages,
            backend: (engine.backend as Backend | undefined) ?? f.backend,
            type: f.type === "MD" ? f.type : (engine.kind ?? f.type),
            stages: engine.stages,
            signatures: engine.signatures ?? f.signatures,
            warning: f.warning || engine.warning,
            error: engine.error ?? f.error,
            legacy: false,
            lastLog: line,
          };
        }
        // legacy:老版 CLI 的中文日志(仅兜底,progress 只能估)
        return {
          ...f,
          legacy: !seenEvent.has(file),
          progress: Math.min(95, f.progress + 5),
          type: f.type === "MD" ? f.type : (parseTypeFromLine(line, f.name) ?? f.type),
          stages: updateStagesFromLine(line, f.stages),
          warning: f.warning || parseWarningFromLine(line),
          log: [...f.log.slice(-100), line],
          lastLog: line,
        };
      }));
    }).then(un => { unlisten = un; });
    return () => { unlisten?.(); };
  }, []);

  // ── 主题 / 语言 ──
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    localStorage.setItem("pdf2epub.dark", darkMode ? "1" : "0");
  }, [darkMode]);
  useEffect(() => {
    document.documentElement.classList.toggle("lang-zh", lang === "zh");
    localStorage.setItem("pdf2epub.lang", lang);
  }, [lang]);

  // ── 设置页刷新环境检查 ──
  useEffect(() => {
    if (screen !== "settings") return;
    invoke<EnvState>("check_env").then(setEnv).catch(() => setEnv(null));
  }, [screen]);

  // ── 书库:打开时同步 library.json ──
  const loadLibrary = useCallback(async () => {
    setLibraryLoading(true);
    try {
      const list = await invoke<LibEntry[]>("library_sync", { outputDir });
      setDiskBooks(list);
    } catch {
      setDiskBooks([]);
    } finally {
      setLibraryLoading(false);
    }
  }, [outputDir]);

  useEffect(() => {
    if (screen !== "library") return;
    void loadLibrary();
  }, [screen, loadLibrary]);

  // ── 印前检查(CLI --dry-run --json) ──
  const runPreflight = useCallback(async (paths: string[]) => {
    if (paths.length === 0) return;
    setBusyPreflight(true);
    try {
      const raw = await invoke<string>("preflight", { filePaths: paths, cliPath: cliPath || null });
      const report = JSON.parse(raw) as PreflightReport;
      setPreflights(prev => {
        const next = { ...prev };
        for (const f of report.files) next[normPath(f.path)] = f;
        return next;
      });
      setBatchNotice(report);
      // 预检结果直接落到队列行:转换前就能看到类型 / 页数 / 后端 / 折帖
      setFiles(prev => prev.map(f => {
        const plan = report.files.find(p => normPath(p.path) === normPath(f.path));
        if (!plan) return f;
        return {
          ...f,
          type: f.type ?? KIND_TO_TYPE[plan.kind],
          backend: backendFromPlan(plan.backend),
          pages: plan.pages || f.pages,
          signatures: plan.shards > 1 ? { current: 0, total: plan.shards } : undefined,
          cloudPages: plan.ocr_pages,
        };
      }));
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), { ts, level: "ERROR" as const, text: `[PREFLIGHT] ${String(e)}` }]);
    } finally {
      setBusyPreflight(false);
    }
  }, [cliPath]);

  // ── 空状态下的「最近文件」自动预检:避免一排未知「?」徽章 ──
  useEffect(() => {
    if (screen !== "convert" || files.length > 0) return;
    const todo = recent
      .slice(0, 3)
      .filter(r => !preflights[normPath(r.path)] && /\.(pdf|md|markdown)$/i.test(r.path));
    if (todo.length === 0) return;
    void runPreflight(todo.map(r => r.path));
  }, [screen, files.length, recent, preflights, runPreflight]);

  // ── 转换 ──
  const convertOne = useCallback(async (f: QueueFile, backendOverride?: string) => {
    if (canceled.current.has(f.id)) return;
    canceled.current.delete(f.id);
    setFiles(prev => prev.map(x => x.id === f.id ? {
      ...x,
      status: "converting" as FileStatus,
      progress: 5,
      error: undefined,
      warning: false,
      legacy: false,
      log: [],
      lastLog: undefined,
      stages: freshStages(),
      engine: initialEventState(),
    } : x));
    try {
      const res = await invoke<{ success: boolean; epub: string | null; error: string | null }>("convert_file", {
        filePath: f.path,
        outputDir,
        backend: backendOverride ?? (backendPref === "auto" ? null : backendPref),
        retries: 1,
        cliPath: cliPath || null,
        taskId: f.id,
        cleanDisable,
        strict: strictVerify,
      });
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status: res.success ? "done" : "failed",
        progress: res.success ? 100 : 60,
        epub: res.epub ?? undefined,
        error: res.error ?? undefined,
        date: res.success ? new Date().toISOString().slice(0, 10) : x.date,
        signatures: res.success ? undefined : x.signatures,
        stages: res.success
          ? x.stages.map(s => ({ ...s, state: "done" as StageState }))
          : x.stages.map(s => s.state === "active" ? { ...s, state: "failed" as StageState } : s),
        lastLog: res.error ?? undefined,
      } : x));
      // 转换完成 → 把类型/后端/页数回写进书库(library.json),重启后仍在
      if (res.success && res.epub) {
        const latest = filesRef.current.find(v => v.id === f.id);
        void invoke("library_set_meta", {
          path: res.epub,
          kind: latest?.type ?? f.type ?? null,
          backend: latest?.backend ?? null,
          pages: latest?.pages ?? 0,
        }).catch(() => { /* 记录尚未建立时忽略 */ });
      }
    } catch (err) {
      if (canceled.current.has(f.id)) return;
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status: "failed" as FileStatus,
        progress: 60,
        error: String(err),
      } : x));
    }
  }, [outputDir, backendPref, cliPath, cleanDisable, strictVerify]);

  const rememberRecent = useCallback((paths: string[]) => {
    setRecent(prev => {
      const now = Date.now();
      const incoming: RecentEntry[] = paths.map(p => ({ path: p, name: baseName(p), at: now }));
      const merged = [...incoming, ...prev.filter(r => !paths.some(p => normPath(p) === normPath(r.path)))];
      const trimmed = merged.slice(0, MAX_RECENT);
      localStorage.setItem(RECENT_KEY, JSON.stringify(trimmed));
      return trimmed;
    });
  }, []);

  const addFiles = useCallback(async (paths: string[]) => {
    const newFiles: QueueFile[] = paths.map((p, i) => ({
      id: `${Date.now()}-${i}`,
      path: p,
      name: baseName(p),
      size: "—",
      status: "pending" as FileStatus,
      progress: 0,
      backend: "Auto" as Backend,
      pages: 0,
      type: /\.md$/i.test(p) ? ("MD" as FileType) : undefined,
      stages: freshStages(),
      log: [],
    }));
    setFiles(prev => [...prev, ...newFiles]);
    rememberRecent(paths);
    setScreen("convert");
    // 先印前检查(检测类型/页数/折帖/云端页数),再串行转换
    void runPreflight(paths);
    for (const f of newFiles) {
      await convertOne(f);
    }
  }, [convertOne, rememberRecent, runPreflight]);

  // ── 原生拖放(WebView2 下 HTML5 DnD 拿不到路径)──
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    getCurrentWebview().onDragDropEvent(event => {
      const p = event.payload;
      if (p.type === "enter" || p.type === "over") {
        setDragging(true);
      } else if (p.type === "leave") {
        setDragging(false);
      } else if (p.type === "drop") {
        setDragging(false);
        if (p.paths.length > 0) void addFiles(p.paths);
      }
    }).then(un => { unlisten = un; });
    return () => { unlisten?.(); };
  }, [addFiles]);

  const pickFiles = useCallback(async () => {
    const sel = await open({
      multiple: true,
      filters: [{ name: "PDF / Markdown", extensions: ["pdf", "md", "markdown"] }],
    });
    if (sel) {
      const paths = Array.isArray(sel) ? sel : [sel];
      await addFiles(paths);
    }
  }, [addFiles]);

  // ── 队列操作 ──
  const cancelFile = useCallback((id: string) => {
    canceled.current.add(id);
    void invoke("cancel_convert", { taskId: id }).catch(() => { /* 已退出 */ });
    setFiles(prev => prev.map(f => f.id === id ? { ...f, status: "cancelled" as FileStatus, error: undefined } : f));
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

  const clearDone = useCallback(() => setFiles(prev => prev.filter(f => f.status !== "done")), []);

  const cancelAll = useCallback(() => {
    setFiles(prev => prev.map(f =>
      (f.status === "pending" || f.status === "converting") ? { ...f, status: "cancelled" as FileStatus } : f,
    ));
    for (const f of files) {
      if (f.status === "pending" || f.status === "converting") {
        canceled.current.add(f.id);
        if (f.status === "converting") void invoke("cancel_convert", { taskId: f.id }).catch(() => { /* 已退出 */ });
      }
    }
  }, [files]);

  const useOcrFor = useCallback((id: string) => {
    const f = files.find(x => x.id === id);
    if (f) void convertOne(f, "mineru");
  }, [files, convertOne]);

  // ── 外观 / 语言(Segment 与 页眉 按钮共用)──
  const handleLang = (l: Lang) => setLang(l);
  // 严格校验与其他设置一致:先进内存,点「保存设置」才落盘
  // (否则「放弃」无法真正回滚 —— 内存回滚了、磁盘已经写进去了)
  const handleStrict = (v: boolean) => setStrictVerify(v);

  const saveSettings = useCallback(async () => {
    localStorage.setItem("pdf2epub.backend", backendPref);
    localStorage.setItem("pdf2epub.outputDir", outputDir);
    localStorage.setItem("pdf2epub.cliPath", cliPath);
    localStorage.setItem("pdf2epub.clean", JSON.stringify(cleanOpts));
    localStorage.setItem("pdf2epub.strict", strictVerify ? "1" : "0");
    try {
      await invoke("set_cli_path", { path: cliPath || null });
    } catch { /* 忽略 */ }
    try {
      setEnv(await invoke<EnvState>("check_env"));
    } catch { /* 忽略 */ }
  }, [backendPref, outputDir, cliPath, cleanOpts, strictVerify]);

  // ── 书库数据:library.json(含转换时回写的类型/后端/页数)+ 本会话兜底 ──
  const books: LibraryBook[] = diskBooks.map(b => {
    const s = files.find(f => f.status === "done" && f.epub && normPath(f.epub) === normPath(b.path));
    return {
      id: b.path,
      title: b.title || baseName(b.path),
      author: b.author,
      size: formatSize(b.size),
      // 页数/类型/后端优先取库里的持久值(重启后仍在),会话内再用实时值纠正
      pages: s?.pages || b.pages || 0,
      date: b.mtime ? new Date(b.mtime * 1000).toISOString().slice(0, 10) : "",
      type: s?.type ?? ((b.kind ?? undefined) as FileType | undefined),
      backend: s?.backend ?? ((b.backend ?? undefined) as Backend | undefined),
      epub: b.path,
      path: s?.path,
    };
  });

  const openEpub = useCallback(async (epub: string) => {
    try {
      await invoke("open_epub", { path: epub });
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: "ERROR" as const,
        text: `[OPEN EPUB] ${epub} — ${String(e)}`,
      }]);
    }
  }, []);

  const openFolder = useCallback(async (epub: string) => {
    try {
      if (epub) await revealItemInDir(epub);
      else await openPath(outputDir);
    } catch (e) {
      console.error("reveal failed", e);
    }
  }, [outputDir]);

  const reconvert = useCallback((path: string) => {
    const f = files.find(x => x.path === path);
    if (f) void convertOne(f);
    else void addFiles([path]);
  }, [files, convertOne, addFiles]);

  const convertingCount = files.filter(f => f.status === "converting").length;
  const activeFile = files.find(f => f.status === "converting");
  const activeStage = activeFile?.stages.find(s => s.state === "active");
  const pendingCount = files.filter(f => f.status === "pending").length;
  const previewFile = files.length > 0 ? files[files.length - 1] : null;
  const t = T[lang];

  const NAV: { id: Screen; label: string; count: string | null }[] = [
    { id: "convert", label: t.screens[0], count: convertingCount > 0 ? String(convertingCount) : null },
    { id: "library", label: t.screens[1], count: String(diskBooks.length) },
    { id: "settings", label: t.screens[2], count: null },
  ];

  return (
    <div
      className={`w-full h-full flex flex-col overflow-hidden select-none ${darkMode ? "dark" : ""} ${lang === "zh" ? "lang-zh" : ""}`}
      style={{ background: "var(--background)", color: "var(--foreground)" }}
    >
      {/* 页眉 — running head */}
      <header className="shrink-0 flex items-stretch" style={{ background: "var(--card)" }}>
        <div className="h-10 flex items-center px-[64px] pr-[72px] w-full gap-6">
          <span className="font-mono text-[11px] tracking-[0.08em] text-[var(--muted-foreground)] shrink-0">
            {t.brand} <span className="opacity-50">{APP_VERSION}</span>
          </span>

          <Rule vertical />

          {/* 目录 — table of contents nav */}
          <nav className="flex items-stretch h-full flex-1" aria-label="Main navigation">
            {NAV.map((s, i) => (
              <span key={s.id} className="flex items-stretch">
                {i > 0 && <span className="font-mono text-[11px] text-[var(--border)] self-center px-3">·</span>}
                <button
                  onClick={() => setScreen(s.id)}
                  className={`relative flex items-center h-full px-1 font-mono text-[11px] tracking-[0.08em] uppercase transition-colors focus:outline-none
                    ${screen === s.id ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
                >
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] mr-1.5">0{i + 1}</span>
                  <span className={lang === "zh" ? "cjk font-medium" : ""}>{s.label}</span>
                  <span className="mx-2 font-mono text-[10px] text-[var(--border)] tracking-widest overflow-hidden" style={{ maxWidth: 48, display: "inline-block" }}>
                    {"·".repeat(8)}
                  </span>
                  {s.count !== null && (
                    <span className={`font-mono text-[10px] tabular-nums ${s.id === "convert" && convertingCount > 0 ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>
                      {s.count}
                    </span>
                  )}
                  {screen === s.id && <span className="absolute bottom-0 left-0 right-0 h-px bg-[var(--primary)]" />}
                </button>
              </span>
            ))}
          </nav>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => setDarkMode(!darkMode)}
              className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
              aria-label="Toggle theme"
            >
              {darkMode ? "☀" : "☾"}
            </button>
            <Rule vertical />
            <button
              onClick={() => handleLang(lang === "en" ? "zh" : "en")}
              className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] hover:text-[var(--foreground)] border border-[var(--border)] px-1.5 py-0.5 hover:border-[var(--foreground)] transition-colors"
            >
              {t.langToggle}
            </button>
            <Rule vertical />
            <span className="font-mono text-[11px] tabular-nums text-[var(--muted-foreground)]">
              {new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" })}
            </span>
          </div>
        </div>
      </header>
      <Rule />

      <main className="flex-1 flex overflow-hidden min-h-0">
        {screen === "convert" && (
          <ConvertScreen
            lang={lang}
            files={files}
            preview={previewFile}
            preflightOf={(p: string) => preflights[normPath(p)]}
            batchNotice={batchNotice}
            dragging={dragging}
            recent={recent}
            busyPreflight={busyPreflight}
            composingExpanded={composingExpanded}
            setComposingExpanded={setComposingExpanded}
            consoleLines={consoleLines}
            onPick={() => void pickFiles()}
            onCancel={cancelFile}
            onRetry={retryFile}
            onClearDone={clearDone}
            onRetryFailed={() => void retryFailed()}
            onCancelAll={cancelAll}
            onUseOcr={useOcrFor}
            onPreflight={() => void runPreflight(files.map(f => f.path))}
            onPreflightRecent={(p: string) => void runPreflight([p])}
            onAddRecent={(p: string) => void addFiles([p])}
          />
        )}
        {screen === "library" && (
          <LibraryScreen
            lang={lang}
            books={books}
            loading={libraryLoading}
            outputDir={outputDir}
            onRefresh={() => void loadLibrary()}
            onOpenFolder={openFolder}
            onOpenEpub={openEpub}
            onReconvert={reconvert}
          />
        )}
        {screen === "settings" && (
          <SettingsScreen
            lang={lang}
            setLang={handleLang}
            darkMode={darkMode}
            setDarkMode={setDarkMode}
            backendPref={backendPref}
            setBackendPref={setBackendPref}
            outputDir={outputDir}
            setOutputDir={setOutputDir}
            cliPath={cliPath}
            setCliPath={setCliPath}
            env={env}
            cleanOpts={cleanOpts}
            setCleanOpts={setCleanOpts}
            strictVerify={strictVerify}
            setStrictVerify={handleStrict}
            onSave={() => void saveSettings()}
          />
        )}
      </main>

      {/* 版心脚注 — status bar */}
      <Rule />
      <footer className="shrink-0 h-6 flex items-center px-[64px] pr-[72px] gap-4" style={{ background: "var(--card)" }}>
        <div className={`w-1.5 h-1.5 shrink-0 ${convertingCount > 0 ? "bg-[var(--primary)]" : "bg-[var(--ok)]"}`} />
        <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--muted-foreground)] uppercase">
          {activeStage
            ? (lang === "zh"
              ? ({ DETECT: "制版", EXTRACT: "检字", CLEAN: "校勘", BUILD: "付印" } as Record<string, string>)[activeStage.key] ?? activeStage.key
              : activeStage.isOcr ? "OCR" : activeStage.key)
            : t.status.ready}
        </span>
        {activeFile && (
          <>
            <Rule vertical />
            <span className="font-mono text-[10px] text-[var(--primary)] truncate max-w-[200px]">{activeFile.name}</span>
          </>
        )}
        <Rule vertical />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{t.status.backend}: {backendPref.toUpperCase()}</span>
        <Rule vertical />
        <span className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.status.pending(pendingCount)}</span>
        <div className="flex-1" />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate max-w-[300px]">{outputDir}</span>
      </footer>
    </div>
  );
}
