import { useState } from "react";
import { errorKind } from "../events";
import { T, type Lang } from "../i18n";
import {
  Rule, TypeBadge, StatusChip, ErrorLine, BackendBadge, StageStepper,
  ComposingRoom, WarningStrip, ChapterHead,
} from "../primitives";
import { KIND_TO_TYPE, backendFromPlan, fmtMonthDay } from "../format";
import type {
  ConsoleLine, FileType, PreflightFile, PreflightReport, QueueFile, RecentEntry,
} from "../types";

// ── CONVERT SCREEN ────────────────────────────────────────────────────────────

export function ConvertScreen({
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
  const skipped = files.filter(f => f.status === "skipped").length;
  const pending = files.filter(f => f.status === "pending").length;
  const total = files.length;
  // 总进度 = 完成率(skip 也算跑完),在跑的行按自身进度补齐。
  // **失败/取消不贡献进度**:否则一批文件里有 2 个失败,大字会永久停在 40%,
  // 看起来像卡死 —— 而右侧明明写着 FAILED 2。
  const settled = done + skipped;
  const running = files.reduce((s, f) => (f.status === "converting" ? s + f.progress / 100 : s), 0);
  const overallPct = total === 0
    ? 0
    : settled === total
      ? 100
      : Math.min(99, Math.round(((settled + running) / total) * 100));

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
      <div className="gutter pt-6 shrink-0">
        <ChapterHead label={`01 ${T[lang].screens[0]}`} state={active > 0 ? t.active(active) : undefined} lang={lang} />
      </div>

      {/* ── 空状态:拖放区 + 类型检测 + 最近文件 ── */}
      {!hasFiles && (
        <div className="flex-1 flex overflow-auto scroll-area gutter py-6">
          <div className="flex flex-1 gap-8 min-h-0">
            <div className="flex-1 flex flex-col items-center min-h-[280px]">
              <div
                className={`relative w-full max-w-[420px] h-[300px] flex flex-col items-center justify-center gap-5 cursor-pointer transition-all duration-[140ms]
                  ${dragging ? "border border-[var(--primary)] bg-[var(--primary)]/4" : "border border-dashed border-[var(--border)] hover:border-[var(--muted-foreground)]"}`}
                onDragOver={e => e.preventDefault()}
                onClick={onPick}
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
                        <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{fmtMonthDay(r.at)}</span>
                        <button onClick={() => onPreflightRecent(r.path)} className="opacity-55 group-hover:opacity-100 focus-visible:opacity-100 font-mono text-[10px] text-[var(--primary)] transition-opacity">
                          {t.preflightRun}
                        </button>
                        <button onClick={() => onAddRecent(r.path)} className="opacity-55 group-hover:opacity-100 focus-visible:opacity-100 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-opacity">
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
                      [t.detection.signatures, <span key="s" className="font-mono text-[10px] text-[var(--primary)] border border-[var(--primary)] px-1.5 py-0.5 tabular-nums">{previewPlan.shards}</span>],
                      [t.detection.cloud, <span key="c" className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{previewPlan.ocr_pages}</span>],
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
          <div className="gutter shrink-0">
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
            <div className="gutter shrink-0">
              <div className={`flex items-center gap-3 py-1.5 border-b ${batchNotice.over_quota ? "border-[var(--danger)]" : "border-[var(--border)]"}`}>
                <span className={`font-mono text-[10px] tracking-[0.1em] uppercase ${batchNotice.over_quota ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]"}`}>{t.preflight}</span>
                <span className={`text-[11px] flex-1 ${batchNotice.over_quota ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]"} ${lang === "zh" ? "cjk" : "font-mono"}`}>
                  {batchNotice.over_quota
                    ? t.quota(batchNotice.total_ocr_pages, batchNotice.quota)
                    : t.quotaOk(batchNotice.total_ocr_pages, batchNotice.total_shards)}
                </span>
              </div>
            </div>
          )}

          <div className="gutter pt-4 shrink-0">
            <div className="flex items-end gap-8 py-4 border-b border-[var(--border)]">
              <div className="flex-1">
                <div className="flex items-baseline gap-6 mb-3">
                  {[{ label: "DONE", val: done, color: "var(--ok)" }, { label: "SKIPPED", val: skipped, color: "var(--info)" }, { label: "ACTIVE", val: active, color: "var(--primary)" }, { label: "FAILED", val: failed, color: "var(--danger)" }, { label: "PENDING", val: pending, color: "var(--muted-foreground)" }].map(({ label, val, color }) => (
                    <div key={label} className="flex items-baseline gap-1.5">
                      <span className="font-mono text-[13px] font-medium tabular-nums" style={{ color }}>{val}</span>
                      <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--muted-foreground)]">{label}</span>
                    </div>
                  ))}
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] ml-auto">{total} FILES</span>
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

            <div className="queue-table grid grid-cols-[minmax(0,1fr)_min-content_minmax(56px,72px)_minmax(48px,64px)_minmax(0,96px)_minmax(0,108px)_72px] gap-3 py-1.5 border-b border-[var(--border)]">
              {["", t.colFile, t.colSize, t.colPages, t.colBackend, t.colStatus, t.colAction].map((h, i) => (
                <span key={i} className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono tracking-[0.1em] uppercase"} ${i >= 2 && i <= 3 ? "text-right" : ""} ${i === 1 ? "head-type" : ""} ${i === 4 ? "head-backend" : ""} ${i === 5 ? "whitespace-nowrap" : ""}`}>{h}</span>
              ))}
            </div>
          </div>

          <div className="flex-1 overflow-auto scroll-area gutter pb-2">
            {files.map((file, fi) => {
              const isExpanded = expandedRows.has(file.id);
              const isSelected = selected.has(file.id);
              return (
                <div key={file.id} className={`row-enter ${isSelected ? "bg-[var(--primary)]/4" : ""}`} style={{ animationDelay: `${Math.min(fi, 8) * 24}ms` }}>
                  <div
                    className="queue-table grid grid-cols-[minmax(0,1fr)_min-content_minmax(56px,72px)_minmax(48px,64px)_minmax(0,96px)_minmax(0,108px)_72px] gap-3 py-2 items-center hover:bg-[var(--secondary)] transition-colors cursor-pointer border-b border-[var(--border)]"
                    onClick={() => toggleRow(file.id)}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <button
                        onClick={e => { e.stopPropagation(); toggleSel(file.id); }}
                        aria-label={lang === "zh" ? "选择" : "Select"}
                        aria-pressed={isSelected}
                        className={`w-2.5 h-2.5 border shrink-0 transition-colors ${isSelected ? "bg-[var(--primary)] border-[var(--primary)]" : "border-[var(--border)] hover:border-[var(--foreground)]"}`}
                      />
                      {/* 展开箭头是唯一的键盘入口:行本身是 div,只能鼠标点 */}
                      <button
                        onClick={e => { e.stopPropagation(); toggleRow(file.id); }}
                        aria-expanded={isExpanded}
                        aria-label={lang === "zh" ? "展开详情" : "Toggle details"}
                        className="shrink-0 p-0.5 -m-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--ring)]"
                      >
                        <svg width="6" height="6" viewBox="0 0 6 6" className={`transition-transform ${isExpanded ? "rotate-90" : ""}`} fill="none">
                          <path d="M1 1 L5 3 L1 5" stroke="currentColor" strokeWidth="1" />
                        </svg>
                      </button>
                      <span className="font-mono text-[11px] text-[var(--foreground)] truncate min-w-0 flex-1">{file.name}</span>
                    </div>
                    <span className="col-type inline-flex"><TypeBadge type={file.type} /></span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums text-right">{file.size}</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums text-right">{file.pages || "—"}</span>
                    <span className="col-backend inline-flex"><BackendBadge backend={file.backend} /></span>
                    <span className="whitespace-nowrap"><StatusChip status={file.status} retrying={!!file.retrying} /></span>
                    <div className="flex justify-end">
                      {(file.status === "pending" || file.status === "converting") && (
                        <button onClick={e => { e.stopPropagation(); onCancel(file.id); }} className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--danger)] transition-colors" aria-label={lang === "zh" ? "取消" : "Cancel"}>✕</button>
                      )}
                      {file.status === "failed" && (
                        <button onClick={e => { e.stopPropagation(); onRetry(file.id); }} className={`font-mono text-[10px] tracking-[0.06em] text-[var(--primary)] uppercase ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.retry}</button>
                      )}
                      {(file.status === "done" || file.status === "skipped") && (
                        <button onClick={e => { e.stopPropagation(); onRetry(file.id); }} className={`font-mono text-[10px] tracking-[0.06em] text-[var(--muted-foreground)] uppercase hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.requeue}</button>
                      )}
                    </div>
                  </div>

                  <div className={`border-b border-[var(--border)] overflow-hidden transition-all duration-[180ms] ${isExpanded ? "" : "py-1"}`}>
                    <div className="relative h-px bg-[var(--border)] mx-0 my-1 overflow-visible">
                      <div
                        className={`absolute inset-y-0 left-0 transition-all duration-700 ${file.status === "failed" ? "bg-[var(--danger)]" : file.status === "cancelled" ? "bg-[var(--warn)]" : file.status === "done" ? "bg-[var(--ok)]" : file.status === "skipped" ? "bg-[var(--info)]" : "bg-[var(--primary)]"}`}
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
                          // 失败:分类码 + 引擎原文(不是笼统的 Conversion failed)
                          <ErrorLine kind={errorKind(file.errorCode)} message={file.error} lang={lang} />
                        ) : file.retrying ? (
                          <span className={`text-[10px] text-[var(--warn)] truncate block ${lang === "zh" ? "cjk" : "font-mono"}`}>
                            {t.retrying(file.retrying.attempt)}
                          </span>
                        ) : file.status === "skipped" ? (
                          <span className={`text-[10px] text-[var(--info)] truncate block ${lang === "zh" ? "cjk" : "font-mono"}`}>
                            {t.skippedHint}
                          </span>
                        ) : file.lastLog ? (
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate block selectable">{file.lastLog}</span>
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
