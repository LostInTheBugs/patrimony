# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting:
**[Report a vulnerability](https://github.com/LostInTheBugs/Patrimony/security/advisories/new)**
(Security tab → "Report a vulnerability").

You can typically expect an acknowledgement within **7 days**. The report
will be investigated and you will be kept informed; you will be credited
in the release notes unless you prefer otherwise.

We follow **coordinated disclosure**: expect a fix and an advisory within
**90 days** of your report, or as soon as a corrected release is
published — whichever comes first. If you intend to publish earlier, a
heads-up lets us ship the fix first.

## Scope

Patrimony is a self-hosted, offline-first application. Areas of special
interest:

- Authentication / authorization bypasses, session handling
- Owner-scoping mistakes (data leaking between family members, or between
  standard and *protected* members)
- The encrypted-vault flow (PBKDF2-600k + AES-256-GCM sealing in the
  browser)
- Anything that would let a malicious web page reach or drive a
  locally-running instance (the desktop build binds to `127.0.0.1` only)
- Backup/restore and CSV import paths (untrusted-input handling)

## Not in scope

- Findings that require a modified or unsigned build, or an already
  compromised OS
- Automated scanner output without a proof of concept
- The public demo instance: it runs **fictional data only** — treat it as
  a demo, never store anything real there

## Supported versions

Only the **latest release** is supported (see Releases). Security fixes
are issued as new releases on `main`.

## Operational notes for operators

- Keep deployments on a private network (LAN or VPN) or behind HTTPS
- Change the seeded admin password on first login
- Run a single worker (open vaults live in process memory)
- Backups: encrypted export (`/api/export/encrypted`) is the recommended
  format — the plain JSON export contains everything in clear text
