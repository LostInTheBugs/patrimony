# Changelog

All notable changes to Patrimony are documented in this file.

## [2026.09.056] — 2026-09-09

### Added — Loans module backend (💳 Crédits)

First step of the dedicated-pages chantier (design
`claude/design-credits-2026.md`, validated by Fred): liabilities are now
tracked per loan instead of living on the real-estate account row.

- New `loans` table (owner-scoped, vault-aware like other data tables):
  type (`immo`/`auto`/`conso`), lender, currency, declared remaining
  principal (source of truth), annual rate, monthly payment excluding
  insurance, optional monthly insurance, start date, optional link to the
  real-estate account (`account_id` → equity display), soft-delete flag.
- Boot migration: legacy `accounts.loan_*` columns (v033 linked loan,
  immo only) are migrated into `loans` rows and neutralized on the
  account — idempotent, also run when a protected vault is opened. Demo
  seed now creates the loan as a module row.
- API: `GET/POST/PUT/DELETE /api/loans` (family/member scoping, soft
  delete, validation incl. monthly payment covering first-month interest),
  `GET /api/loans/{id}/schedule` (deterministic French-amortization
  schedule, computed on demand — no stored table), `POST
  /api/loans/{id}/recompute` (theoretical remaining vs declared, never
  applied automatically).
- `src/loans.py`: single amortization engine (exact port of the former JS
  curve), shared by list projections, schedule and recompute.
- `/api/summary`: `total_debt`/`net_worth` now aggregate the active loans
  (multi-currency via latest ECB rate ≤ today, `fx_missing` listed) +
  new `debt` block `{total_eur, per_type, part_pct, fx_missing}`.
- Export/import JSON round-trip carries the loans section; deleting an
  account unlinks its loan (loan survives, `account_id` nulled).
- Legacy API compatibility: creating a real-estate account with
  `loan_principal > 0` materializes the loan into the module.

## [2026.09.055] — 2026-09-08

### Added — Investissements UI: « 📈 Actions » & « 🛡️ Assurance vie » pages

Frontend of the Actions/AV chantier (backend v054). No backend change.

- Nav: two entries after Actifs — `Actions` (PEA/CTO) and `Assurance vie`.
- Actions page: net cards (value / cost / gain / dividends YTD), PEA &
  CTO sections with per-account rows (value, cost, gain, dividends,
  last valuation date + stale warning > 35 days) expanding to full
  per-line tables (symbol, label, qty, cost basis, cached price, EUR
  value, gain/Loss %, dividends) — cache prices only, never network at
  render; `↻ Refresh quotes` button (admin) calls `/api/actions/refresh`.
- AV page: net cards (value / cost / gain / withdrawals YTD) + contracts
  table (funds € / UC badge, value, cost, gain, contributions,
  withdrawals, dividends, last valuation).
- i18n fr/en/de/lu (keys parity checked programmatically), member views
  read-only, discreet mode honoured (mon()).
- Verified end-to-end in browser on the demo seed (EN + FR switch):
  PEA €26 384,73 (+31,92 %) · CTO €16 220,67 (+35,17 %) — AI.PA line
  Air Liquide · 12 · PRU 141,50 · cours 168,40 · 2 020,80 · +322,80
  (+19,01 %) · div 38,40 ; AV Linxea Avenir funds € 30 447,95 (+21,79 %).

## [2026.09.054] — 2026-09-08

### Added — Investissements backend (Actions PEA/CTO & Assurance vie pages)

First step of the Actions/AV chantier (design:
`claude/design-actions-av-2026.md`, never pushed): dedicated overview
routes — no schema change, everything reads existing
accounts/positions/dividend_events/prices/transactions/valuations.

- `GET /api/actions/overview`: PEA & CTO sections, per account (value =
  latest valuation — authoritative like the dashboard, cost from
  transactions or cost_basis, gain/%, inflows/outflows, dividends total &
  YTD) with full per-line positions payload (quantity, PRU, cached price,
  EUR value, gain, dividends).
- `GET /api/av/overview`: AV contracts (class épargne → funds €, bourse →
  UC), same metrics + withdrawals YTD.
- `POST /api/actions/refresh`: fresh Yahoo quotes for every active
  position symbol of PEA/CTO (once per symbol) into the `prices` cache —
  never writes valuations.
- Demo seed: PEA/CTO/AV wrappers set, new CTO « Boursorama » with 2
  positions + dividend events + cached prices, new AV « Linxea Avenir »
  (funds €).
- EUR conversion mirrors /api/summary (fx at valuation date, `fx_missing`
  list); member views read-only; 5 deterministic tests (191 total).

## [2026.09.053] — 2026-09-08

### Fixed — Current prices from DefiLlama for all tokens (staking shares)

