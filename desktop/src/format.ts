// 纯函数:路径 / 尺寸 / 日期 / localStorage 读写,以及老版 CLI 的日志正则兜底。
// 都不依赖 React —— 单独成文件是为了能被测试,而不是为了「文件多看起来整洁」。
import type { StageNode, StageState } from "./events";
import {
  CLEAN_DEFAULTS, CLEAN_KEYS, LEGACY_KEYS, MAX_RECENT,
  type Backend, type CleanKey, type CleanOpts, type FileType, type RecentEntry,
  type ReviewRow, type RowBackend, type SettingsFile,
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

// ── 复核表的逐行后端 ──────────────────────────────────────────────────────────
//  顺序即界面上的分段顺序。放在最左的 auto 是「不指定」,不是某个后端 ——
//  它让「我没意见」与「我指定本地」成为两件可区分的事。
export const ROW_BACKENDS: readonly RowBackend[] = ["auto", "pymupdf", "mineru", "paddleocr"];

/** 行上的选择 → CLI 的 `--backend` 值。`null` = 不传这个参数。 */
export function rowBackendValue(b: RowBackend | undefined): string | null {
  return !b || b === "auto" ? null : b;
}

/** 云端后端需要 Token;本地提取与「不指定」不需要。 */
export function rowBackendNeedsToken(b: RowBackend): "mineru" | "paddleocr" | null {
  return b === "mineru" || b === "paddleocr" ? b : null;
}

/** 这批文件一共要走多少云端页 —— **按行的当前状态求和**。
 *
 *  不用 preflight 报告里的 `total_ocr_pages`:用户在表里把某行从本地改成
 *  MinerU 之后,那个数是进来时的旧值,底部条形码会与表里的行互相打架,
 *  而它恰好是「这笔要花多少钱」的唯一提示。 */
export function reviewOcrPages(rows: ReviewRow[]): number {
  return rows.reduce((n, r) => n + (r.ocrPages ?? 0), 0);
}

// ── 清理开关的三层取值 ────────────────────────────────────────────────────────
//  内置默认 → `config.yaml` 的 `clean:` 段 → 用户在本机改过的键。
//  第三层只有**用户亲手改过**的项才有值(settings.json 里存的是稀疏映射),
//  所以改了 config.yaml 的项在界面上会立刻反映出来 —— 这正是「桌面端也读
//  config.yaml」的收益,而它只读不写,不会毁掉那份文件的注释、也不怕升级覆盖。

/** 基线取值:config.yaml 里写了就用它,没写才用内置默认。
 *  不能写成 `baseline[k] ?? CLEAN_DEFAULTS[k]` —— 「配置里没有这一项」是
 *  「用户没表态」,不是「关掉」。 */
export function baseValue(baseline: Record<string, boolean>, k: CleanKey): boolean {
  const b = baseline[k];
  return typeof b === "boolean" ? b : CLEAN_DEFAULTS[k];
}

/** 清理开关的最终取值(界面上的复选框状态)。 */
export function mergeCleanOpts(
  baseline: Record<string, boolean>,
  saved: Record<string, boolean> = {},
): CleanOpts {
  const next = {} as CleanOpts;
  for (const k of CLEAN_KEYS) {
    const s = saved[k];
    next[k] = typeof s === "boolean" ? s : baseValue(baseline, k);
  }
  return next;
}

/** 落盘时只写「与基线不同」的键。
 *
 *  与基线相同的项等于「用户没意见」—— 留空才会让 config.yaml 继续管着它。
 *  这也是「恢复默认」能生效的全部原理:把开关拨回基线,保存后这些键就被删掉,
 *  配置文件的管辖自动恢复(而不是写一句「用默认值」把管辖权永久夺走)。
 *
 *  一处**有意的**信息损失:用户显式把某项改成与基线相同的值时,这里认为他没表态,
 *  以后 config.yaml 一变界面就跟着变。要完全保真得改存「用户动过的键名集合」,
 *  为这点差别多一份状态不值。 */
export function diffCleanOpts(
  opts: CleanOpts,
  baseline: Record<string, boolean>,
): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const k of CLEAN_KEYS) {
    if (opts[k] !== baseValue(baseline, k)) out[k] = opts[k];
  }
  return out;
}

// ── 旧版 localStorage 的一次性迁移 ────────────────────────────────────────────

/** 把旧版 localStorage 里的设置读成一份 settings.json;没有任何旧值时返回 null。 */
export function readLegacySettings(): SettingsFile | null {
  const file: SettingsFile = {};
  const dark = localStorage.getItem(LEGACY_KEYS.theme);
  if (dark === "1" || dark === "0") file.theme = dark === "1" ? "dark" : "light";
  const lang = localStorage.getItem(LEGACY_KEYS.lang);
  if (lang === "en" || lang === "zh") file.lang = lang;
  const backend = localStorage.getItem(LEGACY_KEYS.backend);
  if (backend === "auto" || backend === "mineru" || backend === "paddleocr") file.backend = backend;
  const outDir = localStorage.getItem(LEGACY_KEYS.outputDir);
  if (outDir) file.output_dir = outDir;
  const cli = localStorage.getItem(LEGACY_KEYS.cliPath);
  if (cli) file.cli_path = cli;
  const strict = localStorage.getItem(LEGACY_KEYS.strict);
  if (strict === "1" || strict === "0") file.strict = strict === "1";
  const clean = readLegacyClean();
  if (clean) file.clean = clean;
  const recent = readLegacyRecent();
  if (recent.length > 0) file.recent = recent;
  return Object.keys(file).length > 0 ? file : null;
}

/** 旧版把九个开关**全量**存下来,所以只能反推「用户可能改过哪些」:
 *  与内置默认值相同的项算没改过。旧版那种全量存法最大的毛病就是分不清这两者,
 *  迁移时按这个口径收窄,搬过来的就是稀疏映射。 */
function readLegacyClean(): Record<string, boolean> | undefined {
  const saved: Record<string, boolean> = {};
  const keep = (k: CleanKey, v: unknown) => {
    if (typeof v === "boolean" && v !== CLEAN_DEFAULTS[k]) saved[k] = v;
  };
  try {
    const raw = JSON.parse(localStorage.getItem(LEGACY_KEYS.clean) || "null");
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      for (const k of CLEAN_KEYS) keep(k, (raw as Record<string, unknown>)[k]);
    } else if (Array.isArray(raw)) {
      // 更旧的格式(裸数组,按下标对齐)
      CLEAN_KEYS.forEach((k, i) => keep(k, raw[i]));
    }
  } catch { /* 坏数据当没有 */ }
  return Object.keys(saved).length > 0 ? saved : undefined;
}

function readLegacyRecent(): RecentEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem(LEGACY_KEYS.recent) || "[]");
    if (Array.isArray(raw)) return raw.filter(r => r && typeof r.path === "string").slice(0, MAX_RECENT);
  } catch { /* 坏数据当没有 */ }
  return [];
}

/** 迁移成功后清掉旧键:留着会让「设置存在哪」有两个答案。 */
export function clearLegacySettings(): void {
  for (const key of Object.values(LEGACY_KEYS)) localStorage.removeItem(key);
}
