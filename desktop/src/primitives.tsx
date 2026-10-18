// 排版组件:所有屏共用的「铅字」——分隔线、徽标、工序、开关、控制台。
// 只负责长什么样,不含任何业务判断(状态→颜色除外)。
import { useState } from "react";
import { ERROR_KIND_LABEL, type ErrorKind, type Signatures, type StageNode } from "./events";
import { T, type Lang } from "./i18n";
import type { Backend, ConsoleLine, FileStatus, FileType, QueueFile } from "./types";

// ── SHARED PRIMITIVES ─────────────────────────────────────────────────────────

export function Rule({ vertical = false, className = "" }: { vertical?: boolean; className?: string }) {
  return <div className={`bg-[var(--border)] ${vertical ? "w-px self-stretch" : "h-px w-full"} ${className}`} />;
}

export function TypeBadge({ type }: { type?: FileType }) {
  if (!type) {
    return (
      <span className="font-mono text-[10px] leading-none px-1.5 py-0.5 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] shrink-0">
        ?
      </span>
    );
  }
  if (type === "HYB") {
    return (
      <span className="inline-flex overflow-hidden font-mono text-[10px] leading-none shrink-0">
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
  return <span className={`font-mono text-[10px] leading-none px-1.5 py-0.5 border shrink-0 ${s[type]}`}>{type}</span>;
}

// 只在本文件内使用,不导出 —— 导出非组件会让 react-refresh 提示
// (「这个文件不纯,改了就整页重载」),而这些样式表没有跨模块复用价值。
const STATUS_STYLE: Record<FileStatus, string> = {
  pending: "text-[var(--muted-foreground)] border-[var(--border)]",
  converting: "text-[var(--primary)] border-[var(--primary)]",
  done: "text-[var(--ok)] border-[var(--ok)]",
  // 跳过用信息色(不是成功色):产物在,但这次没干活
  skipped: "text-[var(--info)] border-[var(--info)]",
  failed: "text-[var(--danger)] border-[var(--danger)]",
  cancelled: "text-[var(--warn)] border-[var(--warn)]",
};

/** 状态 chip:技术 token,两种语言下都用英文(RETRYING 是可见的中间态)
 *  failed / cancelled 额外带形状标记:主色是朱砂,只在颜色上区分状态不够用 */
export function StatusChip({ status, retrying }: { status: FileStatus; retrying?: boolean }) {
  const isRetrying = retrying === true && status === "converting";
  const label = isRetrying ? "RETRYING" : status === "converting" ? "CONVERTING" : status.toUpperCase();
  const style = isRetrying ? STATUS_STYLE.cancelled : STATUS_STYLE[status];
  const glyph = isRetrying ? "↻" : status === "failed" ? "✕" : status === "cancelled" ? "⊘" : null;
  return (
    <span className={`inline-flex items-center gap-1 font-mono text-[10px] tracking-[0.08em] px-1.5 py-0.5 border ${style}`}>
      {glyph && <span aria-hidden>{glyph}</span>}
      {label}
    </span>
  );
}

/** 错误行:`分类码 + 引擎原文`。分类来自事件里的 code,界面不解析文案。 */
export function ErrorLine({ kind, message, lang }: { kind: ErrorKind; message: string; lang: Lang }) {
  return (
    <span className="flex items-baseline gap-2 min-w-0">
      <span className="font-mono text-[10px] tracking-[0.06em] text-[var(--danger)] border border-[var(--danger)] px-1 py-px shrink-0">
        {ERROR_KIND_LABEL[kind]}
      </span>
      <span className={`text-[11px] text-[var(--danger)] truncate selectable ${lang === "zh" ? "cjk" : "font-mono"}`}>
        {message}
      </span>
    </span>
  );
}

const BACKEND_STYLE: Record<Backend, string> = {
  Local: "border-[var(--border)] text-[var(--muted-foreground)]",
  MinerU: "border-[var(--info)] text-[var(--info)]",
  PaddleOCR: "border-[var(--paddle)] text-[var(--paddle)]",
  Auto: "border-[var(--primary)] text-[var(--primary)]",
};

export function BackendBadge({ backend }: { backend?: Backend }) {
  if (!backend) {
    return (
      <span className="font-mono text-[10px] px-1.5 py-0.5 border border-dashed border-[var(--border)] text-[var(--muted-foreground)] shrink-0">
        —
      </span>
    );
  }
  const label = backend === "PaddleOCR" ? "PADDLE" : backend.toUpperCase();
  return <span className={`font-mono text-[10px] tracking-[0.06em] px-1.5 py-0.5 border shrink-0 ${BACKEND_STYLE[backend]}`}>{label}</span>;
}

/** 工序:制版 → 检字 → 校勘 → 付印(云端 OCR 时标 OCR) */
export function StageStepper({ stages, signatures, lang }: { stages: StageNode[]; signatures?: Signatures; lang: Lang }) {
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
              <span className={`w-4 h-4 border flex items-center justify-center font-mono text-[9px] transition-all ${nodeClass}`}>
                {isDone ? (
                  <svg width="8" height="6" viewBox="0 0 8 6" fill="none" className="check-draw">
                    <path d="M1 3 L3 5 L7 1" stroke="currentColor" strokeWidth="1.2" strokeDasharray="20" strokeDashoffset="0" strokeLinecap="square" />
                  </svg>
                ) : isFailed ? "✕" : nodeLabel}
              </span>
              <span className={`font-mono text-[9px] tracking-[0.04em] ${labelColor}`}>
                {lang === "zh" && !s.isOcr ? (CRAFT_ZH[s.key] ?? s.key) : stageLabel}
              </span>
            </span>
          </span>
        );
      })}
      {signatures && signatures.total > 1 && (
        <span className="ml-2 font-mono text-[10px] px-1.5 py-0.5 border border-[var(--primary)] text-[var(--primary)] shrink-0">
          {lang === "zh"
            ? `第 ${signatures.current} 帖 · 共 ${signatures.total} 帖`
            : `SIGNATURE ${signatures.current} OF ${signatures.total}`}
        </span>
      )}
    </div>
  );
}

