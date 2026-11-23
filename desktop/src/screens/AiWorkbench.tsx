import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { T, type Lang } from "../i18n";
import { Rule } from "../primitives";
import type { AiChapter, AiPreview, AiProgress, AiSettings, LibraryBook } from "../types";

/**
 * AI 校对工作台：左列是书籍信息 + 章节清单，右列是选中章节的全文。
 * 逐章结果通过 `ai-progress` 事件实时落进左列，右列只读预览，确认后才写新文件。
 */
const STATUS_COLOR: Record<string, string> = {
  changed: "var(--ok)",
  unchanged: "var(--muted-foreground)",
  failed: "var(--danger)",
  skipped: "var(--muted-foreground)",
  running: "var(--primary)",
  pending: "var(--muted-foreground)",
};

export function AiWorkbench({
  lang, book, aiSettings, onClose, onSaved,
}: {
  lang: Lang;
  book: LibraryBook;
  aiSettings: AiSettings;
  onClose: () => void;
  /** 另存成功后刷新书目 */
  onSaved: () => void;
}) {
  const t = T[lang].library;
  const [preview, setPreview] = useState<AiPreview | null>(null);
  /** 运行中的实时清单：事件只带状态与字数，不带全文 */
  const [live, setLive] = useState<AiChapter[]>([]);
  const [total, setTotal] = useState(0);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [side, setSide] = useState<"after" | "before">("after");

  useEffect(() => {
    const un = listen<AiProgress>("ai-progress", e => {
      const p = e.payload;
      if (p.phase === "start") {
        setTotal(p.total);
        setLive([]);
        return;
      }
      if (!p.path) return;
      setLive(prev => {
        const next = prev.filter(c => c.path !== p.path);
        next.push({
          path: p.path!, title: p.title ?? p.path!, status: p.status ?? "running",
          note: p.note ?? null, edits: p.edits ?? 0, missed: 0,
          before_chars: p.before_chars ?? 0, after_chars: p.after_chars ?? 0,
          before_text: "", after_text: "",
        });
        return next;
      });
    });
    return () => { void un.then(f => f()); };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const chapters = preview ? preview.chapters : live;
  const current = useMemo(
    () => chapters.find(c => c.path === selected) ?? chapters.find(c => c.status === "changed") ?? chapters[0] ?? null,
    [chapters, selected],
  );
  const done = chapters.filter(c => c.status !== "running" && c.status !== "pending").length;
  const changed = preview ? preview.changed : chapters.filter(c => c.status === "changed").length;

  const start = () => {
    setRunning(true);
    setError(null);
    setSaved(null);
    setPreview(null);
    setLive([]);
    setSelected(null);
    setTotal(0);
    void invoke<AiPreview>("ai_optimize", {
      epub: book.epub, apiUrl: aiSettings.api_url, model: aiSettings.model,
    })
      .then(res => {
        setPreview(res);
        const first = res.chapters.find(c => c.status === "changed") ?? res.chapters[0] ?? null;
        setSelected(first?.path ?? null);
        setSide("after");
      })
      .catch(e => setError(String(e)))
      .finally(() => setRunning(false));
  };

  const apply = () => {
    if (!preview) return;
    setSaving(true);
    setError(null);
    void invoke<string>("ai_apply_preview", { previewId: preview.id })
      .then(path => { setSaved(path); onSaved(); })
      .catch(e => setError(String(e)))
      .finally(() => setSaving(false));
  };

  const mono = "font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)]";
  const bodyText = current
    ? (side === "after" ? current.after_text : current.before_text)
    : "";

  return (
    <div className="fixed inset-0 z-50 flex flex-col" style={{ background: "var(--background)" }} role="dialog" aria-modal="true">
      {/* ── 顶栏 ── */}
      <header className="gutter flex items-center gap-5 py-4 border-b border-[var(--border)] shrink-0">
        <div className="min-w-0">
          <div className="font-mono text-[10px] tracking-[0.12em] text-[var(--primary)] uppercase">{t.aiTitle}</div>
          <div className="font-serif text-[18px] text-[var(--foreground)] truncate" title={book.epub}>{book.title}</div>
        </div>
        <div className="flex-1" />
        {(running || preview) && (
          <span className="font-mono text-[11px] tabular-nums text-[var(--muted-foreground)]">
            {running ? t.aiProgress(done, total || chapters.length) : t.aiProgress(chapters.length, chapters.length)}
          </span>
        )}
        {!saved && (
          <button
            onClick={start}
            disabled={running || saving}
            className="border border-[var(--primary)] text-[var(--primary)] px-4 py-2 font-mono text-[10px] tracking-[0.08em] uppercase disabled:opacity-40"
          >
            {running ? t.aiGenerating : preview ? t.aiRegenerate : t.aiGenerate}
          </button>
        )}
        {preview && !saved && (
          <button
            onClick={apply}
            disabled={saving || running || preview.changed === 0}
            className="bg-[var(--primary)] text-[var(--primary-foreground)] px-4 py-2 font-mono text-[10px] tracking-[0.08em] uppercase disabled:opacity-40"
          >
            {saving ? t.aiApplying : t.aiApply}
          </button>
        )}
        <button onClick={onClose} className="border border-[var(--border)] px-4 py-2 font-mono text-[10px] tracking-[0.08em] uppercase">
          {saved ? t.aiDone : t.aiCancel}
        </button>
      </header>

      <div className="flex-1 flex overflow-hidden">
        {/* ── 左列：书籍信息 + 章节清单 ── */}
        <aside className="w-[340px] shrink-0 border-r border-[var(--border)] flex flex-col overflow-hidden">
          <div className="pl-[var(--gutter)] pr-5 py-4 border-b border-[var(--border)] space-y-2 shrink-0">
            <div className={mono}>{t.aiFileInfo}</div>
            <div className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : ""}`}>
              {book.author || "—"} · {book.pages || "—"} {lang === "zh" ? "页" : "pp"} · {book.size}
            </div>
            <div className="font-mono text-[10px] text-[var(--muted-foreground)] break-all" title={book.epub}>{book.epub}</div>
            <Rule />
            <div className={mono}>{t.aiModel}</div>
            <div className="font-mono text-[11px] text-[var(--foreground)] break-all">{aiSettings.model}</div>
            <div className={`text-[11px] leading-relaxed text-[var(--muted-foreground)] break-all ${lang === "zh" ? "cjk" : "font-mono"}`}>
              {preview?.context ? t.aiContext(preview.context) : preview ? t.aiNoContext : ""}
            </div>
          </div>

          <div className={`px-5 py-3 border-b border-[var(--border)] shrink-0 text-[12px] leading-relaxed ${lang === "zh" ? "cjk" : ""} ${error ? "text-[var(--danger)]" : "text-[var(--muted-foreground)]"}`}>
            {error ?? (preview ? preview.summary : saved ? t.aiSaved(saved) : t.aiIntro)}
          </div>

          <div className="flex-1 overflow-auto scroll-area">
            <div className={`px-5 pt-4 pb-2 ${mono}`}>{t.aiToc}</div>
            {chapters.length === 0 && (
              <p className="px-5 py-3 text-[12px] text-[var(--muted-foreground)]">{running ? t.aiGenerating : t.aiEmptyToc}</p>
            )}
            {chapters.map((c, i) => {
              const active = current?.path === c.path;
              return (
                <button
                  key={c.path}
                  onClick={() => { setSelected(c.path); if (c.status === "changed") setSide("after"); }}
                  title={c.note ?? c.path}
                  className={`w-full flex items-baseline gap-2 px-5 py-2 text-left transition-colors
                    ${active ? "bg-[var(--secondary)]" : "hover:bg-[var(--secondary)]"}`}
                >
                  <span className="font-mono text-[10px] tabular-nums text-[var(--muted-foreground)] w-5 shrink-0">{i + 1}</span>
                  <span className={`flex-1 min-w-0 truncate text-[12px] ${active ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"} ${lang === "zh" ? "cjk" : ""}`}>
                    {c.title}
                  </span>
                  {c.status === "changed" && c.edits > 0 && (
                    <span className="font-mono text-[9px] tabular-nums text-[var(--muted-foreground)]">{t.aiEdits(c.edits)}</span>
                  )}
                  <span className="font-mono text-[9px] tracking-[0.06em] shrink-0" style={{ color: STATUS_COLOR[c.status] }}>
                    {t.aiStatus[c.status]}
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        {/* ── 右列：全文预览 ── */}
        <section className="flex-1 flex flex-col overflow-hidden">
          <div className="flex items-center gap-4 px-6 py-3 border-b border-[var(--border)] shrink-0">
            <div className="min-w-0 flex-1">
              <div className="font-serif text-[15px] text-[var(--foreground)] truncate">{current?.title ?? "—"}</div>
              <div className="font-mono text-[10px] text-[var(--muted-foreground)] break-all">{current?.path ?? ""}</div>
            </div>
            {current && (
              <div className="flex border border-[var(--border)] shrink-0">
                {(["before", "after"] as const).map((k, i) => {
                  const chars = k === "after" ? current.after_chars : current.before_chars;
                  return (
                    <button
                      key={k}
                      onClick={() => setSide(k)}
                      className={`px-3 py-1.5 font-mono text-[10px] tracking-[0.06em] tabular-nums transition-colors ${i > 0 ? "border-l border-[var(--border)]" : ""}
                        ${side === k ? "text-[var(--primary)] border-b-2 border-b-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
                    >
                      {k === "after" ? t.aiSideAfter : t.aiSideBefore} {chars}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="flex-1 overflow-auto scroll-area px-6 py-6">
            {!current && (
              <p className={`text-[13px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : ""}`}>{t.aiPickChapter}</p>
            )}
            {current && !bodyText && (
              <p className={`text-[13px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : ""}`}>
                {current.status === "running" ? t.aiGenerating : current.note ?? t.aiPickChapter}
              </p>
            )}
            {bodyText && (
              <article className={`mx-auto max-w-[44rem] whitespace-pre-wrap break-words font-serif text-[14px] leading-[1.9] text-[var(--foreground)] ${lang === "zh" ? "cjk" : ""}`}>
                {bodyText}
              </article>
            )}
          </div>

          <footer className="px-6 py-3 border-t border-[var(--border)] shrink-0 flex items-center gap-4">
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">
              {changed} / {chapters.length}
            </span>
            {current?.note && (
              <span className={`text-[11px] text-[var(--warn)] truncate ${lang === "zh" ? "cjk" : ""}`} title={current.note}>{current.note}</span>
            )}
            {saved && <span className="font-mono text-[10px] text-[var(--ok)] break-all ml-auto">{t.aiSaved(saved)}</span>}
          </footer>
        </section>
      </div>
    </div>
  );
}
