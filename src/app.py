"""
Patrimony — Data Sovereignty.
Personal wealth dashboard: multi-asset-class net worth tracking, self-hosted.
Backend monolith: FastAPI + SQLite (stdlib, zero ORM). Same family as the
other LostInTheBugs finance trackers, but with a fresh, generalized data model.

Multi-user "family mode" (v2026.09.005+):
- 1 admin (the original account) + N members, each with isolated data.
- Every account row belongs to an owner (username).
- Members have a mode set at creation: 'standard' (admin may reset password,
  data is included in the admin consolidated view) or 'protected' (admin can
  NEVER reset the password, data never appears in the consolidated view;
  only account deletion is allowed). True cryptographic protection of
  'protected' spaces is Phase 2 (E2E WebCrypto blobs) — Phase 1 enforces the
  rules at application level.
"""
import calendar
import base64
import csv
import hashlib
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from src.backup_crypto import decrypt_bytes, encrypt_bytes

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "app.db"
PORT = int(os.environ.get("PORT", "8020"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SEED_DEMO = os.environ.get("SEED_DEMO", "0") == "1"
VERSION = (BASE_DIR / "VERSION").read_text().strip() if (BASE_DIR / "VERSION").exists() else "0.0.0"
COOKIE = "pat_session"
TTL_DAYS = 30
# Verrouillage automatique du coffre après inactivité (minutes, 0 = désactivé)
VAULT_IDLE_MIN = int(os.environ.get("VAULT_IDLE_MIN", "30"))
# Anti-force-brute du login : MAX échecs par (IP, compte) et par compte, fenêtre glissante
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW_SEC = int(os.environ.get("LOGIN_WINDOW_SEC", "900"))
LOGIN_MAX_FAILS_USER = int(os.environ.get("LOGIN_MAX_FAILS_USER", "10"))
MIN_PASSWORD_LEN = 12
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

# ---------------------------------------------------------------- classes d'actifs
CLASSES = [
    {"key": "comptes",      "label": "Comptes courants",       "emoji": "🏦", "color": "#4f8cff"},
    {"key": "epargne",      "label": "Livrets & épargne",       "emoji": "💰", "color": "#3fb950"},
    {"key": "bourse",       "label": "Bourse & assurances-vie", "emoji": "📈", "color": "#39c5cf"},
    {"key": "immobilier",   "label": "Immobilier",              "emoji": "🏠", "color": "#f0883e"},
    {"key": "crowdfunding", "label": "Crowdfunding",            "emoji": "🧱", "color": "#e3628c"},
    {"key": "crypto",       "label": "Cryptomonnaies",          "emoji": "₿",  "color": "#f85149"},
    {"key": "metaux",       "label": "Métaux précieux",         "emoji": "🥇", "color": "#e6c26a"},
    {"key": "divers",       "label": "Divers",                  "emoji": "📦", "color": "#8b949e"},
]
CLASS_KEYS = [c["key"] for c in CLASSES]
CLASS_META = {c["key"]: c for c in CLASSES}

# ---------------------------------------------------------------- cours & indices
CRYPTO_COINGECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "DOT": "polkadot", "AVAX": "avalanche-2",
    "LINK": "chainlink", "LTC": "litecoin", "BNB": "binancecoin", "USDT": "tether",
    "USDC": "usd-coin", "TRX": "tron", "XMR": "monero", "MATIC": "matic-network",
    "NEAR": "near", "ATOM": "cosmos", "UNI": "uniswap", "APT": "aptos", "ARB": "arbitrum",
    "OP": "optimism", "INJ": "injective", "TIA": "celestia", "SEI": "sei-network",
}
CRYPTO_AUTO_CLASSES = {"crypto"}
YAHOO_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Patrimony/0.1"}


def _http_json(url: str, timeout: int = 12) -> dict:
    req = urllib.request.Request(url, headers=YAHOO_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _yahoo_chart(symbol: str, rng: str = "1d", interval: str = "1d") -> dict | None:
    """Dernier cours (range=1d) ou série mensuelle (interval=1mo) via Yahoo chart API."""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
            f"?range={rng}&interval={interval}"
        )
        d = _http_json(url)
        res = d.get("chart", {}).get("result") or []
        if not res:
            return None
        r0 = res[0]
        ts = r0.get("timestamp") or []
        q = r0.get("indicators", {}).get("quote") or [{}]
        closes = q[0].get("close") or []
        adj = (r0.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
        cur = (r0.get("meta") or {}).get("currency", "")
        points = []
        for i, t in enumerate(ts):
            v = (adj[i] if adj and i < len(adj) and adj[i] is not None else closes[i])
            if v is not None:
                points.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), round(float(v), 4)))
        if not points:
            return None
        return {"price": points[-1][1], "currency": cur, "points": points}
    except Exception:
        return None


def _coingecko_price(symbol: str) -> dict | None:
    cid = CRYPTO_COINGECKO.get(symbol.upper())
    if not cid:
        return None
    try:
        d = _http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=eur")
        p = (d.get(cid) or {}).get("eur")
        if p is None:
            return None
        return {"price": round(float(p), 4), "currency": "EUR"}
    except Exception:
        return None


def fetch_quote(symbol: str, asset_class: str) -> dict | None:
    sym = (symbol or "").strip()
    if not sym:
        return None
    if asset_class in CRYPTO_AUTO_CLASSES:
        q = _coingecko_price(sym)
        if q:
            return q
        return _yahoo_chart(sym + "-EUR", "1d", "1d")
    return _yahoo_chart(sym, "1d", "1d")


# ---------------------------------------------------------------- db
_CTX: ContextVar = ContextVar("pat_vault_ctx", default=None)
# username -> coffre ouvert {conn, dek, sessions:{token: dernier usage (monotonic)}}
# État MONO-PROCESS : ne pas lancer uvicorn avec --workers > 1 ni multi-réplicas
# (les coffres ouverts vivent dans la mémoire du process).
_VAULTS: dict[str, dict] = {}
_VAULT_GUARD = threading.Lock()
_LOGIN_GUARD = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}  # "ip|user" ou "u|user" -> échecs (monotonic)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
except Exception:  # pragma: no cover - dépendance requise (requirements.txt)
    _AESGCM = None


class _VConn(sqlite3.Connection):
    """Connexion SQLite du coffre : partagée et persistante entre les requêtes.
    close() est neutralisé (les handlers ferment systématiquement leur
    connexion en fin de route — ils ne doivent pas tuer la base du coffre) ;
    la fermeture réelle passe par _hard_close() (garbage-collection du coffre)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._vault_dirty = False

    def commit(self):
        super().commit()
        self._vault_dirty = True

    def close(self):
        pass

    def _hard_close(self):
        super().close()


def db() -> sqlite3.Connection:
    """Connexion ROUTÉE : si la requête HTTP en cours concerne un compte
    protégé dont le coffre est ouvert, renvoie la base mémoire du coffre ;
    sinon la base principale."""
    ctx = _CTX.get()
    v = ctx.get("vault") if ctx else None
    if v is not None:
        return v["conn"]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_main() -> sqlite3.Connection:
    """Connexion à la base principale (auth, admin, coffres) — jamais routée."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _admin_username() -> str:
    return os.environ.get("ADMIN_USER", "admin").strip() or "admin"