The scan valued every token with Blockscout's `exchange_rate`, which is a
stale provider cache and — for staking tokens (stETH, eETH, …) — is
expressed per raw share: stETH appeared at ≈ ETH/taux-de-part, understating
the position by exactly the share rate (×1.31 stETH, ×1.49 eETH on the
real wallet).

- `scan_portfolio` now queries DefiLlama current prices for EVERY token
  with a balance (chain:`contract`) and for native coins
  (`coingecko:<id>`, since /prices/current ignores bare chain keys);
  Blockscout `exchange_rate` remains only as fallback.
- `_fetch_defillama_current_prices` now accepts full-prefix query strings
  (`ethereum:0x…` / `coingecko:ethereum`) grouped per prefix.
- Real wallet before/after (same refresh): stETH 4 152 $ → 5 419 $,
  eETH 1 755 $ → 2 620 $, ETH repriced live; wallet total 8 988 $ →
  11 094 $. History/series untouched; scan stays authoritative.

### Tests

- +2 deterministic (no network): llama override on staking token + native
  + fallback; native key mapping. 186 total.

## [2026.09.052] — 2026-09-08

### Added — Native movements capture (history fix)

- New `fetch_native_movements`: per wallet chain (present at last scan) it
  captures the native-coin ledger the ERC-20-only engine was blind to:
  external transactions (`value` received/sent), gas fees paid, and
  internal transactions (`/internal-transactions`, to/from filters, stable
  per-tx `index`). Legs are stored in `cw_transfers` with sentinel
  `log_index` (≥ 1e9, per-kind ranges) that can never collide with real
  ERC-20 log indexes; dedup is idempotent.
- Legacy native rows imported from the CWT database (partial capture,
  real log_index 0) are purged per chain on first native pass, so the
  rebuilt history has a single, complete representation of native flows.
- Wired into `refresh_wallet` between transfers and price enrichment
  (report key `native`); demo wallets never hit the network.
- On the real migrated Ledger wallet this removes the ~6 k$ phantom
  (historical series end 18.9 k$ → 12.9 k$ on the migrated base with the
  same token rows); residual series/scan gap is staking-share semantics
  (stETH/eETH raw-share balances), pre-existing, out of scope here —
  current value stays scan-authoritative.

### Tests

- 2 new deterministic tests (no network): native leg directions/amounts/
  sentinel sequences + idempotence; refresh wiring with scan native
  chains. 184 total.

## [2026.09.051] — 2026-09-08

### Added — Crypto wallets page (UI)

- New ₿ « Crypto » section (nav entry, after Actifs): header cards
  (wallet value / total cost / gain / tracked wallets, USD), wallets table
  (label, truncated address, value/cost/gain with reconciliation badge,
  chain chips + token/chain counts, last sync), expandable per-wallet token
  detail (chain, token, category, balance, price, value), add-wallet form
  (public address), manual refresh button with live status, wallet removal.
- Values in USD (narrow $ symbol, discrete mode respected); balances are
  the on-chain scan result — the estimated-transfer history gap is exposed
  as a badge with tooltip; i18n ×4; admin actions hidden in member view;
  demo wallet seeded with synthetic scans.
- Demo seed now materialises cosmetic scan rows + wallet last state.

## [2026.09.050] — 2026-09-08

### Added — Crypto wallets module (non-custodial, backend)

The Crypto Wallet Tracker engine is now a native Patrimony module feeding
the ₿ Cryptocurrencies class through derived auto accounts (one per wallet,
locked, zero double entry).

- **`src/crypto.py`** (~1 700 lines, ported from the CWT engine):
  - 22 EVM chains (Blockscout v2, parallel scans, native coin endpoint,
    pagination, spam filter at fetch time, per-item resilience);
  - DefiLlama current prices (batched, chain-prefixed) and historical daily
    series (200-day windows, idempotent `cw_price_cache`);
  - token transfers with dedup on `(wallet, chain, tx_hash, log_index)`;
  - daily series rebuild (unified timeline, UTC-noon alignment,
    backward-extrapolated prices, per-token weighted-average cost basis,
    orphan-token injection, live-portfolio anchoring);
  - **current value is scan-authoritative**: the « today » valuation and
    wallet overview use the on-chain balances; the reconstructed history is
    flagged with a `recon_pct` reconciliation field when it diverges;
  - auto accounts (class `crypto`, `valuation_mode='auto'`) with month-end
    EUR grid (ECB rates) + daily refresh at boot and 06:00 (thread, opt-out
    `PAT_CRYPTO_AUTO=0`), manual refresh button endpoint;
  - offline deterministic demo wallet (synthetic ETH history, no network);
  - vault seal copies, full export/import payload.
- **Schema**: 5 owner-scoped tables `cw_wallets/cw_transfers/cw_history/
  cw_price_cache/cw_scans`.
