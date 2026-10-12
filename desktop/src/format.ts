// 纯函数:路径 / 尺寸 / 日期 / localStorage 读写,以及老版 CLI 的日志正则兜底。
// 都不依赖 React —— 单独成文件是为了能被测试,而不是为了「文件多看起来整洁」。
import type { StageNode, StageState } from "./events";
import {
  CLEAN_DEFAULTS, CLEAN_KEYS, MAX_RECENT, RECENT_KEY,
  type Backend, type CleanOpts, type FileType, type RecentEntry,
} from "./types";

// ── LOG PARSERS(legacy 兜底:仅在事件流缺失时使用)────────────────────────────
//  正常路径下状态全部来自引擎事件流(src/events.ts)。下面这些正则只服务于
//  「新版前端 + 老版 CLI」的组合,命中即标记 legacy(控制台会显示徽标)。

export function parseTypeFromLine(line: string, name: string): FileType | undefined {
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

export function updateStagesFromLine(line: string, current: StageNode[]): StageNode[] {
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

export function parseWarningFromLine(line: string): boolean {
  return /伪文字层|疑似|乱码|garbage/i.test(line);
}

export function normPath(p: string): string {
  return p.replace(/\\/g, "/").toLowerCase();
}

export function formatSize(bytes: number): string {
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 本地日期 YYYY-MM-DD。**不要用 toISOString()**:那是 UTC,东八区 0-8 点会差一天。 */
export function fmtDate(d: Date | number): string {
  const t = new Date(d);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`;
}

/** 本地 MM-DD(最近文件列表用) */
export function fmtMonthDay(d: Date | number): string {
  const t = new Date(d);
  return `${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

export function baseName(p: string): string {
  return p.split(/[\\/]/).pop() || p;
}

/** CLI 计划 → 界面徽标(预检结果落到队列行的映射) */
export const KIND_TO_TYPE: Record<string, FileType> = {
  "pdf-text": "TXT",
  "pdf-scanned": "SCN",
  "pdf-hybrid": "HYB",
  markdown: "MD",
};

export function backendFromPlan(b: string): Backend {
  if (/pymupdf|markdown/.test(b)) return "Local";
  if (/paddle/.test(b)) return "PaddleOCR";
  if (/mineru/.test(b)) return "MinerU";
  return "Auto";
}

export function loadCleanOpts(): CleanOpts {
  const next: CleanOpts = { ...CLEAN_DEFAULTS };
  try {
    const raw = JSON.parse(localStorage.getItem("pdf2epub.clean") || "null");
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      // 新格式:按 key 取值,后端增删/重排都不影响已存的值
      for (const k of CLEAN_KEYS) {
        const v = (raw as Record<string, unknown>)[k];
        if (typeof v === "boolean") next[k] = v;
      }
    } else if (Array.isArray(raw)) {
      // 旧格式(裸数组):按下标迁移一次,之后一律对象
      CLEAN_KEYS.forEach((k, i) => {
        if (typeof raw[i] === "boolean") next[k] = raw[i];
      });
    }
  } catch { /* 忽略坏数据 */ }
  return next;
}

export function loadRecent(): RecentEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    if (Array.isArray(raw)) return raw.filter(r => r && typeof r.path === "string").slice(0, MAX_RECENT);
  } catch { /* 忽略坏数据 */ }
  return [];
}
