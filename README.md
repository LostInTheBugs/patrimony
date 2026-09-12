# Patrimony

![Patrimony logo](public/logo.png)

**Patrimony — Data Sovereignty.** Personal, self-hosted wealth dashboard:
see your entire financial picture in one place — assets (cash, savings,
stocks, real estate, crowdfunding, crypto, precious metals…) *and* debts
(real-estate and vehicle loans) — track its evolution over time, without
handing your financial data to a third party.

Multilingual UI (FR / DE / LU / EN), feedback-driven.

## Live demo

A public instance with **fictional data** (one account per main asset
class, multi-year history) is hosted for review:

**https://patrimony.cloudfr.net**

- Login: `demo`
- Password: `patrimony-demo-2026`

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
- **Multi-currency**: assets can be held in EUR, USD, CHF, GBP, JPY, CAD
  or AUD — totals are converted to EUR with **ECB reference rates** (daily
  refresh + full history backfill; manual rate override per asset; a
  missing rate is flagged, never guessed)
- **Loans**: real-estate and vehicle loans — remaining balance, monthly
  payment (insurance included), interest still to pay, estimated end date,
  amortization schedule and curve; equity (value − debt) shows on the
  asset line, with a debt box on the dashboard
- **Rentals & total cost of ownership**: per-property rental tracking
  (expected vs received over a rolling 12 months, occupancy, gross / net /
  net-of-loan yields) and full cost tracking for properties and vehicles,
  fed by transaction imputation
- **Crypto wallets**: EVM wallet addresses are scanned for token balances
  (market prices via DefiLlama — staking tokens such as stETH are valued
  at market price); per-wallet cards, monthly history, automatic refresh
  (opt-out)
- **Crowdfunding tracking**: real-estate crowdfunding projects (Bricks.co,
  La Première Brique) — imported operations, computed indicators (accrued
  interest, payment delays, royalty contracts), platform balances, history
  curve
- **Simulators**: capital projection, FIRE study (with an optional
  Monte-Carlo bootstrap run on real index history) and annuity simulation
- **Tax estimates** (« if I liquidated today »): per-asset estimates under
  French or Luxembourg rules from **versioned grids** — a new tax year is
  a new grid, an uncovered year is refused — with the assumptions and
  warnings shown next to every figure. Descriptive by design: they show
  what the rules say, they do not tell you what to do. Disabled by default
  behind a consent screen.
- **Net worth dashboard**: total vs invested, net gain, per-class
  allocation donut, debt box, stacked 12-month → 10-year evolution chart,
  and per-module performance charts (stocks & insurance, loans, rentals,
  crypto, crowdfunding)
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
| `DISCLAIMER` | — | Override the login-screen notice with your own text |
| `PAT_CRYPTO_AUTO` | `1` | `1` = daily crypto wallet refresh (boot catch-up if > 24 h, then 06:00 Europe/Paris); `0` disables |
| `PAT_CRYPTO_MAX_AGE_H` | `24` | Skip the automatic wallet refresh when the data is fresher than this |
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

## Windows desktop build

