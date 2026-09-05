# Patrimony

![Patrimony logo](public/logo.png)

**Patrimony — Data Sovereignty.** Personal, self-hosted wealth dashboard:
see your entire net worth (cash accounts, savings, stocks, real estate,
crowdfunding, crypto, precious metals…) in one place, track its evolution
over time — without handing your financial data to a third party.

Multilingual UI (FR / DE / LU / EN), feedback-driven.

## Live demo

Try it online with **fictional data** (one asset per class, 2020–2026 history):

- **https://patrimony.cloudfr.net**
- Login: `demo` / `patrimony-demo-2026`

> The demo runs isolated public demo data only. For your real financial data,
> self-host the app on your own private network (see Configuration below).

## Features

- **8 asset classes** (customizable later): current accounts, savings,
  stocks & life insurance, real estate, crowdfunding, crypto, precious
  metals, other
- Per-asset **cost basis** and **valuation history** (manual one-click
  updates, or **automatic market prices**: stocks/ETFs via Yahoo Finance,
  crypto via CoinGecko — symbol + quantity, monthly history backfill)
- **Transactions** per asset (deposits, withdrawals, income, expenses):
  they become the source of truth for the invested cost
- **Passive income tracking**: recurring rules (monthly/quarterly/yearly/
  custom), 12-month expected-income calendar, actual income chart
- **Index comparison**: S&P 500, Nasdaq, MSCI World, STOXX 600, CAC 40,
  Livret A — real monthly levels, annualized performance, and a simulation
  of your actual deposits reinvested in each index
- **Net worth dashboard**: total vs invested, net gain, per-class
  allocation donut, stacked 12-month → 10-year evolution chart
- **Discreet mode**: hide amounts, keep shares (chart switches to index 100)
- **Family mode** (admin): members with their own dashboard. Two member
  modes: *standard* (admin may reset the password and sees a consolidated
  family view) and *protected* (admin sees nothing: totals are hidden and
  reset is impossible by design — deletion only)
- **Encrypted vaults for protected accounts**: data is sealed with the
  member's own password in the browser (PBKDF2-600k + AES-256-GCM,
  WebCrypto). The server only stores an encrypted blob — **at rest**,
  unreadable without the password, even by the admin. A lost password means
  the vault is lost. While a vault is *unlocked*, its key lives in server
  memory only (never on disk); vaults auto-lock after 30 min of inactivity
  (`VAULT_IDLE_MIN`, `0` disables).
- Auth (cookie session, pbkdf2), password change, JSON backup / restore
- Demo dataset (`SEED_DEMO=1`) for evaluation
- Passwords: minimum **12 characters**
- **Installable PWA** (manifest + service worker, https or localhost):
  offline shell, static assets cached and version-busted per release; the
  API is deliberately never cached (freshness + personal data at rest)
- **« Patrimony Capture » browser extension** (MV3, in `extension/`):
  right-click any selected amount on any site → send a valuation to your
  instance via a personal API token. No credentials, no content scripts;
  token hashed at rest and revocable. See `extension/README.md`

## Stack

- Backend: Python FastAPI + SQLite (stdlib, zero ORM)
- Frontend: vanilla JS + Chart.js (single HTML file)
- Deploy: Docker — private LAN compose + Traefik demo compose

## Configuration

| Env var | Default | Description |
|---|---|---|
| `PORT` | `8020` | HTTP port |
| `ADMIN_USER` | `admin` | Login username (seeded on first boot) |
| `ADMIN_PASSWORD` | `change-me` | Login password (seeded on first boot) |
| `COOKIE_SECURE` | `0` | Set to `1` behind HTTPS |
| `SEED_DEMO` | `0` | `1` = insert demo assets (empty DB only) |
| `DATA_DIR` | `./data` | SQLite data directory |
| `VAULT_IDLE_MIN` | `30` | Auto-lock open vaults after N idle minutes (`0` = never) |
| `LOGIN_MAX_FAILS` | `5` | Login attempts allowed per (IP, account) per window |
| `LOGIN_MAX_FAILS_USER` | `10` | Login attempts allowed per account per window |
| `LOGIN_WINDOW_SEC` | `900` | Anti-bruteforce sliding window (seconds) |

