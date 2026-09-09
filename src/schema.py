"""Schéma des tables de DONNÉES (extrait de src/app.py, v2026.09.036).

Partagé entre la base principale (init_db de src/app.py) et la base mémoire
des coffres protégés : toute nouvelle table/colonne de données s'ajoute ICI
une seule fois. users/sessions/api_tokens/audit_log/vaults = schéma d'auth,
exclus (ils vivent dans init_db de src/app.py).
"""

import sqlite3
def schema_data(conn: sqlite3.Connection) -> None:
    """Schéma des tables de DONNÉES (utilisé par la base principale ET par la
    base mémoire d'un coffre protégé) + migrations idempotentes + seed
    benchmarks. users/sessions ne sont PAS dans ce schéma (auth = principal)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            institution TEXT DEFAULT '',
            currency TEXT DEFAULT 'EUR',
            valuation_mode TEXT DEFAULT 'manual',
            cost_basis REAL DEFAULT 0,
            fx_override REAL,
            open_date TEXT,
            close_date TEXT,
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            fees_pct REAL,
            wrapper TEXT,
            tax_country TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS valuations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            val_date TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT DEFAULT 'manual',
            note TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_vals_acc_date ON valuations(account_id, val_date);
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            op_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            source_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_tx_acc_date ON transactions(account_id, op_date);
        CREATE TABLE IF NOT EXISTS income_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            amount REAL NOT NULL,
            freq TEXT NOT NULL DEFAULT 'monthly',
            months_int INTEGER DEFAULT 1,
            next_date TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            kind TEXT NOT NULL DEFAULT 'income'
        );
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT PRIMARY KEY,
            price REAL,
            currency TEXT DEFAULT '',
            ts TEXT
        );
        CREATE TABLE IF NOT EXISTS benchmarks (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            annual_pct REAL DEFAULT 0,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS index_levels (
            key TEXT NOT NULL,
            ym TEXT NOT NULL,
            level REAL NOT NULL,
            PRIMARY KEY (key, ym)
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
            ccy TEXT NOT NULL,
            rate_date TEXT NOT NULL,
            rate REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'ecb',
            PRIMARY KEY (ccy, rate_date)
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            label TEXT DEFAULT '',
            quantity REAL NOT NULL DEFAULT 0,
            pru REAL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_positions_account ON positions(account_id);
        CREATE TABLE IF NOT EXISTS dividend_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
            ex_date TEXT NOT NULL,
            per_share REAL NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (position_id, ex_date)
        );
        CREATE TABLE IF NOT EXISTS settings (
            member TEXT NOT NULL,
            key TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (member, key)
        );
        -- module Crowdfunding (v2026.09.046) : suivi projet-par-projet des
        -- plateformes d'investissement participatif (Bricks.co, La Première
        -- Brique). Tables scopées par owner comme accounts ; la valeur agrégée
        -- est matérialisée dans des comptes-auto de classe crowdfunding
        -- (cf_platforms.account_id) pour alimenter dashboard/évolution/historique.
        CREATE TABLE IF NOT EXISTS cf_platforms (
            owner TEXT NOT NULL,
            platform TEXT NOT NULL,
            account_id INTEGER,
            balance REAL DEFAULT 0,
            deposited REAL DEFAULT 0,
            invested_value REAL DEFAULT 0,
            updated_at TEXT DEFAULT '',
            PRIMARY KEY (owner, platform)
        );
        CREATE TABLE IF NOT EXISTS cf_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'bricks',
            name TEXT NOT NULL,
            city TEXT DEFAULT '',
            invested REAL NOT NULL DEFAULT 0,
            rate REAL NOT NULL DEFAULT 0,
            duration_months INTEGER NOT NULL DEFAULT 0,
            start_date TEXT,
            expected_end_date TEXT,
            actual_end_date TEXT,
            status TEXT NOT NULL DEFAULT 'en_cours',
            repaid_capital REAL NOT NULL DEFAULT 0,
            interest_received REAL NOT NULL DEFAULT 0,
            interest_net REAL NOT NULL DEFAULT 0,
            interest_remaining REAL NOT NULL DEFAULT 0,
            interest_remaining_net REAL NOT NULL DEFAULT 0,
            real_rate REAL NOT NULL DEFAULT 0,
            valuation REAL NOT NULL DEFAULT 0,
            contract_type TEXT DEFAULT '',
            infine INTEGER NOT NULL DEFAULT 0,
            rest_months INTEGER NOT NULL DEFAULT 0,
            reinvested_from INTEGER,
            auto_created INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            legacy_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cfp_owner ON cf_projects(owner, platform);
        CREATE TABLE IF NOT EXISTS cf_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL,
            source_id TEXT,
            op_date TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Validée',
            project_id INTEGER REFERENCES cf_projects(id) ON DELETE CASCADE,
            amount REAL NOT NULL DEFAULT 0,
            details TEXT DEFAULT '',
            contract_type TEXT DEFAULT '',
            extra TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (owner, platform, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cfo_project ON cf_operations(owner, project_id);
        CREATE INDEX IF NOT EXISTS idx_cfo_date ON cf_operations(op_date);
        CREATE TABLE IF NOT EXISTS cf_captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL DEFAULT '',
            ts TEXT NOT NULL,
            platform TEXT DEFAULT '',
            url TEXT DEFAULT '',
            status_code INTEGER DEFAULT 0,
            body TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cf_reports (
            owner TEXT PRIMARY KEY,
            data TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        -- module Crypto wallets non-custodial (v2026.09.050) : portefeuilles
        -- EVM suivis par adresse publique (Blockscout 21 chaînes + prix
        -- DefiLlama). La valeur est matérialisée dans des comptes-auto de
        -- classe crypto (cw_wallets.account_id). Tables owner-scopées ; les
        -- prix (cw_price_cache) sont globaux (partagés entre owners).
        CREATE TABLE IF NOT EXISTS cw_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            label TEXT NOT NULL,
            address TEXT NOT NULL,
            chain TEXT DEFAULT '',
            watch_only INTEGER DEFAULT 1,
            demo INTEGER DEFAULT 0,
            account_id INTEGER,
            first_date TEXT,
            last_date TEXT,
            last_value_usd REAL,
            last_cost_usd REAL,
            last_refresh TEXT,
            status TEXT DEFAULT 'ok',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cww_owner ON cw_wallets(owner);
        CREATE TABLE IF NOT EXISTS cw_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_id INTEGER NOT NULL REFERENCES cw_wallets(id) ON DELETE CASCADE,
            owner TEXT NOT NULL DEFAULT '',
            tx_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL DEFAULT 0,
            chain TEXT NOT NULL,
            block_time TEXT DEFAULT '',
            token_symbol TEXT DEFAULT '',
            token_name TEXT DEFAULT '',
            token_addr TEXT DEFAULT '',
            direction TEXT NOT NULL,
            amount REAL DEFAULT 0,
            usd_price REAL DEFAULT 0,
            usd_value REAL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cwt_dedup
            ON cw_transfers(wallet_id, chain, tx_hash, log_index);
        CREATE TABLE IF NOT EXISTS cw_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_id INTEGER NOT NULL REFERENCES cw_wallets(id) ON DELETE CASCADE,
            owner TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            value_usd REAL,
            cost_usd REAL,
            net_flows_usd REAL DEFAULT 0,
            token_symbol TEXT,
            chain TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cwh_wallet_date
            ON cw_history(wallet_id, date);
        CREATE TABLE IF NOT EXISTS cw_price_cache (
            token_symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            price_usd REAL NOT NULL,
            PRIMARY KEY (token_symbol, date)
        );
        CREATE TABLE IF NOT EXISTS cw_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_id INTEGER NOT NULL REFERENCES cw_wallets(id) ON DELETE CASCADE,
            owner TEXT NOT NULL DEFAULT '',
            scanned_at TEXT NOT NULL,
            chain TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            name TEXT DEFAULT '',
            category TEXT DEFAULT 'wallet',
            token_addr TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            usd_price REAL DEFAULT 0,
            usd_value REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_cws_wallet ON cw_scans(wallet_id);
        -- module Crédits (v2026.09.056) : passifs suivis par type
        -- (immo/auto/conso). Le restant dû est DÉCLARÉ (source de vérité du
        -- passif — taux variables/remboursements anticipés : la réalité
        -- prime) ; l'échéancier est calculé à la demande (src/loans.py,
        -- amortissement français, aucune table d'échéances stockée).
        -- account_id = bien immobilier lié (équité « valeur − restant »).
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            loan_type TEXT NOT NULL DEFAULT 'conso',
            lender TEXT DEFAULT '',
            currency TEXT DEFAULT 'EUR',
            principal_initial REAL NOT NULL DEFAULT 0,
            principal_remaining REAL NOT NULL DEFAULT 0,
            rate_annual REAL NOT NULL DEFAULT 0,
            monthly_payment REAL NOT NULL DEFAULT 0,
            insurance_monthly REAL NOT NULL DEFAULT 0,
            start_date TEXT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_loans_owner ON loans(owner);
        CREATE INDEX IF NOT EXISTS idx_loans_account ON loans(account_id);
        CREATE TABLE IF NOT EXISTS loc_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            tenant TEXT NOT NULL,
            rent_monthly REAL NOT NULL,
            deposit REAL DEFAULT 0,
            start_date TEXT NOT NULL,
            end_date TEXT,
            active INTEGER DEFAULT 1,
            notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_loc_contracts_owner ON loc_contracts(owner);
        CREATE INDEX IF NOT EXISTS idx_loc_contracts_account ON loc_contracts(account_id);
        CREATE TABLE IF NOT EXISTS loc_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            contract_id INTEGER NOT NULL REFERENCES loc_contracts(id) ON DELETE CASCADE,
            op_date TEXT NOT NULL,
            amount REAL NOT NULL,
            month TEXT NOT NULL,
            transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
            notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_loc_payments_owner ON loc_payments(owner);
        CREATE INDEX IF NOT EXISTS idx_loc_payments_contract ON loc_payments(contract_id);
        CREATE TABLE IF NOT EXISTS tco_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            loan_id INTEGER REFERENCES loans(id) ON DELETE SET NULL,
            purchase_date TEXT,
            purchase_price REAL,
            resale_value REAL,
            resale_date TEXT,
            active INTEGER DEFAULT 1,
            notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tco_items_owner ON tco_items(owner);
        CREATE TABLE IF NOT EXISTS tco_imputations (
            transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
            owner TEXT NOT NULL,
            item_id INTEGER NOT NULL REFERENCES tco_items(id) ON DELETE CASCADE,
            category TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tco_imp_owner ON tco_imputations(owner);
        CREATE INDEX IF NOT EXISTS idx_tco_imp_item ON tco_imputations(item_id);
        """
    )
    for col, ddl in (
        ("symbol", "ALTER TABLE accounts ADD COLUMN symbol TEXT DEFAULT ''"),
        ("quantity", "ALTER TABLE accounts ADD COLUMN quantity REAL DEFAULT 0"),
        ("owner", "ALTER TABLE accounts ADD COLUMN owner TEXT DEFAULT ''"),
        ("fx_override", "ALTER TABLE accounts ADD COLUMN fx_override REAL"),
        ("fees_pct", "ALTER TABLE accounts ADD COLUMN fees_pct REAL"),
        ("wrapper", "ALTER TABLE accounts ADD COLUMN wrapper TEXT"),
        ("tax_country", "ALTER TABLE accounts ADD COLUMN tax_country TEXT DEFAULT ''"),
        ("loan_principal", "ALTER TABLE accounts ADD COLUMN loan_principal REAL NOT NULL DEFAULT 0"),
        ("loan_rate", "ALTER TABLE accounts ADD COLUMN loan_rate REAL NOT NULL DEFAULT 0"),
        ("loan_monthly", "ALTER TABLE accounts ADD COLUMN loan_monthly REAL NOT NULL DEFAULT 0"),
        ("area_m2", "ALTER TABLE accounts ADD COLUMN area_m2 REAL"),
        ("price_m2", "ALTER TABLE accounts ADD COLUMN price_m2 REAL"),
        ("resale_value", "ALTER TABLE tco_items ADD COLUMN resale_value REAL"),
        ("resale_date", "ALTER TABLE tco_items ADD COLUMN resale_date TEXT"),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # colonne déjà présente
    try:
        conn.execute("ALTER TABLE income_rules ADD COLUMN kind TEXT NOT NULL DEFAULT 'income'")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_acc_owner ON accounts(owner, asset_class)")
    except sqlite3.OperationalError:
        pass
    # seed indices
    conn.executemany(
        "INSERT OR IGNORE INTO benchmarks (key, name, symbol, annual_pct, note) VALUES (?,?,?,?,?)",
        [
            ("sp500", "S&P 500", "^GSPC", 0, ""),
            ("nasdaq", "Nasdaq Composite", "^IXIC", 0, ""),
            ("iwda", "MSCI World (IWDA)", "IWDA.L", 0, "ETF capitalisant en EUR"),
            ("stoxx", "STOXX Europe 600", "^STOXX", 0, ""),
            ("cac", "CAC 40", "^FCHI", 0, ""),
            ("livret", "Livret A", "", 2.2, "taux réglementé, saisi manuellement"),
        ],
    )
