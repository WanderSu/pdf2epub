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

export interface RecentEntry {
  path: string;
  name: string;
  at: number;
}

export const APP_VERSION = "v0.4.3";
export const RECENT_KEY = "pdf2epub.recent";
export const MAX_RECENT = 8;

//: 清理项开关顺序必须与 CLI 的 cleaner.CLEAN_KEYS 一致
export const CLEAN_KEYS = [
  "page_numbers", "running_heads", "join_lines", "ocr_spaces", "cjk_spaces",
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
  join_lines: true,
  ocr_spaces: true,
  cjk_spaces: true,
  dup_headings: true,
  headings: true,
  bold: false,
  images: true,
};