- **API**: 8 routes `/api/cw/*` (wallets CRUD, refresh + status, overview,
  tokens, monthly history) with family read-only views.
- **`scripts/migrate_crypto.py`**: one-shot CWT backup → module import with
  parity checks (wallets, per-chain transfers, history rows, final
  value/cost to the cent, date bounds, orphans, dedup integrity, auto
  accounts). Validated 17/17 against the real prod CWT backup.
- 18 new tests (`tests/test_crypto.py`) — suite at 182 passing.

> Note: the standalone CWT instance on the private LAN production server
> runs an outdated engine (it
> ignores staked positions such as stETH/eETH and its historical series
> diverge from on-chain balances); the module reads primary sources
> (Blockscout/DefiLlama) directly.

## [2026.09.049] — 2026-09-08

### Changed
- Dashboard class legend: the Crowdfunding class now counts its derived
  platform accounts as « plateformes » instead of « assets » (the class is
  fed automatically by the Bricks.co / La Première Brique accounts) — i18n
  ×4.

## [2026.09.048] — 2026-09-08

### Added
- Crowdfunding sub-views « Delays » (platform late badge + auto-overdue list,
  severity buckets, received vs expected, months without interest) and
  « Performance » (per-platform table: deposited / balance / in projects /
  wealth / gain / annualised since first buy, editable deposited & balance —
  LPB in-projects value stays auto) — i18n ×4, discret, mobile.
- « Synchronisation » sub-view: setup steps for the capture extension and the
  latest ingestion report (captures, matched/enriched projects, filled fields,
  site-vs-export conformity table).
- Operations list pagination (200/page, all pages fetched server-side
  limit/offset).
- API token UI: new « crowdfund » scope (selectable, dedicated badge).
- Extension « Patrimony Capture » 1.2.0 : content script `content-cf.js`
  (porté du Crowdfunding Tracker — parseurs Bricks/LPB éprouvés), section
  popup « Envoyer / Capturer l'onglet », options dédiées (jeton portée
  crowdfund, autorisations 3 domaines) — envoi POST /api/cf/sync/ingest.

## [2026.09.047] — 2026-09-08

### Added
- Crowdfunding section (🧱 nav entry, page with Overview / Projects /
  Operations sub-views) for the crowdfunding module introduced in v046:
  - Overview: stat cards (total invested, capital outstanding, interest
    received/accrued, losses + latent, net gain), platform doughnut charts
    with count+amount legends, and a note on the automatic global-wealth
    linkage (no double entry).
  - Projects: searchable/filterable table (platform, status incl. late
    payment, real rate, ≈ estimated due date, royalty 🎵 badge, received
    interest) with full create/edit modal and delete.
  - Operations: platform exports (Bricks.co / La Première Brique) import,
    kind/status filtering, in/out/net chips.
- i18n FR/DE/LU/EN for the whole section (~110 new keys per language);
  full integration with discret mode, member consultation view and mobile
  layout (bottom nav bar includes the new entry).

### Notes
- The operations sub-view lists the 1000 most recent operations; full
  pagination is planned with the v048 additions (delays/performance/sync).

- Crowdfunding module: per-project tracking of equity/loan crowdfunding
  platforms (Bricks.co, La Première Brique) directly inside Patrimony —
  replaces the standalone Crowdfunding Tracker app (single source of truth).
  Backend: owner-scoped tables (`cf_projects`, `cf_operations`,
  `cf_platforms`…), full REST API under `/api/cf/*` (projects CRUD,
  operations history + xlsx platform-export import, platform metadata,
  overview with annualised returns, JSON export/import), extension sync
  endpoints (`POST /api/cf/sync/ingest`, `GET /api/cf/sync/report`) gated by
  a dedicated `crowdfund` API token scope.
- Derived "auto accounts" seam: each platform is materialised as a
  read-only auto-valuation account of the crowdfunding asset class
  (current value + month-end series + initial deposit) so dashboards,
  history, evolution and index simulations include the module without any
  duplicate manual entry. Manual crowdfunding accounts are rejected (400).
- One-shot migration script `scripts/migrate_crowdfunding.py` with built-in
  parity checks (counts, invested totals, platform values, LPB capital due).
- Vault integration: crowdfunding tables of a protected member are sealed
  in their encrypted vault (copy on init, clear-data purge).
- Demo seed now includes fictional crowdfunding platforms + projects.
- 12 new tests (164 total).

### Changed
- `/api/history`: per-class series are summed per month across all accounts
  of the class (a class can now hold several accounts; previously each
  account appended its own value, misaligning the series).
- Crowdfunding module tables live in the shared data schema (main database
  and vault memory databases).

### Fixed
- Manual-account, valuation and transaction routes refuse writes on module
  managed (auto) crowdfunding accounts.

