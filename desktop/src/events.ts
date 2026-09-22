/**
 * 引擎事件流(`--json-events`)的消费者:解析 + 归约。
 *
 * **为什么**:桌面端原先只能从 CLI 的**中文日志**里猜状态(几个正则),文案一改就
 * 静默失效;进度百分比也只能按「日志行到达 +5」估算,永远到不了 95%。引擎现在把
 * 阶段事件写成 **JSON Lines**(stdout),这里把它归约成队列行需要的状态:
 * 阶段、真实进度、页数、后端、分帖数(signatures)、校验结果。
 *
 * 保持纯函数、不依赖 React/DOM —— 便于用 node 直接跑断言(见 scripts/verify-events.mjs),
 * GUI 之外也能验证「解析对了」。
 *
 * 事件表见 `src/events.py`;`hello` 事件带阶段权重,前端不另写一份权重(会分叉)。
 */

export type StageKey = "DETECT" | "EXTRACT" | "CLEAN" | "BUILD" | "VERIFY";
export type StageState = "pending" | "active" | "done" | "failed";
export interface StageNode { key: StageKey; state: StageState; isOcr?: boolean }
export interface Signatures { current: number; total: number }
export type FileKind = "TXT" | "SCN" | "HYB" | "MD";

export const STAGE_KEYS: StageKey[] = ["DETECT", "EXTRACT", "CLEAN", "BUILD", "VERIFY"];

/** 引擎 `hello` 未到达时的兜底权重(与 `src/events.py` 的 STAGES 对应)。 */
export const FALLBACK_WEIGHTS: Record<StageKey, number> = {
  DETECT: 0.05, EXTRACT: 0.6, CLEAN: 0.15, BUILD: 0.15, VERIFY: 0.05,
};

const ENGINE_STAGE: Record<string, StageKey> = {
  detect: "DETECT", extract: "EXTRACT", clean: "CLEAN", build: "BUILD", verify: "VERIFY",
};
const BACKEND_LABEL: Record<string, string> = {
  pymupdf: "Local", markdown: "Local", mineru: "MinerU", paddleocr: "PaddleOCR",
};
const KIND_LABEL: Record<string, FileKind> = {
  text: "TXT", scanned: "SCN", hybrid: "HYB", markdown: "MD",
};

export interface EngineEvent { event: string;[k: string]: unknown }

/** 一条队列行的引擎侧状态(由事件流驱动)。 */
export interface EventState {
  stages: StageNode[];
  weights: Partial<Record<StageKey, number>>;
  active: StageKey | null;
  detail?: { current: number; total: number };
  kind?: FileKind;
  pages?: number;
  backend?: string;
  signatures?: Signatures;
  verify?: { errors: number; warnings: number; message: string };
  warning?: boolean;
  error?: string;
  done: boolean;
  /** 已展示过的最高进度:进度条**只许前进**,否则用户会以为回滚了 */
  peak?: number;
}

export const freshStages = (): StageNode[] =>
  STAGE_KEYS.map(key => ({ key, state: "pending" as StageState }));

export const initialEventState = (): EventState => ({
  stages: freshStages(), weights: {}, active: null, done: false,
});

/** 严格解析:只有「JSON 对象且带 event 字段」才算事件,其余返回 undefined(交给 legacy 兜底)。 */
export function parseEventLine(line: string): EngineEvent | undefined {
  const text = line.trim();
  if (!text.startsWith("{")) return undefined;
  try {
    const obj = JSON.parse(text) as Record<string, unknown>;
    if (obj && typeof obj === "object" && typeof obj.event === "string") {
      return obj as EngineEvent;
    }
  } catch {
    /* 非 JSON 行 */
  }
  return undefined;
}

const num = (v: unknown, fallback = 0): number =>
  typeof v === "number" && Number.isFinite(v) ? v : fallback;
const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function weightOf(st: EventState, key: StageKey): number {
  const w = st.weights[key];
  return typeof w === "number" ? w : FALLBACK_WEIGHTS[key];
}

/**
 * 真实进度:已完成阶段权重之和 + 当前阶段权重 × 阶段内完成比。
 *
 * 阶段刚开始、还没有任何 `progress` 事件时给一个小幅猜测值(推进感),但因为
 * 猜测值可能高于第一段真实进度(例如 8 分片书的 1/8),展示值一律取**历史峰值**:
 * 进度条只许前进 —— 倒退会让人以为出了错。
 */
