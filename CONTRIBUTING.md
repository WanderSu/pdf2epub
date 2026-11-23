# Contributing

Thanks for helping improve pdf2epub. This file covers environment setup, how to make
changes, how to commit, and how releases happen. Design decisions live in
`docs/design.md`.

## Requirements

| Component | Version | Purpose |
|---|---|---|
| Python | 3.12+ | Engine |
| [uv](https://docs.astral.sh/uv/) | latest | Dependencies and virtualenv |
| Node.js | 20+ | Desktop frontend |
| Rust | stable | Desktop shell (Tauri 2) |
| pandoc | 3.x | Markdown → EPUB; tests that need it are skipped when missing |

Cloud OCR (MinerU / PaddleOCR-VL) only runs when explicitly enabled. Offline
development needs no credentials.

## Install dependencies

Always install from the lockfiles — never do an unlocked update:

```bash
uv sync --locked
cd desktop && npm ci
```

Credentials belong in `apikey.json` at the project root (git-ignored) or in the
`MINERU_API_TOKEN` / `PADDLEOCR_TOKEN` environment variables. **Never commit, paste,
or log them.**

## Tests

```bash
uv run pytest -q                    # full offline suite
uvx ruff check                      # static checks (paths listed in pyproject)
cd desktop && npm run check         # tsc + eslint + vitest
cd desktop/src-tauri && cargo test --locked
```

Real cloud OCR costs credits and is skipped by default; enable it explicitly:

```bash
PDF2EPUB_LIVE_OCR=1 uv run pytest -q tests/test_live_ocr_footnotes.py
```

Any change to networking, caching, or archive handling **must** come with tests for
failure modes, timeouts, retries, and size limits — these paths fail silently: a bad
build or a double charge, not an exception.

## Commits

- **One topic per commit**: independently describable, with the relevant tests passing
  on its own. Mixing several changes into one commit gets sent back for a split.
- Run the relevant tests before and after the change; smallest set first, then full.
- State **risk / tests / rollback** in the message; quote real test numbers.
- Do not remove existing cache-compatibility logic, do not weaken EPUB validation, and
  do not change file names, working directories, or artifact-recovery semantics.
- Never commit real tokens, PDF/EPUB samples, `work/`, `output/`, `logs/`, or user data.

## Branching and releases

`main` is protected: force-pushes and deletion are blocked, and a change may only be
merged once CI is green (engine/frontend/Rust on Linux, plus the Windows release-shape
pipeline).

Releases are a maintainer-run, manual step — **never automated in CI**:

```bash
uv run python scripts/bump_version.py <version>
uv run python scripts/build_release.py <version>
uv run python scripts/check_release.py dist_release/<version>
gh release create v<version> <zip> --notes-file <notes>
```

Before publishing, confirm the archive contains no `apikey.json`, `work/`, `output/`,
`logs/`, or local paths.