- **Tax estimates consent gate (Fred 2026-09-07)** — the tax part
  (⚖️ Household tax assumptions in Settings + per-asset tax estimates)
  is now **disabled by default** until the member explicitly acknowledges
  the disclaimer:
  - Full 5-point notice (educational demo, no official/legal value, not tax
    advice, simplified rules, acknowledgment) shown inside a red dashed
    frame on both surfaces (settings panel + estimate modal).
  - Real validation: the user must **type the exact phrase** of the active
    UI language (`je confirme` / `I confirm` / `ich bestätige` / `ech
    bestätegen`) — the enable button stays disabled until the input matches
    (normalized: case, spaces, diacritics, so `ich bestatige` is accepted).
  - Consent stored per member in `localStorage` (`pat_tax_consent_v1_<user>`,
    ISO date), versioned (v1 — bump to re-ask if the notice text changes),
    revocable via a discreet « accepted on {date} — Revoke » line under the
    settings panel; estimates asked before consent show the gate first and
    run immediately after confirmation.
  - 12 new i18n keys ×4 languages; `#taxBody` is hidden by default in the
    markup itself (disabled even before the first JS render).
  - New global `.btn:disabled` style (opacity .45, grayscale, not-allowed) —
    disabled buttons were previously rendered identical to active ones.

## [2026.09.044] — 2026-09-07

### Fixed

- **Mobile pass (Fred's design 2026-09-07, no redesign — CSS only)** —
  audited and fixed every surface at a 390 px viewport (iPhone, DPR 3):
  - Root cause of page-wide horizontal overflow: `#main` is a flex item
    without `min-width:0`, so it followed the max-content of its children
    (tables) — fixed globally; `document.scrollWidth` is now 390 on every
    page.
  - Bottom bar: nav was left without `flex-direction:row` after the
    v2026.09.043 patch (7 links stacked vertically over half the screen) —
    restored; nav is now a swipeable row (`overflow-x:auto`, `flex:1`,
    `flex:0 0 auto` links) with the Discreet/Log out actions pinned on the
    right; bar height 52 px, safe-area padding kept.
  - ≤ 640 px: inputs/selects/textareas at 16 px (iOS auto-zoom below 16),
    `.grid2` and `.formrow` single-column, stat cards 2×2 (`min-width:0`),
    params grid single column, search full width, hero/page-title tuned,
    modals ≤ 96vw with `.wide` tables scrolling internally
    (`min-width:520px`).
  - Visual verification at 390 px (CDP device emulation, fresh browser
    session): zero document overflow on login/dash/evolution/assets/
    transactions/income/fire/settings; member view banner wraps cleanly;
    screenshots reviewed by eye for dashboard, assets, FIRE, settings and
    the account + positions modals.

## [2026.09.043] — 2026-09-07

### Added

- **Per-member family view (read-only consultation mode)** — Fred's design
  2026-09-07 (« vue détaillée par membre pour la famille »):
  - Server: `ScopeError` (+ handler) and `_member_target()`; the existing
    `_visible_owners` scope gains `member=` — admin only, target must be a
    standard member; a protected account answers 404 indistinguishable from
    an unknown one (vault guarantee untouched). `member` param added to the
    GET routes: summary, history, evolution, benchmarks,
    refresh-benchmarks, accounts, transactions, income-rules,
    income-calendar, income-actual, cashflow. Writes are never scoped
    (structural guarantee); routes without the param ignore it.
  - Front: 👁 button per standard member in the family list → consultation
    mode on the member's own pages (Dashboard, Evolution, Assets,
    Transactions, Income) with a banner « 👁 Consultation du membre {name}
    (lecture seule) — ✕ Revenir à mes données »; nav reduced (no FIRE, no
    Settings, no Moi/Famille scope switch); write buttons hidden by CSS;
    api() rejects any non-GET while in member mode and injects `member=`
    on GET (single injection point); FR/DE/LU/EN.
  - Visual recipe (real data): member kelly with 2 assets (11 250 €) viewed
    by the admin — hero shows kelly's net worth, banner FR/EN/DE/LU
    verified, no action button visible, write guard active, exit returns to
    Settings.

## [2026.09.042] — 2026-09-07

### Added

