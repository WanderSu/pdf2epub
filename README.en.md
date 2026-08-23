# pdf2epub

[![Release](https://img.shields.io/github/v/release/WanderSu/pdf2epub?color=FF4D00&label=release)](https://github.com/WanderSu/pdf2epub/releases)
[![Stars](https://img.shields.io/github/stars/WanderSu/pdf2epub?color=0A0A0A&label=stars)](https://github.com/WanderSu/pdf2epub)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/WanderSu/pdf2epub/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文](README.md) | **English**

A PDF → EPUB converter that handles **digital PDFs**, **scanned PDFs**, **MinerU desktop/cloud output**, and **existing Markdown** — with automatic type detection, cleaning, and well-typeset EPUB output.

Two interfaces: **CLI** and a **desktop app (Tauri + Figma-designed UI)**.

> Design document (Chinese): [`IDEA.md`](IDEA.md)

## What it does

```text
Digital PDF ──► PyMuPDF4LLM (local) ──┐
Scanned PDF ──► Cloud OCR ────────────┼──► Markdown cleaning ──► Pandoc + CSS ──► EPUB
MinerU output ─► full.md + images ────┤     (page numbers / broken lines / spacing / bold)
Existing Markdown ────────────────────┘
```

- **Auto-detects** PDF type: clean text layer → local extraction; pure scans → cloud OCR; mixed → page-level routing
- **Fake-text-layer detection**: PDFs whose text layer is corrupted (renders fine but extracts garbage) trigger a prompt — you decide whether to use OCR
- **Cloud OCR**: MinerU or PaddleOCR-VL (switchable); MinerU failures automatically fall back to "render-to-image and retry"
- **Metadata**: filenames matching `Title - Author` are parsed into EPUB `dc:title` / `dc:creator`
- **Cleaning**: page-number removal, cross-page line joining, CJK spacing fixes, emphasis-font → bold, image-reference validation
- **Batch mode**: exponential-backoff retries, skip completed, resume support, per-file failure isolation

## Desktop app (recommended)

Built with **Tauri 2 + React**, UI from a Figma design (Swiss International style, black/white + signal orange, light/dark themes).

- **Import**: drag & drop / pick PDF, Markdown
- **Queue**: live progress, per-file logs, backend badges (LOCAL / MINERU / PADDLE)
- **Library**: conversion results, reveal in Explorer with one click
- **Settings**: OCR backend (auto / MinerU / PaddleOCR-VL), output directory, CLI path, cleaning options, theme

### Install & run

Grab `pdf2epub.exe` from [Releases](https://github.com/WanderSu/pdf2epub/releases) (portable, no installer). **Put it in the project root** so it auto-locates the converter engine in `.venv` (else set the CLI path manually in Settings).

### Build from source

```bash
cd desktop
npm install
npm run tauri dev                    # dev mode
npm run tauri build -- --no-bundle   # release exe → src-tauri/target/release/
```

Prerequisites: Node.js ≥ 20, Rust (stable-x86_64-pc-windows-msvc), Visual Studio Build Tools (C++ workload).

## CLI install

**Deps**: Python 3.12, [uv](https://docs.astral.sh/uv/), [Pandoc](https://pandoc.org/installing.html) (≥3.0, on PATH).

```bash
git clone <repo-url> pdf2epub
cd pdf2epub
uv sync   # creates .venv and installs the ebook-converter command
```

## Quick start

### 1. Credentials (scanned PDFs / OCR only)

Create `apikey.json` in the project root (gitignored):

```json
{
    "MinerU": "your MinerU token",
    "PaddleOCR-VL": "your PaddleOCR token"
}
```

Or use env vars `MINERU_API_TOKEN` / `PADDLEOCR_TOKEN` (higher priority).

### 2. Convert

```bash
# single file (auto-detect)
.venv/Scripts/ebook-converter.exe "my_book.pdf" -o output

# directory batch
.venv/Scripts/ebook-converter.exe books/ -o output

# existing Markdown (e.g. MinerU desktop full.md, laid out as "bookname.md + images/")
.venv/Scripts/ebook-converter.exe "my_book.md" -o output

# force a backend
.venv/Scripts/ebook-converter.exe "scan.pdf" -o output --backend mineru
.venv/Scripts/ebook-converter.exe "scan.pdf" -o output --backend paddleocr
```

Output: `output/<book>.epub`; intermediates in `work/<book>/book.md + images/` (editable, re-buildable).

### 3. Interactive prompt

When a fake text layer is suspected, single-file conversion asks you:

```
⚠️ 84/155 pages look like corrupted text layers (garbled), local extraction may be unreadable.
Use cloud OCR instead?
  [1] MinerU
  [2] PaddleOCR-VL
  [3] Continue with local extraction
  [0] Cancel
```

Batch mode never interrupts — it logs a warning with the `--backend` hint instead.

## CLI options

```
ebook-converter <files-or-dirs>... [-o outdir] [options]

  --backend {auto,mineru,paddleocr,pymupdf}  force backend (default auto)
  --retries N      retries per file (exponential backoff, default 2)
  --force          ignore "already done" and reconvert
  --no-log         skip log file
  --verbose        DEBUG logs to console
```

## Config

`config/config.yaml` highlights:

```yaml
ocr_backend: mineru          # default OCR backend: mineru / paddleocr
pymupdf:
  write_images: true
  bold_fonts: [...]          # fonts whose text becomes **bold** in output
```

`config/book.css` — EPUB-wide styling (CJK-first, responsive images, formula protection).

## Verify

```bash
.venv/Scripts/python.exe scripts/verify_epub.py output/my_book.epub --expect-images 2
```

Always open the result in a real reader (Apple Books / WeRead / KOReader) for a final check.

## FAQ

- **Slow / token-hungry scans?** OCR is a metered cloud service. `--retries 0` avoids wasted retries.
- **"Corrupted text layer" but want local?** Pick `[3]` at the prompt, or `--backend pymupdf` (result may be unreadable).
- **MinerU "parsing failed"?** The tool auto-falls-back to rendered-image OCR; try `--backend paddleocr` if it still fails.
- **Wrong title/author?** Name files `Title - Author.pdf`; otherwise the EPUB title falls back to the filename.

## Known limitations

- PyMuPDF4LLM renders display formulas as images (not LaTeX); cloud OCR formulas convert to MathML correctly
- Two-column PDFs occasionally merge the last line
- Cleaning rules are heuristic; extreme layouts may mis-join or mis-remove
- EPUBCheck not bundled; standard-compliance validation is on the user side

## Directory layout

```text
src/
  backends/         # base (abstract) / pymupdf / mineru / paddleocr
  detector/         # PDF type detection (incl. fake text layer)
  markdown/         # cleaner / bold annotation
  epub/             # pandoc wrapper
  batch.py          # batch processing (retry / skip / resume)
  cli.py            # ebook-converter entry
  convert.py        # auto routing (text / scanned / hybrid)
  paths.py          # paths & credentials (apikey.json)
config/             # config.yaml + book.css
desktop/            # Tauri 2 desktop app (React + Tailwind v4)
scripts/            # test fixtures / e2e tests / EPUB verification
```

## Copyright notice

This repository contains **no book content** — only code, self-generated test fixtures, and documentation. The tool itself is a converter; converting copyrighted books is your responsibility under your local laws and is intended for **personal use only**. Do not distribute converted copies of copyrighted works.