> ⚠️ **Security**: always override `ADMIN_USER` / `ADMIN_PASSWORD`
> (`.env` file, never commit it). The app holds sensitive financial data —
> do not expose it to the public internet without strong credentials and
> HTTPS. Protected members' vaults are encrypted at rest; the demo instance
> runs fictional data only.
>
> ⚠️ **Single process**: encrypted vaults (open DB + key) live in the
> process memory, so run uvicorn with **one worker** (default — never
> `--workers > 1`) and a single replica.

## Run locally

```bash
uv venv && uv pip install -r requirements.txt
SEED_DEMO=1 ADMIN_USER=admin ADMIN_PASSWORD=change-me \
  uv run uvicorn src.app:app --port 8020
# open http://localhost:8020
```

## Financial model & limitations

Read this before trusting the numbers — the dashboard is deliberately simple:

- **Asset valuation is what you record, nothing more.** Manual assets show
  your latest entry; automatic assets show `quantity × latest market price`
  (Yahoo Finance for stocks/ETFs, CoinGecko for crypto, last successful
  fetch). The UI shows the date of the last valuation and marks automatic
  quotes **stale when older than 7 days** — a failed refresh never silently
  presents an old price as current.
- **No currency conversion.** An asset is valued in the currency of its
  symbol (e.g. `IWDA.L` trades in USD, `IWDA.DE` in EUR). Mixed-currency
  totals are a known simplification.
- **Index comparison is gross and approximate** — treat it as an order of
  magnitude, not a benchmark report:
  - `^GSPC`, `^IXIC`, `^STOXX`, `^FCHI` are **price indices: dividends are
    not reinvested**. `IWDA.L` (MSCI World) is an accumulating ETF, so it
    does include reinvested dividends.
  - **No fees, no taxes, no FX effects** are simulated.
  - Your deposits are simulated as if invested at the **monthly level** of
    the index (the granularity of the fetched data), whatever the actual
    deposit date within the month.
  - Livret A is synthetic (`(1 + r/12)^n` from the rate stored in the
    database, no tax either).
- **Cost tracking**: with transactions, the invested cost is
  deposits − withdrawals; otherwise it is the manually entered cost basis.

## Backup & restore

The whole dataset — including encrypted vault blobs — lives in the SQLite
file `DATA_DIR/app.db` (default `./data/app.db`). Back it up with SQLite's
online backup so a live instance produces a consistent file:

```bash
mkdir -p backups
# containerized instance:
docker exec <container> python -c \
  "import sqlite3; src=sqlite3.connect('data/app.db'); dst=sqlite3.connect('/tmp/app.bak.db'); src.backup(dst); dst.close()"
docker cp <container>:/tmp/app.bak.db backups/app-$(date +%F).db
# bare instance:
python -c "import sqlite3; src=sqlite3.connect('data/app.db'); dst=sqlite3.connect('backups/app.bak.db'); src.backup(dst); dst.close()"
```

That single file is everything: users, sessions, valuations, prices and the
encrypted vaults. **Restore**: stop the app, replace `data/app.db` with the
backup, start the app — boot migrations are idempotent and will upgrade an
older backup in place. Verify a restore at least once: boot the restored
file with a throwaway `DATA_DIR`, log in, and check `/api/version` and one
data endpoint before trusting it in production.

### Encrypted backups (3 layers)

The **JSON export** (`GET /api/export`) is a portable snapshot, but it is
plaintext and misses users/vaults. The backup strategy has three layers:

1. **Full-DB file copy** (above): plaintext, must stay on your own
   machines. Automatically taken before every production upgrade.
2. **Encrypted ops file backup** — `scripts/backup.py`
   (`PATRIMONY_BACKUP_PASS=... python scripts/backup.py encrypt app.db
   app-<date>.pat.b64`): encrypts *any file* (typically the SQLite dump)
   with AES-256-GCM + PBKDF2-HMAC-SHA256 (310 000 iterations, random salt
   and nonce). The `.pat.b64` artifact is safe to store **off-site**
   (other disk, other machine); the passphrase is the only secret. Decrypt
   with `scripts/backup.py decrypt`. Format: versioned, self-describing
   envelope — `src/backup_crypto.py` (shared by the app and the CLI).
