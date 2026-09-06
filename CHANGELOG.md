# Changelog

All notable changes to Patrimony are documented in this file.

## [2026.09.029] — 2026-09-06

### Added

- **Wealth evolution engine** — `GET /api/evolution?months=N`: answers
  "why did my wealth change?" with an *exact by-construction* additive
  monthly decomposition per account, per asset class and total:
  `ΔV = Flows + Income + Market effect` where Flows = deposits −
  withdrawals − expenses (signed transactions of the month), Income =
  received dividends/interest/rent (income transactions), and Market
  effect = the **residual** (price and FX moves) — anything not declared
  as a flow or income lands in market effect, so the split never drifts
  from the tracked valuations. Same conventions as the rest of the
  model: last valuation of the month, active accounts, open/close dates,
  BCE FX. Annual snapshots (last December valuation per class, current
  year partial) reuse the same windowing.
- **Evolution page** (new 📈 nav entry): "Net worth by year" stacked
  bar chart per asset class + "What is driving your wealth": last 12
  months, each month summarized as signed chips (Flows / Income / Market
  effect → change), click to expand the per-class breakdown table. One
  short definition note under the data (Flows / Income / Market effect).
  i18n ×4.

### Tests

- `tests/test_evolution.py` (4): additive split with deposit, income,
  withdrawal, expense and price moves (exact cents on every level:
  account, class, total), December-snapshot annuals with current-year
  partial, closed-account exclusion and member isolation, market
  residual covering an untracked price move. 65 passing.

## [2026.09.028] — 2026-09-06

### Added

- **Cash-flow projection (recurring expense rules).** `income_rules` gains a
  `kind` (`income` default — existing data and old exports stay valid — or
  `expense`); rules of both signs feed the upcoming schedule and a new
  `GET /api/cashflow?months=N` endpoint (3-36): per-month forecast
  `in`/`out`/`net` and a cumulative `balance` starting from the current
  real cash (last valuation of `comptes`-class accounts). Same recurrence
  engine as the income calendar (monthly/quarterly/yearly/custom, day
  clamping), inactive rules excluded, ownership-isolated.
- **UI** — the Income page becomes "Income & expenses": rules list with
  direction arrows and signed colored amounts, upcoming schedule showing
  both signs with a monthly net, and a cash-flow projection chart (net
  bars + projected balance line on its own axis) with the starting balance
  and an explicit "indicative projection" note; warning when the projected
  balance turns negative. i18n ×4. Chart month labels no longer overlap
  (auto-skip on multi-month charts).
- Rules of both kinds travel through JSON/encrypted export-import
  (old files without `kind` import as income).

### Tests

- `tests/test_cashflow.py` (4): kind default/validation/calendar presence,
  projection math (starting balance = cash accounts only, monthly and
  quarterly rules, net and cumulative balance, deactivated rule excluded,
  member isolation), round-trip preserving `expense` + legacy export
  without `kind`, future-dated rules not weighing earlier months.
  61 passing.

## [2026.09.027] — 2026-09-06

### Added

- **Tax wrapper per asset (`accounts.wrapper`)** — skeleton of the envelope
  work: PEA / AV (assurance-vie) / CTO flags on any stock or savings
  account (`bourse` and `epargne` classes only, 400 otherwise), with the
  account `open_date` acting as the envelope opening date (PEA 5-year
  clock, AV seniority). UI: envelope selector in the asset modal (hidden
  for classes where it makes no sense), PEA/AV/CTO badge next to the
  account name, i18n ×4. The field travels through the vault copy,
  JSON/encrypted export-import round trips and CSV exports untouched.
  **Net capital-gains rules (FR/LU per-envelope rates, allowances,
  social levies) are intentionally NOT computed yet — they will plug in
  once the rule sheet is provided; the payload already exposes the
  wrapper for each account.** Test locks storage, payload exposure,
  class validation and round-trip preservation.

## [2026.09.026] — 2026-09-06

### Fixed