const ACTIVE_GUESS = 0.25;

function rawProgress(st: EventState): number {
  let p = 0;
  for (const node of st.stages) {
    const w = weightOf(st, node.key);
    if (node.state === "done") {
      p += w;
    } else if (node.state === "active") {
      const d = st.detail;
      p += w * (d && d.total > 0 ? Math.min(1, d.current / d.total) : ACTIVE_GUESS);
    }
  }
  return p;
}

export function progressOf(st: EventState): number {
  if (st.done) return 100;
  const p = Math.max(st.peak ?? 0, rawProgress(st));
  return Math.max(0, Math.min(99, Math.round(p * 100)));
}

function markStages(st: EventState, key: StageKey, state: StageState, isOcr?: boolean): EventState {
  return {
    ...st,
    stages: st.stages.map(n => (n.key === key ? { ...n, state, isOcr: isOcr ?? n.isOcr } : n)),
  };
}

/** 归约一条事件;不认识的 event 原样返回(向前兼容新事件)。 */
export function reduceEvent(state: EventState, ev: EngineEvent): EventState {
  const next = applyEvent(state, ev);
  // 记录峰值:进度只许前进(见 progressOf 的说明)
  const peak = Math.max(state.peak ?? 0, rawProgress(next), next.done ? 1 : 0);
  return { ...next, peak };
}

function applyEvent(state: EventState, ev: EngineEvent): EventState {
  let st = state;
  switch (ev.event) {
    case "hello": {
      const weights: Partial<Record<StageKey, number>> = {};
      const list = Array.isArray(ev["stages"]) ? (ev["stages"] as unknown[]) : [];
      for (const item of list) {
        const o = (item ?? {}) as Record<string, unknown>;
        const key = ENGINE_STAGE[str(o["name"])];
        if (key) weights[key] = num(o["weight"]);
      }
      return { ...st, weights };
    }
    case "detect": {
      const kind = KIND_LABEL[str(ev["type"])];
      return { ...st, kind: kind ?? st.kind, pages: num(ev["pages"], st.pages ?? 0) };
    }
    case "plan": {
      const backend = str(ev["backend"]);
      const hit = Object.keys(BACKEND_LABEL).find(k => backend.includes(k));
      const shards = num(ev["shards"]);
      return {
        ...st,
        backend: hit ? BACKEND_LABEL[hit] : st.backend,
        pages: num(ev["pages"], st.pages ?? 0),
        signatures: shards > 1 ? { current: 0, total: shards } : st.signatures,
      };
    }
    case "stage": {
      const key = ENGINE_STAGE[str(ev["name"])];
      if (!key) return st;
      const state = str(ev["state"]);
      const isOcr = /mineru|paddle/i.test(str(ev["backend"]));
      if (state === "start") {
        st = markStages(st, key, "active", isOcr);
        return { ...st, active: key, detail: undefined };
      }
      st = markStages(st, key, "done", isOcr);
      return { ...st, active: st.active === key ? null : st.active, detail: undefined };
    }
    case "progress": {
      const key = ENGINE_STAGE[str(ev["stage"])];
      const current = num(ev["current"]);
      const total = num(ev["total"]);
      if (key && key === st.active) st = { ...st, detail: { current, total } };
      if (st.signatures && current > st.signatures.current) {
        st = { ...st, signatures: { ...st.signatures, current } };
      }
      return st;
    }
    case "shards": {
      const total = num(ev["total"]);
      return total > 1 ? { ...st, signatures: { current: 0, total } } : st;
    }
    case "verify":
      return {
        ...st,
        verify: {
          errors: num(ev["errors"]),
          warnings: num(ev["warnings"]),
          message: str(ev["message"]),
        },
      };
    case "warning":
      return { ...st, warning: true };
    case "error":
      return { ...st, error: str(ev["message"]) || "转换失败" };
    case "skip":
    case "complete": {
      const finished = STAGE_KEYS.reduce<EventState>((acc, key) => markStages(acc, key, "done"), st);
      return { ...finished, active: null, detail: undefined, done: true };
    }
    default:
      return st;
  }
}