/** 书脊色块封面(真实封面提取尚未实现 → 几何占位,不用照片) */
export function SpineBlock({ title }: { title: string }) {
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
export function Switch({ checked, onChange, label, description, disabled, lang }: {
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
      <span className={`font-mono text-[10px] tracking-[0.08em] shrink-0 ${checked ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>
        {checked ? "ON" : "OFF"}
      </span>
      <button
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={e => { e.stopPropagation(); if (!disabled) onChange(!checked); }}
        className={`relative shrink-0 w-[40px] h-[22px] border transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-[var(--ring)] focus:ring-offset-2 focus:ring-offset-[var(--background)]
          ${checked ? "bg-[var(--primary)] border-[var(--primary)]" : "bg-[var(--card)] border-[var(--foreground)]"}
          ${disabled ? "border-dashed" : ""}`}
      >
        <span className={`absolute top-[2px] w-4 h-4 transition-transform duration-200 ${checked ? "translate-x-[20px] bg-white" : "translate-x-[2px] border border-[var(--foreground)] bg-transparent"}`} />
      </button>
    </div>
  );
}

/* 分段控件 — 排序 / 主题 / 语言 / 后端筛选 */
export function Segment<T extends string>({ options, value, onChange }: {
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
export function RadioRow({ label, desc, active, onClick, lang }: {
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
      {active && <span className="font-mono text-[10px] tracking-[0.1em] text-[var(--primary)]">ACTIVE</span>}
    </button>
  );
}

/* 排字记录(控制台):真实 conv://progress 日志 */
export function ComposingRoom({ expanded, setExpanded, lang, lines }: {
  expanded: boolean; setExpanded: (v: boolean) => void; lang: Lang; lines: ConsoleLine[];
}) {
  // 暂停 = 冻结当前快照,而不是「只显示最后 80 行」——
  // 后者日志还在长,视图跟着变,按钮看起来像坏了。
  const [frozen, setFrozen] = useState<ConsoleLine[] | null>(null);
  const paused = frozen !== null;
  const t = T[lang].convert;
  const shown = frozen ?? lines;
  return (
    <div className="border-t border-[var(--border)] shrink-0 flex flex-col overflow-hidden transition-[height] duration-[220ms] ease-[cubic-bezier(0.2,0,0,1)]"
      style={{ height: expanded ? 220 : 28 }}>
      <div
        className="h-7 flex items-center gutter gap-4 cursor-pointer hover:bg-[var(--secondary)] transition-colors shrink-0"
        onClick={() => setExpanded(!expanded)}
      >
        <span className={`font-mono text-[10px] tracking-[0.12em] text-[var(--muted-foreground)]`}>{t.composing}</span>
        <Rule className="flex-1" />
        {expanded && (
          <>
            <button onClick={e => { e.stopPropagation(); setFrozen(paused ? null : [...lines]); }}
              className={`font-mono text-[10px] tracking-[0.1em] ${paused ? "text-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}>
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
        <div className="flex-1 overflow-auto scroll-area composing-bg gutter py-3 space-y-0.5">
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
                <span className="font-mono text-[10px] px-1 border border-[var(--warn)] text-[var(--warn)] shrink-0 self-center" title="状态由旧正则从日志文本猜测(新版 CLI 会发 JSON 事件)">
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
export function WarningStrip({ file, lang, onUseOcr }: { file: QueueFile; lang: Lang; onUseOcr: (id: string) => void }) {
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
      <span className={`text-[11px] text-[var(--warn)] flex-1 selectable ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.msg(file.name)}</span>
      <button onClick={() => { onUseOcr(file.id); setDismissed(true); }}
        className="font-mono text-[10px] tracking-[0.08em] text-[var(--primary)] border border-[var(--primary)] px-2 py-0.5 hover:bg-[var(--primary)] hover:text-[var(--primary-foreground)] transition-colors">
        {t.ocr}
      </button>
      <button onClick={() => setDismissed(true)}
        className="font-mono text-[10px] tracking-[0.08em] text-[var(--warn)] border border-[var(--warn)] px-2 py-0.5 transition-colors">
        {t.local}
      </button>
      <button onClick={() => setDismissed(true)} className="text-[var(--muted-foreground)] hover:text-[var(--foreground)] p-1 transition-colors" aria-label={lang === "zh" ? "关闭" : "Dismiss"}>
        <svg width="7" height="7" viewBox="0 0 7 7" fill="none"><line x1="1" y1="1" x2="6" y2="6" stroke="currentColor" strokeWidth="1" /><line x1="6" y1="1" x2="1" y2="6" stroke="currentColor" strokeWidth="1" /></svg>
      </button>
    </div>
  );
}

/* 章节头 — 通栏 1px 线 + 屏号屏名 + 右侧状态 */
export function ChapterHead({ label, state, lang }: { label: string; state?: string; lang: Lang }) {
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