- **Annual fees over-counted when several valuations share a month.** The
  cumulative fees estimator applied one twelfth of the annual rate to every
  valuation row — an account valued several times in the same month (daily
  auto refresh, capture extension) accumulated N months of fees for that
  month. Now only the last valuation of each month counts (last `val_date`,
  `MAX(id)` wins on same-day ties, same convention as the rest of the
  financial model). `test_fees_pct_cumul` now locks the rule with
  mid-month valuations and a same-day duplicate.

## [2026.09.025] — 2026-09-06

### Added

- **Portfolio lines (`positions`) for auto-priced stock accounts**: a PEA /
  brokerage account becomes a container; its composition lives in a new
  `positions` table (symbol × quantity × PRU). Account value = Σ(qty ×
  price), refreshed per distinct symbol (deduplicated quotes), monthly
  backfill merged across lines for fresh accounts. Existing single-symbol
  auto accounts are migrated at boot into a one-line portfolio (idempotent,
  nothing lost) — and creating/updating such an account with a symbol still
  mirrors position #1 for API compatibility. New endpoints
  `POST /api/accounts/{id}/positions`, `PUT|DELETE /api/positions/{id}`,
  per-line gains (`gain_eur`/`gain_pct` vs PRU), weights and live prices in
  the accounts payload, UI ⚖️ manager (wide modal, i18n ×4).
- **Dividends as per-position events** (`POST /api/positions/{id}/dividend`,
  `DELETE /api/dividends/{id}`): ex-date + amount per share; a mirrored
  income transaction (source_id `div:{position}:{date}`) is upserted into
  the account ledger on every save — idempotent, resynced on quantity or
  rate change, removed with the event/line, and protected from manual
  deletion (400). UI 💶 per line.
