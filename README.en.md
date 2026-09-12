# pdf2epub

> **PDF → EPUB converter** · One shot for digital / scanned PDFs and Markdown, with automatic cleanup and book-grade typesetting

[![Release](https://img.shields.io/github/v/release/WanderSu/pdf2epub?color=B5342A&label=release)](https://github.com/WanderSu/pdf2epub/releases)
[![Stars](https://img.shields.io/github/stars/WanderSu/pdf2epub?color=14110E&label=stars)](https://github.com/WanderSu/pdf2epub)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/WanderSu/pdf2epub/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文](README.md) | **English**

---

## Contents

- [Screenshots](#screenshots)
- [Features](#features)
- [Pipeline](#pipeline)
- [Quick start](#quick-start)
- [Desktop app](#desktop-app)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [FAQ](#faq)
- [Known limitations](#known-limitations)
- [Project layout](#project-layout)
- [Development](#development)
- [License](#license)

---

## Screenshots

| CONVERT (light) | CONVERT (dark) |
|---|---|
| ![convert light](docs/screenshot-convert-light.png) | ![convert dark](docs/screenshot-convert-dark.png) |

| SETTINGS |
|---|
| ![settings](docs/screenshot-settings-light.png) |

The UI is themed as a **letterpress workshop**, built from three materials only — paper, ink and cinnabar.
The conversion pipeline is therefore named after the trade: **crafts** (plate-making → typesetting → proofing → printing),
shards are **signatures** ("signature 2 of 3"), the cleaning switches are a **proofreader's checklist**,
the dry run is a **preflight**, the log panel is the **composing room**, and the library is a **catalogue**.

---

## Features

### Engine

| Feature | What it does |
|---|---|
| 🔍 **Auto detection** | Text layer intact → local extraction · scanned → cloud OCR · hybrid → per-page routing |
| 🧠 **Pseudo-text-layer detection** | Warns when a PDF extracts as garbage and lets *you* decide whether to use OCR (never decides for you) |
| ☁️ **Cloud OCR** | MinerU / PaddleOCR-VL, switchable; MinerU failures fall back to re-rendering pages as images |
| 🪧 **Preflight** | `--dry-run` reports type / pages / planned backend / signature count / the day's cloud-page budget — writes nothing, spends nothing |
| 📚 **Automatic sharding >200 pages** | MinerU caps a task at 200 pages / 200 MB; larger books are submitted as page-ranges, parsed in parallel and merged in order |
| ♻️ **Resume** | Submitted cloud tasks (batch/job id) are persisted; after a timeout or a closed window the next run polls the same task instead of re-uploading or re-spending quota |
| 🧹 **Editorial cleanup** | Page numbers, running heads, broken lines, OCR spaces, CJK spaces, duplicate/empty headings and level jumps, emphasis fonts → bold. Nine switches, each independent |
| 📇 **Metadata** | `Title - Author` filenames become `dc:title` / `dc:creator` |
| 📦 **Batch mode** | Exponential-backoff retries, skip completed, resume, per-file failure isolation |
| 🎨 **Book-grade typesetting** | Bundled `config/book.css`: CJK serif body, first-line indent, heading hierarchy, formula/table/image protection |
| ✅ **Structural verification** | Every conversion verifies the EPUB (images / math / footnotes / TOC / internal links / CSS); `--strict` turns any error into a failed file |

### Desktop app

| Feature | What it does |
|---|---|
| 🗂 **Three workspaces** | `01 CONVERT` / `02 LIBRARY` / `03 SETTINGS` — table-of-contents navigation, no separate import screen (the drop zone *is* the empty state of CONVERT) |
| 🪧 **Preflight panel** | Dropping a file immediately shows detection (type / pages / signatures / cloud pages) and writes it back onto the queue row |
| 🔧 **Craft progress** | Plate-making → typesetting → proofing → printing; the extract node reads `OCR` when cloud OCR runs; signatures show "2 of 3" |
| 🗒 **Composing room** | Live CLI log with timestamps and levels, collapsible, TAIL / PAUSE |
| ⛔ **Real cancellation** | Cancel runs `taskkill /T` on the whole CLI process tree — no more silently finishing the job and burning cloud quota |
| 📖 **Catalogue** | Persisted in `library.json`; title/author come from the EPUB metadata; type / backend / pages are written back on conversion and survive restarts |
| ⚙️ **Settings** | OCR backend, credentials, output directory, converter path, environment check, the nine cleanup switches, strict validation, theme, language. Unsaved changes are counted and DISCARD really rolls back |
| 🌐 **Bilingual + two themes** | English / 简体中文 (technical tokens stay English), light and dark |

---

## Pipeline

```text
Digital PDF ──► PyMuPDF4LLM (local) ─┐
Scanned PDF ──► Cloud OCR ───────────┼──► Markdown cleanup ──► Pandoc + CSS ──► EPUB
MinerU output ─► full.md + images ───┤      (page numbers / broken lines / spaces / bold)
Existing Markdown ───────────────────┘
```

---

## Quick start

### 1. Desktop app (recommended)

Download `pdf2epub-v0.3.0-win-x64.zip` from [Releases](https://github.com/WanderSu/pdf2epub/releases), extract anywhere, double-click `pdf2epub.exe`.

> 💡 The zip bundles the engine (`cli.exe`, self-contained Python — no Python install needed). Keep `pdf2epub.exe`, `cli.exe` and `config/` in the same folder; no need to place it in a project root.

**First run, two steps:**

1. Install [Pandoc](https://pandoc.org/installing.html) (the EPUB engine, required): `winget install pandoc`
2. Create `apikey.json` in the extracted folder (cloud OCR credentials — see *Preparing credentials*), or paste the tokens into **03 SETTINGS → OCR & CREDENTIALS** and save

Workspaces: `01 CONVERT` (drop + queue + composing room) · `02 LIBRARY` (results) · `03 SETTINGS`.

### 2. CLI

**Requirements:** Python 3.12 · [uv](https://docs.astral.sh/uv/) · [Pandoc](https://pandoc.org/installing.html) (≥3.0, on PATH)

```bash
git clone https://github.com/WanderSu/pdf2epub.git
cd pdf2epub
uv sync    # creates .venv and installs the ebook-converter command
```

**Preparing credentials** (cloud OCR only) — create `apikey.json` in the project root (git-ignored):

```json
{
    "MinerU": "your MinerU token",
    "PaddleOCR-VL": "your PaddleOCR token"
}
```

Environment variables `MINERU_API_TOKEN` / `PADDLEOCR_TOKEN` also work and take precedence.

**Converting**:

```bash
# one file (type auto-detected)
.venv/Scripts/ebook-converter.exe "my-book.pdf" -o output

# a whole directory
.venv/Scripts/ebook-converter.exe books/ -o output

# existing Markdown (including MinerU's full.md, as "<title>.md + images/")
.venv/Scripts/ebook-converter.exe "my-book.md" -o output

# preflight only (writes nothing, spends nothing)
.venv/Scripts/ebook-converter.exe "scanned.pdf" --dry-run

# force a backend
.venv/Scripts/ebook-converter.exe "scanned.pdf" -o output --backend mineru

# disable individual cleanup steps (comma separated; all on by default, bold off)
.venv/Scripts/ebook-converter.exe "my-book.pdf" -o output --clean-disable join_lines,cjk_spaces
```

**Output:** `output/<title>.epub` per book; intermediates stay in `work/<title>/book.md + images/` (edit them and rebuild if you like).

> ⚠️ For a suspected **pseudo-text layer**, single-file conversion asks interactively whether to switch to OCR; batch mode never blocks, it only logs a warning.

---

## Desktop app

Built with **Tauri 2 + React 19 + Tailwind v4**; the UI comes from a Figma design (letterpress theme: paper / ink / cinnabar).

### Build from source

```bash
cd desktop
npm install
npm run tauri dev                    # dev mode (vite HMR)
npm run tauri build -- --no-bundle   # release exe (produces desktop.exe)
```

Requirements: Node.js ≥ 20 · Rust (`stable-x86_64-pc-windows-msvc`) · Visual Studio Build Tools (C++ workload)

One-shot packaging (version guard: four files must agree):

```bash
uv run python scripts/build_release.py 0.3.0            # engine + shell + package + self-check
uv run python scripts/build_release.py 0.3.0 --skip-build --no-check   # repackage existing artifacts
```

---

## CLI reference

```
ebook-converter <file-or-dir>... [-o output-dir] [options]
```

| Option | Description |
|---|---|
| `--backend {auto,pymupdf,mineru,paddleocr}` | Force a backend (default `auto`) |
| `-o, --output DIR` | EPUB output directory (default `output/`) |
| `--retries N` | Retries per file with exponential backoff (default 2) |
| `--force` | Ignore the "already done" state |
| `--clean-disable LIST` | Disable cleanup steps (comma separated): `page_numbers,running_heads,join_lines,ocr_spaces,cjk_spaces,dup_headings,headings,bold,images` |
| `--strict` | Verify the produced EPUB and fail the file on any structural error (default: warn only) |
| `--dry-run` | Preflight: type / pages / planned backend / signatures / daily quota, writes nothing |
| `--dry-run --json` | Same, as JSON (this is what the desktop preflight panel consumes) |
| `--no-resume` | Do not reuse a submitted cloud OCR task |
| `--no-log` | Do not write a log file |
| `--verbose` | DEBUG logging |

---

## Configuration

`config/config.yaml`:

```yaml
ocr_backend: mineru          # default OCR backend: mineru / paddleocr
mineru:
  max_pages_per_task: 200    # MinerU per-task page cap; larger books are sharded via page_ranges
  resume: true               # reuse a submitted cloud task after an interruption
clean:                       # cleanup switches (shared with the desktop UI and --clean-disable)
  page_numbers: true         # strip standalone page-number lines
  running_heads: true        # drop short lines repeated across pages
  join_lines: true           # re-flow paragraphs split across pages
  ocr_spaces: true           # merge spaces OCR inserted inside Latin words on CJK lines
  cjk_spaces: true           # remove stray spaces between CJK characters
  dup_headings: true         # keep one of adjacent duplicate headings
  headings: true             # drop empty headings, flatten level jumps
  bold: false                # emphasis fonts → ** bold
  images: true               # verify image references
pymupdf:
  write_images: true
  bold_fonts: [...]          # emphasis font families (KaiTi / STZhongsong …) → ** bold
```

`config/book.css` — the global EPUB stylesheet (CJK serif body, first-line indent, heading hierarchy, formula/table/image protection). Edit freely.

---

## FAQ

<details>
<summary><b>Where is the API key stored?</b></summary>

In `apikey.json` next to the shell exe. So a portable build and a dev-mode build keep **two different credential files** — switching exes can look like "my key disappeared" when it is simply reading the other file. You can paste and save keys directly in **03 SETTINGS**.
</details>

<details>
<summary><b>Scanned PDFs are slow / burn tokens?</b></summary>

Cloud OCR bills per page. The desktop **preflight** tells you how many cloud pages a book needs and whether it exceeds the daily budget *before* you start. `--retries 0` avoids wasting quota on retries.
</details>

<details>
<summary><b>It says "text layer corrupted" but I want local extraction?</b></summary>

Pick `[3] continue locally` when prompted, or pass `--backend pymupdf`; in the desktop app use **CONTINUE LOCAL** on the pseudo-text-layer strip. Expect the output to be hard to read.
</details>

<details>
<summary><b>MinerU reports "parsing failed"?</b></summary>

The tool automatically retries after re-rendering the pages as plain images, which usually fixes it. If not, try `--backend paddleocr` or check the file itself.
</details>

<details>
<summary><b>Can it convert books over 200 pages?</b></summary>

Yes. MinerU caps a task at 200 pages, so larger books are sharded automatically (page_ranges), parsed in parallel and merged in order — the log says "自动分片 N 段" and the UI shows it as signature count. Note the 1,000 cloud pages/day budget; preflight warns you up front.
</details>

<details>
<summary><b>Output has garbage / page numbers / broken lines?</b></summary>

`src/markdown/cleaner.py` does the cleanup and all nine steps can be toggled: **03 SETTINGS → cleaning pipeline** in the app, or `--clean-disable join_lines` on the CLI. If a rule hurts your book, turn that one off and look again.
</details>

<details>
<summary><b>The OCR job timed out or I closed the window — is the quota gone?</b></summary>

No. The batch/job id is persisted (`work/<title>/.ocr_task.json`), so re-running the same file polls the original task instead of re-uploading; the log says "发现未取回的云端任务". Use `--no-resume` to force a fresh submission.
</details>

<details>
<summary><b>How do I know images / formulas actually made it into the EPUB?</b></summary>

Every conversion runs a structural check (package structure, image references, MathML, footnotes, TOC, internal links, CSS) and logs `[verify] N errors N warnings`. To make any error fail the file, enable **strict EPUB validation** in **03 SETTINGS** or pass `--strict`. For a standalone re-check: `uv run python scripts/verify_epub.py output/book.epub --expect-images 2`.
</details>

<details>
<summary><b>Title / author are wrong?</b></summary>

Name files as `Title - Author` (e.g. `Meditations - Marcus Aurelius.pdf`) and the metadata follows. The catalogue shows the metadata read back from the produced EPUB.
</details>

<details>
<summary><b>Is the progress percentage real?</b></summary>

**Partly.** The CLI emits no percentage events, so the bar advances from log lines (capped at 95%) and jumps to 100% on completion; when a book is sharded the signature counter drives it more precisely. The craft nodes (plate-making / typesetting / proofing / printing) and the signature indicator are real state.
</details>

---

## Known limitations

- PyMuPDF4LLM extracts inline formulas as images (not LaTeX); cloud OCR LaTeX becomes MathML
- Two-column PDFs occasionally merge adjacent lines (edge case)
- Page-number stripping and line joining are heuristics; unusual typography can suffer
- After MinerU sharding, tables/paragraphs spanning a segment boundary can be cut
- EPUBCheck is not installed, so EPUB spec compliance is not verified
- Progress percentage is estimated (above); **real cover extraction is not implemented** — catalogue covers are geometric placeholders
- The queue is not persisted across restarts (the recent-files list is); library records created before v0.3.0 lack type/backend/pages until the book is converted again

---

## Project layout

```text
src/
  backends/         # base (abstract) / pymupdf / mineru / paddleocr
  detector/         # PDF type detection (incl. pseudo-text-layer detection)
  markdown/         # cleaner / bold (emphasis fonts)
  epub/             # pandoc wrapper + EPUB structural verification
  dryrun.py         # preflight (--dry-run / --dry-run --json)
  batch.py          # batching (retries / skip / resume)
  cli.py            # ebook-converter entry point
  convert.py        # auto routing (text / scanned / hybrid)
  paths.py          # paths and credentials (apikey.json)
config/             # config.yaml + book.css
desktop/            # Tauri 2 desktop app (React 19 + Tailwind v4)
  src/App.tsx       # the whole UI (three workspaces + component family)
  src-tauri/src/lib.rs  # IPC: convert_file / preflight / cancel_convert / library / env / apikey
docs/               # UI screenshots
scripts/            # sample generation / e2e tests / EPUB verify / version + packaging
tests/              # pytest (detector / cleaner / dryrun / epub verify / paths / version sync)
```

---

## Development

```bash
uv run pytest -q                       # Python tests
cd desktop/src-tauri && cargo test     # Rust tests (IPC / library / credential merge)
cd desktop && npm run build            # frontend type-check + build
```

The roadmap lives in `.hermes/plans/`; design notes are in [IDEA.md](IDEA.md) (Chinese).

---

## License

[MIT](LICENSE) © 2026 WanderSu

> ⚠️ This repository contains no book content — only code and self-generated test samples. Converting copyrighted books is for personal use; do not redistribute the output.
