// 领域类型:队列行 / 书库条目 / CLI 计划 / 清理项键。
// 这里只放**跨模块共享**的类型;只在一个组件里用的局部类型留在该组件文件。
import type { EventState, Signatures, StageNode } from "./events";

// ── TYPES ─────────────────────────────────────────────────────────────────────

export type Screen = "convert" | "library" | "settings";
/**
 * 队列行的状态。**三个终态不能混**:
 *   done    = 本次真的跑完了整条链路(引擎 `complete`)
 *   skipped = 本次什么都没做,产物早已存在(引擎 `skip`)—— 不是「成功」
 *   failed  = 失败(带分类码与原文)
 */
export type FileStatus = "pending" | "converting" | "done" | "skipped" | "failed" | "cancelled";
export type Backend = "Local" | "MinerU" | "PaddleOCR" | "Auto";
export type FileType = "TXT" | "SCN" | "HYB" | "MD";
export type BackendPref = "auto" | "mineru" | "paddleocr";
/**
 * 复核表里**逐行**可以指定的后端。比 `BackendPref` 多一个 `pymupdf`:
 * 检测把扫描书误判成「自带文字层」时可以强制 OCR,反过来把带乱码文字层的书
 * 强制走本地也可以。`auto`(或没选)= 不指定,听检测 + 设置里的全局偏好。
 */
export type RowBackend = "auto" | "pymupdf" | "mineru" | "paddleocr";

export interface QueueFile {
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
  /** 错误的机器可读分类(引擎 error 事件 / convert_file 的 error_code) */
  errorCode?: string;
  /** 引擎正在重试(第 N 次尝试失败,即将重跑)—— 与「卡住不动」区分开 */
  retrying?: { attempt: number; total: number } | null;
  epub?: string;
  date?: string;
  /** 引擎事件流状态(--json-events);进度与阶段由它驱动 */
  engine?: EventState;
  /** 该文件出现过「非事件」日志 → 退化为旧正则解析(老版 CLI 兜底) */
  legacy?: boolean;
}

export interface ConsoleLine {
  ts: string;
  level: "INFO" | "WARN" | "ERROR";
  text: string;
  /** 该行来自「老版 CLI + 正则兜底」路径(事件流缺失) */
  legacy?: boolean;
}

/** Toast 抽屉里的一条播报。tone 只决定左侧 2px 竖线的颜色。 */
export interface Notice {
  id: string;
  tone: "ok" | "danger" | "info";
  message: string;
}

export interface LibraryBook {
  id: string;
  title: string;
  author: string;
  /** 已格式化的展示值("1.2 MB")—— 只用于显示,排序与合计一律用 bytes */
  size: string;
  /** 原始字节数:排序/合计必须用它,不能 parseFloat("512 KB") */
  bytes: number;
  pages: number;
  date: string;
  type?: FileType;
  backend?: Backend;
  epub: string;
  path?: string;
}

export interface LibEntry {
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
  /** 源文件路径(PDF/MD):书库「重新转换」要用它。后端只在文件真的存在时给出,
   *  所以有值即可放心显示按钮。旧记录可能是 null。 */
  source?: string | null;
}

export interface EnvState {
  pandoc: { status: string; version: string | null; path: string | null };
  engine: { status: string; version: string | null; path: string | null };
  mineru_configured: boolean;
  paddle_configured: boolean;
  apikey_path: string | null;
}

/** CLI `--dry-run --json` 的单个文件计划(印前检查的原始数据) */
export interface PreflightFile {
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

export interface PreflightReport {
  files: PreflightFile[];
  total_ocr_pages: number;
  total_shards: number;
  quota: number;
  over_quota: boolean;
}

/**
 * 导入复核表的一行:数据来自**真实 preflight**(CLI `--dry-run --json`)。
 * `known=false` = 这个文件还没拿到计划(命令行失败/引擎没回),界面显示占位,
 * 不自己脑补类型与页数 —— 假数据比空更难发现,它会骗过下一个人。
 */
export interface ReviewRow {
  id: string;
  name: string;
  path: string;
  type?: FileType;
  pages?: number;
  backend?: Backend;
  shards?: number;
  ocrPages?: number;
  known: boolean;
  /** 用户在这一行上亲手指定的后端(见 `RowBackend`);缺省 = 听检测与全局偏好 */
  override?: RowBackend;
  /** 这一行正在按新后端重跑 preflight(换了后端,类型/页数/折帖都要重算) */
  busy?: boolean;
}

/** 一批新导入文件的复核请求(App 持有,ConvertScreen 渲染)。 */
export interface ReviewSheet {
  rows: ReviewRow[];
}

export interface RecentEntry {
  path: string;
  name: string;
  at: number;
}

/**
 * `%LOCALAPPDATA%\pdf2epub\settings.json` 的内容。
 *
 * 字段**全部可缺省**:旧版本写的文件、或用户手改过的文件都要能读进来,缺的项由
 * 内置默认值补齐(读失败不该让人丢设置)。
 *
 * `clean` 存的是**稀疏映射** —— 只记用户亲手改过的开关。存全量会把「用户没意见」
 * 也变成「用户要这样」,`config.yaml` 从此再也管不着那几项,而用户不知道自己
 * 刚把可配置性关掉了。没记的键 = 听 config.yaml(基线)。
 */
export interface SettingsFile {
  theme?: "dark" | "light";
  lang?: "zh" | "en";
  backend?: BackendPref;
  output_dir?: string;
  cli_path?: string;
  clean?: Record<string, boolean>;
  strict?: boolean;
  recent?: RecentEntry[];
}

/** 渲染前就读好的设置:文件内容 + 清理开关的基线(引擎那侧的口径)。 */
export interface InitialSettings {
  file: SettingsFile;
  baseline: Record<string, boolean>;
}

export const APP_VERSION = "v0.5.4";
export const MAX_RECENT = 8;

/**
 * 旧版把设置存在 localStorage 里(%LOCALAPPDATA%\com.wandersu.pdf2epub\)。
 * 现在设置与书库都落在 `%LOCALAPPDATA%\pdf2epub\`,下面这些键**只用于一次性迁移**,
 * 迁移成功后即删除 —— 留着它们会让「设置到底存在哪」有两个答案。
 */
export const LEGACY_KEYS = {
  theme: "pdf2epub.dark",
  lang: "pdf2epub.lang",
  backend: "pdf2epub.backend",
  outputDir: "pdf2epub.outputDir",
  cliPath: "pdf2epub.cliPath",
  clean: "pdf2epub.clean",
  strict: "pdf2epub.strict",
  recent: "pdf2epub.recent",
} as const;

//: 清理项开关顺序必须与 CLI 的 cleaner.CLEAN_KEYS 一致
export const CLEAN_KEYS = [
  "page_numbers", "running_heads", "front_matter", "join_lines", "ocr_spaces", "cjk_spaces",
  "dup_headings", "headings", "bold", "images",
] as const;
export type CleanKey = (typeof CLEAN_KEYS)[number];
/**
 * 以 key 为索引存开关,不用 boolean[]:
 * 数组靠下标与 Python 侧对齐,后端**顺序一变**(项数不变)长度校验照样通过,
 * 开关就会静默错位 —— 用户以为关掉了「删除页码」,实际关掉的是「校验图片引用」。
 */
export type CleanOpts = Record<CleanKey, boolean>;

export const CLEAN_DEFAULTS: CleanOpts = {
  page_numbers: true,
  running_heads: true,
  front_matter: true,
  join_lines: true,
  ocr_spaces: true,
  cjk_spaces: true,
  dup_headings: true,
  headings: true,
  bold: false,
  images: true,
};
