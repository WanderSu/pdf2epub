# Security Policy

## Reporting

**Do not open a public issue.** Use GitHub's private security advisory
(`Security` → `Report a vulnerability`) or contact the maintainer directly.

Include the affected version, reproduction steps, and impact. **Do not attach real
tokens, real books, or logs containing personal information** — strip credentials and
paths first.

Expect a reply within three working days; a fix and a release follow once confirmed.

## In scope

Reported issues are handled as security matters when they involve:

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