def _schema_data(conn: sqlite3.Connection) -> None:
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
            active INTEGER DEFAULT 1
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
        """
    )
    for col, ddl in (
        ("symbol", "ALTER TABLE accounts ADD COLUMN symbol TEXT DEFAULT ''"),
        ("quantity", "ALTER TABLE accounts ADD COLUMN quantity REAL DEFAULT 0"),
        ("owner", "ALTER TABLE accounts ADD COLUMN owner TEXT DEFAULT ''"),
        ("fx_override", "ALTER TABLE accounts ADD COLUMN fx_override REAL"),
        ("fees_pct", "ALTER TABLE accounts ADD COLUMN fees_pct REAL"),
    ):
        try:
            conn.execute(ddl)
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


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = db_main()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            role TEXT DEFAULT 'member',
            mode TEXT DEFAULT 'standard',
            must_change INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            scope TEXT DEFAULT 'full',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            last_used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        CREATE TABLE IF NOT EXISTS vaults (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            salt TEXT NOT NULL,
            wrapped TEXT NOT NULL,
            blob TEXT DEFAULT '',
            canary TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    _schema_data(conn)
    # migrations idempotentes (bases antérieures à v2026.09.010)
    for col, ddl in (
        ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''"),
        ("role", "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'member'"),
        ("mode", "ALTER TABLE users ADD COLUMN mode TEXT DEFAULT 'standard'"),
        ("must_change", "ALTER TABLE users ADD COLUMN must_change INTEGER DEFAULT 0"),
        ("created_at", "ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT ''"),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # colonne déjà présente
    try:
        conn.execute("ALTER TABLE vaults ADD COLUMN canary TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente (v2026.09.011)
    try:
        conn.execute("ALTER TABLE api_tokens ADD COLUMN scope TEXT DEFAULT 'full'")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente (v2026.09.024)
    admin = _admin_username()
    # migration mono-utilisateur → famille : l'utilisateur existant devient l'admin
    conn.execute(
        "UPDATE users SET role='admin', mode='standard', display_name=COALESCE(NULLIF(display_name,''), username)"
        " WHERE username=? AND role='member'",
        (admin,),
    )
    n = conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
    if n == 0:
        # aucun admin : promouvoir le 1er utilisateur existant, sinon en créer un
        first = conn.execute("SELECT username FROM users ORDER BY created_at LIMIT 1").fetchone()
        if first:
            conn.execute(
                "UPDATE users SET role='admin', mode='standard', display_name=username WHERE username=?",
                (first["username"],),
            )
        else:
            pwd = os.environ.get("ADMIN_PASSWORD", "change-me")
            conn.execute(
                "INSERT INTO users (username, password, display_name, role, mode) VALUES (?,?,?, 'admin', 'standard')",
                (admin, _hash(pwd), admin),
            )
    # backfill owner des comptes orphelins (mono-utilisateur d'origine)
    conn.execute("UPDATE accounts SET owner=? WHERE owner=''", (admin,))
    # backfill created_at des users migrés
    conn.execute("UPDATE users SET created_at=datetime('now') WHERE created_at=''")
    # v2026.09.010 : un compte protégé sans coffre doit changer son mot de passe
    # initial (choisi par l'admin) avant toute utilisation — l'admin ne doit
    # jamais pouvoir déchiffrer le coffre.
    conn.execute(
        "UPDATE users SET must_change=1 WHERE mode='protected' AND must_change=0"
        " AND NOT EXISTS (SELECT 1 FROM vaults v WHERE v.username=users.username)"
    )
    # purge des données claires orphelines (comptes protégés déjà dotés d'un
    # coffre — reliquat d'une migration interrompue entre la copie et l'effacement)
    conn.execute(
        "DELETE FROM accounts WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    # journal d'audit : rétention 90 jours (purge au boot)
    conn.execute("DELETE FROM audit_log WHERE ts < datetime('now', '-90 days')")
    # v2026.09.025 : un compte bourse auto « 1 symbole » (modèle historique)
    # devient un portefeuille à 1 ligne — rien n'est perdu, l'actif reste le
    # conteneur. Idempotent : jamais de doublon si des lignes existent déjà.
    # (gardé pour les bases très anciennes sans valuation_mode)
    acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if {"valuation_mode", "symbol", "quantity"} <= acc_cols:
        conn.execute(
            "INSERT INTO positions (account_id, symbol, label, quantity, active)"
            " SELECT id, symbol, name, quantity, 1 FROM accounts"
            " WHERE asset_class='bourse' AND valuation_mode='auto' AND symbol<>''"
            " AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.account_id=accounts.id)"
        )
    conn.commit()
    conn.close()
    if SEED_DEMO:
        _seed_demo()


def _hash(pwd: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def _verify(pwd: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        return secrets.compare_digest(_hash(pwd, bytes.fromhex(salt_hex)).split("$")[2], dk_hex)
    except Exception:
        return False


# Hash factice pour égaliser le temps de réponse du login quand l'utilisateur
# n'existe pas (sinon : énumération de comptes par timing).
DUMMY_STORED = _hash("patrimony-dummy-password-2026")


# ---------------------------------------------------------------- demo
def _seed_demo() -> None:
    """Jeu de données de DÉMO (jamais sur la prod réelle) : un actif par classe,
    valorisations mensuelles 2021-2026 reconstruites en interpolation + bruit."""
    conn = db()
    if conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] > 0:
        conn.close()
        return
    owner = _admin_username()
    rnd = random.Random(20260904)
    demos = [
        ("Compte courant",          "comptes",      "Crédit Mutuel",  "2023-09", 8500,  1200, 4100, 0.02),
        ("Livret A",                "epargne",      "Crédit Mutuel",  "2023-01", 15000, 10000, 15800, 0.002),
        ("PEA (ETF MSCI World)",    "bourse",       "Boursorama",     "2022-06", 20000, 15000, 26500, 0.03),
        ("Appartement locatif",     "immobilier",   "—",              "2021-03", 145000, 150000, 172000, 0.001),
        ("Bricks.co (crowdfunding)","crowdfunding", "Bricks.co",      "2022-10", 2400,  2400, 2600, 0.005),
        ("Bitcoin + Ethereum",      "crypto",       "Binance",        "2021-01", 3000,  3000, 6400, 0.05),
        ("Pièces d'or (Napoléon)",  "metaux",       "Comptoir",       "2020-05", 5000,  5000, 8200, 0.01),
        ("Montre & objets",         "divers",       "—",              "2023-06", 800,   800, 950, 0.004),
    ]
    today = date.today()
    for name, cls, inst, open_ym, cost, v0, v1, noise in demos:
        cur = conn.execute(
            "INSERT INTO accounts (owner, name, asset_class, institution, cost_basis, open_date, valuation_mode)"
            " VALUES (?,?,?,?,?,?, 'manual')",
            (owner, name, cls, inst, cost, open_ym + "-01"),
        )
        aid = cur.lastrowid
        oy, om = int(open_ym[:4]), int(open_ym[5:7])
        start = date(oy, om, 1)
        months = (today.year - start.year) * 12 + (today.month - start.month) + 1
        for k in range(months):
            y = start.year + (start.month - 1 + k) // 12
            mo = (start.month - 1 + k) % 12 + 1
            d = date(y, mo, 1)
            if d > today:
                break
            t = k / max(1, months - 1)
            val = v0 + (v1 - v0) * t
            val *= 1 + rnd.uniform(-noise, noise)
            conn.execute(
                "INSERT INTO valuations (account_id, val_date, value, source) VALUES (?,?,?, 'demo')",
                (aid, d.isoformat(), round(val, 2)),
            )
    conn.commit()
    conn.close()


def _add_months(_y: int, m: int) -> date:
    base = date.today().replace(day=1)
    y = base.year + (base.month - 1 + m) // 12
    mo = (base.month - 1 + m) % 12 + 1
    return date(y, mo, 1)


# ---------------------------------------------------------------- coffres (comptes protégés)
def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


_CANARY_PT = b"patrimony-vault-key-canary-v1"


def _vault_canary(dek: bytes) -> str:
    """Valeur témoin chiffrée par la DEK : permet de vérifier une clé fournie
    SANS déchiffrer tout le blob (open à chaud)."""
    nonce = secrets.token_bytes(12)
    ct = _AESGCM(dek).encrypt(nonce, _CANARY_PT, None)
    return _b64e(nonce + ct)


def _vault_check_canary(dek: bytes, canary_b64: str) -> bool:
    if not canary_b64:
        return True  # coffre hérité (pré-v011) : la preuve est le déchiffrement du blob à froid
    try:
        raw = _b64d(canary_b64)
        _AESGCM(dek).decrypt(raw[:12], raw[12:], None)
        return True
    except Exception:
        return False


def _vault_store_canary(username: str, canary_b64: str) -> None:
    m = db_main()
    try:
        m.execute("UPDATE vaults SET canary=? WHERE username=?", (canary_b64, username))
        m.commit()
    finally:
        m.close()


def _vault_mem_new(dek: bytes) -> sqlite3.Connection:
    """Base mémoire vide d'un coffre (schéma de données complet)."""
    if _AESGCM is None:
        raise RuntimeError("cryptography manquante (pip install cryptography)")
    conn = sqlite3.connect(":memory:", factory=_VConn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _schema_data(conn)
    # clôt la transaction éventuelle du seed sans marquer dirty (le backup vers
    # une destination en transaction échoue : « destination database is in use »)
    sqlite3.Connection.commit(conn)
    return conn


def _vault_mem_from_blob(dek: bytes, blob_b64: str) -> sqlite3.Connection:
    """Base mémoire d'un coffre déchiffrée depuis vaults.blob.
    Jamais de clair sur le disque : deserialize() reste en mémoire ; repli
    fichier temporaire uniquement si le SQLite du runtime ne le supporte pas
    (nettoyé en finally)."""
    conn = _vault_mem_new(dek)
    if not blob_b64:
        return conn
    nonce_ct = _b64d(blob_b64)
    raw = _AESGCM(dek).decrypt(nonce_ct[:12], nonce_ct[12:], None)
    try:
        conn.deserialize(raw)
    except sqlite3.Error:
        tmp = DATA_DIR / f".vault_{os.getpid()}.tmp"
        tmp.write_bytes(raw)
        try:
            src = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            try:
                src.backup(conn)
            finally:
                src.close()
        finally:
            tmp.unlink(missing_ok=True)
    # coffres antérieurs à v2026.09.025 : schéma sans positions/dividend_events
    # ni fees_pct — CREATE/ALTER idempotents après la restauration
    _schema_data(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _vault_flush(username: str, v: dict) -> None:
    """Re-chiffre la base mémoire du coffre et met à jour vaults.blob."""
    conn = v["conn"]
    if conn is None or not conn._vault_dirty:
        return
    # annule d'éventuels résidus non commités (le backup échouerait sinon)
    sqlite3.Connection.rollback(conn)
    try:
        # serialize() reste en mémoire : AUCUN clair sur le disque. Repli
        # fichier temporaire si le SQLite du runtime ne le supporte pas —
        # supprimé en finally (un crash entre backup et unlink ne laisse
        # plus de .vault_tmp_* en clair derrière lui).
        raw = conn.serialize()
    except sqlite3.Error:
        tmp = DATA_DIR / f".vault_tmp_{username}.db"
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
            raw = tmp.read_bytes()
        finally:
            dst.close()
            tmp.unlink(missing_ok=True)
    nonce = secrets.token_bytes(12)
    blob = nonce + _AESGCM(v["dek"]).encrypt(nonce, raw, None)
    m = db_main()
    try:
        m.execute(
            "UPDATE vaults SET blob=?, updated_at=datetime('now') WHERE username=?",
            (_b64e(blob), username),
        )
        m.commit()
    finally:
        m.close()
    conn._vault_dirty = False


def _vault_gc(username: str, v: dict) -> None:
    """Purge les sessions expirées OU inactives (auto-lock) ; ferme le coffre
    si plus aucune session."""
    if not v["sessions"]:
        if v["conn"] is not None:
            _vault_flush(username, v)
            try:
                v["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass
            v["conn"] = None
        _VAULTS.pop(username, None)
        return
    m = db_main()
    try:
        now = datetime.now(timezone.utc).isoformat()
        idle_cut = time.monotonic() - VAULT_IDLE_MIN * 60 if VAULT_IDLE_MIN > 0 else 0
        for tok, last in list(v["sessions"].items()):
            if idle_cut and last < idle_cut:
                v["sessions"].pop(tok, None)  # inactivité → verrouillage
            elif not m.execute(
                "SELECT 1 FROM sessions WHERE token=? AND expires_at>?", (tok, now)
            ).fetchone():
                v["sessions"].pop(tok, None)  # session expirée (TTL 30 j)
    finally:
        m.close()
    if not v["sessions"]:
        _vault_gc(username, v)


async def _vault_ctx_mw(request: Request, call_next):
    """Persistance du coffre en fin de requête : le middleware résout lui-même
    la session (cookie) — sans ContextVar, car BaseHTTPMiddleware exécute le
    handler dans une sous-tâche au contexte isolé. Le routage db() vers le
    coffre, lui, est posé par _need() dans le contexte du handler.
    Limité aux chemins /api/* : les assets statiques n'ont pas d'état à gérer
    (évite 1-2 requêtes SQL + un rollback par fichier servi)."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    resp = await call_next(request)
    token = request.cookies.get(COOKIE)
    if token:
        try:
            conn = db_main()
            try:
                row = conn.execute(
                    "SELECT u.username FROM sessions s JOIN users u ON u.username=s.username"
                    " WHERE s.token=? AND s.expires_at>?",
                    (token, datetime.now(timezone.utc).isoformat()),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            row = None
        if row:
            with _VAULT_GUARD:
                v = _VAULTS.get(row["username"])
                if v is not None and token in v["sessions"]:
                    v["conn"].rollback()  # annule d'éventuels résidus non commités
                    _vault_flush(row["username"], v)
                    _vault_gc(row["username"], v)
    return resp


class VaultLocked(Exception):
    pass


class TokenScopeDenied(Exception):
    """Jeton API à portée 'capture' utilisé hors de ses deux appels autorisés."""


def _copy_rows(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, where: str, args: tuple) -> None:
    """Copie les lignes d'une table (base principale → base du coffre)."""
    cols = [r["name"] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
    rows = src.execute(f"SELECT * FROM {table} WHERE {where}", args).fetchall()
    if not rows:
        return
    ph = ",".join("?" * len(cols))
    dst.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
        [tuple(r[c] for c in cols) for r in rows],
    )


# ---------------------------------------------------------------- auth
def _mk_session(conn: sqlite3.Connection, username: str) -> str:
    token = secrets.token_hex(24)
    exp = (datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)).isoformat()
    conn.execute("INSERT INTO sessions (token, username, expires_at) VALUES (?,?,?)", (token, username, exp))
    conn.commit()
    return token


def _me(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(COOKIE)
    if token:
        conn = db_main()
        try:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.username = s.username"
                " WHERE s.token=? AND s.expires_at > ?",
                (token, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
        finally:
            conn.close()
        return row
    # Jeton API (extension) : Authorization: Bearer *** — hash en base,
    # jamais le jeton lui-même. Les comptes protégés ne peuvent pas en créer :
    # leur coffre exige une session interactive.
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and auth[7:].strip():
        h = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
        conn = db_main()
        try:
            row = conn.execute(
                "SELECT u.*, t.scope AS token_scope FROM api_tokens t"
                " JOIN users u ON u.username = t.username"
                " WHERE t.token_hash = ? AND (t.expires_at IS NULL OR t.expires_at > ?)",
                (h, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            # Portée 'capture' (extension Patrimony Capture) : SEULEMENT
            # GET /api/accounts et POST /api/accounts/{id}/valuation — rien
            # d'autre (ni exports/imports, ni CRUD, ni admin famille, ni
            # /api/auth/me). Réduit le jeton de « clé du coffre-fort » à
            # « clé de la boîte aux lettres ».
            if row is not None and row["token_scope"] == "capture":
                p = request.url.path
                allowed = (
                    request.method == "GET" and p == "/api/accounts"
                ) or (
                    request.method == "POST"
                    and p.startswith("/api/accounts/")
                    and p.endswith("/valuation")
                    and p[len("/api/accounts/"):-len("/valuation")].isdigit()
                )
                if not allowed:
                    raise TokenScopeDenied()
            if row is not None:
                conn.execute("UPDATE api_tokens SET last_used_at=datetime('now') WHERE token_hash=?", (h,))
                conn.commit()
        finally:
            conn.close()
        return row
    return None


def _need_main(request: Request) -> sqlite3.Row:
    """Garde d'authentification simple (base principale)."""
    row = _me(request)
    if row is None:
        raise PermissionError("auth")
    return row


def _need(request: Request) -> sqlite3.Row:
    """Garde d'authentification + routage du coffre pour les comptes protégés :
    une requête de données d'un compte protégé exige un coffre ouvert."""
    row = _need_main(request)
    if row["mode"] == "protected":
        token = request.cookies.get(COOKIE)
        v = _VAULTS.get(row["username"])
        if v is None or token not in v["sessions"]:
            raise VaultLocked()
        v["sessions"][token] = time.monotonic()  # activité → repousse l'auto-lock
        ctx = _CTX.get() or {}
        ctx["vault"] = v
        ctx["username"] = row["username"]
        _CTX.set(ctx)
    return row


def _visible_owners(conn: sqlite3.Connection, u: sqlite3.Row, family: bool = False) -> list[str]:
    """Propriétaires dont les données sont visibles : soi-même, et si l'admin
    demande la vue famille, tous les membres 'standard' (jamais 'protected')."""
    if family and u["role"] == "admin":
        rows = conn.execute(
            "SELECT username FROM users WHERE role='member' AND mode='standard'"
        ).fetchall()
        return [u["username"]] + [r["username"] for r in rows]
    return [u["username"]]


def _owner_clause(owners: list[str]) -> tuple[str, list]:
    return "owner IN (%s)" % ",".join("?" * len(owners)), owners


def _guard_owned_account(conn: sqlite3.Connection, aid: int, owner: str) -> bool:
    return conn.execute("SELECT id FROM accounts WHERE id=? AND owner=?", (aid, owner)).fetchone() is not None


app = FastAPI(title="Patrimony", docs_url=None, redoc_url=None)
app.middleware("http")(_vault_ctx_mw)


@app.exception_handler(PermissionError)
async def _perm(_req, _exc):
    return JSONResponse({"detail": "Non authentifié"}, status_code=401)


@app.exception_handler(VaultLocked)
async def _vl_h(_req, _exc):
    return JSONResponse({"detail": "Coffre verrouillé", "code": "vault_locked"}, status_code=403)


@app.exception_handler(TokenScopeDenied)
async def _ts_h(_req, _exc):
    return JSONResponse(
        {"detail": "Jeton à portée limitée — action non autorisée", "code": "scope_denied"},
        status_code=403,
    )


# ---------------------------------------------------------------- auth routes
# Événements autorisés pour un membre PROTÉGÉ dans le journal principal :
# uniquement l'authentification (sans détail). Ses actions de données ne
# sont pas journalisées — un journal dans son coffre serait réinscriptible
# par son détenteur (mauvaise piste d'audit) et nommerait ses actifs.
_AUDIT_AUTH_EVENTS = {
    "Connexion", "Échec de connexion", "Déconnexion",
    "Initialisation du coffre", "Ouverture du coffre",
}
_audit_mode_cache: dict[str, str] = {}


def _audit_mode(username: str) -> str | None:
    """'standard'|'protected' ou None (utilisateur inexistant)."""
    cached = _audit_mode_cache.get(username)
    if cached is not None:
        return cached
    m = db_main()
    try:
        row = m.execute("SELECT mode FROM users WHERE username=?", (username,)).fetchone()
    finally:
        m.close()
    if row is None:
        return None
    _audit_mode_cache[username] = row["mode"]
    return row["mode"]


def _audit(username: str, action: str, detail: str = "") -> None:
    """Journal d'audit (base principale). Méta-données uniquement : jamais de
    montants ni de contenu financier. Règle de confidentialité (coffres) :
    un membre protégé n'émet QUE ses événements d'authentification, sans
    détail — nommer ses actifs ou refléter son rythme d'activité dans le
    journal reviendrait à fuiter la structure de données que le coffre
    protège. Ne casse jamais l'action journalisée."""
    try:
        if _audit_mode(username or "") == "protected":
            if action not in _AUDIT_AUTH_EVENTS:
                return
            detail = ""
        m = db_main()
        try:
            m.execute(
                "INSERT INTO audit_log (username, action, detail) VALUES (?,?,?)",
                (username or "?", action, (detail or "")[:200]),
            )
            m.commit()
        finally:
            m.close()
    except Exception:
        pass


class LoginIn(BaseModel):
    username: str
    password: str


def _login_ratelimited(key: str, limit: int) -> tuple[bool, int]:
    """True si la tentative est autorisée. Retourne (autorisé, secondes restantes)."""
    now = time.monotonic()
    with _LOGIN_GUARD:
        fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW_SEC]
        _LOGIN_FAILS[key] = fails
        if len(fails) >= limit:
            return False, int(LOGIN_WINDOW_SEC - (now - fails[0])) + 1
        return True, 0


def _login_record_fail(key: str) -> None:
    with _LOGIN_GUARD:
        _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())


def _login_clear(key: str) -> None:
    with _LOGIN_GUARD:
        _LOGIN_FAILS.pop(key, None)


def _login_ban_response(retry_in: int) -> JSONResponse:
    return JSONResponse(
        {"detail": f"Trop de tentatives. Réessayez dans {retry_in // 60 + 1} min.",
         "code": "rate_limited"},
        status_code=429,
    )


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    uname = body.username.strip()
    ip = request.client.host if request.client else ""
    # Limitation par (IP, compte) ET par compte seul (l'IP peut être partagée
    # derrière un proxy — le compteur par compte reste efficace).
    per_key = f"ip|{ip}|{uname}"
    user_key = f"u|{uname}"
    ok, retry = _login_ratelimited(user_key, LOGIN_MAX_FAILS_USER)
    if not ok:
        return _login_ban_response(retry)
    ok2, retry2 = _login_ratelimited(per_key, LOGIN_MAX_FAILS)
    if not ok2:
        return _login_ban_response(retry2)
    conn = db_main()
    try:
        row = conn.execute(
            "SELECT username, password, mode, must_change FROM users WHERE username=?",
            (uname,),
        ).fetchone()
    except Exception:
        conn.close()
        raise
    if row is None:
        _verify(body.password, DUMMY_STORED)  # temps constant (anti-énumération)
        _login_record_fail(user_key)
        _login_record_fail(per_key)
        conn.close()
        _audit(uname, "Échec de connexion (compte inconnu)", ip)
        return JSONResponse({"detail": "Identifiants invalides"}, status_code=401)
    if not _verify(body.password, row["password"]):
        _login_record_fail(user_key)
        _login_record_fail(per_key)
        conn.close()
        _audit(uname, "Échec de connexion", ip)
        return JSONResponse({"detail": "Identifiants invalides"}, status_code=401)
    _login_clear(user_key)
    _login_clear(per_key)
    token = _mk_session(conn, uname)
    vault = None
    if row["mode"] == "protected":
        vault = conn.execute(
            "SELECT salt, wrapped FROM vaults WHERE username=?", (row["username"],)
        ).fetchone()
    conn.close()
    response.set_cookie(
        COOKIE, token, max_age=TTL_DAYS * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE
    )
    _audit(uname, "Connexion", ip)
    return {
        "ok": True,
        "mode": row["mode"],
        "must_change": bool(row["must_change"]),
        "vault_init": vault is not None,
        "salt": vault["salt"] if vault else "",
        "wrapped": vault["wrapped"] if vault else "",
    }


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    uname = None
    if token:
        conn = db_main()
        uname = conn.execute("SELECT username FROM sessions WHERE token=?", (token,)).fetchone()
        if uname:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
            with _VAULT_GUARD:
                v = _VAULTS.get(uname["username"])
                if v is not None:
                    v["sessions"].pop(token, None)
                    if not v["sessions"]:
                        _vault_gc(uname["username"], v)
        conn.close()
    response.delete_cookie(COOKIE)
    if token and uname:
        _audit(uname["username"], "Déconnexion")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(request: Request):
    row = _me(request)
    if row is None:
        return {"auth": False}
    out = {"auth": True, "username": row["username"], "role": row["role"],
           "mode": row["mode"], "display_name": row["display_name"] or row["username"],
           "must_change": bool(row["must_change"])}
    if row["mode"] == "protected":
        conn = db_main()
        vault = conn.execute(
            "SELECT salt, wrapped FROM vaults WHERE username=?", (row["username"],)
        ).fetchone()
        conn.close()
        out["vault_init"] = vault is not None
        out["salt"] = vault["salt"] if vault else ""
        out["wrapped"] = vault["wrapped"] if vault else ""
    return out


class TokenIn(BaseModel):
    name: str = "extension"
    expires_days: int | None = None
    scope: str = "full"  # full | capture (extension : 2 appels seulement)


@app.get("/api/tokens")
async def tokens_list(request: Request):
    u = _need(request)
    conn = db_main()
    try:
        rows = conn.execute(
            "SELECT id, name, scope, created_at, expires_at, last_used_at FROM api_tokens"
            " WHERE username=? ORDER BY id", (u["username"],)
        ).fetchall()
    finally:
        conn.close()
    return {"tokens": [dict(r) for r in rows]}


@app.post("/api/tokens")
async def tokens_create(body: TokenIn, request: Request):
    """Crée un jeton API (affiché UNE seule fois, stocké haché). Interdit aux
    comptes protégés : leur coffre exige une session interactive (défi DEK)."""
    u = _need(request)
    if u["mode"] == "protected":
        return JSONResponse({"detail": "Non disponible pour les comptes protégés"}, status_code=403)
    scope = body.scope
    if scope not in ("full", "capture"):
        return JSONResponse({"detail": "Portée invalide (full|capture)"}, status_code=400)
    name = (body.name or "").strip()[:40] or "extension"
    if body.expires_days is not None and not (1 <= body.expires_days <= 3650):
        return JSONResponse({"detail": "Expiration invalide (1-3650 jours)"}, status_code=400)
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    exp = (
        (datetime.now(timezone.utc) + timedelta(days=body.expires_days)).isoformat()
        if body.expires_days else None
    )
    conn = db_main()
    try:
        cur = conn.execute(
            "INSERT INTO api_tokens (username, name, token_hash, scope, expires_at) VALUES (?,?,?,?,?)",
            (u["username"], name, h, scope, exp),
        )
        conn.commit()
        tid = cur.lastrowid
    finally:
        conn.close()
    _audit(u["username"], "Création de jeton API", f"{name} ({scope})")
    return {"id": tid, "name": name, "scope": scope, "token": raw, "expires_at": exp}


@app.delete("/api/tokens/{tid}")
async def tokens_delete(tid: int, request: Request):
    u = _need(request)
    conn = db_main()
    try:
        cur = conn.execute("DELETE FROM api_tokens WHERE id=? AND username=?", (tid, u["username"]))
        conn.commit()
        gone = cur.rowcount
    finally:
        conn.close()
    if not gone:
        return JSONResponse({"detail": "Jeton introuvable"}, status_code=404)
    _audit(u["username"], "Révocation de jeton API", f"#{tid}")
    return {"ok": True}


class PwdIn(BaseModel):
    current: str
    new: str
    wrapped: str = ""  # re-wrap du coffre (comptes protégés) : b64(nonce+ct)
    salt: str = ""     # nouveau sel KDF (comptes protégés)


@app.post("/api/auth/password")
async def change_password(body: PwdIn, request: Request):
    u = _need_main(request)
    conn = db_main()
    row = conn.execute("SELECT password, mode FROM users WHERE username=?", (u["username"],)).fetchone()
    if row is None or not _verify(body.current, row["password"]):
        conn.close()
        return JSONResponse({"detail": "Mot de passe actuel incorrect"}, status_code=400)
    if len(body.new) < MIN_PASSWORD_LEN:
        conn.close()
        return JSONResponse(
            {"detail": f"Mot de passe trop court (min. {MIN_PASSWORD_LEN} caractères)"}, status_code=400
        )
    if row["mode"] == "protected":
        vault = conn.execute(
            "SELECT salt FROM vaults WHERE username=?", (u["username"],)
        ).fetchone()
        if vault is not None and (not body.wrapped or not body.salt):
            conn.close()
            return JSONResponse(
                {"detail": "Re-chiffrement du coffre requis (wrapped + salt)"}, status_code=400
            )
        if vault is not None:
            conn.execute(
                "UPDATE vaults SET salt=?, wrapped=?, updated_at=datetime('now') WHERE username=?",
                (body.salt, body.wrapped, u["username"]),
            )
    conn.execute(
        "UPDATE users SET password=?, must_change=0 WHERE username=?",
        (_hash(body.new), u["username"]),
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Changement de mot de passe")
    return {"ok": True}


# ---------------------------------------------------------------- coffre (comptes protégés)
class VaultInitIn(BaseModel):
    salt: str
    wrapped: str
    dek: str


@app.post("/api/vault/init")
async def vault_init(body: VaultInitIn, request: Request):
    u = _need_main(request)
    if u["mode"] != "protected":
        return JSONResponse({"detail": "Compte non protégé"}, status_code=400)
    token = request.cookies.get(COOKIE)
    dek = _b64d(body.dek)
    if len(dek) != 32:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    conn = db_main()
    if conn.execute("SELECT 1 FROM vaults WHERE username=?", (u["username"],)).fetchone():
        conn.close()
        return JSONResponse({"detail": "Coffre déjà initialisé"}, status_code=400)
    # 1) base mémoire du coffre + transfert des éventuelles données en clair
    mem = _vault_mem_new(dek)
    src = db_main()
    try:
        _copy_rows(src, mem, "accounts", "owner=?", (u["username"],))
        _copy_rows(src, mem, "valuations",
                   "account_id IN (SELECT id FROM accounts WHERE owner=?)", (u["username"],))
        _copy_rows(src, mem, "transactions",
                   "account_id IN (SELECT id FROM accounts WHERE owner=?)", (u["username"],))
        _copy_rows(src, mem, "income_rules",
                   "account_id IN (SELECT id FROM accounts WHERE owner=?)", (u["username"],))
        _copy_rows(src, mem, "positions",
                   "account_id IN (SELECT id FROM accounts WHERE owner=?)", (u["username"],))
        _copy_rows(src, mem, "dividend_events",
                   "position_id IN (SELECT p.id FROM positions p"
                   " JOIN accounts a ON a.id=p.account_id WHERE a.owner=?)", (u["username"],))
    finally:
        src.close()
    # 2) le coffre est ouvert pour cette session
    v = {"conn": mem, "dek": dek, "sessions": {token: time.monotonic()}, "username": u["username"]}
    with _VAULT_GUARD:
        old = _VAULTS.get(u["username"])
        if old is not None and old["conn"] is not None:
            try:
                old["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass
        _VAULTS[u["username"]] = v
    # 3) stockage : la ligne vaults doit exister avant le flush du blob
    try:
        conn.execute(
            "INSERT INTO vaults (username, salt, wrapped, canary, blob) VALUES (?,?,?,?,'')",
            (u["username"], body.salt, body.wrapped, _vault_canary(dek)),
        )
        conn.commit()
    finally:
        conn.close()
    mem.commit()  # marque le coffre sale → le flush du middleware l'écrit chiffré
    # 4) effacement des données claires (après chiffrement — reliquat purgé au boot)
    m = db_main()
    try:
        m.execute("DELETE FROM accounts WHERE owner=?", (u["username"],))
        m.commit()
    finally:
        m.close()
    _audit(u["username"], "Initialisation du coffre")
    return {"ok": True}


class VaultOpenIn(BaseModel):
    dek: str


@app.post("/api/vault/open")
async def vault_open(body: VaultOpenIn, request: Request):
    u = _need_main(request)
    if u["mode"] != "protected":
        return JSONResponse({"detail": "Compte non protégé"}, status_code=400)
    token = request.cookies.get(COOKIE)
    dek = _b64d(body.dek)
    if len(dek) != 32:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    conn = db_main()
    try:
        vault = conn.execute(
            "SELECT salt, wrapped, blob, canary FROM vaults WHERE username=?", (u["username"],)
        ).fetchone()
    finally:
        conn.close()
    if vault is None:
        return JSONResponse({"detail": "Coffre non initialisé"}, status_code=400)
    with _VAULT_GUARD:
        v = _VAULTS.get(u["username"])
        if v is None:
            # open à froid : la clé est prouvée par le déchiffrement réel du blob
            # (et par le canary s'il est déjà armé)
            if not _vault_check_canary(dek, vault["canary"] or ""):
                return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
            try:
                mem = _vault_mem_from_blob(dek, vault["blob"])
            except Exception:
                return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
            v = {"conn": mem, "dek": dek, "sessions": {token: time.monotonic()},
                 "username": u["username"]}
            _VAULTS[u["username"]] = v
            if not vault["canary"]:
                # rétro-armement : la DEK vient d'être prouvée par le blob — les
                # prochains opens (même à chaud) la vérifieront via le canary
                try:
                    _vault_store_canary(u["username"], _vault_canary(dek))
                except Exception:
                    pass
        else:
            # open à chaud : vérifier la DEK fournie même si le coffre est déjà
            # en cache (sinon n'importe quelle clé ouvrirait une session)
            if not _vault_check_canary(dek, vault["canary"] or ""):
                return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
            v["sessions"][token] = time.monotonic()
    _audit(u["username"], "Ouverture du coffre")
    return {"ok": True}


# ---------------------------------------------------------------- famille (admin)
class FamilyIn(BaseModel):
    username: str
    display_name: str = ""
    password: str
    mode: str = "standard"  # standard | protected


def _member_totals(conn: sqlite3.Connection, username: str) -> dict:
    """Total valeur + coût d'un membre (standard uniquement — jamais appelé pour protected)."""
    rows = conn.execute("SELECT id, cost_basis FROM accounts WHERE owner=? AND active=1", (username,)).fetchall()
    latest = _latest_valuations(conn)
    txns = _txn_summary(conn)
    value = cost = 0.0
    for r in rows:
        l = latest.get(r["id"])
        if l:
            value += l["value"]
        t = txns.get(r["id"])
        cost += (t["cost"] if t else (r["cost_basis"] or 0.0))
    return {"total_value": round(value, 2), "total_cost": round(cost, 2)}


@app.get("/api/family")
async def family_list(request: Request):
    u = _need(request)
    if u["role"] != "admin":
        return JSONResponse({"detail": "Administrateur requis"}, status_code=403)
    conn = db()
    rows = conn.execute(
        "SELECT username, display_name, role, mode, created_at FROM users ORDER BY created_at, username"
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if r["username"] == u["username"]:
            d["is_self"] = True
            d["totals"] = None
        else:
            d["is_self"] = False
            if r["mode"] == "protected":
                d["totals"] = None  # invisibles par conception
            else:
                d["totals"] = _member_totals(conn, r["username"])
        out.append(d)
    conn.close()
    return {"members": out}


@app.post("/api/family")
async def family_create(body: FamilyIn, request: Request):
    u = _need(request)
    if u["role"] != "admin":
        return JSONResponse({"detail": "Administrateur requis"}, status_code=403)
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        return JSONResponse({"detail": "Nom d'utilisateur invalide (3-32 : a-z 0-9 . _ -)"}, status_code=400)
    if body.mode not in ("standard", "protected"):
        return JSONResponse({"detail": "Mode invalide"}, status_code=400)
    if len(body.password) < MIN_PASSWORD_LEN:
        return JSONResponse(
            {"detail": f"Mot de passe trop court (min. {MIN_PASSWORD_LEN} caractères)"}, status_code=400
        )
    conn = db()
    if conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone():
        conn.close()
        return JSONResponse({"detail": "Ce nom d'utilisateur existe déjà"}, status_code=400)
    conn.execute(
        "INSERT INTO users (username, password, display_name, role, mode, must_change)"
        " VALUES (?,?,?, 'member', ?, ?)",
        (username, _hash(body.password), (body.display_name.strip() or username),
         body.mode, 1 if body.mode == "protected" else 0),
    )
    conn.commit()
    conn.close()
    _audit_mode_cache.pop(username, None)  # le mode est figé à la création
    _audit(u["username"], "Création de membre", f"{username} ({body.mode})")
    return {"ok": True, "username": username}


class FamilyPwdIn(BaseModel):
    password: str


@app.post("/api/family/{username}/reset-password")
async def family_reset_password(username: str, body: FamilyPwdIn, request: Request):
    u = _need(request)
    if u["role"] != "admin":
        return JSONResponse({"detail": "Administrateur requis"}, status_code=403)
    uname = username.strip().lower()
    if uname == u["username"]:
        return JSONResponse({"detail": "Impossible sur votre propre compte"}, status_code=400)
    conn = db()
    row = conn.execute("SELECT mode FROM users WHERE username=?", (uname,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Membre introuvable"}, status_code=404)
    if row["mode"] == "protected":
        conn.close()
        return JSONResponse(
            {"detail": "Compte protégé : réinitialisation impossible par conception."}, status_code=403
        )
    if len(body.password) < MIN_PASSWORD_LEN:
        conn.close()
        return JSONResponse(
            {"detail": f"Mot de passe trop court (min. {MIN_PASSWORD_LEN} caractères)"}, status_code=400
        )
    conn.execute("UPDATE users SET password=? WHERE username=?", (_hash(body.password), uname))
    conn.execute("DELETE FROM sessions WHERE username=?", (uname,))  # déconnecte l'ancien
    conn.commit()
    conn.close()
    _audit(u["username"], "Réinitialisation du mot de passe d'un membre", uname)
    return {"ok": True}


@app.delete("/api/family/{username}")
async def family_delete(username: str, request: Request):
    u = _need(request)
    if u["role"] != "admin":
        return JSONResponse({"detail": "Administrateur requis"}, status_code=403)
    uname = username.strip().lower()
    if uname == u["username"]:
        return JSONResponse({"detail": "Impossible sur votre propre compte"}, status_code=400)
    conn = db()
    row = conn.execute("SELECT username FROM users WHERE username=?", (uname,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Membre introuvable"}, status_code=404)
    # ferme un éventuel coffre ouvert (suppression = destruction des données chiffrées)
    with _VAULT_GUARD:
        v = _VAULTS.pop(uname, None)
        if v is not None and v["conn"] is not None:
            try:
                v["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass
    conn.execute("DELETE FROM sessions WHERE username=?", (uname,))
    conn.execute("DELETE FROM users WHERE username=?", (uname,))  # cascade : vaults
    conn.execute("DELETE FROM accounts WHERE owner=?", (uname,))  # cascade : valuations/transactions/règles
    conn.commit()
    conn.close()
    _audit_mode_cache.pop(uname, None)  # un recréé du même nom repart d'un mode propre
    _audit(u["username"], "Suppression de membre", uname)
    return {"ok": True}


# ---------------------------------------------------------------- helpers métier
def _month_bounds(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def _parse_ym(ym: str) -> date:
    return date(int(ym[:4]), int(ym[5:7]), 1)


def _latest_valuations(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT account_id, val_date, value, source FROM valuations v1 WHERE val_date ="
        " (SELECT MAX(val_date) FROM valuations v2 WHERE v2.account_id = v1.account_id)"
        " AND id = (SELECT MAX(id) FROM valuations v3 WHERE v3.account_id = v1.account_id"
        " AND v3.val_date = v1.val_date)"
    ).fetchall()
    return {r["account_id"]: {"date": r["val_date"], "value": r["value"], "source": r["source"]} for r in rows}


def _txn_summary(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT account_id,"
        "  SUM(CASE WHEN kind IN ('deposit','income') THEN amount ELSE 0 END) AS inflow,"
        "  SUM(CASE WHEN kind='withdrawal' THEN amount ELSE 0 END) AS outflow,"
        "  SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) AS income,"
        "  COUNT(*) AS n"
        " FROM transactions GROUP BY account_id"
    ).fetchall()
    return {
        r["account_id"]: {
            "has_tx": True,
            "cost": round((r["inflow"] or 0) - (r["outflow"] or 0), 2),
            "income": r["income"] or 0.0,
            "n": r["n"],
        }
        for r in rows
    }


def _account_payload(row: sqlite3.Row, latest: dict | None, txn: dict | None = None,
                     conn: sqlite3.Connection | None = None) -> dict:
    p = {k: row[k] for k in row.keys()}
    cls = CLASS_META.get(row["asset_class"], {})
    p["class_emoji"] = cls.get("emoji", "📦")
    p["last_value"] = latest["value"] if latest else None
    p["last_val_date"] = latest["date"] if latest else None
    cost = row["cost_basis"] or 0.0
    cost_from_tx = False
    if txn and txn["has_tx"]:
        cost = txn["cost"]
        cost_from_tx = True
    p["cost_effective"] = round(cost, 2)
    p["cost_from_tx"] = cost_from_tx
    p["txn_count"] = txn["n"] if txn else 0
    p["income_received"] = txn["income"] if txn else 0.0
    # source de la dernière valorisation + âge en jours (honnêteté des cours :
    # une valeur auto peut être ancienne si le refresh a échoué)
    if latest:
        p["last_val_source"] = latest.get("source") or None
        try:
            p["last_val_age_days"] = max(0, (date.today() - date.fromisoformat(latest["date"])).days)
        except ValueError:
            p["last_val_age_days"] = None
    else:
        p["last_val_source"] = None
        p["last_val_age_days"] = None
    # frais de gestion annuels % : cumul ≈ sur l'historique mensuel réel
    p["fees_pct"] = row["fees_pct"] if "fees_pct" in row.keys() else None
    p["fees_paid"] = _fees_paid_eur(conn, row) if (p["fees_pct"] and conn) else None
    gain = None
    if latest and cost:
        gain = round(latest["value"] - cost, 2)
    p["gain"] = gain
    p["gain_pct"] = round(gain / cost * 100, 2) if (gain is not None and cost) else None
    # multi-devises : équivalent EUR + taux utilisé (null si EUR ou taux manquant)
    ccy = row["currency"] or "EUR"
    p["currency"] = ccy
    p["fx_override"] = row["fx_override"]
    p["fx"] = None
    if latest and ccy != "EUR":
        fx = _fx_lookup(conn, ccy, latest["date"], row["fx_override"]) if conn else None
        if fx is None:
            p["fx"] = {"rate": None, "value_eur": None, "error": "rate_missing"}
        else:
            p["fx"] = {
                "rate": fx["rate"],
                "date": fx["date"],
                "source": fx["source"],
                "value_eur": round(latest["value"] / fx["rate"], 2),
                "stale": _fx_warn(fx, latest["date"]),
            }
    return p


# ---------------------------------------------------------------- routes actifs
@app.get("/api/accounts")
async def list_accounts(request: Request):
    u = _need(request)
    conn = db()
    try:
        latest = _latest_valuations(conn)
        txns = _txn_summary(conn)
        rows = conn.execute(
            "SELECT * FROM accounts WHERE owner=? ORDER BY asset_class, name", (u["username"],)
        ).fetchall()
        out = [_account_payload(r, latest.get(r["id"]), txns.get(r["id"]), conn) for r in rows]
        # portefeuille : composition des comptes bourse auto (v2026.09.025)
        pf_ids = [r["id"] for r in rows
                  if r["asset_class"] == "bourse" and r["valuation_mode"] == "auto"]
        for a in out:
            if a["id"] in pf_ids:
                a["positions"] = _positions_payload(conn, a["id"])
    finally:
        conn.close()
    return {"accounts": out}


class AccountIn(BaseModel):
    name: str
    asset_class: str
    institution: str = ""
    currency: str = "EUR"
    cost_basis: float = 0
    fx_override: float | None = None
    open_date: str | None = None
    notes: str = ""
    active: int = 1
    valuation_mode: str = "manual"
    symbol: str = ""
    quantity: float = 0
    initial_value: float | None = None
    fees_pct: float | None = None  # frais de gestion annuels % (v2026.09.025)


@app.post("/api/accounts")
async def create_account(body: AccountIn, request: Request):
    u = _need(request)
    if not body.name.strip():
        return JSONResponse({"detail": "Nom requis"}, status_code=400)
    if body.asset_class not in CLASS_KEYS:
        return JSONResponse({"detail": "Classe d'actif invalide"}, status_code=400)
    mode = body.valuation_mode if body.valuation_mode in ("manual", "auto") else "manual"
    # les actifs auto sont valorisés en EUR (cours converti au refresh)
    ccy = "EUR" if mode == "auto" else (body.currency or "EUR").upper()
    if ccy not in FX_SUPPORTED:
        return JSONResponse({"detail": "Devise non supportée"}, status_code=400)
    if body.fees_pct is not None and body.fees_pct < 0:
        return JSONResponse({"detail": "Frais annuels invalides"}, status_code=400)
    conn = db()
    cur = conn.execute(
        "INSERT INTO accounts (owner, name, asset_class, institution, currency, fx_override, cost_basis, fees_pct, open_date, notes, active,"
        " valuation_mode, symbol, quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (u["username"], body.name.strip(), body.asset_class, body.institution.strip(), ccy,
         round(body.fx_override, 6) if body.fx_override else None, body.cost_basis or 0,
         round(body.fees_pct, 4) if body.fees_pct is not None else None,
         body.open_date, body.notes.strip(), body.active,
         mode,
         body.symbol.strip().upper(), body.quantity or 0),
    )
    aid = cur.lastrowid
    # v2026.09.025 — un compte bourse auto créé avec un symbole (ancien modèle
    # 1 ligne) devient un portefeuille à 1 position : le compte reste conteneur.
    if body.asset_class == "bourse" and mode == "auto" and (body.symbol or "").strip():
        conn.execute(
            "INSERT INTO positions (account_id, symbol, label, quantity, pru) VALUES (?,?,?,?,NULL)",
            (aid, body.symbol.strip().upper(), body.name.strip(), body.quantity or 0),
        )
    if body.initial_value is not None and body.valuation_mode != "auto":
        d = body.open_date or date.today().isoformat()
        conn.execute(
            "INSERT INTO valuations (account_id, val_date, value, source) VALUES (?,?,?, 'manual')",
            (aid, d, round(body.initial_value, 2)),
        )
    conn.commit()
    conn.close()
    _audit(u["username"], "Création d'actif", f"#{aid} {body.name.strip()}")
    return {"id": aid}


@app.put("/api/accounts/{aid}")
async def update_account(aid: int, body: AccountIn, request: Request):
    u = _need(request)
    conn = db()
    if not _guard_owned_account(conn, aid, u["username"]):
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    mode = body.valuation_mode if body.valuation_mode in ("manual", "auto") else "manual"
    ccy = "EUR" if mode == "auto" else (body.currency or "EUR").upper()
    if ccy not in FX_SUPPORTED:
        conn.close()
        return JSONResponse({"detail": "Devise non supportée"}, status_code=400)
    conn.execute(
        "UPDATE accounts SET name=?, asset_class=?, institution=?, currency=?, fx_override=?, cost_basis=?, fees_pct=?, open_date=?, notes=?,"
        " active=?, valuation_mode=?, symbol=?, quantity=?, updated_at=datetime('now') WHERE id=?",
        (body.name.strip(), body.asset_class, body.institution.strip(), ccy,
         round(body.fx_override, 6) if body.fx_override else None, body.cost_basis or 0,
         round(body.fees_pct, 4) if body.fees_pct is not None else None,
         body.open_date, body.notes.strip(), body.active,
         mode,
         body.symbol.strip().upper(), body.quantity or 0, aid),
    )
    # v2026.09.025 — passage d'un compte bourse existant en auto avec symbole
    # (sans ligne déjà gérée) : on matérialise la position #1
    if body.asset_class == "bourse" and mode == "auto" and (body.symbol or "").strip():
        has_pos = conn.execute(
            "SELECT 1 FROM positions WHERE account_id=?", (aid,)
        ).fetchone()
        if not has_pos:
            conn.execute(
                "INSERT INTO positions (account_id, symbol, label, quantity, pru) VALUES (?,?,?,?,NULL)",
                (aid, body.symbol.strip().upper(), body.name.strip(), body.quantity or 0),
            )
    conn.commit()
    conn.close()
    _audit(u["username"], "Modification d'actif", f"#{aid} {body.name.strip()}")
    return {"ok": True}


@app.delete("/api/accounts/{aid}")
async def delete_account(aid: int, request: Request):
    u = _need(request)
    conn = db()
    row = conn.execute(
        "SELECT name FROM accounts WHERE id=? AND owner=?", (aid, u["username"])
    ).fetchone()
    conn.execute("DELETE FROM accounts WHERE id=? AND owner=?", (aid, u["username"]))
    conn.commit()
    conn.close()
    if row:
        _audit(u["username"], "Suppression d'actif", f"#{aid} {row['name']}")
    return {"ok": True}


# ---------------------------------------------------------------- positions (portefeuille)
# v2026.09.025 — un compte bourse 'auto' est un CONTENEUR ; sa composition
# vit dans `positions` (symbole × quantité × PRU). Valeur du compte = Σ des
# lignes au cours du jour ; l'historique de valorisation (mensuel) reste au
# niveau du compte — rien de cassé pour les graphiques existants.

def _latest_price(conn: sqlite3.Connection, symbol: str) -> dict | None:
    r = conn.execute(
        "SELECT price, currency, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return dict(r) if r else None


def _pos_quote_eur(conn: sqlite3.Connection, symbol: str, d: str | None = None) -> dict | None:
    """Cours (cache prices) converti en EUR — jamais de réseau ici."""
    px = _latest_price(conn, symbol)
    if px is None:
        return None
    ccy = px["currency"] or "EUR"
    out = {"price": px["price"], "currency": ccy, "ts": px["ts"]}
    if ccy in ("", "EUR"):
        out["price_eur"] = px["price"]
    else:
        fx = _fx_lookup(conn, ccy, d or date.today().isoformat(), None)
        if fx is None:
            return None
        out["price_eur"] = px["price"] / fx["rate"]
    return out


def _positions_payload(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    """Lignes d'un compte avec cours, valeur EUR, poids, PV brute (PRU) et
    dividendes enregistrés (montant = quantité ACTUELLE × montant/action)."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE account_id=? ORDER BY id", (account_id,)
    ).fetchall()
    divs = conn.execute(
        "SELECT d.*, p.symbol, p.quantity FROM dividend_events d JOIN positions p ON p.id=d.position_id"
        " WHERE p.account_id=? ORDER BY d.ex_date", (account_id,)
    ).fetchall()
    by_pos: dict[int, list[dict]] = {}
    for d in divs:
        by_pos.setdefault(d["position_id"], []).append({
            "id": d["id"], "ex_date": d["ex_date"], "per_share": d["per_share"],
            "note": d["note"], "amount": round((d["quantity"] or 0) * d["per_share"], 2)
            if d["quantity"] else None, "symbol": d["symbol"],
        })
    out = []
    for r in rows:
        q = _pos_quote_eur(conn, r["symbol"]) if r["active"] else None
        line = {
            "id": r["id"], "account_id": r["account_id"], "symbol": r["symbol"], "label": r["label"],
            "quantity": r["quantity"], "pru": r["pru"], "active": bool(r["active"]),
            "price": q["price"] if q else None,
            "price_currency": q["currency"] if q else None,
            "price_ts": q["ts"] if q else None,
            "value_eur": round((r["quantity"] or 0) * q["price_eur"], 2) if q else None,
            "gain_eur": round((r["quantity"] or 0) * (q["price_eur"] - (r["pru"] or 0)), 2)
            if q and r["pru"] is not None else None,
            "gain_pct": round((q["price_eur"] / r["pru"] - 1) * 100, 2)
            if q and r["pru"] else None,
            "dividends": by_pos.get(r["id"], []),
        }
        if q and r["pru"] is not None:
            line["cost_eur"] = round((r["quantity"] or 0) * r["pru"], 2)
        out.append(line)
    vals = [l for l in out if l["value_eur"] is not None and l["active"]]
    tot = sum(l["value_eur"] for l in vals)
    for l in out:
        l["weight_pct"] = round(l["value_eur"] / tot * 100, 2) if (tot and l["value_eur"] is not None) else None
        l["portfolio_eur"] = round(tot, 2)
    return out


def _fees_paid_eur(conn: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    """Cumul ≈ des frais de gestion : taux annuel appliqué au prorata mensuel
    sur chaque valorisation de fin de mois (historique réel)."""
    pct = row["fees_pct"]
    if pct is None or pct <= 0:
        return None
    vals = conn.execute(
        "SELECT val_date, value FROM valuations WHERE account_id=?"
        " AND val_date <= date('now') ORDER BY val_date", (row["id"],)
    ).fetchall()
    if not vals:
        return None
    tot = 0.0
    for v in vals:
        tot += (v["value"] or 0) * pct / 100.0 / 12.0
    first = vals[0]["val_date"][:7]
    last = vals[-1]["val_date"][:7]
    y1, m1 = int(first[:4]), int(first[5:7])
    y2, m2 = int(last[:4]), int(last[5:7])
    months = (y2 - y1) * 12 + (m2 - m1) + 1
    return {"fees_pct": pct, "paid_eur": round(tot, 2), "months": months,
            "from_ym": first, "to_ym": last}


class PositionIn(BaseModel):
    symbol: str
    label: str = ""
    quantity: float = 0
    pru: float | None = None
    active: int = 1


class DividendIn(BaseModel):
    ex_date: str
    per_share: float
    note: str = ""


def _div_source_id(pid: int, ex_date: str) -> str:
    return f"div:{pid}:{ex_date}"


def _div_sync(conn: sqlite3.Connection, pos: sqlite3.Row, ex_date: str, per_share: float, note: str = "") -> None:
    """Miroir comptable d'un dividende : une opération income liée par
    source_id (idempotent — l'événement est la source de vérité, la ligne
    d'opération est recalculée à chaque sauvegarde)."""
    amount = round((pos["quantity"] or 0) * per_share, 2)
    sid = _div_source_id(pos["id"], ex_date)
    if amount <= 0:
        conn.execute("DELETE FROM transactions WHERE source_id=?", (sid,))
        return
    ex = conn.execute("SELECT id FROM transactions WHERE source_id=?", (sid,)).fetchone()
    if ex:
        conn.execute(
            "UPDATE transactions SET amount=?, note=?, op_date=? WHERE id=?",
            (amount, f"Dividende {pos['symbol']}", ex_date, ex["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO transactions (account_id, op_date, kind, amount, note, source_id)"
            " VALUES (?,?,?,?,?,?)",
            (pos["account_id"], ex_date, "income", amount, f"Dividende {pos['symbol']}", sid),
        )


def _div_unsync(conn: sqlite3.Connection, pid: int, ex_date: str) -> None:
    conn.execute("DELETE FROM transactions WHERE source_id=?", (_div_source_id(pid, ex_date),))


def _pos_owned(conn: sqlite3.Connection, pid: int, owner: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, a.asset_class, a.valuation_mode FROM positions p"
        " JOIN accounts a ON a.id=p.account_id WHERE p.id=? AND a.owner=?",
        (pid, owner),
    ).fetchone()


@app.post("/api/accounts/{aid}/positions")
async def create_position(aid: int, body: PositionIn, request: Request):
    u = _need(request)
    conn = db()
    acc = conn.execute(
        "SELECT * FROM accounts WHERE id=? AND owner=?", (aid, u["username"])
    ).fetchone()
    if acc is None:
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    if acc["asset_class"] != "bourse" or acc["valuation_mode"] != "auto":
        conn.close()
        return JSONResponse({"detail": "Lignes réservées aux comptes bourse valorisés au cours"}, status_code=400)
    sym = (body.symbol or "").strip().upper()
    if not sym or len(sym) > 24:
        conn.close()
        return JSONResponse({"detail": "Symbole invalide"}, status_code=400)
    if not body.quantity or body.quantity <= 0:
        conn.close()
        return JSONResponse({"detail": "Quantité invalide"}, status_code=400)
    if body.pru is not None and body.pru < 0:
        conn.close()
        return JSONResponse({"detail": "PRU invalide"}, status_code=400)
    cur = conn.execute(
        "INSERT INTO positions (account_id, symbol, label, quantity, pru)"
        " VALUES (?,?,?,?,?)",
        (aid, sym, (body.label or "").strip()[:80],
         round(body.quantity, 6),
         round(body.pru, 6) if body.pru is not None else None),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    _audit(u["username"], "Ajout de ligne portefeuille", f"#{pid} {sym}")
    return {"id": pid}


@app.put("/api/positions/{pid}")
async def update_position(pid: int, body: PositionIn, request: Request):
    u = _need(request)
    conn = db()
    pos = _pos_owned(conn, pid, u["username"])
    if pos is None:
        conn.close()
        return JSONResponse({"detail": "Ligne introuvable"}, status_code=404)
    sym = (body.symbol or "").strip().upper()
    if not sym or len(sym) > 24:
        conn.close()
        return JSONResponse({"detail": "Symbole invalide"}, status_code=400)
    if not body.quantity or body.quantity <= 0:
        conn.close()
        return JSONResponse({"detail": "Quantité invalide"}, status_code=400)
    if body.pru is not None and body.pru < 0:
        conn.close()
        return JSONResponse({"detail": "PRU invalide"}, status_code=400)
    conn.execute(
        "UPDATE positions SET symbol=?, label=?, quantity=?, pru=?, active=?, updated_at=datetime('now') WHERE id=?",
        (sym, (body.label or "").strip()[:80], round(body.quantity, 6),
         round(body.pru, 6) if body.pru is not None else None,
         1 if body.active else 0, pid),
    )
    # la quantité change le montant des dividendes : resynchroniser les miroirs
    for d in conn.execute("SELECT ex_date, per_share FROM dividend_events WHERE position_id=?", (pid,)):
        _div_sync(conn, conn.execute(
            "SELECT id, account_id, symbol, quantity FROM positions WHERE id=?", (pid,)).fetchone(),
            d["ex_date"], d["per_share"])
    conn.commit()
    conn.close()
    _audit(u["username"], "Modification de ligne portefeuille", f"#{pid} {sym}")
    return {"ok": True}


@app.delete("/api/positions/{pid}")
async def delete_position(pid: int, request: Request):
    u = _need(request)
    conn = db()
    pos = _pos_owned(conn, pid, u["username"])
    if pos is None:
        conn.close()
        return JSONResponse({"detail": "Ligne introuvable"}, status_code=404)
    # retire les miroirs comptables des dividendes avant la cascade
    for d in conn.execute("SELECT ex_date FROM dividend_events WHERE position_id=?", (pid,)):
        _div_unsync(conn, pid, d["ex_date"])
    conn.execute("DELETE FROM positions WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    _audit(u["username"], "Suppression de ligne portefeuille", f"#{pid} {pos['symbol']}")
    return {"ok": True}


@app.post("/api/positions/{pid}/dividend")
async def upsert_dividend(pid: int, body: DividendIn, request: Request):
    u = _need(request)
    conn = db()
    pos = _pos_owned(conn, pid, u["username"])
    if pos is None:
        conn.close()
        return JSONResponse({"detail": "Ligne introuvable"}, status_code=404)
    try:
        date.fromisoformat(body.ex_date)
    except ValueError:
        conn.close()
        return JSONResponse({"detail": "Date invalide"}, status_code=400)
    if body.per_share is None or body.per_share <= 0:
        conn.close()
        return JSONResponse({"detail": "Montant par action invalide"}, status_code=400)
    conn.execute(
        "INSERT INTO dividend_events (position_id, ex_date, per_share, note) VALUES (?,?,?,?)"
        " ON CONFLICT(position_id, ex_date) DO UPDATE SET per_share=excluded.per_share,"
        " note=excluded.note",
        (pid, body.ex_date, round(body.per_share, 6), (body.note or "").strip()[:120]),
    )
    _div_sync(conn, pos, body.ex_date, body.per_share)
    conn.commit()
    conn.close()
    _audit(u["username"], "Dividende enregistré", f"#{pid} {pos['symbol']} {body.ex_date}")
    return {"ok": True}


@app.delete("/api/dividends/{did}")
async def delete_dividend(did: int, request: Request):
    u = _need(request)
    conn = db()
    row = conn.execute(
        "SELECT d.id, d.position_id, d.ex_date FROM dividend_events d"
        " JOIN positions p ON p.id=d.position_id JOIN accounts a ON a.id=p.account_id"
        " WHERE d.id=? AND a.owner=?", (did, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Dividende introuvable"}, status_code=404)
    _div_unsync(conn, row["position_id"], row["ex_date"])
    conn.execute("DELETE FROM dividend_events WHERE id=?", (did,))
    conn.commit()
    conn.close()
    _audit(u["username"], "Dividende supprimé", f"#{row['position_id']} {row['ex_date']}")
    return {"ok": True}


class ValIn(BaseModel):
    value: float
    val_date: str | None = None
    note: str = ""


@app.post("/api/accounts/{aid}/valuation")
async def add_valuation(aid: int, body: ValIn, request: Request):
    u = _need(request)
    conn = db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE id=? AND active=1 AND owner=?", (aid, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    d = body.val_date or date.today().isoformat()
    conn.execute(
        "INSERT INTO valuations (account_id, val_date, value, source, note) VALUES (?,?,?,?,?)",
        (aid, d, round(body.value, 2), "manual", body.note.strip()),
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Saisie de valorisation", f"#{aid} le {d}")
    return {"ok": True}


# ---------------------------------------------------------------- multi-devises
# Devises manuelles supportées (taux BCE « 1 EUR = X devises »). EUR = référence.
FX_SUPPORTED = ["EUR", "USD", "CHF", "GBP", "JPY", "CAD", "AUD"]
FX_META = {
    "EUR": {"symbol": "€", "label": "Euro"},
    "USD": {"symbol": "$", "label": "US Dollar"},
    "CHF": {"symbol": "CHF", "label": "Franc suisse"},
    "GBP": {"symbol": "£", "label": "Livre sterling"},
    "JPY": {"symbol": "¥", "label": "Yen japonais"},
    "CAD": {"symbol": "C$", "label": "Dollar canadien"},
    "AUD": {"symbol": "A$", "label": "Dollar australien"},
}
FX_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
# Historique complet BCE (depuis 1999) : backfill des fins de mois pour les
# conversions des historiques anciens (le fichier fait ~8 Mo, une seule passe)
FX_ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"


def _fx_lookup(conn: sqlite3.Connection, ccy: str, d: str | None, override: float | None = None) -> dict | None:
    """Taux EUR pour `ccy` le jour `d` (ou taux le plus proche dispo) :
    rate = unités de `ccy` pour 1 EUR → EUR = valeur / rate.
    Priorité : override manuel de l'actif > taux BCE <= d > taux BCE le plus
    ancien. Retourne {rate, date, source} ou None (ccy EUR ⇒ rate 1)."""
    if ccy in (None, "", "EUR"):
        return {"rate": 1.0, "date": None, "source": "fixed"}
    if override:
        return {"rate": float(override), "date": None, "source": "manual"}
    row = None
    if d:
        row = conn.execute(
            "SELECT rate, rate_date, source FROM fx_rates WHERE ccy=? AND rate_date<=?"
            " ORDER BY rate_date DESC LIMIT 1", (ccy, d)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT rate, rate_date, source FROM fx_rates WHERE ccy=?"
            " ORDER BY rate_date ASC LIMIT 1", (ccy,)
        ).fetchone()
    if row is None:
        return None
    return {"rate": row["rate"], "date": row["rate_date"], "source": row["source"]}


def _fx_warn(rate: dict | None, d: str | None) -> bool:
    """⚠️ taux BCE âgé de plus de 7 jours (saisie manuelle honnête)."""
    if not rate or rate["source"] == "manual" or rate["date"] is None:
        return False
    try:
        return (date.fromisoformat(d or rate["date"]) - date.fromisoformat(rate["date"])).days > 7
    except ValueError:
        return False


def _parse_ecb_xml(xml_text: str) -> list[tuple[str, str, float]]:
    """(ccy, YYYY-MM-DD, rate) depuis le XML BCE (eurofxref-daily)."""
    import xml.etree.ElementTree as ET

    out = []
    root = ET.fromstring(xml_text)
    ns = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
    day = None
    for cube in root.iter(ns + "Cube"):
        if "time" in cube.attrib:
            day = cube.attrib["time"]
        elif "currency" in cube.attrib and day:
            try:
                out.append((cube.attrib["currency"], day, float(cube.attrib["rate"])))
            except (ValueError, KeyError):
                continue
    return out


def _ecb_fetch_http() -> list[tuple[str, str, float]]:
    """Rates du jour BCE — BLOQUANT, exécuté dans le threadpool."""
    req = urllib.request.Request(FX_ECB_URL, headers={**YAHOO_UA, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return _parse_ecb_xml(r.read().decode("utf-8"))


async def _fx_refresh(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Rate du jour BCE → fx_rates. Retourne (nb ccy, date) ; None si échec."""
    try:
        rates = await run_in_threadpool(_ecb_fetch_http)
    except Exception:
        return 0, None
    if not rates:
        return 0, None
    today = date.today().isoformat()
    for ccy, day, rate in rates:
        if ccy in FX_SUPPORTED and day <= today:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source) VALUES (?,?,?, 'ecb')",
                (ccy, day, rate),
            )
    conn.commit()
    return len([r for r in rates if r[0] in FX_SUPPORTED]), today


@app.post("/api/fx/refresh")
async def fx_refresh(request: Request):
    """Met à jour les taux de change (BCE). Appel réseau dans le threadpool."""
    u = _need(request)
    conn = db()
    try:
        n, day = await _fx_refresh(conn)
    finally:
        conn.close()
    if n == 0:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    _audit(u["username"], "Mise à jour des taux", f"{n} devises")
    return {"updated": n, "asof": day}


def _parse_ecb_hist(xml_text: str) -> list[tuple[str, str, float]]:
    """Fins de mois (dernier jour BCE dispo du mois) sur l'historique complet :
    (ccy, YYYY-MM-DD, rate) — un seul taux par devise et par mois."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    ns = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
    last: dict[tuple[str, str], tuple[str, float]] = {}  # (ccy, ym) -> (day, rate)
    day = None
    for cube in root.iter(ns + "Cube"):
        if "time" in cube.attrib:
            day = cube.attrib["time"]
        elif "currency" in cube.attrib and day:
            ccy = cube.attrib["currency"]
            try:
                rate = float(cube.attrib["rate"])
            except (ValueError, KeyError):
                continue
            ym = day[:7]
            prev = last.get((ccy, ym))
            if prev is None or day > prev[0]:
                last[(ccy, ym)] = (day, rate)
    return [(ccy, d, r) for (ccy, _ym), (d, r) in last.items()]


def _ecb_fetch_hist_http() -> list[tuple[str, str, float]]:
    """Fins de mois BCE (historique depuis 1999) — BLOQUANT, threadpool."""
    req = urllib.request.Request(FX_ECB_HIST_URL, headers={**YAHOO_UA, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return _parse_ecb_hist(r.read().decode("utf-8"))


@app.post("/api/fx/history")
async def fx_history_backfill(request: Request):
    """Backfill idempotent des fins de mois BCE (conversions historiques
    exactes au lieu du repli 'taux le plus ancien'). ~8 Mo téléchargés une
    fois ; INSERT OR REPLACE, aucune donnée existante touchée."""
    u = _need(request)
    try:
        rates = await run_in_threadpool(_ecb_fetch_hist_http)
    except Exception:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    if not rates:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    conn = db()
    try:
        for ccy, day, rate in rates:
            if ccy in FX_SUPPORTED:
                conn.execute(
                    "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source) VALUES (?,?,?, 'ecb')",
                    (ccy, day, rate),
                )
        conn.commit()
        months = len({day[:7] for ccy, day, _ in rates if ccy in FX_SUPPORTED})
    finally:
        conn.close()
    _audit(u["username"], "Taux historiques", f"backfill {months} mois")
    return {"months": months, "currencies": FX_SUPPORTED[1:]}


# ---------------------------------------------------------------- synthèse
@app.get("/api/summary")
async def summary(request: Request, family: int = 0):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family))
    wc, args = _owner_clause(owners)
    latest = _latest_valuations(conn)
    txns = _txn_summary(conn)
    rows = conn.execute(f"SELECT * FROM accounts WHERE active=1 AND {wc}", args).fetchall()
    by_class = {k: {"key": k, "value": 0.0, "cost": 0.0, "count": 0} for k in CLASS_KEYS}
    total_value = total_cost = 0.0
    asof = None
    fx_missing: list[str] = []
    fx_dates: set[str] = set()
    fx_applied = False
    for r in rows:
        lv = latest.get(r["id"])
        if lv is None:
            continue
        t = txns.get(r["id"])
        cost = t["cost"] if t else (r["cost_basis"] or 0.0)
        ccy = r["currency"] or "EUR"
        # conversion EUR : valeur et coût au taux du jour de la valorisation
        fx = _fx_lookup(conn, ccy, lv["date"], r["fx_override"])
        if fx is None:
            fx_missing.append(r["name"])  # actif non convertible → exclu des totaux EUR
            continue
        fx_applied = True
        value_eur = lv["value"] / fx["rate"] if ccy != "EUR" else lv["value"]
        cost_eur = cost / fx["rate"] if (cost and ccy != "EUR") else cost
        c = by_class[r["asset_class"]]
        c["value"] += value_eur
        c["count"] += 1
        if cost_eur:
            c["cost"] += cost_eur
        total_value += value_eur
        total_cost += cost_eur
        if fx.get("date"):
            fx_dates.add(fx["date"])
        if asof is None or lv["date"] > asof:
            asof = lv["date"]
    conn.close()
    classes = []
    for k in CLASS_KEYS:
        c = by_class[k]
        if c["count"] == 0:
            continue
        c["gain"] = round(c["value"] - c["cost"], 2) if c["cost"] else None
        c["gain_pct"] = round((c["value"] - c["cost"]) / c["cost"] * 100, 2) if c["cost"] else None
        c["share_pct"] = round(c["value"] / total_value * 100, 1) if total_value else 0
        c["emoji"] = CLASS_META[k]["emoji"]
        c["color"] = CLASS_META[k]["color"]
        classes.append(c)
    gain = round(total_value - total_cost, 2) if total_cost else None
    fx_asof = max(fx_dates) if fx_dates else None
    return {
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "gain": gain,
        "gain_pct": round(gain / total_cost * 100, 2) if gain is not None and total_cost else None,
        "asof": asof,
        "classes": classes,
        "nb_accounts": sum(c["count"] for c in by_class.values()),
        # multi-devises : taux utilisés (date max) et actifs exclus faute de taux
        "fx_asof": fx_asof,
        "fx_missing": fx_missing,
        "fx_applied": fx_applied,
    }


# ---------------------------------------------------------------- historique
@app.get("/api/history")
async def history(request: Request, months: int = 60, family: int = 0):
    u = _need(request)
    months = max(6, min(months, 240))
    conn = db()
    owners = _visible_owners(conn, u, bool(family))
    wc, args = _owner_clause(owners)
    rows = conn.execute(
        f"SELECT id, name, asset_class, currency, fx_override, open_date, close_date, active"
        f" FROM accounts WHERE {wc}", args
    ).fetchall()
    vals = conn.execute(
        "SELECT v.account_id, v.val_date, v.value FROM valuations v JOIN accounts a ON a.id = v.account_id"
        f" WHERE {wc} ORDER BY v.val_date, v.id", args
    ).fetchall()
    by_acc: dict[int, list[tuple[str, float]]] = {}
    for v in vals:
        by_acc.setdefault(v["account_id"], []).append((v["val_date"][:10], v["value"]))

    today = date.today()
    y = today.year + (today.month - 1 - (months - 1)) // 12
    mo = (today.month - 1 - (months - 1)) % 12 + 1
    d = date(y, mo, 1)

    labels: list[str] = []
    series = {k: [] for k in CLASS_KEYS}
    totals: list[float] = []
    while d <= today:
        labels.append(d.strftime("%Y-%m"))
        end_str = f"{d.strftime('%Y-%m')}-{calendar.monthrange(d.year, d.month)[1]:02d}"
        msum = 0.0
        for r in rows:
            if not r["active"]:
                continue
            if r["open_date"] and r["open_date"][:10] > end_str:
                continue
            if r["close_date"] and r["close_date"][:10] < end_str:
                continue
            val = 0.0
            ccy = r["currency"] or "EUR"
            fx = None
            for vd, vv in by_acc.get(r["id"], []):
                if vd <= end_str:
                    val = vv
                else:
                    break
            if val and ccy != "EUR":
                # taux BCE au plus proche ≤ fin de mois (sinon override manuel)
                fx = _fx_lookup(conn, ccy, end_str, r["fx_override"])
                if fx is None:
                    val = 0.0  # actif non convertible : exclu de ce mois (approximation assumée)
                else:
                    val = val / fx["rate"]
            series[r["asset_class"]].append(round(val, 2))
            msum += val
        for k in CLASS_KEYS:
            if len(series[k]) < len(labels):
                series[k].append(0.0)
        totals.append(round(msum, 2))
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    conn.close()
    return {
        "labels": labels,
        "series": series,
        "totals": totals,
        "current": totals[-1] if totals else 0,
    }


# ---------------------------------------------------------------- divers
@app.get("/api/version")
async def version():
    # disclaimer optionnel de la page de login (env DISCLAIMER, lu à la
    # demande : configurable par déploiement sans redémarrage)
    return {"version": VERSION, "disclaimer": (os.environ.get("DISCLAIMER") or "").strip() or None}


def _export_data(conn: sqlite3.Connection, username: str) -> dict:
    """Payload JSON complet d'un propriétaire : actifs, valorisations,
    opérations ET règles de revenu (une restauration ne doit rien perdre)."""
    return {
        "app": "patrimony",
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "owner": username,
        "accounts": [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE owner=?", (username,)).fetchall()],
        "valuations": [dict(r) for r in conn.execute(
            "SELECT v.* FROM valuations v JOIN accounts a ON a.id=v.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "transactions": [dict(r) for r in conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "income_rules": [dict(r) for r in conn.execute(
            "SELECT ir.* FROM income_rules ir JOIN accounts a ON a.id=ir.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "positions": [dict(r) for r in conn.execute(
            "SELECT p.* FROM positions p JOIN accounts a ON a.id=p.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "dividend_events": [dict(r) for r in conn.execute(
            "SELECT d.* FROM dividend_events d JOIN positions p ON p.id=d.position_id"
            " JOIN accounts a ON a.id=p.account_id WHERE a.owner=?", (username,)).fetchall()],
    }


def _do_import(u: sqlite3.Row, body: dict) -> str | None:
    """Remplace les données du propriétaire par le payload. Retourne une
    erreur lisible ou None. Transactions + règles incluses (v2026.09.019) ;
    les fichiers anciens (actifs+valorisations seuls) restent acceptés."""
    if body.get("app") != "patrimony" or "accounts" not in body or "valuations" not in body:
        return "Fichier non reconnu"
    conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM accounts WHERE owner=?", (u["username"],))  # cascade enfants
        for a in body["accounts"]:
            conn.execute(
                "INSERT INTO accounts (id, owner, name, asset_class, institution, currency, valuation_mode,"
                " cost_basis, fees_pct, open_date, close_date, notes, active, created_at, updated_at)"
                " VALUES (:id,:owner,:name,:asset_class,:institution,:currency,:valuation_mode,:cost_basis,"
                " :fees_pct,:open_date,:close_date,:notes,:active,:created_at,:updated_at)",
                {**a, "owner": u["username"], "fees_pct": a.get("fees_pct")},
            )
        for v in body["valuations"]:
            conn.execute(
                "INSERT INTO valuations (id, account_id, val_date, value, source, note)"
                " VALUES (:id,:account_id,:val_date,:value,:source,:note)",
                v,
            )
        for t in body.get("transactions") or []:
            conn.execute(
                "INSERT INTO transactions (id, account_id, op_date, kind, amount, note, source_id, created_at)"
                " VALUES (:id,:account_id,:op_date,:kind,:amount,:note,:source_id,:created_at)",
                t,
            )
        for ir in body.get("income_rules") or []:
            conn.execute(
                "INSERT INTO income_rules (id, account_id, label, amount, freq, months_int, next_date, active)"
                " VALUES (:id,:account_id,:label,:amount,:freq,:months_int,:next_date,:active)",
                ir,
            )
        for p in body.get("positions") or []:
            conn.execute(
                "INSERT INTO positions (id, account_id, symbol, label, quantity, pru, active, created_at, updated_at)"
                " VALUES (:id,:account_id,:symbol,:label,:quantity,:pru,:active,:created_at,:updated_at)",
                p,
            )
        for d in body.get("dividend_events") or []:
            conn.execute(
                "INSERT INTO dividend_events (id, position_id, ex_date, per_share, note, created_at)"
                " VALUES (:id,:position_id,:ex_date,:per_share,:note,:created_at)",
                d,
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return f"Import impossible : {e}"
    conn.close()
    return None


@app.get("/api/export")
async def export(request: Request):
    u = _need(request)
    conn = db()
    try:
        data = _export_data(conn, u["username"])
    finally:
        conn.close()
    _audit(u["username"], "Export JSON", f"{len(data['accounts'])} actifs")
    return JSONResponse(data)


class EncIn(BaseModel):
    password: str
    payload: str = ""


@app.post("/api/export/encrypted")
async def export_encrypted(body: EncIn, request: Request):
    """Export chiffré (AES-256-GCM + PBKDF2) : le seul artefact à conserver
    hors de l'instance. Le mot de passe n'est jamais stocké."""
    u = _need(request)
    if len(body.password or "") < 8:
        return JSONResponse({"detail": "Mot de passe trop court (8 caractères minimum)"}, status_code=400)
    conn = db()
    try:
        data = _export_data(conn, u["username"])
    finally:
        conn.close()
    try:
        enc = encrypt_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), body.password)
    except Exception:
        return JSONResponse({"detail": "Chiffrement impossible"}, status_code=500)
    _audit(u["username"], "Export chiffré", f"{len(data['accounts'])} actifs")
    return {"payload": enc}


@app.post("/api/import/encrypted")
async def import_encrypted(body: EncIn, request: Request):
    """Restauration d'une sauvegarde chiffrée — remplace les données du
    propriétaire. Vérification authentifiée : mauvais mot de passe ou fichier
    altéré → 400, aucune donnée touchée."""
    u = _need(request)
    if not body.payload:
        return JSONResponse({"detail": "Fichier manquant"}, status_code=400)
    try:
        plain = decrypt_bytes(body.payload, body.password)
        data = json.loads(plain)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"detail": "Fichier illisible"}, status_code=400)
    err = _do_import(u, data)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    _audit(u["username"], "Restauration chiffrée", f"{len(data['accounts'])} actifs")
    return {"ok": True}


@app.post("/api/import")
async def import_data(request: Request):
    u = _need(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "JSON invalide"}, status_code=400)
    err = _do_import(u, body)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    _audit(u["username"], "Import JSON", f"{len(body['accounts'])} actifs")
    return {"ok": True}


# ---------------------------------------------------------------- imports/exports CSV
# Libellés humains des exports CSV : la langue suit le navigateur
# (Accept-Language, défaut FR) — les IDENTIFIANTS restent canoniques.
_CSV_L10N: dict[str, dict[str, dict[str, str]]] = {
    "cls": {
        "fr": {"comptes": "Comptes courants", "epargne": "Livrets & épargne", "bourse": "Bourse & assurances-vie",
               "immobilier": "Immobilier", "crowdfunding": "Crowdfunding", "crypto": "Cryptomonnaies",
               "metaux": "Métaux précieux", "divers": "Divers"},
        "en": {"comptes": "Current accounts", "epargne": "Savings accounts", "bourse": "Stocks & life insurance",
               "immobilier": "Real estate", "crowdfunding": "Crowdfunding", "crypto": "Cryptocurrencies",
               "metaux": "Precious metals", "divers": "Other"},
        "de": {"comptes": "Girokonten", "epargne": "Sparkonten", "bourse": "Aktien & Lebensversicherung",
               "immobilier": "Immobilien", "crowdfunding": "Crowdfunding", "crypto": "Kryptowährungen",
               "metaux": "Edelmetalle", "divers": "Sonstiges"},
        "lb": {"comptes": "Lafend Konten", "epargne": "Spuerkonten", "bourse": "Aktien & Liewensversécherung",
               "immobilier": "Immobilien", "crowdfunding": "Crowdfunding", "crypto": "Kryptowährungen",
               "metaux": "Edelmetaller", "divers": "Divis"},
    },
    "kind": {
        "fr": {"deposit": "Dépôt", "withdrawal": "Retrait", "income": "Revenu", "expense": "Frais / dépense"},
        "en": {"deposit": "Deposit", "withdrawal": "Withdrawal", "income": "Income", "expense": "Fee / expense"},
        "de": {"deposit": "Einzahlung", "withdrawal": "Auszahlung", "income": "Einkommen", "expense": "Gebühr / Ausgabe"},
        "lb": {"deposit": "Akommes", "withdrawal": "Ofhuelen", "income": "Akommes (Zënssaz…)", "expense": "Frais / Ausgab"},
    },
}
_CSV_LANG_ORDER = ("fr", "en", "de", "lb")


def _csv_lang(request: Request) -> str:
    hdr = request.headers.get("accept-language", "")
    for part in hdr.split(","):
        tag = part.strip().split(";")[0].lower()
        base = tag.split("-")[0]
        if base in _CSV_LANG_ORDER:
            return base
    return "fr"


def _l10n_map(kind: str, lang: str) -> dict[str, str]:
    return _CSV_L10N[kind].get(lang) or _CSV_L10N[kind]["fr"]


class TxCsvIn(BaseModel):
    account_id: int
    default_kind: str = "deposit"
    csv_text: str


TX_KINDS = {"deposit", "withdrawal", "income", "expense"}
_TX_SIGN_FLIP = {
    "deposit": "withdrawal", "withdrawal": "deposit",
    "income": "expense", "expense": "income",
}


def _csv_num(s):
    """Montant CSV -> float ou None. Gère '1 234,56', '1.234,56', débit/crédit '(', '€'."""
    if s is None:
        return None
    s = s.replace("\u00a0", " ").replace(" ", "").replace("€", "").replace("EUR", "").strip()
    if not s:
        return None
    neg = s.startswith("-") or s.startswith("(")
    s = s.lstrip("-(+").rstrip(")")
    if "," in s and "." in s:
        s = s.replace(".", "") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _csv_date(s):
    """Date CSV -> 'YYYY-MM-DD' ou None. Accepte JJ/MM/AAAA, JJ.MM.AAAA, AAAA-MM-JJ…"""
    if not s:
        return None
    s = s.strip().strip('"').split(" ")[0].split("T")[0]
    for pat in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            if s == datetime.strptime(s, pat).strftime(pat):
                return datetime.strptime(s, pat).date().isoformat()
        except ValueError:
            continue
    return None


@app.post("/api/transactions/import-csv")
async def import_tx_csv(body: TxCsvIn, request: Request):
    """Importe un CSV bancaire (opérations) dans un actif appartenant à l'utilisateur.
    Colonnes d'en-tête : date + libellé + montant (ou débit/crédit).
    Montant négatif = type inversé (dépôt↔retrait, revenu↔dépense). Doublons ignorés."""
    u = _need(request)
    if body.default_kind not in TX_KINDS:
        return JSONResponse({"detail": "Type inconnu"}, status_code=400)
    if not body.csv_text or len(body.csv_text) > 2_000_000:
        return JSONResponse({"detail": "Fichier vide ou trop volumineux (2 Mo max)"}, status_code=400)
    conn = db()
    try:
        acc = conn.execute(
            "SELECT id, name FROM accounts WHERE id=? AND owner=?", (body.account_id, u["username"])
        ).fetchone()
        if acc is None:
            return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
        text = body.csv_text.lstrip("\ufeff")
        first = text.splitlines()[0] if text else ""
        delims = [d for d in (",", ";", "\t") if d in first]
        delim = max(delims, key=first.count) if delims else ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        except csv.Error as e:
            return JSONResponse({"detail": f"CSV illisible : {e}"}, status_code=400)
        if len(rows) < 2:
            return JSONResponse({"detail": "Fichier vide (en-tête + au moins une ligne)"}, status_code=400)
        hdr = [c.strip().lower() for c in rows[0]]

        def find_col(*names):
            for i, h in enumerate(hdr):
                if h in names:
                    return i
            return None

        i_date = find_col("date", "op_date", "value_date", "datetime", "date_operation")
        i_note = find_col("note", "libelle", "label", "description", "memo", "libellé", "nom")
        i_amt = find_col("montant", "amount", "total", "montant_euro", "valeur")
        i_db = find_col("debit", "débit")
        i_cr = find_col("credit", "crédit")
        if i_date is None or (i_amt is None and i_db is None and i_cr is None):
            return JSONResponse(
                {"detail": "En-tête incompréhensible — colonnes attendues : date, libellé,"
                 " montant (ou débit/crédit). Séparateur virgule, point-virgule ou tabulation."},
                status_code=400,
            )
        existing = {
            (d, round(a, 2), (n or "").strip().lower())
            for d, a, n in conn.execute(
                "SELECT op_date, amount, note FROM transactions WHERE account_id=?", (body.account_id,)
            )
        }
        cur = conn.cursor()
        inserted = skipped = invalid = 0
        errors, seen = [], set()
        for ln, r in enumerate(rows[1:], start=2):
            if not r or not any(c.strip() for c in r):
                continue
            d = _csv_date(r[i_date]) if i_date < len(r) else None
            amt = _csv_num(r[i_amt]) if i_amt is not None and i_amt < len(r) else None
            if amt is None and (i_db is not None or i_cr is not None):
                dbv = _csv_num(r[i_db]) if i_db is not None and i_db < len(r) else None
                crv = _csv_num(r[i_cr]) if i_cr is not None and i_cr < len(r) else None
                amt = (crv or 0) - (dbv or 0) if (dbv or crv) else None
            note = (r[i_note] or "").strip()[:200] if i_note is not None and i_note < len(r) else ""
            if d is None or amt is None:
                invalid += 1
                if len(errors) < 5:
                    errors.append(f"ligne {ln} : date ou montant invalide")
                continue
            kind = body.default_kind if amt >= 0 else _TX_SIGN_FLIP[body.default_kind]
            a = round(abs(amt), 2)  # montants stockés positifs (le type porte le sens, cf. add_transaction)
            if a == 0:
                invalid += 1
                if len(errors) < 5:
                    errors.append(f"ligne {ln} : montant nul")
                continue
            key = (d, a, note.lower())
            if key in seen or key in existing:
                skipped += 1
                continue
            cur.execute(
                "INSERT INTO transactions (account_id, op_date, kind, amount, note) VALUES (?,?,?,?,?)",
                (body.account_id, d, kind, a, note),
            )
            inserted += 1
            seen.add(key)
        if inserted:
            conn.commit()
    finally:
        conn.close()
    if invalid and inserted == 0 and skipped == 0:
        return JSONResponse({"detail": "Aucune ligne importée — " + "; ".join(errors)}, status_code=400)
    _audit(u["username"], "Import CSV d'opérations", f"#{body.account_id} +{inserted} "
            f"({skipped} doublons, {invalid} invalides)")
    return {"inserted": inserted, "skipped": skipped, "invalid": invalid, "errors": errors[:5]}


# ---------------------------------------------------------------- exports CSV
CSV_KINDS = {
    "accounts": ("actifs", "SELECT * FROM accounts WHERE owner=?"),
    "transactions": (
        "operations",
        "SELECT t.id, a.name AS compte, t.op_date AS date, t.kind AS type, t.amount AS montant,"
        " t.note AS note FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.owner=?",
    ),
    "valuations": (
        "valorisations",
        "SELECT v.id, a.name AS compte, v.val_date AS date, v.value AS valeur, v.source AS source"
        " FROM valuations v JOIN accounts a ON a.id=v.account_id WHERE a.owner=?",
    ),
    "rules": (
        "regles-revenus",
        "SELECT r.id, a.name AS compte, r.label AS libelle, r.amount AS montant, r.freq AS frequence,"
        " r.next_date AS prochaine_date, r.active AS active FROM income_rules r"
        " JOIN accounts a ON a.id=r.account_id WHERE a.owner=?",
    ),
}


@app.get("/api/export/csv/{kind}")
async def export_csv(kind: str, request: Request):
    """Export CSV UTF-8 (BOM pour Excel) de ses propres données, par type."""
    u = _need(request)
    spec = CSV_KINDS.get(kind)
    if spec is None:
        return JSONResponse({"detail": "Type inconnu (accounts|transactions|valuations|rules)"}, status_code=404)
    fname, sql = spec
    conn = db()
    try:
        rows = conn.execute(sql, (u["username"],)).fetchall()
    finally:
        conn.close()
    buf = io.StringIO()
    buf.write("\ufeff")
    if rows:
        cols = list(rows[0].keys())
        lang = _csv_lang(request)
        cls_map = _l10n_map("cls", lang)
        kind_map = _l10n_map("kind", lang)
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(cols)
        for r in rows:
            row = dict(r)
            if "asset_class" in row:
                row["asset_class"] = cls_map.get(row["asset_class"], row["asset_class"])
            if "type" in row:
                row["type"] = kind_map.get(row["type"], row["type"])
            w.writerow([row[k] for k in cols])
    _audit(u["username"], f"Export CSV ({fname})", f"{len(rows)} lignes")
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=patrimony-{kind}-{date.today().isoformat()}.csv"
        },
    )


@app.get("/api/audit")
async def audit_list(request: Request, limit: int = 200):
    """Journal d'audit — admin uniquement. Méta-données : jamais de montants."""
    u = _need(request)
    if u["role"] != "admin":
        return JSONResponse({"detail": "Administrateur requis"}, status_code=403)
    limit = max(10, min(limit, 1000))
    conn = db_main()
    try:
        rows = conn.execute(
            "SELECT ts, username, action, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return {"events": [dict(r) for r in rows]}


# ---------------------------------------------------------------- opérations
KIND_LABELS = {
    "deposit": "Dépôt", "withdrawal": "Retrait",
    "income": "Revenu (intérêt/dividende/loyer)", "expense": "Frais / dépense",
}
KIND_SIGN = {"deposit": 1, "withdrawal": -1, "income": 1, "expense": -1}


class TxIn(BaseModel):
    account_id: int
    op_date: str
    kind: str
    amount: float
    note: str = ""


@app.get("/api/transactions")
async def list_transactions(request: Request, account_id: int | None = None,
                            kind: str | None = None, limit: int = 300):
    u = _need(request)
    limit = max(1, min(limit, 1000))
    where, args = ["a.owner=?"], [u["username"]]
    if account_id:
        where.append("t.account_id=?")
        args.append(account_id)
    if kind and kind in KIND_LABELS:
        where.append("t.kind=?")
        args.append(kind)
    sql = ("SELECT t.*, a.name AS account_name, a.asset_class, a.institution FROM transactions t"
           " JOIN accounts a ON a.id=t.account_id")
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.op_date DESC, t.id DESC LIMIT ?"
    args.append(limit)
    conn = db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["signed"] = round(r["amount"] * KIND_SIGN.get(r["kind"], 1), 2)
        d["kind_label"] = KIND_LABELS.get(r["kind"], r["kind"])
        out.append(d)
    return {"transactions": out, "total": len(out)}


@app.post("/api/transactions")
async def add_transaction(body: TxIn, request: Request):
    u = _need(request)
    if body.kind not in KIND_LABELS:
        return JSONResponse({"detail": "Type d'opération invalide"}, status_code=400)
    if body.amount <= 0:
        return JSONResponse({"detail": "Montant invalide"}, status_code=400)
    conn = db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE id=? AND owner=?", (body.account_id, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    cur = conn.execute(
        "INSERT INTO transactions (account_id, op_date, kind, amount, note) VALUES (?,?,?,?,?)",
        (body.account_id, body.op_date[:10], body.kind, round(body.amount, 2), body.note.strip()),
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Ajout d'opération", f"#{body.account_id} {body.op_date[:10]}")
    return {"id": cur.lastrowid}


@app.delete("/api/transactions/{tid}")
async def delete_transaction(tid: int, request: Request):
    u = _need(request)
    conn = db()
    row = conn.execute(
        "SELECT source_id FROM transactions WHERE id=? AND account_id IN"
        " (SELECT id FROM accounts WHERE owner=?)", (tid, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Opération introuvable"}, status_code=404)
    if (row["source_id"] or "").startswith("div:"):
        conn.close()
        return JSONResponse({"detail": "Dividende géré depuis la ligne du portefeuille"}, status_code=400)
    conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    _audit(u["username"], "Suppression d'opération", f"#{tid}")
    return {"ok": True}


# ---------------------------------------------------------------- revenus passifs
class RuleIn(BaseModel):
    account_id: int
    label: str
    amount: float
    freq: str = "monthly"
    months_int: int = 1
    next_date: str
    active: int = 1


def _freq_months(freq: str, months_int: int) -> int:
    return {"monthly": 1, "quarterly": 3, "yearly": 12}.get(freq, max(1, months_int or 1))


@app.get("/api/income-rules")
async def list_rules(request: Request):
    u = _need(request)
    conn = db()
    rows = conn.execute(
        "SELECT r.*, a.name AS account_name FROM income_rules r JOIN accounts a ON a.id=r.account_id"
        " WHERE a.owner=? ORDER BY r.next_date, r.label", (u["username"],)
    ).fetchall()
    conn.close()
    return {"rules": [dict(r) for r in rows]}


@app.post("/api/income-rules")
async def add_rule(body: RuleIn, request: Request):
    u = _need(request)
    if body.amount <= 0 or not body.label.strip():
        return JSONResponse({"detail": "Libellé ou montant invalide"}, status_code=400)
    conn = db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE id=? AND owner=?", (body.account_id, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    cur = conn.execute(
        "INSERT INTO income_rules (account_id, label, amount, freq, months_int, next_date, active)"
        " VALUES (?,?,?,?,?,?,?)",
        (body.account_id, body.label.strip(), round(body.amount, 2), body.freq,
         body.months_int, body.next_date[:10], body.active),
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Ajout de règle de revenu", f"#{cur.lastrowid} {body.label.strip()}")
    return {"id": cur.lastrowid}


@app.put("/api/income-rules/{rid}")
async def update_rule(rid: int, body: RuleIn, request: Request):
    u = _need(request)
    conn = db()
    row = conn.execute(
        "SELECT r.id FROM income_rules r JOIN accounts a ON a.id=r.account_id"
        " WHERE r.id=? AND a.owner=?", (rid, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Règle introuvable"}, status_code=404)
    conn.execute(
        "UPDATE income_rules SET account_id=?, label=?, amount=?, freq=?, months_int=?, next_date=?,"
        " active=? WHERE id=?",
        (body.account_id, body.label.strip(), round(body.amount, 2), body.freq, body.months_int,
         body.next_date[:10], body.active, rid),
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Modification de règle de revenu", f"#{rid}")
    return {"ok": True}


@app.delete("/api/income-rules/{rid}")
async def delete_rule(rid: int, request: Request):
    u = _need(request)
    conn = db()
    conn.execute(
        "DELETE FROM income_rules WHERE id=? AND account_id IN"
        " (SELECT id FROM accounts WHERE owner=?)", (rid, u["username"])
    )
    conn.commit()
    conn.close()
    _audit(u["username"], "Suppression de règle de revenu", f"#{rid}")
    return {"ok": True}


@app.get("/api/income-calendar")
async def income_calendar(request: Request, months: int = 12):
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    rows = conn.execute(
        "SELECT r.*, a.name AS account_name, a.asset_class FROM income_rules r"
        " JOIN accounts a ON a.id=r.account_id WHERE r.active=1 AND a.owner=? ORDER BY r.label",
        (u["username"],),
    ).fetchall()
    conn.close()
    today = date.today()
    end = date(today.year + (today.month - 1 + months) // 12, (today.month - 1 + months) % 12 + 1, 1)
    out = []
    for r in rows:
        step = _freq_months(r["freq"], r["months_int"])
        d = date.fromisoformat(r["next_date"][:10])
        day0 = min(d.day, 28)

        def _adv(dt: date) -> date:
            y2 = dt.year + (dt.month - 1 + step) // 12
            m2 = (dt.month - 1 + step) % 12 + 1
            return date(y2, m2, min(day0, calendar.monthrange(y2, m2)[1]))

        if d < today:
            n = 0
            while d < today and n < 240:
                d = _adv(d)
                n += 1
        n = 0
        while d < end and n < 60:
            out.append({
                "ym": d.strftime("%Y-%m"),
                "date": d.isoformat(),
                "rule_id": r["id"],
                "label": r["label"],
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "asset_class": r["asset_class"],
                "amount": r["amount"],
            })
            d = _adv(d)
            n += 1
    out.sort(key=lambda x: (x["ym"], -x["amount"]))
    return {"calendar": out, "end_ym": end.strftime("%Y-%m")}


@app.get("/api/income-actual")
async def income_actual(request: Request, months: int = 12):
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    rows = conn.execute(
        "SELECT substr(t.op_date,1,7) ym, SUM(t.amount) total FROM transactions t"
        " JOIN accounts a ON a.id=t.account_id"
        " WHERE t.kind='income' AND a.owner=? GROUP BY ym ORDER BY ym DESC LIMIT ?",
        (u["username"], months),
    ).fetchall()
    conn.close()
    by_ym = {r["ym"]: r["total"] for r in rows}
    today = date.today()
    labels, totals = [], []
    for k in range(months - 1, -1, -1):
        y = today.year + (today.month - 1 - k) // 12
        m = (today.month - 1 - k) % 12 + 1
        ym = f"{y:04d}-{m:02d}"
        labels.append(ym)
        totals.append(round(by_ym.get(ym, 0.0), 2))
    return {"labels": labels, "totals": totals}


# ---------------------------------------------------------------- valorisation auto
@app.post("/api/refresh-prices")
async def refresh_prices(request: Request):
    u = _need(request)
    conn = db()
    today = date.today().isoformat()
    status = []

    async def fetch_sym(symbol: str, asset_class: str) -> tuple[float | None, str | None]:
        """Cours frais (réseau hors event loop) → cache prices → EUR.
        Retourne (prix EUR, erreur)."""
        q = await run_in_threadpool(fetch_quote, symbol, asset_class)
        if not q:
            return None, "cours introuvable"
        conn.execute(
            "INSERT OR REPLACE INTO prices (symbol, price, currency, ts) VALUES (?,?,?,?)",
            (symbol, q["price"], q.get("currency", ""), datetime.now(timezone.utc).isoformat()),
        )
        ccy_q = q.get("currency") or "EUR"
        if ccy_q in ("", "EUR"):
            return q["price"], None
        fx = _fx_lookup(conn, ccy_q, today, None)
        if fx is None:
            try:  # taux manquant : un seul appel BCE, puis échec propre
                rates = await run_in_threadpool(_ecb_fetch_http)
                for cc2, day, rate in rates:
                    if cc2 in FX_SUPPORTED and day <= today:
                        conn.execute(
                            "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source)"
                            " VALUES (?,?,?, 'ecb')", (cc2, day, rate),
                        )
                conn.commit()
                fx = _fx_lookup(conn, ccy_q, today, None)
            except Exception:
                fx = None
        if fx is None:
            return None, "taux de change indisponible (BCE)"
        return q["price"] / fx["rate"], None

    def chart_factor(symbol: str) -> float:
        """close (devise de cotation) → EUR : même conversion que le cours du
        jour (facteur prix_eur/prix brut lus dans le cache prices)."""
        r = conn.execute(
            "SELECT price, currency FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if r is None or (r["currency"] or "EUR") in ("", "EUR") or not r["price"]:
            return 1.0
        px = _pos_quote_eur(conn, symbol)
        return (px["price_eur"] / r["price"]) if px else 1.0

    def insert_today(aid: int, value: float) -> None:
        if value <= 0:
            return
        dup = conn.execute(
            "SELECT id FROM valuations WHERE account_id=? AND val_date=?", (aid, today)
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO valuations (account_id, val_date, value, source) VALUES (?,?,?, 'auto')",
                (aid, today, value),
            )

    # --- actifs auto mono-symbole (crypto…) : chemin historique ---
    rows = conn.execute(
        "SELECT id, name, asset_class, symbol, quantity, open_date FROM accounts"
        " WHERE active=1 AND valuation_mode='auto' AND symbol<>''"
        " AND asset_class<>'bourse' AND owner=?", (u["username"],)
    ).fetchall()
    for r in rows:
        price_eur, err = await fetch_sym(r["symbol"], r["asset_class"])
        if err:
            status.append({"id": r["id"], "name": r["name"], "symbol": r["symbol"], "error": err})
            continue
        assert price_eur is not None
        value = round((r["quantity"] or 0) * price_eur, 2)
        nvals = conn.execute("SELECT COUNT(*) c FROM valuations WHERE account_id=?", (r["id"],)).fetchone()["c"]
        if nvals == 0 and r["asset_class"] not in CRYPTO_AUTO_CLASSES and r["open_date"]:
            years = max(1, min(10, date.today().year - date.fromisoformat(r["open_date"][:10]).year + 1))
            chart = await run_in_threadpool(_yahoo_chart, r["symbol"], f"{years}y", "1mo")
            if chart:
                f = chart_factor(r["symbol"])
                for dstr, close in chart["points"]:
                    ex = conn.execute(
                        "SELECT id FROM valuations WHERE account_id=? AND val_date=?", (r["id"], dstr)
                    ).fetchone()
                    if not ex:
                        conn.execute(
                            "INSERT INTO valuations (account_id, val_date, value, source) VALUES (?,?,?, 'auto')",
                            (r["id"], dstr, round((r["quantity"] or 0) * close * f, 2)),
                        )
        insert_today(r["id"], value)
        status.append({
            "id": r["id"], "name": r["name"], "symbol": r["symbol"],
            "price": round(price_eur, 4), "currency": "EUR", "value": value,
        })

    # --- comptes bourse : portefeuille multi-lignes (positions) ---
    baccs = conn.execute(
        "SELECT a.id, a.name, a.open_date FROM accounts a"
        " WHERE a.active=1 AND a.valuation_mode='auto' AND a.asset_class='bourse' AND a.owner=?",
        (u["username"],),
    ).fetchall()
    if baccs:
        pos_rows = conn.execute(
            "SELECT p.* FROM positions p JOIN accounts a ON a.id=p.account_id"
            " WHERE p.active=1 AND a.asset_class='bourse' AND a.valuation_mode='auto'"
            " AND a.owner=?", (u["username"],)
        ).fetchall()
        by_acc: dict[int, list] = {}
        for p in pos_rows:
            by_acc.setdefault(p["account_id"], []).append(p)
        quotes: dict[str, tuple[float | None, str | None]] = {}
        for s in sorted({p["symbol"] for p in pos_rows}):
            quotes[s] = await fetch_sym(s, "bourse")
        for acc in baccs:
            aid, name = acc["id"], acc["name"]
            poss = by_acc.get(aid, [])
            if not poss:
                status.append({"id": aid, "name": name, "error": "portefeuille vide — ajoutez une ligne"})
                continue
            missing = next((p["symbol"] for p in poss if quotes[p["symbol"]][0] is None), None)
            if missing:
                status.append({"id": aid, "name": name, "symbol": missing,
                               "error": quotes[missing][1] or "cours introuvable"})
                continue
            pos_px = {s: (quotes[s][0] or 0.0) for s in quotes}
            value = round(sum((p["quantity"] or 0) * pos_px[p["symbol"]] for p in poss), 2)
            nvals = conn.execute("SELECT COUNT(*) c FROM valuations WHERE account_id=?", (aid,)).fetchone()["c"]
            if nvals == 0 and acc["open_date"]:
                years = max(1, min(10, date.today().year - date.fromisoformat(acc["open_date"][:10]).year + 1))
                charts: dict[str, dict] = {}
                for s in sorted({p["symbol"] for p in poss}):
                    ch = await run_in_threadpool(_yahoo_chart, s, f"{years}y", "1mo")
                    if ch and ch.get("points"):
                        charts[s] = {d: float(c) for d, c in ch["points"]}
                need = {p["symbol"] for p in poss}
                if charts and need.issubset(charts):
                    months = sorted(set.intersection(*[set(charts[s].keys()) for s in need]))
                    fct = {s: chart_factor(s) for s in need}
                    for dstr in months:
                        if dstr < acc["open_date"][:10]:
                            continue
                        mv = round(sum((p["quantity"] or 0) * charts[p["symbol"]][dstr] * fct[p["symbol"]]
                                       for p in poss), 2)
                        ex = conn.execute(
                            "SELECT id FROM valuations WHERE account_id=? AND val_date=?", (aid, dstr)
                        ).fetchone()
                        if not ex:
                            conn.execute(
                                "INSERT INTO valuations (account_id, val_date, value, source) VALUES (?,?,?, 'auto')",
                                (aid, dstr, mv),
                            )
            insert_today(aid, value)
            status.append({
                "id": aid, "name": name, "symbol": f"{len(poss)} lignes",
                "price": None, "currency": "EUR", "value": value,
            })
    conn.commit()
    conn.close()
    return {"status": status, "asof": today}


# ---------------------------------------------------------------- benchmarks
def _account_cashflows(conn: sqlite3.Connection, owners: list[str]) -> tuple[list[tuple[str, float]], float]:
    """Flux nets par actif des propriétaires donnés : opérations si présentes,
    sinon coût manuel à l'ouverture. Retourne (flows triés, total déposé)."""
    wc, args = _owner_clause(owners)
    rows = conn.execute(
        f"SELECT id, open_date, created_at, cost_basis FROM accounts WHERE active=1 AND {wc}", args
    ).fetchall()
    txn_rows = conn.execute(
        "SELECT t.account_id, t.op_date, t.kind, t.amount FROM transactions t"
        f" JOIN accounts a ON a.id=t.account_id WHERE t.kind IN ('deposit','withdrawal') AND {wc}", args
    ).fetchall()
    by_acc: dict[int, list[tuple[str, float]]] = {}
    for t in txn_rows:
        amt = t["amount"] if t["kind"] == "deposit" else -t["amount"]
        by_acc.setdefault(t["account_id"], []).append((t["op_date"][:10], round(amt, 2)))
    flows: list[tuple[str, float]] = []
    for r in rows:
        if r["id"] in by_acc:
            flows.extend(by_acc[r["id"]])
        elif r["cost_basis"]:
            d = (r["open_date"] or r["created_at"] or "")[:10]
            flows.append((d, round(r["cost_basis"], 2)))
    flows.sort(key=lambda x: x[0])
    deposited = round(sum(a for _, a in flows if a > 0), 2)
    return flows, deposited


def _bench_fetch_http(bench_needs: list[tuple[str, str, int]]) -> list[tuple[str, dict | None]]:
    """Fetch Yahoo des niveaux manquants — BLOQUANT, exécuté dans le threadpool.
    bench_needs: (key, symbol, années) -> [(key, chart|None), ...]"""
    out = []
    for key, symbol, years in bench_needs:
        try:
            out.append((key, _yahoo_chart(symbol, f"{years}y", "1mo")))
        except Exception:
            out.append((key, None))
    return out


async def _fetch_bench_levels(conn: sqlite3.Connection, start_ym: str, force: bool = False) -> None:
    """Complète index_levels. Les appels réseau partent dans le threadpool :
    l'event loop reste libre (le GET /api/benchmarks déclenche ce remplissage)."""
    benches = conn.execute("SELECT key, name, symbol FROM benchmarks WHERE symbol<>''").fetchall()
    today = date.today()
    need: list[tuple[str, str, int]] = []
    for b in benches:
        y, m = int(start_ym[:4]), int(start_ym[5:7])
        missing = conn.execute(
            "SELECT COUNT(*) c FROM index_levels WHERE key=? AND ym>=?", (b["key"], start_ym)
        ).fetchone()["c"]
        span_months = (today.year - y) * 12 + (today.month - m) + 1
        if not force and missing >= min(span_months, 2):
            continue
        years = max(1, min(10, today.year - y + 1))
        need.append((b["key"], b["symbol"], years))
    if need:
        for key, chart in await run_in_threadpool(_bench_fetch_http, need):
            if not chart:
                continue
            for dstr, close in chart["points"]:
                if dstr[:7] < start_ym:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO index_levels (key, ym, level) VALUES (?,?,?)",
                    (key, dstr[:7], close),
                )
    conn.commit()


@app.get("/api/benchmarks")
async def benchmarks(request: Request, family: int = 0):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family))
    flows, deposited = _account_cashflows(conn, owners)
    today = date.today()
    if flows:
        fy = date.fromisoformat(flows[0][0])
    else:
        fy = date(today.year - 4, today.month, 1)
    start_ym = f"{fy.year:04d}-{fy.month:02d}"
    await _fetch_bench_levels(conn, start_ym)
    lvl_rows = conn.execute("SELECT key, ym, level FROM index_levels").fetchall()
    levels: dict[str, dict[str, float]] = {}
    for r in lvl_rows:
        levels.setdefault(r["key"], {})[r["ym"]] = r["level"]
    rate = 2.2
    bench_row = conn.execute("SELECT annual_pct FROM benchmarks WHERE key='livret'").fetchone()
    if bench_row and bench_row["annual_pct"]:
        rate = bench_row["annual_pct"]
    lv = levels.setdefault("livret", {})
    y0, m0 = int(start_ym[:4]), int(start_ym[5:7])
    n = 0
    d = date(y0, m0, 1)
    while d <= today:
        lv[d.strftime("%Y-%m")] = (1 + rate / 100 / 12) ** n
        n += 1
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    latest = _latest_valuations(conn)
    wc, args = _owner_clause(owners)
    tot_value = 0.0
    for r in conn.execute(
        f"SELECT id, currency, fx_override FROM accounts WHERE active=1 AND {wc}", args
    ).fetchall():
        l = latest.get(r["id"])
        if l:
            ccy = r["currency"] or "EUR"
            if ccy != "EUR":
                fx = _fx_lookup(conn, ccy, l["date"], r["fx_override"])
                if fx is None:
                    continue  # actif non convertible : exclu du benchmark utilisateur
                tot_value += l["value"] / fx["rate"]
            else:
                tot_value += l["value"]
    benches = [dict(r) for r in conn.execute("SELECT * FROM benchmarks").fetchall()]
    conn.close()

    rows_out = []
    end_ym = None
    for b in benches:
        lk = levels.get(b["key"], {})
        yms = sorted(lk.keys())
        if not yms:
            continue
        first, last = yms[0], yms[-1]
        end_ym = last if end_ym is None else max(end_ym, last)
        l_first, l_last = lk[first], lk[last]
        span_m = (int(last[:4]) - int(first[:4])) * 12 + (int(last[5:7]) - int(first[5:7]))
        annualized = None
        if span_m >= 3 and l_first > 0:
            annualized = round(((l_last / l_first) ** (12 / span_m) - 1) * 100, 2)
        sim_value = sim_gain = None
        if flows and l_last > 0:
            sv = 0.0
            for fdate, famt in flows:
                fym = fdate[:7]
                if fym < first:
                    fym = first
                elif fym > last:
                    fym = last
                lf = lk.get(fym)
                if lf:
                    sv += famt * (l_last / lf)
            sim_value = round(sv, 2)
            sim_gain = round(sv - deposited, 2)
        rows_out.append({
            "key": b["key"], "name": b["name"], "symbol": b["symbol"],
            "note": b["note"], "annualized": annualized, "sim_value": sim_value,
            "sim_gain": sim_gain, "first_ym": first, "last_ym": last,
        })
    rows_out.sort(key=lambda x: (x["annualized"] is None, -(x["annualized"] or 0)))
    user_ann = None
    user_net = round(sum(a for _, a in flows), 2) if flows else 0.0
    if flows and user_net > 0 and tot_value:
        d0 = date.fromisoformat(flows[0][0])
        days = max(1, (today - d0).days)
        ratio = tot_value / user_net - 1
        if ratio > -1:
            user_ann = round(((1 + ratio) ** (365 / days) - 1) * 100, 2)
    user = {
        "deposited": deposited, "net": user_net, "value": round(tot_value, 2),
        "gain": round(tot_value - user_net, 2) if user_net else None,
        "annualized": user_ann, "first_ym": start_ym, "last_ym": today.strftime("%Y-%m"),
    }
    return {"user": user, "benchmarks": rows_out, "end_ym": end_ym}


@app.post("/api/refresh-benchmarks")
async def refresh_benchmarks(request: Request, family: int = 0):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family))
    flows, _ = _account_cashflows(conn, owners)
    today = date.today()
    fy = date.fromisoformat(flows[0][0]) if flows else date(today.year - 4, today.month, 1)
    await _fetch_bench_levels(conn, f"{fy.year:04d}-{fy.month:02d}", force=True)
    conn.close()
    return await benchmarks(request, family=family)


# ---------------------------------------------------------------- statique
@app.get("/manifest.webmanifest")
async def manifest_pwa():
    return JSONResponse(
        {
            "name": "Patrimony — Data Sovereignty",
            "short_name": "Patrimony",
            "description": "Self-hosted wealth dashboard — your data stays on your network.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0d1117",
            "theme_color": "#0d1117",
            "lang": "fr",
            "icons": [
                {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
    )


@app.get("/sw.js")
async def sw_js():
    """Service worker — nom de cache versionné par VERSION (invalidation au déploiement)."""
    return Response(
        SW_TEMPLATE.replace("__CACHE__", f"patrimony-{VERSION}").replace("__VERSION__", VERSION),
        media_type="text/javascript",
    )


SW_TEMPLATE = """'use strict';
const CACHE = '__CACHE__';
const PRECACHE = ['/', '/index.html', '/manifest.webmanifest', '/logo.png', '/logo-mark.png',
  '/favicon.png', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/maskable-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || !req.url.startsWith(self.location.origin)) return;
  const u = new URL(req.url);
  // API : jamais en cache (fraîcheur + données personnelles au repos)
  if (u.pathname.startsWith('/api/')) return;
  // Navigation : réseau d'abord, repli sur le shell cache (hors ligne)
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).then(r => {
      const cp = r.clone();
      caches.open(CACHE).then(c => c.put('/', cp));
      return r;
    }).catch(() => caches.match('/')));
    return;
  }
  // Statique : cache d'abord, rafraîchi en arrière-plan
  e.respondWith(caches.match(req).then(hit => {
    const upd = fetch(req).then(r => {
      if (r.ok) caches.open(CACHE).then(c => c.put(req, r.clone()));
      return r;
    }).catch(() => hit);
    return hit || upd;
  }));
});
"""


app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")

init_db()