3. **In-app encrypted export** — Settings → « Export chiffré » /
   « Restaurer chiffré » (`POST /api/export/encrypted`,
   `POST /api/import/encrypted`): same AES-256-GCM envelope around the
   JSON snapshot (accounts, valuations, **transactions, income rules**),
   per-user, download to your own storage. Restore is transactional and
   replaces only your data; a wrong passphrase or a tampered file fails
   cleanly (authenticated encryption) without touching the data.

**Restore test protocol** (run it, it is automated in `tests/test_backup.py`):
encrypt → wipe → restore → compare accounts, valuations, transactions and
rules; wrong-passphrase and bit-flip attempts must fail and leave the data
untouched. Off-site copies: refresh at least weekly; keep the last three
generations; write the passphrase somewhere you cannot lose (password
manager) — there is no recovery without it.

The JSON export is not a substitute for the SQLite backup: it cannot
restore users, sessions or vault keys. Per-table
**CSV exports** (`/api/export/csv/{kind}`, Excel-ready UTF-8) are available
from the Settings page. An **audit log** (admin-only) records logins,
changes, imports and deletions as metadata only — never amounts — and keeps
90 days of history. Failed logins store the caller IP (personal data, same
90-day retention). **Protected members** only produce their auth events in
the journal (login/logout/vault open — no detail): data events on their
vault are not logged at all, so the journal can never reveal their asset
structure or activity rhythm.

Deployment notes: keep the app on a private network (LAN or VPN), behind
HTTPS (see `docker-compose.lan.yml` for a LAN setup; any reverse proxy with
TLS works), change the seeded admin password on first login, and never run
it with more than one worker (open vaults are process memory).

## API

| Endpoint | Description |
|---|---|
| `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` | Auth |
| `POST /api/auth/password` | Change password (protected accounts re-wrap the vault key) |
| `GET /api/accounts` · `POST /api/accounts` · `PUT/DELETE /api/accounts/{id}` | Assets CRUD |
| `POST /api/accounts/{id}/valuation` | Record a valuation |
| `GET /api/summary` | Net worth, gain, per-class breakdown |
| `GET /api/history?months=60` | Monthly net worth series per class |
| `GET /api/transactions` · `POST /api/transactions` · `DELETE /api/transactions/{id}` | Transactions CRUD |
| `POST /api/transactions/import-csv` | Bulk import of bank CSV into one of your assets — headers `date`, `libellé`/`description`, `montant` (or `débit`/`crédit`); separator `,`, `;` or tab; negative amount = inverted type; duplicates skipped |
| `GET /api/export` · `POST /api/import` | JSON backup / restore (accounts, valuations, transactions, income rules) |
| `POST /api/export/encrypted` · `POST /api/import/encrypted` | **Encrypted** backup / restore (AES-256-GCM + PBKDF2 envelope, `scripts/backup.py` compatible) |
| `GET /api/export/csv/{kind}` | CSV export — `accounts`, `transactions`, `valuations`, `rules` (UTF-8, Excel-ready) |
| `GET /api/audit` (admin) | Audit log: logins, changes, imports, deletions (metadata only, 90-day retention) |
| `GET/POST /api/tokens` · `DELETE /api/tokens/{id}` | Personal API tokens (Bearer auth) — hashed at rest, shown once, revocable; forbidden for protected accounts |
| `GET /api/family` · `POST /api/family` (admin) | List / create members |
| `POST /api/family/{user}/reset-password` (admin) | Reset a **standard** member (forbidden on protected) |
| `DELETE /api/family/{user}` (admin) | Delete a member (destroys the vault too) |
| `POST /api/vault/init` · `POST /api/vault/open` (protected) | First-time seal / unlock of the encrypted vault |
| `GET /api/version` | App version |

## Repository conventions

- Version: see `VERSION` (YEAR.MONTH.NNN, no `v` prefix)
- Changelog: `CHANGELOG.md`
- Git identity: `LostInTheBugs` (never push real server names / IPs /
  credentials — audit before any push)

## License

MIT — see `LICENSE`.
