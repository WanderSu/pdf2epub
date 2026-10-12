import { useState } from "react";
import { T, type Lang } from "../i18n";
import { Rule, TypeBadge, BackendBadge, SpineBlock, ChapterHead } from "../primitives";
import { formatSize } from "../format";
import type { Backend, LibraryBook } from "../types";

// ── LIBRARY SCREEN(书目)──────────────────────────────────────────────────────

type SortKey = "date" | "title" | "size";

export function LibraryScreen({ lang, books, loading, outputDir, onRefresh, onOpenFolder, onOpenEpub, onReconvert }: {
  lang: Lang;
  books: LibraryBook[];
  loading: boolean;
  outputDir: string;
  onRefresh: () => void;
  onOpenFolder: () => void;
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
      return b.bytes - a.bytes;
    });

  const totalBytes = books.reduce((s, b) => s + b.bytes, 0);
  const totalLabel = books.length ? formatSize(totalBytes) : "—";
  const newest = books.reduce((m, b) => (b.date > m ? b.date : m), "");
  const countOf = (bd: Backend) => books.filter(b => b.backend === bd).length;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="gutter pt-6 shrink-0">
        <ChapterHead
          label={`02 ${T[lang].screens[1]}`}
          state={loading ? t.loading : t.books(books.length)}
          lang={lang}
        />
      </div>

      <div className="gutter py-3 flex items-center gap-4 shrink-0 border-b border-[var(--border)]">
        <div className="relative flex items-center border-b border-[var(--border)]">
          <span className="font-mono text-[11px] text-[var(--muted-foreground)] mr-2">/</span>
          <input
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
          <div className="grid grid-cols-[44px_1fr_80px_64px_88px_140px] gap-4 pb-1.5 border-b border-[var(--border)]">
            {["", t.colTitle, t.colSize, t.colPages, t.colDate, ""].map((h, i) => (
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
              <div className={`flex items-center gap-3 self-center transition-opacity duration-[140ms] ${hoveredId === book.id ? "opacity-100" : "opacity-55"} focus-within:opacity-100`}>
                <button onClick={() => onOpenEpub(book.epub)} className={`font-mono text-[10px] tracking-[0.06em] uppercase text-[var(--foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.openEpub}</button>
                {book.path && (
                  <>
                    <Rule vertical />
                    <button onClick={() => onReconvert(book.path!)} className={`font-mono text-[10px] tracking-[0.06em] uppercase text-[var(--muted-foreground)] hover:text-[var(--primary)] transition-colors ${lang === "zh" ? "cjk font-mono" : ""}`}>{t.reconvert}</button>
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
