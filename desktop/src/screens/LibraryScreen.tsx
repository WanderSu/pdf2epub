import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { T, type Lang } from "../i18n";
import {
  Rule, TypeBadge, BackendBadge, SpineBlock, ChapterHead, DeleteDialog, TrashIcon,
} from "../primitives";
import { formatSize } from "../format";
import type { AiSettings, Backend, ConversionReport, LibraryBook } from "../types";
import { AiWorkbench } from "./AiWorkbench";

// ── LIBRARY SCREEN(书目)──────────────────────────────────────────────────────

type SortKey = "date" | "title" | "size";

/** EPUB 内的 cover-image 是最可信的缩略图来源。加载失败（旧书、无封面、损坏包）
 * 保留原来的文字书脊，不能因为一张图让书目行消失。 */
function CoverThumb({ book }: { book: LibraryBook }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setSrc(null); setFailed(false);
    void invoke<{ data_url: string } | null>("epub_cover", { path: book.epub })
      .then(cover => { if (alive) setSrc(cover?.data_url ?? null); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [book.epub]);
  if (!src || failed) return <SpineBlock title={book.title} />;
  return (
    <div className="w-11 h-[58px] border border-[var(--border)] bg-[var(--secondary)] overflow-hidden flex items-center justify-center" title={book.title}>
      <img src={src} alt={`${book.title} cover`} className="w-full h-full object-contain" onError={() => setFailed(true)} />
    </div>
  );
}

/** 转换报告:引擎在 `work/<书名>/report.json` 落的事实清单(页数/校验/清理/覆盖)。 */
function ReportPanel({ lang, book, report, error, loading, onClose }: {
  lang: Lang;
  book: LibraryBook;
  report: ConversionReport | null;
  error: string | null;
  loading: boolean;
  onClose: () => void;
}) {
  const t = T[lang].library;
  const cjk = lang === "zh" ? "cjk" : "font-mono";
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const verify = report?.verify ?? {};
  const clean = report?.clean ?? {};
  const coverage = report?.coverage ?? {};
  const issues = [
    ...(verify.issues ?? []).map(i => `${i.level === "error" ? "✕" : "!"} [${i.code}] ${i.message}`),
    ...(clean.issues ?? []).map(i => `! ${i}`),
  ];
  const rows: [string, string][] = report ? [
    [t.reportPages, String(report.pages ?? "—")],
    [t.reportBackend, report.backend ?? "—"],
    [t.reportSeconds, report.seconds != null ? `${report.seconds}s` : "—"],
    [t.reportCached, report.cached ? t.reportCachedYes : t.reportCachedNo],
    [t.reportVerify, `${verify.errors ?? 0} / ${verify.warnings ?? 0}`],
    [t.reportClean, `${clean.applied ?? 0} / ${clean.suggested ?? 0} / ${clean.failed ?? 0}`],
    [t.reportCoverage, coverage.pages_expected
      ? `${coverage.pages_covered ?? 0} / ${coverage.pages_expected}`
      : "—"],
    [t.reportPageQuality, t.reportPageQualityValue(
      coverage.pages_blank ?? 0, coverage.pages_image_only ?? 0, coverage.pages_short ?? 0)],
  ] : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--foreground)]/20 p-8"
      role="dialog" aria-modal="true" aria-label={t.reportTitle}
      onClick={onClose}
    >
      <div
        className="w-full max-w-[560px] max-h-[80vh] flex flex-col bg-[var(--card)] border border-[var(--border)]"
        onClick={e => e.stopPropagation()}
      >
        <div className="px-8 pt-6 pb-4 border-b border-[var(--border)] shrink-0">
          <div className="flex items-baseline gap-4">
            <span className="font-mono text-[11px] tracking-[0.1em] text-[var(--primary)]">{t.reportTitle}</span>
            <span className={`font-serif text-[18px] leading-tight text-[var(--foreground)] truncate ${lang === "zh" ? "cjk-serif" : ""}`}>{book.title}</span>
          </div>
          <div className={`mt-1 text-[10px] text-[var(--muted-foreground)] break-all selectable ${cjk}`}>{book.epub}</div>
        </div>

        <div className="px-8 py-5 overflow-auto scroll-area">
          {loading && <div className={`text-[12px] text-[var(--muted-foreground)] ${cjk}`}>{t.reportLoading}</div>}
          {!loading && error && (
            <div className={`text-[12px] leading-relaxed text-[var(--muted-foreground)] ${cjk}`}>
              {t.reportMissing}
              <div className="mt-2 text-[10px] opacity-70 break-all selectable">{error}</div>
            </div>
          )}
          {!loading && report && (
            <>
              <div className="space-y-2">
                {rows.map(([label, value]) => (
                  <div key={label} className="flex items-baseline gap-3">
                    <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--muted-foreground)] w-[96px] shrink-0 uppercase">{label}</span>
                    <span className={`text-[12px] text-[var(--foreground)] ${cjk}`}>{value}</span>
                  </div>
                ))}
              </div>
              <div className="mt-5 pt-4 border-t border-[var(--border)]">
                <div className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-2">{t.reportIssues}</div>
                {issues.length === 0 ? (
                  <div className={`text-[12px] text-[var(--ok)] ${cjk}`}>{t.reportNoIssues}</div>
                ) : (
                  <ul className="space-y-1.5">
                    {issues.slice(0, 40).map((line, i) => (
                      <li key={i} className={`text-[11px] leading-relaxed break-words ${cjk} ${line.startsWith("✕") ? "text-[var(--danger)]" : "text-[var(--warn)]"}`}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </div>

        <div className="flex items-center justify-end px-8 py-3 border-t border-[var(--border)] bg-[var(--background)] shrink-0">
          <button
            onClick={onClose}
            className={`text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] ${lang === "zh" ? "cjk" : "font-mono tracking-[0.08em] uppercase"}`}
          >
            {t.reportClose}
          </button>
        </div>
      </div>
    </div>
  );
}

export function LibraryScreen({ lang, books, loading, outputDir, aiSettings = { provider: "OpenAI", api_url: "https://api.openai.com/v1", model: "gpt-4.1-mini" }, onRefresh, onOpenFolder, onOpenEpub, onReconvert, onDelete }: {
  lang: Lang;
  books: LibraryBook[];
  loading: boolean;
  outputDir: string;
  aiSettings?: AiSettings;
  onRefresh: () => void;
  onOpenFolder: () => void;
  onOpenEpub: (epub: string) => void;
  onReconvert: (path: string) => void;
  /** 删除一条书目记录(连 EPUB 文件一起删) */
  onDelete: (book: LibraryBook) => void;
}) {
  const t = T[lang].library;
  const [sort, setSort] = useState<SortKey>("date");
  const [filter, setFilter] = useState<Backend | "All">("All");
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  /** 正在等确认删除的书 —— 对话框里由用户决定删不删文件 */
  const [deleteTarget, setDeleteTarget] = useState<LibraryBook | null>(null);
  /** 打开工作台的目标书；生成、预览、另存都在 AiWorkbench 内部完成 */
  const [aiTarget, setAiTarget] = useState<LibraryBook | null>(null);
  /** 正在看转换报告的书 + 报告内容(读不到时给明确提示,不假装成功) */
  const [reportTarget, setReportTarget] = useState<LibraryBook | null>(null);
  const [report, setReport] = useState<ConversionReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  const openReport = (book: LibraryBook) => {
    setReportTarget(book);
    setReport(null);
    setReportError(null);
    setReportLoading(true);
    void invoke<ConversionReport>("conversion_report", { epub: book.epub })
      .then(r => setReport(r))
      .catch(e => setReportError(String(e)))
      .finally(() => setReportLoading(false));
  };
  const searchRef = useRef<HTMLInputElement>(null);

  // 页快捷键「/」由 App 广播(Booklist 自己不该关心键盘),这里只认事件
  useEffect(() => {
    const focusSearch = () => searchRef.current?.focus();
    window.addEventListener("pdf2epub:search", focusSearch);
    return () => window.removeEventListener("pdf2epub:search", focusSearch);
  }, []);

  const filtered = books
    .filter(b => filter === "All" || b.backend === filter)
    .filter(b => !search || b.title.toLowerCase().includes(search.toLowerCase()) || b.author.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      if (sort === "date") return (b.date || "").localeCompare(a.date || "");
      if (sort === "title") return a.title.localeCompare(b.title);
      return b.bytes - a.bytes;
    });

  const totalBytes = books.reduce((s, b) => s + b.bytes, 0);
  const totalLabel = books.length ? formatSize(totalBytes) : "—";
  const newest = books.reduce((m, b) => (b.date > m ? b.date : m), "");
  const countOf = (bd: Backend) => books.filter(b => b.backend === bd).length;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="gutter pt-8 shrink-0">
        <ChapterHead
          label={`02 ${T[lang].screens[1]}`}
          description={t.desc}
          state={loading ? t.loading : t.books(books.length)}
          lang={lang}
        />
      </div>

      <div className="gutter py-3 flex items-center gap-4 shrink-0 border-b border-[var(--border)]">
        <div className="relative flex items-center border-b border-[var(--border)]">
          <span className="font-mono text-[11px] text-[var(--muted-foreground)] mr-2">/</span>
          <input
            ref={searchRef}
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder={t.search}
            spellCheck={false}
            autoComplete="off"
            className={`bg-transparent text-[12px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none w-48 ${lang === "zh" ? "cjk" : "font-mono"}`}
          />
          {search && <span className="font-mono text-[10px] text-[var(--muted-foreground)] ml-2 tabular-nums">{filtered.length}</span>}
        </div>

        <div className="flex border border-[var(--border)]">
          {([["date", t.sortDate], ["title", t.sortTitle], ["size", t.sortSize]] as [SortKey, string][]).map(([k, label], i) => (
            <button
              key={k}
              onClick={() => setSort(k)}
              className={`px-3 py-1 transition-colors ${lang === "zh" ? "cjk text-[11px]" : "font-mono text-[10px] tracking-[0.08em] uppercase"} ${i > 0 ? "border-l border-[var(--border)]" : ""}
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
              className={`px-2 py-1 font-mono text-[10px] tracking-[0.06em] uppercase transition-colors ${i > 0 ? "border-l border-[var(--border)]" : ""}
                ${filter === b ? "text-[var(--primary)] border-b-2 border-b-[var(--primary)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
            >
              {b === "All" ? t.filterAll : b === "PaddleOCR" ? "PADDLE" : b.toUpperCase()}
            </button>
          ))}
        </div>

        <div className="flex-1" />
        <button onClick={onRefresh} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.refresh}</button>
        <Rule vertical />
        <button onClick={onOpenFolder} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.openFolder}</button>
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* 左侧书志汇总栏 */}
        <div className="w-[192px] shrink-0 border-r border-[var(--border)] pl-[var(--gutter)] pr-6 py-6 overflow-auto scroll-area">
          <div className="space-y-4">
            <div>
              <div className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1">{t.totalLabel}</div>
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
                  <span className="font-mono text-[10px] tracking-[0.06em]">{name === "PaddleOCR" ? "PADDLE" : name.toUpperCase()}</span>
                  <span className="font-mono text-[11px] tabular-nums" style={{ color }}>{count}</span>
                </button>
              ))}
            </div>
            <Rule />
            <div>
              <div className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] uppercase mb-1">{t.newest}</div>
              <div className="font-mono text-[10px] text-[var(--foreground)] tabular-nums">{newest ? newest.slice(5) : "—"}</div>
            </div>
          </div>
        </div>

        {/* 书目 */}
        <div className="flex-1 overflow-auto scroll-area pl-8 pr-[var(--gutter)] py-4">
          <div className="library-table grid grid-cols-[44px_minmax(0,1fr)_minmax(0,140px)_80px_64px_88px_168px] gap-4 pb-1.5 border-b border-[var(--border)]">
            {["", t.colTitle, t.colAuthor, t.colSize, t.colPages, t.colDate, ""].map((h, i) => (
              <span key={i} className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono tracking-[0.1em] uppercase"}`}>{h}</span>
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
              {/* 筛完为零要有出路:否则用户以为书丢了,只能自己回想刚才点过什么 */}
              {(search || filter !== "All") && (
                <button
                  onClick={() => { setSearch(""); setFilter("All"); }}
                  className={`mt-2 border border-[var(--primary)] text-[var(--primary)] px-4 py-1.5 text-[11px] hover:bg-[var(--primary)] hover:text-[var(--primary-foreground)] transition-colors ${lang === "zh" ? "cjk" : "font-mono tracking-[0.08em]"}`}
                >
                  {t.clearFilters}
                </button>
              )}
            </div>
          )}

          {filtered.map((book, i) => (
            <div
              key={book.id}
              className="library-table grid grid-cols-[44px_minmax(0,1fr)_minmax(0,140px)_80px_64px_88px_168px] gap-4 py-3 border-b border-[var(--border)] hover:bg-[var(--secondary)] transition-colors row-enter"
              style={{ animationDelay: `${Math.min(i, 8) * 24}ms` }}
              onMouseEnter={() => setHoveredId(book.id)}
              onMouseLeave={() => setHoveredId(null)}
              title={book.epub}
            >
              <CoverThumb book={book} />
              <div className="min-w-0 flex flex-col justify-center gap-1">
                <span className="font-serif text-[14px] text-[var(--foreground)] truncate">{book.title}</span>
                <div className="flex items-center gap-1.5">
                  <TypeBadge type={book.type} />
                  <BackendBadge backend={book.backend} />
                  {/AI优化/.test(book.epub) && (
                    <span
                      className="font-mono text-[9px] tracking-[0.08em] border border-[var(--ok)] text-[var(--ok)] px-1 leading-[14px]"
                      title={lang === "zh" ? "这本书是 AI 优化产物" : "AI-optimized output"}
                    >AI</span>
                  )}
                </div>
              </div>
              <span className={`text-[12px] text-[var(--muted-foreground)] truncate self-center ${lang === "zh" ? "cjk" : ""}`}>{book.author || "—"}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{book.size}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{book.pages || "—"}</span>
              <span className="font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums self-center">{(book.date || "").slice(5)}</span>
              <div className={`flex items-center gap-3 self-center transition-opacity duration-[140ms] ${hoveredId === book.id ? "opacity-100" : "opacity-55"} focus-within:opacity-100`}>
                <button onClick={() => onOpenEpub(book.epub)} className={`font-mono text-[10px] tracking-[0.06em] uppercase text-[var(--foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.openEpub}</button>
                <Rule vertical />
                <button
                  onClick={() => openReport(book)}
                  aria-label={t.report}
                  title={t.report}
                  className="p-0.5 text-[var(--muted-foreground)] hover:text-[var(--primary)] transition-colors"
                >
                  <svg width="11" height="13" viewBox="0 0 11 13" fill="none" stroke="currentColor" strokeWidth="1">
                    <path d="M1 1h6l3 3v8H1z" />
                    <path d="M7 1v3h3" />
                    <path d="M3 7h5M3 9.5h5" />
                  </svg>
                </button>
                <Rule vertical />
                <button onClick={() => setAiTarget(book)} className={`font-mono text-[10px] tracking-[0.06em] uppercase text-[var(--primary)] hover:text-[var(--foreground)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.aiButton}</button>
                {book.path && (
                  <>
                    <Rule vertical />
                    <button onClick={() => onReconvert(book.path!)} className={`font-mono text-[10px] tracking-[0.06em] uppercase text-[var(--muted-foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.reconvert}</button>
                  </>
                )}
                <Rule vertical />
                <button
                  onClick={() => setDeleteTarget(book)}
                  aria-label={lang === "zh" ? "删除" : "Delete"}
                  title={lang === "zh" ? "删除记录或文件" : "Delete record or file"}
                  className="p-0.5 text-[var(--muted-foreground)] hover:text-[var(--danger)] transition-colors"
                >
                  <TrashIcon />
                </button>
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

      {deleteTarget && (
        <DeleteDialog
          lang={lang}
          kind="library"
          subject={deleteTarget.title}
          file={deleteTarget.epub}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const book = deleteTarget;
            setDeleteTarget(null);
            onDelete(book);
          }}
        />
      )}
      {reportTarget && (
        <ReportPanel
          lang={lang}
          book={reportTarget}
          report={report}
          error={reportError}
          loading={reportLoading}
          onClose={() => { setReportTarget(null); setReport(null); setReportError(null); }}
        />
      )}
      {aiTarget && (
        <AiWorkbench
          lang={lang}
          book={aiTarget}
          aiSettings={aiSettings}
          onClose={() => setAiTarget(null)}
          onSaved={onRefresh}
        />
      )}
    </div>
  );
}
