# pdf2epub

A tool that converts PDF and Markdown files into EPUB ebooks.

It handles digital PDFs, scanned PDFs, mixed PDFs, and existing Markdown files. Text extraction, OCR, Markdown cleanup and EPUB generation are handled for you, so the result reads properly on Kindle, KOReader, Apple Books, Calibre and similar readers.

[![Release](https://img.shields.io/github/v/release/WanderSu/pdf2epub?label=release)](https://github.com/WanderSu/pdf2epub/releases)
[![Stars](https://img.shields.io/github/stars/WanderSu/pdf2epub?label=stars)](https://github.com/WanderSu/pdf2epub)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/WanderSu/pdf2epub/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文](README.md) | **English**

---

## Contents

- [What it does](#what-it-does)
- [Screenshots](#screenshots)
- [Features](#features)
- [Installation](#installation)
- [Using the desktop app](#using-the-desktop-app)
- [Command line](#command-line)
- [OCR setup](#ocr-setup)
- [Output](#output)
- [EPUB styling](#epub-styling)
- [FAQ](#faq)
- [CLI options](#cli-options)
- [Known limitations](#known-limitations)
- [Running from source](#running-from-source)
- [Reporting issues](#reporting-issues)

---

## What it does

pdf2epub converts a PDF into an EPUB for reading.

It is mainly meant for this situation:

> You have a PDF, but you would rather read it as an EPUB on Kindle, KOReader, Apple Books, Calibre or another reader.

Different kinds of PDF are handled differently:

- **Digital PDF**: text and images are extracted directly from the file.
- **Scanned PDF**: text, figures and formulas are recognised with OCR.
- **Mixed PDF**: text pages and scanned pages are routed separately, page by page.
- **Markdown**: converted to EPUB as is, or used as the starting point after OCR.

Along the way it also deals with the usual PDF and OCR problems: page numbers, running heads, broken lines, spacing in Chinese text, heading levels and so on.

---

## Screenshots

### Convert

| Light | Dark |
| --- | --- |
| ![convert, light theme](docs/screenshot-convert-light.png) | ![convert, dark theme](docs/screenshot-convert-dark.png) |

### Settings

![settings](docs/screenshot-settings-light.png)

The desktop app uses a letterpress-inspired layout and splits the work into three workspaces:

- **Convert**: add files and start converting
- **Library**: browse the ebooks you have already produced
- **Settings**: OCR, cleanup, output and EPUB checks

---

## Features

### Automatic PDF detection

The tool decides what kind of PDF it is looking at and picks the right path:

- PDF with a usable text layer → local extraction
- Scanned PDF → OCR
- Text pages mixed with scanned pages → handled page by page
- Damaged or fake text layer → asks you how to proceed

You do not have to work out the right method for each book yourself.

### OCR

Supported services:

- [MinerU](https://mineru.net/)
- PaddleOCR-VL

OCR runs in the cloud, so scanned PDFs need an API token (see [OCR setup](#ocr-setup)).
When cloud OCR is used, the page images of your PDF are sent to the service you selected.

Books longer than a single cloud task are split automatically and merged back in the original order.

### Footnotes

Footnotes in scanned books (notes printed at the bottom of a page) come from the OCR structured result. The tool links a marker in the body text to its note and writes real **native EPUB footnotes**: the marker jumps to the note, and the note links back to the text.

A link is only created when the evidence is solid: same page (or the page right after), matching marker, and matching counts on both sides. A footnote that cannot be confirmed is left in place as plain text and the body marker stays untouched — better a missing link than a wrong one.

### Resuming after an interruption

Submitted OCR tasks are recorded.

If a conversion is interrupted by a network failure, an OCR timeout, a closed window or a failed EPUB build, running it again continues from the submitted task instead of uploading the whole book a second time.

Finished parts are kept as well.

### Markdown cleanup

Markdown produced by OCR or by PDF extraction usually is not pleasant to read as is, so the conversion cleans it up:

- remove standalone page numbers
- remove repeated running heads
- join lines broken across pages
- fix odd spaces introduced by OCR
- fix spacing in Chinese text
- drop duplicate headings
- repair heading levels
- optional bold detection
- check image references

Each rule can be turned off for books with unusual typography.

### EPUB

The final output is an EPUB containing:

- table of contents
- heading hierarchy
- images
- tables
- formulas
- footnotes
- code blocks
- internal links
- EPUB stylesheet

The result is also checked for basic structural problems.

### Batch conversion

Several files, or a whole directory, can be converted at once.

One failing file does not affect the others, and the batch supports:

- automatic retries
- skipping files that are already done
- resuming after an interruption
- checking whether an existing output file is complete

---

## Installation

### Windows desktop app

If you just want to use pdf2epub, the desktop app is the recommended way. It is Windows only; on other systems you can [run from source](#running-from-source).

Go to:

[GitHub Releases](https://github.com/WanderSu/pdf2epub/releases)

and download the latest Windows archive, for example:

```text
pdf2epub-v0.4.2-win-x64.zip
```

The version number in the file name changes with each release. Unzip it and run:

```text
pdf2epub.exe
```

The desktop build bundles the Python runtime it needs, so no separate Python installation is required.

Keep `pdf2epub.exe`, `cli.exe` and `config/` in the same folder — do not move the exe out on its own.

### Pandoc is also required

EPUB files are produced by Pandoc, so Pandoc has to be installed.

On Windows:

```powershell
winget install pandoc
```

You can also install it from the Pandoc website:

[https://pandoc.org/installing.html](https://pandoc.org/installing.html)

Pandoc is only picked up by programs started afterwards, so reopen pdf2epub (or open a new terminal window) once the installation is finished.

### First run

For an ordinary digital PDF, installing Pandoc is all you need.

Scanned PDFs additionally need an OCR service, configured in the desktop app under:

```text
03 Settings → OCR & credentials
```

You can also create this file in the program folder:

```text
apikey.json
```

for example:

```json
{
    "MinerU": "your MinerU token",
    "PaddleOCR-VL": "your PaddleOCR token"
}
```

`apikey.json` is never committed to Git.

---

## Using the desktop app

Open `pdf2epub.exe` and drop a PDF or Markdown file onto the convert area.

The flow:

```text
add a file
   ↓
detect the PDF type
   ↓
choose how to handle it
   ↓
extract text / OCR
   ↓
clean up the Markdown
   ↓
build the EPUB
   ↓
check the EPUB
```

Once it finishes, the EPUB is in the output directory configured in settings.

---

## Command line

The CLI is meant for batch work, or for hooking pdf2epub into your own workflow.

### Install

You need:

* Python 3.12
* [uv](https://docs.astral.sh/uv/)
* [Pandoc](https://pandoc.org/installing.html) 3.0 or newer

```bash
git clone https://github.com/WanderSu/pdf2epub.git
cd pdf2epub
uv sync
```

The command is then available as:

```text
.venv/Scripts/ebook-converter.exe
```

### Convert a PDF

```bash
.venv/Scripts/ebook-converter.exe "my-book.pdf" -o output
```

The PDF type is detected automatically.

### Batch conversion

```bash
.venv/Scripts/ebook-converter.exe books/ -o output
```

### Convert Markdown

```bash
.venv/Scripts/ebook-converter.exe "my-book.md" -o output
```

Images referenced by the Markdown go in an `images/` folder next to it:

```text
my-book.md
images/
```

If the Markdown was produced by a tool such as MinerU, rename its `full.md` to `书名.md` (keep the image folder as `images/`) and convert it directly. The EPUB title comes from the file name, so name the file after the book.

### Preflight

To see how a PDF would be handled before converting anything:

```bash
.venv/Scripts/ebook-converter.exe "my-book.pdf" --dry-run
```

It writes no EPUB and submits no OCR task. It reports:

* PDF type
* page count
* planned backend
* how the book would be split
* how many cloud OCR pages it needs

### Choosing an OCR backend

For example, to use MinerU:

```bash
.venv/Scripts/ebook-converter.exe "scanned.pdf" -o output --backend mineru
```

`--backend paddleocr` and `--backend pymupdf` work the same way.

The default is `--backend auto`, which lets the program decide.

---

## OCR setup

Only scanned PDFs need OCR.

### Getting a token

- **MinerU**: register at [mineru.net](https://mineru.net/) and create an API token in the console. The free allowance is counted per day and is normally more than enough for a book.
- **PaddleOCR-VL**: the token comes from [Baidu AI Studio](https://aistudio.baidu.com/); enable the service there to get one.

You only need to fill in the token for the service you actually use.

### Credentials

In the project root (the program folder for the desktop build) create:

```text
apikey.json
```

with:

```json
{
    "MinerU": "your MinerU token",
    "PaddleOCR-VL": "your PaddleOCR token"
}
```

Environment variables work too:

```text
MINERU_API_TOKEN
PADDLEOCR_TOKEN
```

They take precedence over `apikey.json`. In the desktop app you can also paste and save the tokens under `03 Settings → OCR & credentials`.

### Quota

OCR is a cloud service and is normally billed per page.

Before a real conversion you can run:

```bash
.venv/Scripts/ebook-converter.exe "scanned.pdf" --dry-run
```

The desktop app also shows how many OCR pages a book is expected to need before it starts.

Preflight assumes a daily allowance of 1000 pages and warns when a book exceeds it.

If you would rather not have failed pages retried automatically, use `--retries 0`.

---

## Output

By default each book produces:

```text
output/
└── <title>.epub
```

The EPUB keeps the source file name, with the extension changed to `.epub`.

Intermediate files are kept in:

```text
work/
└── <title>/
    ├── book.md
    └── images/
```

If you want to fix something by hand, edit `book.md` and build the EPUB again. That is convenient for books with unusual typography.

---

## EPUB styling

The default stylesheet lives at:

```text
config/book.css
```

It covers:

* body text for Chinese
* first-line indentation
* headings
* images
* tables
* formulas
* code blocks
* footnotes

The stylesheet does not force text or background colours, so it generally works with a reader's dark mode. Fonts and font sizes can be overridden by the reader as well.

If you have your own preferences, edit `book.css`.

---

## FAQ

<details>
<summary><b>Why do scanned PDFs need an API token?</b></summary>

A scanned PDF is a set of page images with no text to extract, so OCR is required.

pdf2epub uses MinerU or PaddleOCR-VL for cloud OCR.

Ordinary digital PDFs are extracted locally and need no token.

</details>

<details>
<summary><b>It cannot find Pandoc — what now?</b></summary>

Pandoc is required to build the EPUB.

Make sure it is installed (`winget install pandoc`), then reopen pdf2epub or open a new terminal window — a program that is already running does not pick up a newly installed tool.

In the desktop app you can check `03 Settings → Environment check`.

When Pandoc is missing, the conversion stops before extraction or OCR, so no OCR quota is spent.

</details>

<details>
<summary><b>Can it convert books longer than 200 pages?</b></summary>

Yes.

When an OCR backend has a per-task page limit (MinerU allows 200 pages per task), pdf2epub splits the PDF into parts automatically and merges them back in order.

There is no need to split a large book into several PDFs by hand.

</details>

<details>
<summary><b>I closed the program in the middle of OCR — what happens?</b></summary>

Run the same book again.

Submitted cloud tasks are recorded in:

```text
work/<title>/.ocr_task.json
```

The program continues the existing task instead of uploading the book again.

Parts that already finished are kept too.

</details>

<details>
<summary><b>The text in the PDF looks fine, but the output is garbage. Why?</b></summary>

Some PDFs do have a text layer, but it is damaged, or it was only added for searching.

pdf2epub tries to detect this.

If the text layer really is unusable, switch to OCR.

You can also force local extraction (`--backend pymupdf`), but the result may still be unreadable.

</details>

<details>
<summary><b>The output has too many line breaks. Can I change that?</b></summary>

pdf2epub joins lines that were broken across pages by default.

For books with unusual typography you can turn the corresponding cleanup rule off in the desktop settings, or use `--clean-disable join_lines`.

Poetry and other content that must keep its line structure is handled by heuristics, so unusual layouts may still need a manual check.

</details>

<details>
<summary><b>Are images and formulas kept?</b></summary>

Yes. Images end up in the EPUB.

Formulas depend on the backend:

* local PDF extraction: some formulas are stored as images
* OCR backends: LaTeX is recognised and converted to MathML in the EPUB

For a book with many formulas, an OCR backend that recognises formulas is usually the better choice.

</details>

<details>
<summary><b>The title or author is wrong.</b></summary>

Name the file like this:

```text
Title - Author.pdf
```

(a space on both sides of the dash) and the metadata follows the file name.

For example:

```text
Meditations - Marcus Aurelius.pdf
```

becomes:

```text
title: Meditations
author: Marcus Aurelius
```

If the file name does not follow that pattern, the file name is used as the title.

</details>

<details>
<summary><b>How do I convert a book again?</b></summary>

Use `--force`:

```bash
.venv/Scripts/ebook-converter.exe "my-book.pdf" -o output --force
```

By default the program skips files that are already done.

</details>

<details>
<summary><b>How do I check whether the EPUB is complete?</b></summary>

Every conversion runs a basic EPUB check covering:

* the EPUB container
* the manifest
* the spine
* XHTML
* the table of contents
* the cover
* internal links
* CSS
* image references

If you want a failed check to fail the conversion, use `--strict`:

```bash
.venv/Scripts/ebook-converter.exe "my-book.pdf" -o output --strict
```

</details>

---

## CLI options

```
ebook-converter <file-or-dir>... [-o output-dir] [options]
```

| Option | Description |
| --- | --- |
| `--backend {auto,pymupdf,mineru,paddleocr}` | Force a backend; the default is `auto` |
| `-o, --output DIR` | EPUB output directory, default `output/` |
| `--work DIR` | Intermediate file directory, default `work/` |
| `--retries N` | Retries per file, default 2 |
| `--force` | Ignore the "already done" state and convert again |
| `--clean-disable LIST` | Turn off Markdown cleanup rules (comma separated, repeatable) |
| `--strict` | Fail the file when the EPUB check reports an error; default is to warn only |
| `--lang LANG` | EPUB language (`zh-CN`, `en`, `ja`, ...); detected from the text by default |
| `--dry-run` | Preflight only, no files are written |
| `--json` | With `--dry-run`, print the preflight result as JSON |
| `--json-events` | Emit stage events as JSON Lines (used by the desktop app) |
| `--no-resume` | Do not reuse a cloud OCR task that was already submitted |
| `--log FILE` | Log file, default `logs/batch-<timestamp>.log` |
| `--no-log` | Do not write a log file |
| `--verbose, -v` | DEBUG logging |

Cleanup rules:

| Rule | Description |
| --- | --- |
| `page_numbers` | remove standalone page numbers |
| `running_heads` | remove running heads repeated across pages |
| `join_lines` | join lines broken across pages (line structure such as poetry is preserved where possible) |
| `ocr_spaces` | merge words that OCR split apart |
| `cjk_spaces` | fix stray spaces inside Chinese text |
| `dup_headings` | drop duplicate adjacent headings |
| `headings` | drop empty headings and repair heading levels |
| `bold` | mark emphasised text as bold based on its font (off by default) |
| `images` | check that image references exist |

For example, to turn off line joining:

```bash
--clean-disable join_lines
```

or several rules at once:

```bash
--clean-disable join_lines,cjk_spaces
```

---

## Known limitations

Some problems come from PDF conversion itself and cannot be solved completely.

### Extracted text is not the original layout

A PDF stores a layout, not a structured book. These may need a manual check:

* two-column layouts
* complex tables
* unusual headings
* poetry
* complex footnotes
* content spanning pages
* unusual formulas
* text inside images

### Formulas from local extraction

When PyMuPDF4LLM extracts formulas, some of them are stored as images rather than LaTeX.

Equation numbers can also end up as a separate image from the formula itself.

Books with many formulas are better handled by an OCR backend.

### Poetry and unusual line breaks

Content that must keep its line structure, such as poetry, is recognised by heuristics. Long verses and single-line poems in particular may still need a manual check.

Local extraction also folds line breaks inside a paragraph into spaces; once that information is gone it cannot be recovered, so books that depend on line structure are better handled by an OCR backend.

### Splitting and mixed layouts

After MinerU splits a book, tables and paragraphs that cross a boundary can be cut.

Books with text pages and scanned pages interleaved are merged by real page number; when there are too many scanned ranges, the program merges some of them to limit the number of cloud tasks, and the page order inside a merged range may differ from the original — the log warns about this.

### EPUB checking

The project includes checks for the practical problems that affect reading, but EPUB spec compliance has not been verified with EPUBCheck.

### UI and other

* When cover rendering fails, the library shows a placeholder block.
* The conversion queue is not kept across restarts (the recent-files list is).

---

## Project layout

If you want to work on the code, these are the folders to start from:

```text
src/
├── backends/       # PDF / OCR backends
├── detector/       # PDF type detection
├── markdown/       # Markdown cleanup
├── epub/           # EPUB generation and checks
├── batch.py        # batch processing
├── cli.py          # CLI
├── convert.py      # conversion flow
└── paths.py        # paths and configuration

config/
├── config.yaml     # default configuration
└── book.css        # EPUB stylesheet

desktop/             # Windows desktop app
docs/                # screenshots
scripts/             # test, verification and build scripts
tests/               # automated tests
```

---

## Running from source

### Python / CLI

```bash
uv sync
uv run ebook-converter "my-book.pdf" -o output
```

### Desktop app

The desktop app is built with:

* Tauri 2
* React 19
* Tailwind CSS 4
* Rust

Install the dependencies:

```bash
cd desktop
npm install
```

Development mode:

```bash
npm run tauri dev
```

Build:

```bash
npm run tauri build -- --no-bundle
```

Building the desktop app needs:

* Node.js ≥ 20
* Rust
* Visual Studio Build Tools
* the Windows C++ toolchain

---

## Development

Tests and builds:

```bash
uv run pytest -q                      # Python tests
cd desktop/src-tauri && cargo test    # Rust tests
cd desktop && npm run build           # frontend type check and build
```

Design notes and the reasoning behind some of the technical choices are in:

[IDEA.md](IDEA.md) (Chinese)

---

## Reporting issues

If a conversion goes wrong, an issue is easier to act on with:

* your operating system
* the pdf2epub version
* the PDF type (digital / scanned / mixed)
* the backend used
* the error log
* what is wrong with the layout of that PDF

If the problem is about a specific PDF, mention which page it appears on.

---

## License

[MIT](LICENSE) © 2026 WanderSu

This repository contains no book content, only program code and self-generated test samples.

Make sure you are allowed to convert the books you use. For copyrighted material, follow local law and the terms of the services involved.