A standalone **Windows build** is produced by CI (see `desktop/`) and
attached to the [latest
release](https://github.com/LostInTheBugs/Patrimony/releases/latest) as
**`Patrimony-Windows.zip`**: a `Patrimony.exe` that embeds the whole app —
no Python, no Docker, nothing to run in a terminal. Your data lives in a
`data/` folder created next to the executable; exports use a native
« Save as » dialog, and the package ships with end-user guides in four
languages (FR / EN / DE / LU). The
executable is **unsigned**: if Windows Defender flags it, use
« Actions → Allow on device » (known false positive — reported to
Microsoft, cleared in September 2026).

## Financial model & limitations

Read this before trusting the numbers — the dashboard is deliberately simple:

- **Asset valuation is what you record, nothing more.** Manual assets show
  your latest entry; automatic assets show `quantity × latest market price`
  (Yahoo Finance for stocks/ETFs, CoinGecko for crypto, last successful
  fetch). The UI shows the date of the last valuation and marks automatic
  quotes **stale when older than 7 days** — a failed refresh never silently
  presents an old price as current.
- **Multi-currency, converted with ECB rates.** An asset is valued in its
  own currency (e.g. `IWDA.L` trades in USD, `IWDA.DE` in EUR) and totals
  convert to EUR at the latest **ECB reference rate up to the valuation
  date** (a manual per-asset override wins; a stale or missing rate is
  flagged, never guessed). No bid/ask spreads, no intraday rates —
  conversions stay approximate.
- **Index comparison is gross and approximate** — treat it as an order of
  magnitude, not a benchmark report:
  - `^GSPC`, `^IXIC`, `^STOXX`, `^FCHI` are **price indices: dividends are
    not reinvested**. `IWDA.L` (MSCI World) is an accumulating ETF, so it
    does include reinvested dividends.
  - **No fees, no taxes, no FX adjustment** are simulated (each index is
    followed in its own listing currency).
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

Main routes (the source — `src/app.py` — is the exhaustive list; the
FastAPI docs pages are disabled by design for this private app):

| Endpoint | Description |
|---|---|
| `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` | Auth |
| `POST /api/auth/password` | Change password (protected accounts re-wrap the vault key) |
| `GET /api/accounts` · `POST /api/accounts` · `PUT/DELETE /api/accounts/{id}` | Assets CRUD |
| `POST /api/accounts/{id}/valuation` | Record a valuation |
| `GET /api/accounts/{id}/positions` · `PUT/DELETE /api/positions/{id}` · `POST /api/positions/{id}/dividend` | Portfolio positions & dividends (stocks) |
| `GET /api/summary` | Net worth, gain, per-class breakdown |
| `GET /api/history?months=60` · `GET /api/evolution?months=12` · `GET /api/cashflow` | Monthly series, net-worth decomposition (flows / income / market), cash flow |
| `GET /api/transactions` · `POST /api/transactions` · `DELETE /api/transactions/{id}` | Transactions CRUD |
| `POST /api/transactions/import-csv` | Bulk import of bank CSV into one of your assets — headers `date`, `libellé`/`description`, `montant` (or `débit`/`crédit`); separator `,`, `;` or tab; negative amount = inverted type; duplicates skipped |
| `GET/POST /api/income-rules` · `GET /api/income-calendar` · `GET /api/income-actual` | Passive income rules, 12-month calendar, actuals |
| `GET /api/actions/overview` · `POST /api/actions/refresh` · `GET /api/av/overview` | Stocks (PEA/CTO) & life-insurance pages |
| `GET/POST /api/loans` · `GET/PUT/DELETE /api/loans/{id}` · `GET /api/loans/{id}/schedule` · `POST /api/loans/{id}/recompute` · `GET /api/loans/curve` | Loans: CRUD, amortization schedule, recalculation, curve |
| `GET/POST /api/loc/contracts` · `POST /api/loc/payments` · `GET /api/loc/overview` | Rental contracts, encashments, yields |
| `GET/POST /api/tco/items` · `POST /api/tco/impute` · `GET /api/tco/overview` · `GET /api/tco/curve` | Total cost of ownership (properties & vehicles) |
| `GET /api/cw/overview` · `GET /api/cw/tokens` · `POST /api/cw/refresh` · `GET /api/cw/history` · `GET /api/cw/curve` | Crypto wallets & tokens |
| `GET /api/cf/overview` · `/api/cf/projects` · `/api/cf/operations` · `/api/cf/platforms` · `/api/cf/curve` · `POST /api/cf/import-xlsx` | Crowdfunding module |
| `POST /api/sim/project` · `POST /api/sim/rente` · `POST /api/fire/simulate` · `POST /api/fire/montecarlo` | Simulators: projection, annuity, FIRE, Monte-Carlo |
| `GET /api/tax-estimate?account_id=…&year=…` | « If I liquidated today » tax estimate (FR/LU grids) |
| `POST /api/fx/refresh` · `POST /api/fx/history` | ECB rate refresh & historical backfill |
| `GET /api/benchmarks` · `POST /api/refresh-benchmarks` · `POST /api/refresh-prices` | Index benchmarks & market-price refresh |
| `GET/PUT /api/settings` | User settings (tax assumptions…) |
| `GET /api/export` · `POST /api/import` | JSON backup / restore (accounts, valuations, transactions, income rules, positions, dividends, loans) |
| `POST /api/export/encrypted` · `POST /api/import/encrypted` | **Encrypted** backup / restore (AES-256-GCM + PBKDF2 envelope, `scripts/backup.py` compatible) |
| `GET /api/export/csv/{kind}` | CSV export — `accounts`, `transactions`, `valuations`, `rules` (UTF-8, Excel-ready) |
| `GET /api/audit` (admin) | Audit log: logins, changes, imports, deletions (metadata only, 90-day retention) |
| `GET/POST /api/tokens` · `DELETE /api/tokens/{id}` | Personal API tokens (Bearer auth) — hashed at rest, shown once, revocable; forbidden for protected accounts |
| `GET /api/family` · `POST /api/family` (admin) | List / create members |
| `POST /api/family/{user}/reset-password` (admin) | Reset a **standard** member (forbidden on protected) |
| `DELETE /api/family/{user}` (admin) | Delete a member (destroys the vault too) |
| `POST /api/vault/init` · `POST /api/vault/open` (protected) | First-time seal / unlock of the encrypted vault |
| `GET /api/version` | App version |

## Security

See [`SECURITY.md`](SECURITY.md) for the vulnerability reporting policy
(private advisory, latest release only). The public demo runs **fictional
data only**; keep real deployments on a private network or behind HTTPS,
and change the seeded admin password on first login.

## Repository conventions

- Version: see `VERSION` (YEAR.MONTH.NNN, no `v` prefix)
- Changelog: `CHANGELOG.md`
- Git identity: `LostInTheBugs` (never push real server names / IPs /
  credentials — audit before any push)

## Development cost (LLM)

This project was built entirely through AI-assisted sessions (Hermes Agent).
Usage since the first version (2026-09-04 bootstrap; see `TOKENS.md` for the
per-session detail):

| Metric | deepseek-v4-flash | gemini-3.6-flash (vision) | **Total** |
|---|---|---|---|
| Dev sessions (interactive + scripted) | 15 | (same sessions) | **15** |
| API calls | 5 070 | 47 | **5 117** |
| Input tokens | 8 952 795 | 55 337 | **9 009 672** |
| Output tokens | 4 334 009 | 58 783 | **4 392 808** |
| **Subtotal (input + output)** | **13 286 804** | **114 120** | **13 402 480** |
| Cache read (reused at reduced price) | 1 048 377 472 | 0 | **1 048 377 472** |
| **Estimated cost** | **≈ 5.27 USD** | **≈ 0.46 USD** | **≈ 5.73 USD** |

## Disclaimer

Patrimony is a **personal project, built for fun** — not a professional
product, and it is provided as-is, without warranty of any kind.

- It is **not financial, tax or legal advice**. Figures shown — tax
  estimates in particular — are best-effort, may contain errors or gaps,
  and depend on the assumptions displayed next to them. Check with a
  professional before acting on them.
- Tax estimates are **descriptive**: they show what the rules say under
  the displayed assumptions — they never tell you what to do.
- Tax rules are versioned per year and the engine **refuses to compute**
  years it does not cover — but the grids can still be wrong.
- The public demo runs on **fictional data only**.
- Expect rough edges and the occasional breaking change between versions.

The same notice is shown on the app's login screen (in all four UI
languages).

## License

MIT — see `LICENSE`.