- **Monte-Carlo FIRE by bootstrap** (Fred's design, 2026-09-07):
  - `src/mc.py` (pure): 12-month rolling-block bootstrap — each simulated
    year draws its nominal return from the real monthly series (moving
    windows of 12 calendar months, drawn with replacement), preserving
    annual autocorrelation without freezing cycles; every trajectory runs
    through `fire.simulate(returns=path)` (new optional param, `None` =
    constant rate, behavior unchanged, 147 tests).
  - Series source: the ETF world benchmark IWDA.L fetched at Yahoo's max
    depth via the existing infra, cached in `index_levels` under the
    reserved `mc:<key>` key (invisible to the benchmarks comparator);
    guardrails: < 5 blocks → 502, < 60 blocks → "indicative" flag.
  - `GET /api/fire/montecarlo`: same contract as `/fire/simulate`
    (monthly amounts, same ranges, fire_* member defaults) + `index`
    (default iwda), `n_sims` (100-5000, default 2000), `seed`
    (reproducibility). Output: success rate by horizon (capital never
    ≤ 0, at 10y steps up to max_years), median capital at the final
    horizon, median depletion year among failures.
  - FIRE page: 🎲 Monte-Carlo block under the deterministic result —
    horizons success rates, P50, median depletion, "short series" warning
    (FR/DE/LU/EN). Visual recipe with the real IWDA series across the four
    languages.

## [2026.09.041] — 2026-09-07

### Changed

- **SQLite in WAL mode** (Fred's roadmap): the main database is initialized
  with `PRAGMA journal_mode=WAL` (persistent) + `busy_timeout=5000` —
  crash-robust writes (no truncated `.db`) and reads never blocked by a
  writer (foundation for background refreshes to come, e.g. Open Banking).
  Vault in-memory databases are unaffected. The sqlite `backup()` ritual
  (pre-version snapshots) reads the full state of a WAL database — proven
  by a dedicated test; 141 tests.

## [2026.09.040] — 2026-09-07

### Changed

- **Tax UI: explicit non-value framing + playful trend gauge** (Fred):
  - Every tax surface now carries the same strong notice — modal 🧮 banner at
    the top (replacing the discreet footnote), and the tax-assumptions panel:
    « Démonstration — aucune valeur officielle : une simple idée de ce que
    serait votre impôt, et cela ne remplace pas un fiscaliste. » (FR/DE/LU/EN,
    same `txEstNote` key — one wording everywhere).
  - New 5-segment trend gauge under the estimate breakdown: qualitative level
    from the share of levies (income tax + social contributions + extra tax)
    in the gross gain — thresholds 10/20/35 % → levels
    « Rien à payer — tranquille 😎 » → « Ouhla, vraiment beaucoup… fais gaffe 🚨 »
    (FR/DE/LU/EN). A trend, not a rate: humorous on purpose, framed by the
    banner above. Segments stay grey when an estimate is not possible.
  - All front-only; 140 server tests + i18n check + visual recipe across the
    four languages.

## [2026.09.039] — 2026-09-07

### Fixed

- **CSV exports: class/kind values finally follow Luxembourgish** — the
  value tables in `src/transfer.py` dated from v025 with a `lb` language
  key that the UI never sends (the front uses `pat_lang` fr/de/lu/en), so
  `Accept-Language: lu` silently fell back to French *values* while headers
  were already translated. Tables now use the `lu` key with wording aligned
  on the front i18n (Lafend Konten, Spuerkonten, Aktien &
  Liewensversécherung…, Abezuelung/Auszuelung/Akommes/Fraisen / Ausgab) and
  `_CSV_LANG_ORDER` is `(fr, en, de, lu)`. Identifiers stay canonical and
  re-importable. Regression test added; 140 tests.

## [2026.09.038] — 2026-09-07

### Changed

- **FX & benchmarks extracted from the monolith** (v037 follow-up): ECB
  rates (`src/fx.py` — EUR-currency lookups with manual override priority,
  freshness warning, daily/historical XML parsers, blocking fetches, stores)
  and index benchmarks (`src/bench.py` — cashflows, level fetching split
  needs→charts→store so network runs in the threadpool while SQLite writes
  stay on the handler thread, synthetic Livret A curve, per-index
  annualized + same-deposits simulation, user line) are now pure domain
  modules. `src/app.py` keeps the HTTP wrappers: 3,445 → 3,178 lines. Zero
  behavior change — every message, status code and audit event preserved;
  140 tests pass (FX conversions, ECB parsers, history backfill, benchmarks
  simulation).

## [2026.09.037] — 2026-09-07

### Changed

- **Data transfers extracted from the monolith** (v036 follow-up): the JSON
  export payload + transactional restore (`export_data`/`do_import`), the
  bank-CSV transaction importer (separator sniffing, FR amount/date
  formats, dedup, per-row errors) and the localized CSV exporters (headers
  via `src/l10n`, class/kind values via internal tables, canonical
  identifiers kept re-importable) now live in `src/transfer.py` — a pure
  domain module; connections are always passed in (main DB or an open
  vault's in-memory DB, routed by the caller). `src/app.py` keeps only the
  HTTP wrappers (guards, audit, status codes): 3,726 → 3,445 lines. Zero
  behavior change — every message, status code and audit event preserved;
  140 tests pass (encrypted round-trip, CSV import/export, security).

## [2026.09.036] — 2026-09-07

### Changed

- **Vault domain extracted from the monolith** (external review follow-up):
  the encrypted-vault block (in-memory state, AES-256-GCM blob handling,
  DEK canary, `serialize()`-only persistence, auto-lock GC, recovery-key
  arming and proof checks) now lives in `src/vault.py` — a pure domain
  module with no FastAPI/HTTP import; the shared data schema moved to
  `src/schema.py` (main database AND vault memory use the same source).
  `src/app.py` keeps only the HTTP layer: guards, audit events and the
  end-of-request persistence middleware. Zero behavior change — every error
  message, audit event and state transition is preserved; the full suite
  (vault cycle, auto-lock, recovery, security, migrations, loans/settings in
  vaults) passes, with one white-box test updated to the new module API.

## [2026.09.035-c1] — 2026-09-07

### Added

- **Explicit "not tax advice" note on every tax estimate** (external review
  recommendation): the 🧮 estimate modal now ends each breakdown with the
  line "⚠️ Personal estimate — not tax advice." (FR/DE/LU/EN), below the
  data, next to the existing "if I liquidate today" framing. The operator
  demo disclaimer is untouched (it covers fictional data, not advice).

## [2026.09.035] — 2026-09-06

### Added

- **Server-side localization (remaining "exports/localization" lot)**. The
  API used to emit every error message in French regardless of the UI
  language; CSV exports localized values (v025) but not their headers; the
  demo disclaimer was French-only.
  - `src/l10n.py` (generated by a build script, integrity-tested): 77 error
    messages (64 direct + 5 interpolated templates + 2 import-row patterns
    + 6 helper messages), 11 CSV header columns and the demo disclaimer,
    each in EN/DE/LU — keys are the exact French strings the code emits.
  - Translation middleware (HTTP, JSON responses only): reads
    `Accept-Language`, translates `detail`/`disclaimer` on the fly, falls
    back to French without a header (no existing test/behavior changes);
    exports excluded, any failure returns the response untouched. The
    French strings stay in the code as the readable source of truth.
  - Interpolated messages (password length, rate-limit retry, CSV row
    errors…) are matched by template regexes and re-filled per language,
    including the segmented "No rows imported — row 2: …" import summary.
  - CSV exports: column headers now follow the language (values already
    did since v025). Front: `api()` and the CSV download send the chosen
    UI language (`Accept-Language: LANG`), so the server follows the
    in-app language switch, not just the browser's.
  - Out of scope (documented): audit log stays French (internal security
    journal), user data (asset names) untranslatable.
  - Tests: `tests/test_l10n.py` (5) — dictionary integrity (77 keys × 4
    languages, identical placeholders), direct/template/segmented
    translation, per-language API errors through the middleware, CSV
    headers per language, localized disclaimer, large JSON responses
    untouched. 139 passing.

## [2026.09.034] — 2026-09-06

### Added

- **Deterministic FIRE simulator (roadmap ②, validated by Fred: "deterministic
  simulator — target age/date, nominal AND real return, inflation, scheduled
  withdrawals, annuities/pensions + sensitivity curves", no Monte-Carlo).**
  - `src/fire.py`: pure engine (zero I/O): French-style year-by-year model —
    accumulation with inflation-indexed savings, financial independence when
    capital ≥ (expenses − annuities)/SWR (both indexed), then withdrawal phase
    with honest erosion/exhaustion when the withdrawal rate exceeds the real
    return (nothing hidden); "already independent" when annuities ≥ expenses.
  - `GET /api/fire/simulate` (principal, savings/expenses/pension per month,
    return/inflation/SWR %, defaults resolved from the member settings).
  - Household assumptions persisted per member like the tax ones (settings
    keys `fire_return` 5 %, `fire_inflation` 2 %, `fire_swr` 4 %,
    `fire_birthyear` optional; range-validated, isolation between members).
  - New 🔮 page (FR/DE/LU/EN): prefilled from real data (net worth from the
    summary, expenses/savings monthly totals from the recurring rules —
    income − expense kinds), assumptions saved back to settings; verdict
    (independence year + age when birth year is set, "never" case, erosion /
    exhaustion warnings), capital at the FIRE point, net expenses to cover,
    real return after inflation, sensitivity ±2 pts (3/5/7 %), Chart.js
    curve of projected capital vs. target capital until the FIRE point,
    methodology note (deterministic, taxes on withdrawals excluded).
  - Tests: `tests/test_fire.py` (6) — engine (accumulation, already
    independent, exhaustion, never-FIRE, sensitivity ordering) + route
    (settings defaults, overrides, ranges, member isolation). 134 passing.

## [2026.09.033] — 2026-09-06

### Added

- **Loan linked to the property (option ③-a: liabilities live inside the
  asset — no "debt" class).** Real-estate assets can carry their mortgage:
  `accounts.loan_principal/loan_rate/loan_monthly` (validation: immo only,
  non-negative, rate ≤ 100%; clearing the principal resets the loan).
  - Summary: `total_debt` + `net_worth` (= total_value − total_debt) —
    strictly additive, 0 loan → net_worth == total_value (nothing else
    changed: classes/donut/history remain gross assets; the hero shows the
    net with a fine "Assets − liabilities" line when debt > 0).
  - Assets table: value cell shows the debt and the clickable equity
    (value − remaining principal); footer gains "Liabilities" and "Net
    total (after loans)" rows when the selection holds loans.
  - Asset modal (immo): loan fields (remaining principal, annual rate,
    monthly payment) + note (update the principal like the property value;
    track the payment as a recurring expense for cash-flow).
  - 🧮-style 📉 modal: French-amortization projection from the stated
    principal (months-to-go formula, monthly simulation aggregated by
    year), yearly table (principal paid / interest / remaining), Chart.js
    curve of the remaining principal, estimated end date + remaining
    interest; explicit warnings when the payment does not cover interest
    (never repaid) or is missing.
  - Demo seed: the rental flat now carries a realistic loan (€92k @ 2.8%,
    €520/mo) — net worth visible on the public demo.
  - JSON export/import round-trips the loan columns; vault (protected
    members) stores them with the rest of the data (nothing in clear).
  - Tests: `tests/test_loans.py` (5, isolated members per test) — immo-only
    validation, CRUD + summary net, CHF conversion at the valuation rate,
    export/import round-trip, protected vault white-box. 128 passing.

## [2026.09.032] — 2026-09-06

### Added

- **Household tax assumptions (settings), per member.** The tax engine
  does not know the household income, so its marginal-rate inputs were
  hard defaults; they are now editable and persisted per member.
  - New `settings(member, key, value)` table (both main DB and vaults,
    copied at vault init, exported/imported in JSON with the member
    forced to the importer). `GET/PUT /api/settings` — values: `tmi_lu`
    (default 42.8%), `tmi_fr` (default 0 = unset, progressive options
    refused until set), `married` (doubled allowances), `av_150k`
    (premiums ≤ €150k, AV 7.5% strate), `substantial` (LU holding ≥ 10%
    as the default). Validation 400 out-of-range; isolation per member.
  - `GET /api/tax-estimate` now feeds the engine from the member's
    settings, with per-call overrides `opt=2op|3cn` (progressive options
    — kept strictly separate, 2OP for CTO/PEA/AV, 3CN for crypto) and
    `sub=0|1` (LU substantial holding).
  - UI: ⚖️ "Tax assumptions" panel in Settings (visible to every member,
    routed to the vault for protected accounts) + per-estimate toggles in
    the 🧮 modal (simulate the flat-rate option / holding ≥ 10%) that
    re-fetch the breakdown without closing. i18n ×4.
  - Fix (v031 bug): JSON import dropped `accounts.tax_country` (the
    INSERT listed columns without it) — restored, regression-tested.
  - Tests: `tests/test_settings.py` (5) — CRUD + defaults, validation &
    per-member isolation, engine wiring (settings respected, sub override,
    married → €100k allowance, speculation at the set marginal rate,
    2OP refused without TMI then computed at 30%), protected settings live
    in the vault (nothing in the main DB), export/import round-trip keeps
    tax_country and settings. 123 passing.

## [2026.09.031] — 2026-09-06

### Added

- **Tax engine `src/tax/` — pure module, versioned rulesets.** "If I
  liquidate today" estimate per asset (FEUILLET-FISCAL-2026.md is the
  normative v1 source, validated by Fred after cross-checking every rule
  against official sources; PDF in the repo docs, never pushed).
  - `src/tax/` is pure calculation: zero I/O, no dashboard dependency.
    `compute(TaxInput)` → `TaxResult` with `gross_gain → losses →
    taxable_gain → income_tax → social_contributions → extra_tax →
    estimated_net_gain`, a line-by-line breakdown where **every line
    carries its auditable rule id** (FR_CTO_PFU_IR_2026 …), plus
    `warnings[]` and `assumptions[]`. Input takes country, asset class,
    wrapper, acquisition date, cost, value, losses, tax options
    (progressive 2OP/3CN) and household assumptions (marginal rates,
    married, premiums cap, substantial holding).
  - **Rules are versioned by (country, year)**: `rules_fr.py` /
    `rules_lu.py` behind a registry (`FR-2026`, `LU-2026`); an unknown
    year refuses to compute. FR 2026: PFU 31.4% (12.8 + 18.6, LFSS 2026),
    PEA (IR exemption after 5 years, PS at current rate with the
    historical-layering warning, closure before 5 years at 31.4%), AV
    strates (12.8 / 7.5 within €150k / 12.8 above, PS 17.2% — never a
    single 30% rate), real estate (19% + 17.2%, 7.5% fees + 15% works
    uplift, distinct 6%/1.65% holding allowances, full IR exemption at 22
    years / PS at 30, >€50k surtax schedule), crypto (31.4%, separate 3CN
    progressive option, crypto→crypto sursis). LU 2026: securities
    exemption (<10% and >6 months), 6-month speculation at the marginal
    rate (42.8%/43.6% caps from guichet.lu — modelled as barème +
    employment fund + dependency insurance, never a single rate),
    substantial holding (>10%: half-rate capped 21.4%, €50k allowance
    doubled when married), 5-year real estate boundary (law of
    22/05/2024), half-rate + €50/100k decennial allowance beyond, AV
    exempt (art. 115 LIR), crypto same 6-month rule, €500 franchises.
  - **Never a silent rule** (Fred's corrigenda): every INCONFIRMED point
    (PEA layering, LU revaluation coefficients, AV pre-2017 contracts,
    early AV redemption, non-EUR currency conversion, fees, losses…)
    surfaces as an explicit warning/assumption — the estimate shows its
    own limits instead of hiding them.
  - `accounts.tax_country` (fr|lu|'' — idempotent ALTER, validated 400,
    travels through vaults/imports/exports via the existing column copy).
    `GET /api/tax-estimate?account_id=N&year=` feeds the engine with the
    effective cost (transactions), last valuation, open date, wrapper and
    tax country; 404 for foreign assets, 400 with clear reasons when the
    country/valuation/date is missing or the year is not versioned.
  - UI: tax country select in the asset modal (shown for
    securities/savings/real estate/crypto), country badges (🇫🇷/🇱🇺) next
    to wrapper badges, 🧮 button per estimable asset opening the estimate
    modal: breakdown with rule ids under each line, ⚠️ warnings and ℹ️
    assumptions blocks, ruleset version in the header. i18n ×4.

### Tests

- `tests/test_tax_engine.py` (43): pure engine — exact amounts on every
  regime (the 42,800 example matches the display contract to the cent),
  MV-before-allowance ordering, 2OP/3CN independence, no 30% shortcut for
  AV, PEA warnings pre-2018, real-estate surtax schedule boundaries,
  LU exemption/speculation/substantial holding, 5-year immo boundary,
  non-estimated classes, missing-date refusals, unknown year KeyError,
  every assumption explicitly emitted.
- `tests/test_tax_api.py` (6): route wiring, clean 400/404s, per-member
  isolation, invalid country 400, non-EUR warning. 118 passing.

## [2026.09.030] — 2026-09-06

### Added

- **Vault recovery key.** A protected member can now arm a second DEK wrap
  under a client-generated recovery key (128 random bits, base32 + 32-bit
  SHA-256 checksum, displayed once as 8×4 groups, printable/copyable), so
  a forgotten password no longer means a lost vault — even the
  administrator still cannot recover anything.
  - `vaults` gains `r_salt`, `r_auth_salt`, `r_wrapped`, `r_auth`
    (idempotent ALTERs, empty for existing vaults). The server stores
    only PBKDF2-600k material: the DEK wrapped under
    PBKDF2(recovery-key, r_salt) and an authentication proof
    PBKDF2(recovery-key, r_auth_salt) — distinct salts, so the proof can
    never unwrap the DEK and the raw key never transits or is stored.
  - `POST /api/vault/recovery` arms/replaces the key (authenticated
    session with an OPEN vault required: only the DEK holder can produce
    `r_wrapped`); `POST /api/vault/recover/start` returns the public
    materials for a username (generic 400 otherwise, anti-enumeration);
    `POST /api/vault/recover` combines authentication by proof, vault
    open (canary + real blob decryption), new password hashing and DEK
    re-wrap under it — one call, no "old password" required since it is
    lost by definition. Old keys are revoked the moment a new one is
    armed. Failures are audited, success too (auth events only for
    protected accounts).
  - UI: login screen gains "Forgot password?" (username + key + new
    password); Settings shows a vault panel for protected accounts
    (status + generate/regenerate, key shown exactly once, "I saved the
    key" arms it). i18n ×4.
  - Vault in-memory connections now open with `check_same_thread=False`
    (SQLite serialized + existing `_VAULT_GUARD`): the connection lives
    beyond the handler that created it and serves subsequent requests,
    which can run on a different worker thread under TestClient/anyio.

### Tests

- `tests/test_recovery.py` (4): arming requires an open vault + exposes
  `recovery_armed`, locked-vault arming rejected, full lost-password
  cycle (start → proof → new session + new password + vault data intact,
  old password dead), re-armed key revokes the previous one, generic
  answers for unknown/unarmed accounts. 69 passing.

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