- **Annual fees per account** (`fees_pct`, any class): cumulative ≈ fees are
  computed over the real monthly valuation history (monthly rate applied to
  each month-end value) and shown under the asset row ("≈ €X cumulative
  fees (≈ Y years)").
- **CSV exports localize human values** (asset classes, transaction types)
  following `Accept-Language` (FR default); canonical identifiers and
  headers stay stable so exports remain re-importable. Removed dead
  server-side French labels (`class_label`, summary `label`) — the UI was
  already fully translated via i18n keys.
- Vaults (protected accounts) carry positions and dividend events too:
  schema upgraded on cold open of older blobs, rows copied at vault init,
  included in JSON/encrypted export-import round trips.

### Tests

- New `tests/test_positions.py` (8): legacy-symbol mirroring, CRUD guards
  and ownership isolation, per-line gains/weights, fees cumulation,
  portfolio refresh aggregation + monthly backfill + honest failure,
  dividend mirror lifecycle (create/update/delete/quantity resync/guarded
  manual delete), export-import round trip, CSV localization. 56 passing.

## [2026.09.024] — 2026-09-06

### Added

- **API token scopes** (`full` | `capture`): a `capture` token only grants
  `GET /api/accounts` and `POST /api/accounts/{id}/valuation` — everything
  else (exports, imports, CRUD, family admin, even `/api/auth/me`) answers
  `403 scope_denied`, never 401: the token *is* authenticated, it is merely
  out of scope. An authenticated capture token is reduced from a vault key
  to a mailbox key (external review finding). Existing tokens migrate to
  `full` (idempotent `ALTER TABLE api_tokens ADD COLUMN scope TEXT DEFAULT
  'full'`); `POST /api/tokens` accepts `scope` (default `full`, invalid
  values → 400) and returns/audits it (`"name (scope)"`); token list exposes
  the scope. UI: scope select in the token modal (capture preselected —
  the panel is for the extension) and a scope badge in the token list, i18n
  ×4. The Patrimony Capture extension is unaffected: it only uses the two
  allowed calls.

## [2026.09.023] — 2026-09-05

### Added

- **Historical FX backfill**: `POST /api/fx/history` downloads the full ECB
  series (eurofxref-hist.xml, ~8 MB, since 1999) and stores **month-end
  rates only** (last ECB day of each month per currency, ~10k rows,
  idempotent INSERT OR REPLACE — daily recent rates untouched). Deep
  histories are now converted with the actual month-end rate at or before
  the valuation date instead of the oldest-rate fallback. Settings panel:
  « 🗓️ Charger l'historique BCE » button + note (i18n ×4), audited.
- Tests: `tests/test_fx.py` extended — parser keeps the max day per month
  (intermediate days dropped, single-day months kept), mocked backfill
  route, old valuation (2020) converted at its month-end rate in both the
  summary and the monthly history. 47/47 green. Real-file check: 10 364
  month-end rows for the 7 supported currencies (USD 1999-01-29 →
  2026-09-04).

## [2026.09.022] — 2026-09-05

### Added

- **Configurable login-page disclaimer**: optional `DISCLAIMER` env var is
  exposed (read per request) through the public `GET /api/version` and
  rendered under the login form on unauthenticated screens. Unset = no
  banner (LAN private unchanged); set on the public demo
  (« Démo publique — données fictives. »). No i18n needed (operator
  text, single language per deployment).

### Removed

- All references to commercial wealth-tracking products in the changelog
  (v2026.09.001 entry) — the public repository no longer names any
  third-party product (neutral wording kept).

## [2026.09.021] — 2026-09-05

### Added

- **« Patrimony Capture » extension v1.1.0 — CSS auto-mappings** (the
  passive v2 of the extension): in options, map a host + CSS selector +
  target asset; the extension dynamically registers a content script on
  mapped hosts only (`chrome.scripting.registerContentScripts`,
  reconciled at startup/on change), reads the first matching element
  (text, `aria-label`/`data-value` fallback) and pushes a valuation —
  **once per day max, only when the value changed**. Manual ▶ capture
  (`chrome.scripting.executeScript`, tab of the site must be open),
  edit/delete rows (script unregistered), per-site optional host
  permission, per-mapping status (date/value/error) shown in popup and
  options. Content script: `extension/content.js` (message `pat-read`).
  No credentials, nothing else read.

## [2026.09.020] — 2026-09-05

### Added

- **Multi-currency support** (manual accounts in EUR/USD/CHF/GBP/JPY/CAD/AUD;
  auto accounts stay EUR-valued at fetch time):
  - `fx_rates` table + `accounts.fx_override` (migration included);
    ECB daily rates via `POST /api/fx/refresh` (threadpool, XML parser,
    audited) or a fixed manual rate per account (« 1 EUR = X »), manual
    wins; rate = units per EUR.
  - **EUR conversions** in summary (per valuation date, missing-rate
    accounts excluded and listed `fx_missing`, `fx_asof`, `fx_applied`),
    monthly history (rate ≤ month end, oldest fallback), benchmarks
    (user totals), account payloads (`currency`, `fx`, stale > 7 days
    flagged).
  - `refresh_prices` now converts quote currency to EUR at fetch time
    (single ECB fallback, clean per-symbol error) — previously a
    USD-quoted auto asset was silently summed as EUR.
  - Accounts API: validated `currency` + `fx_override` (create/update);
    auto accounts forced to EUR.
  - UI (i18n ×4): currency selector + fixed-rate field in the asset
    modal; currency badges, local-currency amounts and « ≈ EUR » lines in
    the accounts table (mixed footer + footnote); FX note under the
    dashboard hero; Settings « 💱 Taux de change » panel with ECB refresh.
- Tests: `tests/test_fx.py` (4) — summary conversion + override + missing
  exclusion; history month-end/fallback/no-rate; ECB parser + mocked
  refresh + benchmark conversion; validation. 44/44 green.

## [2026.09.019] — 2026-09-05

### Added

- **Business test suite on financial calculations** (`tests/test_finance.py`,
  5 tests, exact expected values): summary (latest valuation per account
  incl. same-date tie → most recent wins; classes totals/shares; inactive
  and no-valuation accounts excluded; value-without-cost accounts counted
  with `gain: null`), cost semantics (transactions override the manual cost
  basis; `expense` ≠ `withdrawal` and does not reduce cost; income counts
  as inflow), monthly history (carry-forward of the last known value,
  open-date window, same-date tie, empty-DB and 6/240-month clamps),
  benchmarks (annualization pinned: 12.68 % for a 1 %/month level — exact
  formula; Livret A synthetic compounding; cashflow simulation; user
  annualized return formula; index_levels seeded, no network).
- **Encrypted backup & restore strategy**:
  - `src/backup_crypto.py`: versioned self-describing envelope —
    AES-256-GCM + PBKDF2-HMAC-SHA256 (310 000 iterations, random
    salt/nonce), authenticated (wrong passphrase or tampered file fails
    cleanly). Shared by the app and the CLI.
  - `POST /api/export/encrypted`, `POST /api/import/encrypted`
    (8-character passphrase minimum, audited, never stored). Restore is
    transactional; 400 with data untouched on bad passphrase/tampering.
  - **JSON export/import now include transactions and income rules**
    (`_export_data`/`_do_import` shared helpers); legacy payloads
    (accounts+valuations only) stay importable.
  - Settings UI: « 🔐 Export chiffré » / « 🔐 Restaurer chiffré »
    (passphrase + confirmation modal, i18n ×4).
  - `scripts/backup.py` ops CLI (encrypt/decrypt any file — typically the
    SQLite dump — `PATRIMONY_BACKUP_PASS`, getpass fallback, restore
    runbook in the docstring).
  - README: « Encrypted backups (3 layers) » section + restore test
    protocol + off-site rotation guidance.
- Tests: `tests/test_backup.py` (5) — full round trip (encrypt → wipe →
  restore → compare accounts/valuations/transactions/rules), wrong
  passphrase + bit-flip leave data intact, legacy plaintext import,
  crypto units (unicode, 100 KB, tamper, unknown envelope), CLI file
  round trip. **40/40 green.**

### Changed

- `GET /api/export` payload enriched (transactions + income_rules) —
  backward compatible with the previous import format.

## [2026.09.018] — 2026-09-05

### Added

- **Personal API tokens** (enabler for the browser extension): `GET/POST
  /api/tokens`, `DELETE /api/tokens/{id}` — `Authorization: Bearer`
  accepted by `_me()` alongside session cookies. Tokens are stored
  **SHA-256 hashed** (never in clear), displayed once at creation,
  expirable (1-3650 days), revocable, `last_used_at` tracked, every
  creation/revocation audited. **Forbidden for protected accounts** (a
  vault requires an interactive session by design). Settings page: new
  « API access (extension) » panel with create/copy/revoke (i18n ×4);
  panel hidden for protected members.
- **« Patrimony Capture » extension** (`extension/`, Chrome/Edge MV3,
  v1.0.0): select any amount on any page → right-click « 📥 Capturer vers
  Patrimony » → popup prefilled (amount cleaned, date today, note with
  source host) → pick the target asset → valuation posted. No content
  scripts, no credentials, optional host permission limited to your
  instance URL, config (URL + token + default note) in options with a
  connection test. Icons generated from `logo-mark.png`.
- Tests: `tests/test_tokens.py` (3 tests) — lifecycle + hash at rest +
  Bearer data access + revocation, expiry (simulated) + invalid windows,
  protected forbidden + per-user isolation (member token only reaches
  member data). 30/30 green; tokens panel visually checked.

### Changed

- README: API table (+tokens), feature list (+extension, +PWA bullet).

## [2026.09.017] — 2026-09-05

### Added

- **Installable PWA** (roadmap): `manifest.webmanifest` (standalone,
  theme `#0d1117`, icons 192/512/maskable + apple-touch-icon generated
  from `logo-mark.png`), service worker `/sw.js` with a **cache name
  versioned by VERSION** (instant invalidation on deploy). Strategy:
  navigation = network-first with cached-shell fallback (offline), static
  assets = cache-first refreshed in background, **API never cached**
  (freshness + personal data at rest). Offline banner (i18n ×4) shown on
  `offline` events. SW registration on https/localhost only (secure
  context — plain-HTTP LAN instances stay on classic serving).
- Tests: `tests/test_pwa.py` — manifest shape + maskable icon, SW
  versioned + API-uncached rule + icons 200, head links present.
  Verified live: SW active, reload while offline serves the cached shell
  with the banner, back online recovers.

## [2026.09.016] — 2026-09-05

### Fixed

- **Audit log leaked protected members' asset structure to the admin**
  (regression introduced in 2026.09.014): `_audit()` always wrote to the
  main database, and `detail` carried asset names and per-account row
  counts ("Création d'actif — #12 Appartement rue des Fleurs", "Export CSV
  — 318 lignes"). The admin could thus read the composition and activity
  rhythm of a protected member — exactly what the vault design forbids.
  Now: protected members only emit their **auth events** (login, failed
  login, logout, vault init/open) **without any detail**; their data
  events are not logged at all. An audit journal written *inside* the
  vault was considered and rejected: its holder owns the DEK and could
  rewrite it — a self-editable log is no audit trail.
- `export_csv`: removed local `import csv`/`import io` (module-level since
  the CSV import feature).

### Changed

- README « Backup & restore »: the audit section now documents the
  90-day retention of failed-login IPs (personal data) and the protected
  member policy.

### Tests

- `test_audit_hides_protected_member_asset_structure` (test_security.py):
  full protected journey (login → password → vault init → open → asset
  named « Bijou rue des Fleurs ») then asserts the admin sees only auth
  events with empty details for that member — and that standard members
  are still fully logged (no regression).

## [2026.09.015] — 2026-09-05

### Added

- **Bank CSV import for transactions** (roadmap « import CSV »):
  `POST /api/transactions/import-csv` + an « Import CSV » button on the
  Operations page. Header columns `date` + `libellé`/`description` +
  `montant` (or French-bank `débit`/`crédit` pair); separator `,`, `;` or
  tab (sniffed); UTF-8 with BOM tolerated; dates in ISO, `JJ/MM/AAAA` or
  `JJ.MM.AAAA`; amounts with French formatting (`1 234,56`, `1.234,56`,
  `€`). Negative amounts map to the inverted kind (deposit↔withdrawal,
  income↔expense). Amounts are stored positive — the kind carries the
  meaning, consistent with manual entries. In-batch and database
  duplicates are skipped (same date + amount + note), each import is
  audited, per-row errors are reported (first 5). Ownership enforced:
  imports target only your own assets.
- Tests: `tests/test_import_csv.py` (FR semicolon files, debit/credit
  columns, sign inversion, duplicate re-import, within-batch dedup,
  per-row errors, cross-owner 404, unauthenticated 401, unknown kind).

### Changed

- The private LAN production instance was upgraded to v2026.09.014
  (audit log + CSV exports), with a pre-upgrade SQLite backup kept under
  `backups/`.

## [2026.09.014] — 2026-09-05

### Added

- **Audit log** (review priority « journal d'audit »): `audit_log` table
  (90-day retention, purged at boot) records logins (success and failure),
  logouts, password changes, vault init/open, family member
  create/reset/delete, asset create/update/delete, valuation entries,
  transactions and income-rule mutations, JSON import/export and CSV
  exports. **Metadata only — never amounts or financial content**, so the
  log cannot leak protected-account data to the admin. `GET /api/audit` is
  admin-only; the Settings page shows the last 200 events (UTC, full-width
  panel with a fixed-layout table).
- **CSV exports** (Excel-ready, UTF-8 BOM): `GET /api/export/csv/{kind}`
  for `accounts`, `transactions`, `valuations`, `rules` — download links on
  the Settings backup panel. Each user exports only their own rows; audits
  every export.

### Changed

- The private LAN production instance (`docker-compose.lan.yml`) was
  upgraded to v2026.09.013, with a pre-upgrade SQLite backup kept on the
  host under `backups/`.

## [2026.09.013] — 2026-09-05

### Changed

- **Quote freshness is now visible** (review: « ne pas présenter une
  valorisation comme exacte si elle est ancienne »): `/api/accounts` now
  returns the source of the last valuation (`last_val_source`) and its age
  in days (`last_val_age_days`). The assets table shows “auto quote” under
  the date for market-tracked assets, and an orange warning with the age
  when the last automatic quote is older than 7 days — a failed price
  refresh is no longer silent. Manual assets stay uncluttered (their date
  already is the manual entry).

### Added

- **“Financial model & limitations” README section**: valuation semantics,
  no currency conversion, and an honest description of the index
  comparison — price indices without reinvested dividends (except the
  accumulating `IWDA.L`), no fees/taxes/FX, deposits simulated at the
  monthly index level, synthetic Livret A.
- **“Backup & restore” README section**: online SQLite backup procedure
  (containerized and bare), restore steps (boot migrations are
  idempotent), the warning that the JSON export is not a substitute for
  the database file, and deployment notes (private network, HTTPS, single
  worker).
- **HTTP security tests** (`tests/test_security.py`, 5 tests): every data
  endpoint rejects unauthenticated requests, admin-only endpoints reject
  members, standard members cannot init/open a vault, malformed imports
  are rejected, and an import colliding with another owner's asset id is
  rejected with a full rollback (no data loss on either side). Login
  cookie flags (HttpOnly, SameSite=lax) asserted.
- **Database migration tests** (`tests/test_migrations.py`, 3 tests): boot
  on a v010 database (vaults without `canary`, orphan clear-text rows
  purged, encrypted blob untouched), boot on an ancient schema (columns
  added, single user promoted to admin, accounts backfilled to the admin),
  and fresh-boot idempotence.

## [2026.09.012] — 2026-09-05

### Security

- **Vault plaintext never touches the disk anymore** (review follow-up): the
  vault flush used to write the decrypted database to
  `.vault_tmp_<user>.db` in `DATA_DIR` before encrypting — a crash between
  the backup and the unlink left the full plaintext behind. Flush now uses
  `sqlite3.Connection.serialize()` (in-memory snapshot), and cold opens use
  `deserialize()` — both without any temporary file. Runtimes whose SQLite
  lacks `SQLITE_ENABLE_DESERIALIZE` fall back to the temporary file, now
  removed in a `finally` block (no leftover on error or crash either).

### Added

- GitHub Actions CI (`.github/workflows/tests.yml`): the test suite runs on
  every push / pull request (Python 3.12, `pytest tests -q`).

### Changed

- Tests extended: the vault cycle now asserts that no `.vault*` plaintext
  file exists in `DATA_DIR` after init, after write+flush and after a cold
  open.

## [2026.09.011] — 2026-09-05

### Fixed

- **Event loop no longer blocked by market data fetches** (code-review
  finding #1): Yahoo/CoinGecko calls are plain `urllib` with a 12 s timeout —
  run in series inside async handlers they froze the whole server for every
  user. `POST /api/refresh-prices` now awaits `fetch_quote` /
  `_yahoo_chart` through `run_in_threadpool`, and `_fetch_bench_levels`
  (used by `GET /api/benchmarks` — even on a cold cache) fetches all missing
  index levels in a worker thread. Note: handlers were deliberately NOT
  converted to sync `def`: open vaults are shared in-memory SQLite
  connections created by async handlers — running them from threadpool
  threads would trip `check_same_thread` and interleave transactions.
- **Vault middleware limited to `/api/*` paths**: static assets (logo,
  favicon…) no longer trigger a session SQL query + rollback per file
  served. The flush itself was already guarded by the `_vault_dirty` flag —
  no change needed there.
- **Auto-lock for open vaults**: a protected vault now locks itself after
  `VAULT_IDLE_MIN` minutes of inactivity (default 30, `0` disables) instead
  of staying decrypted in process memory until session TTL (30 days). The
  front already handles the `vault_locked` response with its unlock prompt.
- Session timestamps switched from `datetime.utcnow()` /
  `utcfromtimestamp()` (deprecated in 3.12) to timezone-aware
  `datetime.now(timezone.utc)` — 7 call sites.
- Removed the stale duplicate `logo.png` (772 KB) at the repository root;
  only `public/logo.png` is used (Dockerfile, README).

### Security

- **Login rate limiting** (review finding #3): sliding window of
  `LOGIN_MAX_FAILS` (5) attempts per (IP, account) and
  `LOGIN_MAX_FAILS_USER` (10) per account over `LOGIN_WINDOW_SEC` (900 s) →
  HTTP 429 with `code: rate_limited`. Counters are per-account too, so
  proxies that hide client IPs don't defeat the lockout.
- **Login timing equalized**: a request for a non-existent user now runs a
  dummy PBKDF2 verify (was: short-circuit → account enumeration by
  response time). The double SELECT on the same row was merged into one.
- **Minimum password length raised to 12 characters** (was 6) on member
  creation, admin reset and password change — front validation updated in
  all 4 languages. Existing passwords are not invalidated.
- **Vault key proof on warm opens** (review finding #4): `vaults.canary`
  stores a fixed message AES-256-GCM-encrypted under the data key at vault
  init. `POST /api/vault/open` now verifies the presented key against the
  canary on every open — cold *and* warm — instead of trusting the cache.
  Legacy vaults (no canary) are retro-armed at their first cold open, which
  proves the key by actually decrypting the blob.

### Documented

- README now states the vault guarantee applies **at rest**, that an
  unlocked vault keeps its key in server memory only (auto-lock above), and
  that the app is **single-process by design** (`--workers` must stay 1 —
  open vaults are in-process state, review finding #6).
- New env vars documented: `VAULT_IDLE_MIN`, `LOGIN_MAX_FAILS`,
  `LOGIN_MAX_FAILS_USER`, `LOGIN_WINDOW_SEC`.

### Added

- First automated test suite (`tests/test_app.py`, 9 tests, pytest +
  TestClient): hash/verify round-trip, login (ok / wrong / unknown /
  rate-limit + release), password minimum, owner isolation between
  members, and the full protected-vault cycle (init → canary armed, data
  isolated from the main DB, blob decryptable to a real SQLite file, warm
  open with wrong key rejected, cold open wrong/right key, legacy
  retro-arm, idle auto-lock).

## [2026.09.010] — 2026-09-04

### Added

- **Encrypted vaults for protected accounts** (end-to-end at rest): when an
  admin creates a protected member, the account is sealed with a throwaway
  password (`must_change`). At first sign-in the member picks their own
  password; the browser derives a key (PBKDF2-SHA256, 600 000 iterations,
  WebCrypto) that wraps a random data-encryption key (AES-256-GCM). The
  server only ever stores: the salt, the wrapped key and an encrypted blob —
  readable by no one, including the admin, without the member's password.
- Protected members' data now lives in an in-memory SQLite database
  decrypted per session (server holds the key in memory only while sessions
  are open) and is re-encrypted to `vaults.blob` after every write. Existing
  clear-text data is migrated into the vault at first unlock.
- Password change on a protected account re-wraps the vault key in the
  browser (old password required); the admin reset endpoint stays forbidden
  by design — a lost password means the vault is unrecoverable, only
  deletion remains.
- Page reload on a protected session asks for the password again to unlock
  the vault (the key never persists in the browser).

### Security

- Server storage of protected accounts now holds zero plaintext: verified
  by tests (no member rows in shared tables, secret names absent from the
  database file, blob not decryptable without the key).
- New dependency: `cryptography` (AES-GCM for the vault blob).

## [2026.09.009] — 2026-09-04

### Fixed

- Family panel redesigned as 3 columns (member with mode badge, patrimony,
  actions): icons no longer bleed into the patrimony column (the culprit was
  `display:flex` on a `<td>`, which collapses the cell to its minimal width;
  buttons are now inline-flex inside a right-aligned cell).

## [2026.09.008] — 2026-09-04

### Fixed

- Family rows: action buttons now use flex layout, so the delete icon of a
  standard member (2 buttons) stays inside the panel frame.
- Asset-class labels in charts/legend: 'Real estate' / 'Crowdfunding' no
  longer fall back to raw internal keys in non-French UIs.

## [2026.09.007] — 2026-09-04

### Fixed

- Family panel rows with two actions (standard member): delete (🗑️) and
  reset buttons no longer overlap/overflow their cell (compact buttons +
  wider actions column).

## [2026.09.006] — 2026-09-04

### Fixed

- Family panel in Settings: member table no longer overflows the panel
  card (constrained table layout + compact rows, long names wrap).

## [2026.09.005] — 2026-09-04

### Added

- **Family mode (Phase 1)** — multi-user accounts on one instance:
  - One admin (original account, data preserved) + N members, fully isolated
    data (every asset, transaction, valuation and rule is owner-scoped)
  - Admin **Family panel** (Settings): create members with a mode chosen at
    creation — **standard** (password resettable by admin, data visible) or
    **protected** (no reset possible — a forgotten password means a lost
    account — data never visible, deletion only)
  - Admin consolidated dashboard view: toggle « My net worth » /
    « Whole family » (net worth, history and index benchmarks aggregated over
    the admin + all *standard* members — protected members never contribute)
  - Members log in with their own credentials; each space is 100 % isolated
    (no cross-reading possible, even by the admin, at API level)
- Notes: protected accounts are Phase-1 enforced at application level
  (rules + isolation); true end-to-end encryption of protected data
  (WebCrypto, server-stored blobs) is the planned Phase 2.

## [2026.09.004] — 2026-09-04

### Added

- **Multilingual UI — FR / DE / LU / EN** (fallback FR, like other
  LostInTheBugs sites): asset classes, asset/transaction/income tables,
  modals, benchmark panel, settings and messages are fully translated
- Language picker on the **login screen** (FR / DE / LU / EN pills) and a
  language selector in **Settings** — choice is persisted per device,
  initial language follows the browser (fr/de/lb/en detection)
- Localized number/date formats (currency, percentages, month names) and
  localized asset-class labels everywhere (charts, badges, filters)

## [2026.09.003] — 2026-09-04

### Added

- **Transactions** (Operations page): deposits / withdrawals / income /
  expenses per asset; they become the source of truth for the invested cost
  (deposits − withdrawals) as soon as an asset has any
- **Passive income**: recurring income rules (monthly / quarterly / yearly /
  custom), 12-month expected-income calendar, actual income bar chart
  (last 12 months from income transactions)
- **Automatic valuations**: assets with `valuation_mode=auto` (symbol +
  quantity) get market prices — crypto via CoinGecko (EUR), stocks/ETFs via
  Yahoo Finance; daily valuation insert + monthly history backfill on first
  sync; one-click "Refresh prices" button
- **Index benchmarks**: S&P 500, Nasdaq Composite, MSCI World (IWDA),
  STOXX Europe 600, CAC 40, Livret A — real monthly levels (Yahoo),
  annualized performance over the user's investment span, and a simulation
  of what the actual deposits would be worth if invested in each index
- Multi-row notes under assets (manual cost warning when transactions exist)

## [2026.09.002] — 2026-09-04

### Added

- First runnable version: FastAPI + SQLite backend, single-file dark SPA
- Multi-asset-class model (8 classes: cash accounts, savings, stocks &
  life insurance, real estate, crowdfunding, crypto, precious metals,
  other), each asset tracks cost basis + valuation history
- Net worth dashboard: hero total (value/cost/gain), per-class donut with
  legends (share %, amount, asset count), stacked net-worth evolution chart
  (12m–10y window)
- Assets page: searchable/filterable table, add / edit / delete, one-click
  valuation updates
- Discreet mode (hide amounts, share % only, index-100 chart)
- Auth (cookie session, pbkdf2), change password, JSON backup / restore
- Demo seed (`SEED_DEMO=1`: one asset per class, 2020–2026 monthly history)
- Branding assets: logo (transparent), logo mark, favicon
- Dockerfiles: LAN compose (port 8020) + demo compose (Traefik labels)

## [2026.09.001] — 2026-09-04

### Added

- Project scaffold: repo conventions, README, logo
- Competitive scope analysis of wealth-tracking tools (local planning doc)
