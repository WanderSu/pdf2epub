# Security Policy

> 中文版：[SECURITY.zh-CN.md](SECURITY.zh-CN.md)

## Reporting a vulnerability

General questions and feature requests are welcome as public issues, and PRs are welcome too.

Please include the affected version, reproduction steps and potential impact where you can. **Do not attach real tokens, real book files, or logs containing personal information** — remove credentials and personal paths before sharing logs.

## Maintenance and response

This project is maintained by a single developer, with limited and irregular time. Issues, PRs and security reports are reviewed on a best-effort basis: replies may be delayed, and there is no guarantee that every issue gets an answer or that every PR is reviewed or merged. No fix or release timeline is promised.

Security reports are assessed and handled on a best-effort basis as well. If a vulnerability is confirmed, the fix and release are scheduled as maintenance time and the nature of the issue allow. Please reproduce on the latest release before reporting.

## Scope

The following are in scope for security reports and are assessed and handled on a best-effort basis:

- **Credentials**: how `apikey.json` is written and permissioned, environment-variable
  precedence, tokens appearing in logs/exceptions/events, credentials leaking into
  caches or snapshots.
- **Unpacking cloud result archives**: path traversal, absolute paths, symlinks,
  decompression bombs (entry count / single file / total size).
- **Network downloads**: missing timeouts, unbounded size, failed downloads leaving a
  half-written file that later counts as a valid result.
- Dependency vulnerabilities (include the CVE and affected version).

## Known trade-offs by design

- **Cloud OCR uploads the PDF to a third-party service.** That is the feature, not a
  vulnerability; use the local backend (`--backend pymupdf`) when OCR is not needed.
- **The credential file is plain text.** Permissions are tightened to "current user
  only", but other programs running as the same account can still read it — that
  same-account trust model is out of scope.
- **The release self-check verifies structure and runnability**, not content
  correctness.

## Supported versions

Only the latest release is maintained. Reproduce on the latest version before
reporting.
