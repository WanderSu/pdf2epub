# Security Policy

## Reporting security vulnerabilities

General bug reports and feature requests are welcome as public issues, and contributions via pull requests are welcome. 

Please include the affected version, reproduction steps, and potential impact. **Do not attach real tokens, real books, or logs containing personal information** — redact credentials and personal paths before sharing logs.

## Maintenance and response

This project is maintained by an independent developer with limited and irregular availability. The maintainer will do their best to review issues, pull requests, and security reports, but responses may be delayed. There is no guarantee that every issue will receive a reply, every pull request will be reviewed or merged, or that a fix or release will be delivered by a particular date.

Security reports will be assessed and handled on a best-effort basis. If a vulnerability is confirmed, remediation and a release will be scheduled as time and circumstances allow. Please reproduce the issue on the latest release before reporting, if possible.

## In scope

The following are within the scope of security reports and will be assessed on a best-effort basis:

- **Credentials**: how `apikey.json` is written and permissioned, environment-variable precedence, tokens appearing in logs/exceptions/events, and credentials leaking into caches or snapshots.
- **Unpacking cloud result archives**: path traversal, absolute paths, symlinks, and decompression bombs (entry count / single-file / total size).
- **Network downloads**: missing timeouts, unbounded size, or failed downloads leaving a partial file that is later treated as a valid result.
- Dependency vulnerabilities (include the CVE and affected version).

## Known trade-offs by design

- **Cloud OCR uploads the PDF to a third-party service.** That is the feature, not a vulnerability; use the local backend (`--backend pymupdf`) when OCR is not needed.
- **The credential file is plain text.** Permissions are tightened to "current user only", but other programs running as the same account can still read it — that same-account trust model is out of scope.
- **The release self-check verifies structure and runnability**, not content correctness.

## Supported versions

Only the latest release is maintained. Reproduce on the latest version before reporting, if possible.