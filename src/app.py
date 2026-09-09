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
import hashlib
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
from src.tax import compute as tax_compute
from src.tax import TaxInput
from src import fire
from src import fx
from src import l10n
from src import mc
from src import transfer
from src import vault
from src.schema import schema_data
from src import bench
from src import crowdfund
from src import crypto
from src import estate
from src import loans

FX_SUPPORTED = fx.SUPPORTED  # liste canonique des devises (module src/fx.py)

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
# État des coffres ouverts (mono-process) : vit dans src/vault.py —
# alias ci-dessous pour les routes et les tests white-box (même objet).
_VAULTS = vault.VAULTS
_VAULT_GUARD = vault.GUARD
_LOGIN_GUARD = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}  # "ip|user" ou "u|user" -> échecs (monotonic)





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




def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = db_main()
    # v2026.09.041 — WAL (write-ahead logging), persistant dans le fichier :
    # robustesse crash (pas de .db tronqué) + lectures non bloquées pendant
    # une écriture ; busy_timeout : contention = attente au lieu d'erreur.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
            r_salt TEXT DEFAULT '',
            r_auth_salt TEXT DEFAULT '',
            r_wrapped TEXT DEFAULT '',
            r_auth TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    schema_data(conn)
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
    for col, ddl in (
        ("r_salt", "ALTER TABLE vaults ADD COLUMN r_salt TEXT DEFAULT ''"),
        ("r_auth_salt", "ALTER TABLE vaults ADD COLUMN r_auth_salt TEXT DEFAULT ''"),
        ("r_wrapped", "ALTER TABLE vaults ADD COLUMN r_wrapped TEXT DEFAULT ''"),
        ("r_auth", "ALTER TABLE vaults ADD COLUMN r_auth TEXT DEFAULT ''"),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # colonne déjà présente (v2026.09.030 — clé de récupération)
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
        "DELETE FROM cf_operations WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM cf_projects WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM cf_platforms WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    # module Crypto (v2026.09.050) : même purge pour les clairs orphelins
    conn.execute(
        "DELETE FROM cw_scans WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM cw_history WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM cw_transfers WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM cw_wallets WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    conn.execute(
        "DELETE FROM accounts WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    # module Crédits (v2026.09.056) : même purge pour les clairs orphelins
    conn.execute(
        "DELETE FROM loans WHERE owner IN"
        " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username WHERE u.mode='protected')"
    )
    # module Locations & TCO (v2026.09.058) : même purge (imputations → txs,
    # fiches → comptes/crédits, encaissements → contrats/txs, contrats → biens)
    for tbl in ("tco_imputations", "tco_items", "loc_payments", "loc_contracts"):
        conn.execute(
            f"DELETE FROM {tbl} WHERE owner IN"
            " (SELECT v.username FROM vaults v JOIN users u ON u.username=v.username"
            " WHERE u.mode='protected')"
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
    # module Crédits (v2026.09.056) : les crédits liés legacy (accounts.loan_*,
    # immo v033) migrent vers la table loans puis sont neutralisés (idempotent)
    _ = loans.migrate_legacy(conn)
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
        ("CTO (Boursorama)",        "bourse",       "Boursorama",     "2021-01", 12000, 9000, 15800, 0.04),
        ("AV (Linxea Avenir)",      "epargne",      "Linxea",         "2018-06", 25000, 18000, 30500, 0.002),
        ("Appartement locatif",     "immobilier",   "—",              "2021-03", 145000, 150000, 172000, 0.001),
        # (la classe crowdfunding est alimentée par le module → cf. seed_demo)
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
        if name == "Appartement locatif":
            # crédit du bien (démo v2026.09.056) : le passif vit dans le module
            # Crédits (table loans liée au compte) — équité = valeur − restant
            conn.execute(
                "INSERT INTO loans (owner, name, loan_type, lender, currency,"
                " principal_initial, principal_remaining, rate_annual,"
                " monthly_payment, start_date, account_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner, "Prêt Appartement locatif", "immo", "", "EUR",
                 92000, 92000, 2.8, 520, open_ym + "-01", aid),
            )
            # estimation indicative (v2026.09.060) : 65 m² × 2 800 €/m²
            conn.execute(
                "UPDATE accounts SET area_m2=?, price_m2=? WHERE id=?",
                (65, 2800, aid),
            )
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
    # v2026.09.054 : wrappers fiscaux (PEA/CTO/AV) + lignes titres démo
    for nm, wr in (("PEA (ETF MSCI World)", "pea"), ("CTO (Boursorama)", "cto"),
                   ("AV (Linxea Avenir)", "av")):
        conn.execute("UPDATE accounts SET wrapper=? WHERE name=? AND owner=?",
                     (wr, nm, owner))
    cto = conn.execute(
        "SELECT id FROM accounts WHERE name='CTO (Boursorama)' AND owner=?",
        (owner,)).fetchone()
    if cto:
        ts = datetime.now(timezone.utc).isoformat()
        pa = conn.execute(
            "INSERT INTO positions (account_id, symbol, label, quantity, pru)"
            " VALUES (?,?,?,?,?)",
            (cto["id"], "AI.PA", "Air Liquide", 12, 141.5))
        pb = conn.execute(
            "INSERT INTO positions (account_id, symbol, label, quantity, pru)"
            " VALUES (?,?,?,?,?)",
            (cto["id"], "IWDA.AS", "iShares Core MSCI World UCITS ETF", 38, 118.4))
        for pid, sym, ps, exd in ((pa.lastrowid, "AI.PA", 3.2, "2026-05-15"),
                                  (pb.lastrowid, "IWDA.AS", 0.35, "2026-02-20")):
            conn.execute(
                "INSERT INTO dividend_events (position_id, ex_date, per_share)"
                " VALUES (?,?,?)", (pid, exd, ps))
        for sym, px in (("AI.PA", 168.4), ("IWDA.AS", 135.2)):
            conn.execute(
                "INSERT OR REPLACE INTO prices (symbol, price, currency, ts)"
                " VALUES (?,?,?,?)", (sym, px, "", ts))
    # module Crowdfunding (démo) : plateformes + projets fictifs → comptes-auto
    crowdfund.seed_demo(conn, owner)
    # module Crypto (démo) : wallet fictif synthétique (hors-ligne) → compte-auto
    crypto.seed_demo(conn, owner)
    # module Locations & TCO (démo v2026.09.058) : contrat de location du bien
    # + encaissements mensuels historiques (SANS op matérialisée : import
    # initial — la matérialisation income est couverte par les tests), fiche
    # TCO du bien, véhicule financé + dépenses imputées
    accs = {r["name"]: r["id"] for r in conn.execute(
        "SELECT name, id FROM accounts WHERE owner=?", (owner,)).fetchall()}
    apt = accs.get("Appartement locatif")
    ccur = accs.get("Compte courant")
    if apt:
        loan = conn.execute(
            "SELECT id FROM loans WHERE owner=? AND account_id=?", (owner, apt)).fetchone()
        conn.execute(
            "INSERT INTO tco_items (owner, kind, label, account_id, loan_id)"
            " VALUES (?,?,?,?,?)",
            (owner, "immo", "Appartement locatif", apt,
             loan["id"] if loan else None))
        conn.execute(
            "INSERT INTO loc_contracts (owner, account_id, tenant, rent_monthly,"
            " deposit, start_date, active) VALUES (?,?,?,?,?,?,1)",
            (owner, apt, "M. Weber", 1250, 2500, "2021-04-01"))
        cid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        # encaissements du 3 de chaque mois, de 2021-04 au mois courant si le
        # 3 est déjà passé (sinon mois précédent — jamais de loyer futur)
        ym = "2021-04"
        cur = f"{date.today().year:04d}-{date.today().month:02d}"
        last_ym = cur if date.today().day >= 4 else estate._ym_add(cur, -1)
        while ym <= last_ym:
            conn.execute(
                "INSERT INTO loc_payments (owner, contract_id, op_date, amount,"
                " month, notes) VALUES (?,?,?,?,?,?)",
                (owner, cid, ym + "-03", 1250, ym, ""))
            ym = estate._ym_add(ym, 1)
    # véhicule financé : crédit auto (table loans, sans bien lié) + fiche TCO
    if ccur:
        tl = conn.execute(
            "INSERT INTO loans (owner, name, loan_type, lender, currency,"
            " principal_initial, principal_remaining, rate_annual,"
            " monthly_payment, insurance_monthly, start_date, account_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, "Prêt Tesla", "auto", "ING", "EUR", 30000, 30000, 3.9,
             884, 0, "2024-03-01", None)).lastrowid
        conn.execute(
            "INSERT INTO tco_items (owner, kind, label, account_id, loan_id,"
            " purchase_date, purchase_price, resale_value, resale_date, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (owner, "vehicle", "Tesla Model 3", None, tl, "2024-02-15",
             39990, 28000, "2025-06-30",
             "Achetée 39 990 € — apport 9 990 € + crédit 30 000 €"))
        # estimation argus 28 000 € au 2025-06-30 (> 12 mois au 2026-09 :
        # le badge « à réactualiser » est visible en démo)
        # dépenses imputées (ops expense sur le compte courant — sans impact
        # sur le coût/gain, le modèle ne compte que deposit/income/withdrawal)
        for d, note, amount, item_kind, cat in (
            ("2025-03-10", "Assurance auto Tesla", 480, "vehicle", "insurance"),
            ("2025-09-20", "Entretien Tesla (révision)", 210, "vehicle", "maintenance"),
            ("2026-01-15", "Carburant Tesla", 140, "vehicle", "fuel"),
            ("2025-10-15", "Taxe foncière Appartement", 720, "immo", "tax"),
            ("2026-06-10", "Assurance habitation Appartement (PNO)", 180,
             "immo", "insurance"),
        ):
            tid = conn.execute(
                "INSERT INTO transactions (account_id, op_date, kind, amount,"
                " note) VALUES (?,?,?,?,?)",
                (ccur, d, "expense", amount, note)).lastrowid
            item = conn.execute(
                "SELECT id FROM tco_items WHERE owner=? AND kind=? LIMIT 1",
                (owner, item_kind)).fetchone()
            if item:
                conn.execute(
                    "INSERT INTO tco_imputations (transaction_id, owner, item_id,"
                    " category) VALUES (?,?,?,?)",
                    (tid, owner, item["id"], cat))
    conn.commit()
    conn.close()


def _add_months(_y: int, m: int) -> date:
    base = date.today().replace(day=1)
    y = base.year + (base.month - 1 + m) // 12
    mo = (base.month - 1 + m) % 12 + 1
    return date(y, mo, 1)


# ---------------------------------------------------------------- coffres (comptes protégés)
# Le domaine coffre (état, chiffrement, canary, flush, auto-lock, clé de
# récupération) vit dans src/vault.py — ce fichier ne garde que la couche
# HTTP : gardes d'authentification, audit et ce middleware de persistance.


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
        conn = db_main()
        try:
            try:
                row = conn.execute(
                    "SELECT u.username FROM sessions s JOIN users u ON u.username=s.username"
                    " WHERE s.token=? AND s.expires_at>?",
                    (token, datetime.now(timezone.utc).isoformat()),
                ).fetchone()
            except Exception:
                row = None
            if row:
                with _VAULT_GUARD:
                    v = _VAULTS.get(row["username"])
                    if v is not None and token in v["sessions"]:
                        v["conn"].rollback()  # annule d'éventuels résidus non commités
                        vault.flush(row["username"], v, conn)
                        vault.gc(row["username"], v, conn)
        finally:
            conn.close()
    return resp


class VaultLocked(Exception):
    pass


class TokenScopeDenied(Exception):
    """Jeton API à portée 'capture' utilisé hors de ses deux appels autorisés."""


class ScopeError(Exception):
    """Cible de consultation membre invalide (membre=...)."""

    def __init__(self, detail: str, status: int):
        super().__init__(detail)
        self.detail = detail
        self.status = status




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
            # Portée 'crowdfund' (extension de capture Bricks/LPB) : SEULEMENT
            # l'ingestion des captures du module et la lecture du rapport.
            if row is not None and row["token_scope"] == "crowdfund":
                p = request.url.path
                allowed = (
                    request.method == "POST" and p == "/api/cf/sync/ingest"
                ) or (
                    request.method == "GET" and p == "/api/cf/sync/report"
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


def _member_target(conn: sqlite3.Connection, u: sqlite3.Row, member: str) -> str:
    """Valide la cible d'une consultation membre (v2026.09.043) : admin requis ;
    cible standard UNIQUEMENT — un compte protected répond 404 indistinguable
    d'un compte inexistant (garantie coffre : l'admin ne voit rien des coffres)."""
    if u["role"] != "admin":
        raise ScopeError("Administrateur requis", 403)
    row = conn.execute(
        "SELECT username FROM users WHERE username=? AND role='member' AND mode='standard'",
        (member,),
    ).fetchone()
    if row is None:
        raise ScopeError("Membre introuvable", 404)
    return row["username"]


def _visible_owners(conn: sqlite3.Connection, u: sqlite3.Row, family: bool = False,
                    member: str | None = None) -> list[str]:
    """Propriétaires dont les données sont visibles : soi-même, et si l'admin
    demande la vue famille, tous les membres 'standard' (jamais 'protected') ;
    member=... = vue d'UN membre (admin, standard uniquement)."""
    if member:
        return [_member_target(conn, u, member)]
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


@app.middleware("http")
async def _l10n_middleware(request: Request, call_next):
    """Localisation serveur (v2026.09.035) : le code émet toujours le FR
    (source lisible, repli par défaut) ; cette couche traduit les réponses
    JSON selon Accept-Language — messages d'erreur (detail) et disclaimer.
    Exports exclus (content-disposition) ; toute erreur de traduction
    renvoie la réponse originale intacte."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if not ct.startswith("application/json") or response.headers.get("content-disposition"):
        return response
    al = request.headers.get("accept-language", "")
    chunks = []
    try:
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)
        if b'"detail"' in body or b'"disclaimer"' in body:
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("detail"), str):
                    data["detail"] = l10n.translate_detail(data["detail"], al)
                if isinstance(data.get("disclaimer"), str):
                    data["disclaimer"] = l10n.translate_disclaimer(data["disclaimer"], al)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        hdrs = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=body, status_code=response.status_code,
                        headers=hdrs, media_type=ct)
    except Exception:
        hdrs = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        body = b"".join(chunks)
        return Response(content=body, status_code=response.status_code,
                        headers=hdrs, media_type=ct)


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


@app.exception_handler(ScopeError)
async def _scope_err(_req, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


# ---------------------------------------------------------------- auth routes
# Événements autorisés pour un membre PROTÉGÉ dans le journal principal :
# uniquement l'authentification (sans détail). Ses actions de données ne
# sont pas journalisées — un journal dans son coffre serait réinscriptible
# par son détenteur (mauvaise piste d'audit) et nommerait ses actifs.
_AUDIT_AUTH_EVENTS = {
    "Connexion", "Échec de connexion", "Déconnexion",
    "Initialisation du coffre", "Ouverture du coffre",
    "Armement de la clé de récupération", "Récupération par clé de secours",
    "Échec de récupération",
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
    materials = vault.public_materials(conn, row["username"]) if row["mode"] == "protected" else None
    conn.close()
    response.set_cookie(
        COOKIE, token, max_age=TTL_DAYS * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE
    )
    _audit(uname, "Connexion", ip)
    return {
        "ok": True,
        "mode": row["mode"],
        "must_change": bool(row["must_change"]),
        "vault_init": materials is not None,
        "salt": (materials or {}).get("salt", ""),
        "wrapped": (materials or {}).get("wrapped", ""),
        "recovery_armed": bool(materials and materials["recovery_armed"]),
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
                        vault.gc(uname["username"], v, conn)
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
        materials = vault.public_materials(conn, row["username"])
        conn.close()
        out["vault_init"] = materials is not None
        out["salt"] = (materials or {}).get("salt", "")
        out["wrapped"] = (materials or {}).get("wrapped", "")
        out["recovery_armed"] = bool(materials and materials["recovery_armed"])
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
    if scope not in ("full", "capture", "crowdfund"):
        return JSONResponse({"detail": "Portée invalide (full|capture|crowdfund)"}, status_code=400)
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
        has = vault.has_vault(conn, u["username"])
        if has and (not body.wrapped or not body.salt):
            conn.close()
            return JSONResponse(
                {"detail": "Re-chiffrement du coffre requis (wrapped + salt)"}, status_code=400
            )
        if has:
            vault.rewrap(conn, u["username"], body.salt, body.wrapped)
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
    assert token is not None  # route authentifiée par cookie (session obligatoire)
    try:
        dek = vault.b64d(body.dek)
    except Exception:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    if len(dek) != 32:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    conn = db_main()
    try:
        try:
            # base mémoire + transfert des données claires + ligne vaults +
            # ouverture pour la session (le domaine vit dans src/vault.py)
            vault.init_vault(conn, u["username"], body.salt, body.wrapped, dek, token)
        except vault.VaultError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        # effacement des données claires (après chiffrement — reliquat purgé au boot)
        conn.execute("DELETE FROM cf_operations WHERE owner=?", (u["username"],))
        conn.execute("DELETE FROM cf_projects WHERE owner=?", (u["username"],))
        conn.execute("DELETE FROM cf_platforms WHERE owner=?", (u["username"],))
        conn.execute("DELETE FROM accounts WHERE owner=?", (u["username"],))
        conn.commit()
    finally:
        conn.close()
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
    assert token is not None  # route authentifiée par cookie (session obligatoire)
    try:
        dek = vault.b64d(body.dek)
    except Exception:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    if len(dek) != 32:
        return JSONResponse({"detail": "Clé de coffre invalide"}, status_code=400)
    conn = db_main()
    try:
        try:
            vault.open_vault(conn, u["username"], dek, token)
        except vault.VaultError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
    finally:
        conn.close()
    _audit(u["username"], "Ouverture du coffre")
    return {"ok": True}


# ------------------------------------------------- clé de récupération du coffre
class RecoveryArmIn(BaseModel):
    r_salt: str       # b64 : sel du wrap DEK sous la clé de récupération
    r_auth_salt: str  # b64 : sel de la preuve d'authentification
    r_wrapped: str    # b64(nonce+ct) : DEK chiffrée sous PBKDF2(clé, r_salt)
    r_auth: str       # b64 : PBKDF2(clé, r_auth_salt) — preuve stockée (jamais renvoyée)


@app.post("/api/vault/recovery")
async def vault_recovery_arm(body: RecoveryArmIn, request: Request):
    """Arme (ou remplace) la clé de récupération d'un coffre. Exige une
    session au coffre OUVERT : seul le détenteur de la DEK peut produire
    r_wrapped, et l'ancienne clé est invalidée d'un coup (UPDATE)."""
    u = _need_main(request)
    if u["mode"] != "protected":
        return JSONResponse({"detail": "Compte non protégé"}, status_code=400)
    token = request.cookies.get(COOKIE)
    assert token is not None  # route authentifiée par cookie (session obligatoire)
    conn = db_main()
    try:
        try:
            vault.arm_recovery(conn, u["username"], token,
                               body.r_salt, body.r_auth_salt, body.r_wrapped, body.r_auth)
        except vault.VaultError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
    finally:
        conn.close()
    _audit(u["username"], "Armement de la clé de récupération")
    return {"ok": True}


class RecoveryStartIn(BaseModel):
    username: str


@app.post("/api/vault/recover/start")
async def vault_recover_start(body: RecoveryStartIn, request: Request):
    """Étape 1 du mot de passe oublié : renvoie les matériaux de la clé de
    récupération (publics — le login renvoie déjà salt+wrapped au monde).
    Réponse générique si le compte n'existe pas / n'est pas protégé / n'a
    pas de clé armée (anti-énumération)."""
    uname = body.username.strip()
    conn = db_main()
    try:
        m = vault.recovery_materials(conn, uname)
    finally:
        conn.close()
    if m is None:
        return JSONResponse({"detail": "Récupération impossible"}, status_code=400)
    return {"ok": True, "r_salt": m["r_salt"], "r_auth_salt": m["r_auth_salt"],
            "r_wrapped": m["r_wrapped"]}


class RecoveryIn(BaseModel):
    username: str
    proof: str        # b64 : PBKDF2(clé de récupération, r_auth_salt) — authentifie
    dek: str          # b64 : DEK déchiffrée localement avec la clé
    new_password: str
    wrapped: str      # re-wrap de la DEK sous le nouveau mot de passe
    salt: str


@app.post("/api/vault/recover")
async def vault_recover(body: RecoveryIn, request: Request, response: Response):
    """Mot de passe oublié : preuve par la clé de récupération + nouvelle
    DEK (déchiffrée côté client) + nouveau mot de passe. Combine login,
    ouverture du coffre et changement de mot de passe — l'ancien mdp n'est
    pas requis (perdu par définition)."""
    uname = body.username.strip()
    conn = db_main()
    try:
        try:
            # preuve par la clé de récupération + DEK déchiffrée côté client :
            # toute la logique de vérification vit dans src/vault.py (VaultError
            # audit=1 → « Échec de récupération » à journaliser)
            state = vault.prepare_recover(
                conn, uname, body.proof, body.dek, body.new_password,
                body.wrapped, body.salt, MIN_PASSWORD_LEN)
        except vault.VaultError as e:
            if e.audit:
                _audit(uname, "Échec de récupération")
            return JSONResponse({"detail": str(e)}, status_code=400)
        token = _mk_session(conn, uname)
        conn.execute(
            "UPDATE users SET password=?, must_change=0 WHERE username=?",
            (_hash(body.new_password), uname))
        conn.execute(
            "UPDATE vaults SET salt=?, wrapped=?, updated_at=datetime('now') WHERE username=?",
            (body.salt, body.wrapped, uname))
        conn.commit()
    finally:
        conn.close()
    vault.register(uname, state["conn"], state["dek"], token)
    response.set_cookie(COOKIE, token, max_age=TTL_DAYS * 86400, httponly=True,
                        samesite="lax", secure=COOKIE_SECURE)
    _audit(uname, "Récupération par clé de secours")
    return {"ok": True, "must_change": False}


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
    vault.unregister(uname)
    conn.execute("DELETE FROM sessions WHERE username=?", (uname,))
    conn.execute("DELETE FROM users WHERE username=?", (uname,))  # cascade : vaults
    conn.execute("DELETE FROM cf_operations WHERE owner=?", (uname,))
    conn.execute("DELETE FROM cf_projects WHERE owner=?", (uname,))
    conn.execute("DELETE FROM cf_platforms WHERE owner=?", (uname,))
    conn.execute("DELETE FROM tco_imputations WHERE owner=?", (uname,))
    conn.execute("DELETE FROM tco_items WHERE owner=?", (uname,))
    conn.execute("DELETE FROM loc_payments WHERE owner=?", (uname,))
    conn.execute("DELETE FROM loc_contracts WHERE owner=?", (uname,))  # Locations & TCO v2026.09.058
    conn.execute("DELETE FROM loans WHERE owner=?", (uname,))  # module Crédits v2026.09.056
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
    # estimation indicative immo (v2026.09.060) : surface × prix/m² de
    # référence — JAMAIS une valuation : proposée, appliquée à la main
    p["estimated"] = None
    if (row["asset_class"] == "immobilier" and p.get("area_m2")
            and p.get("price_m2")):
        p["estimated"] = round(p["area_m2"] * p["price_m2"], 2)
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
        fxr = fx.lookup(conn, ccy, latest["date"], row["fx_override"]) if conn else None
        if fxr is None:
            p["fx"] = {"rate": None, "value_eur": None, "error": "rate_missing"}
        else:
            p["fx"] = {
                "rate": fxr["rate"],
                "date": fxr["date"],
                "source": fxr["source"],
                "value_eur": round(latest["value"] / fxr["rate"], 2),
                "stale": fx.warn(fxr, latest["date"]),
            }
    # module Crédits (v2026.09.057) : crédit lié au bien (équité « valeur −
    # restant » + courbe dans la ligne Actifs) — le passif vit dans loans
    p["loan"] = None
    if conn and row["asset_class"] == "immobilier":
        loan = conn.execute(
            "SELECT id, name, currency, principal_remaining, rate_annual,"
            " monthly_payment FROM loans WHERE account_id=? AND active=1"
            " ORDER BY id LIMIT 1", (row["id"],)
        ).fetchone()
        if loan:
            p["loan"] = dict(loan)
    return p


# ---------------------------------------------------------------- routes actifs
@app.get("/api/accounts")
async def list_accounts(request: Request, member: str = ""):
    u = _need(request)
    conn = db()
    owner = _member_target(conn, u, member) if member else u["username"]
    try:
        latest = _latest_valuations(conn)
        txns = _txn_summary(conn)
        rows = conn.execute(
            "SELECT * FROM accounts WHERE owner=? ORDER BY asset_class, name", (owner,)
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


def _fire_num(q: dict, name: str, default: float) -> float:
    v = q.get(name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@app.get("/api/fire/simulate")
async def fire_simulate(request: Request,
                        principal: float = -1, savings_month: float = -1,
                        expenses_month: float = -1, pension_month: float = 0,
                        return_pct: float = -1, inflation_pct: float = -1,
                        swr_pct: float = -1, max_years: int = 70):
    """Simulation FIRE déterministe (v2026.09.034) — moteur pur src/fire.
    Les défauts (return/inflation/swr) viennent des settings du membre
    (clés fire_*, réglables et persistées comme les hypothèses fiscales).
    Montants MENSUELS en entrée (×12 pour le moteur annuel). Réponse en
    années civiles à partir de l'année courante + sensibilité ±2 pts."""
    u = _need(request)
    conn = db()
    try:
        st = _get_settings(conn, u["username"])
    finally:
        conn.close()
    r_pct = return_pct if return_pct >= 0 else st["fire_return"]
    i_pct = inflation_pct if inflation_pct >= 0 else st["fire_inflation"]
    s_pct = swr_pct if swr_pct >= 0 else st["fire_swr"]
    if principal < 0 or savings_month < 0 or expenses_month < 0 or pension_month < 0:
        return JSONResponse({"detail": "Montants invalides (>= 0 attendus)"}, status_code=400)
    if not (-5 <= r_pct <= 25 and 0 <= i_pct <= 15 and 0 < s_pct <= 25):
        return JSONResponse({"detail": "Paramètres hors plage (rendement -5..25, inflation 0..15, retrait 0..25)"}, status_code=400)
    if not (0 < max_years <= 100):
        return JSONResponse({"detail": "Horizon invalide (1-100 ans)"}, status_code=400)
    out = fire.simulate(principal, savings_month * 12, expenses_month * 12,
                        pension_month * 12, r_pct, i_pct, s_pct, max_years)
    year0 = datetime.now().year
    res = {
        "year0": year0,
        "fire": None if out["fire"] is None else {
            "year": year0 + out["fire"]["t"], "t": out["fire"]["t"],
            "capital": out["fire"]["capital"],
        },
        "exhausted": out["exhausted"], "retired": out["retired"],
        "real_return_pct": out["real_return_pct"],
        "net_expenses_year0": out["net_expenses_year0"],
        "rows": [{"year": year0 + r_["t"], "t": r_["t"], "capital": r_["capital"],
                  "target": r_["target"], "retired": r_["retired"]} for r_ in out["rows"]],
        "sensitivity": [{"return_pct": s_["return_pct"],
                         "year": None if s_["fire_t"] is None else year0 + s_["fire_t"]}
                        for s_ in fire.sensitivity(principal, savings_month * 12,
                                                   expenses_month * 12, pension_month * 12,
                                                   r_pct, i_pct, s_pct, max_years=max_years)],
    }
    return res


@app.get("/api/fire/montecarlo")
async def fire_montecarlo(request: Request,
                          principal: float = -1, savings_month: float = -1,
                          expenses_month: float = -1, pension_month: float = 0,
                          return_pct: float = -1, inflation_pct: float = -1,
                          swr_pct: float = -1, max_years: int = 70,
                          n_sims: int = 2000, seed: int = -1,
                          index: str = "iwda"):
    """Monte-Carlo FIRE par bootstrap (v2026.09.042, design Fred 2026-09-07) :
    même contrat que /api/fire/simulate (montants mensuels, plages, défauts
    fire_*) mais le rendement constant est remplacé par des rendements
    annuels réels tirés d'une série mensuelle longue (défaut : ETF monde
    IWDA.L, profondeur maximale Yahoo, cache index_levels sous la clé
    réservée 'mc:<key>' — invisible du comparateur de benchmarks). Sortie :
    taux de réussite par horizon (capital jamais <= 0), capital médian à
    l'horizon final, année médiane d'épuisement."""
    u = _need(request)
    conn = db()
    try:
        st = _get_settings(conn, u["username"])
        r_pct = return_pct if return_pct >= 0 else st["fire_return"]
        i_pct = inflation_pct if inflation_pct >= 0 else st["fire_inflation"]
        s_pct = swr_pct if swr_pct >= 0 else st["fire_swr"]
        bench_row = conn.execute(
            "SELECT key, name, symbol FROM benchmarks WHERE key=?", (index,)
        ).fetchone()
    finally:
        conn.close()
    if principal < 0 or savings_month < 0 or expenses_month < 0 or pension_month < 0:
        return JSONResponse({"detail": "Montants invalides (>= 0 attendus)"}, status_code=400)
    if not (-5 <= r_pct <= 25 and 0 <= i_pct <= 15 and 0 < s_pct <= 25):
        return JSONResponse({"detail": "Paramètres hors plage (rendement -5..25, inflation 0..15, retrait 0..25)"}, status_code=400)
    if not (0 < max_years <= 100):
        return JSONResponse({"detail": "Horizon invalide (1-100 ans)"}, status_code=400)
    if not (100 <= n_sims <= 5000):
        return JSONResponse({"detail": "n_sims invalide (100-5000)"}, status_code=400)
    if bench_row is None or not (bench_row["symbol"] or ""):
        return JSONResponse({"detail": "Indice inconnu (sp500|nasdaq|iwda|stoxx|cac)"}, status_code=400)
    mkey = "mc:" + bench_row["key"]
    yahoo = bench_row["symbol"]
    today = date.today()
    # 1) série longue en cache (clé réservée mc:<key>)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT ym, level FROM index_levels WHERE key=? ORDER BY ym", (mkey,)
        ).fetchall()
    finally:
        conn.close()
    levels = {r["ym"]: r["level"] for r in rows}
    # dernier point dans les 3 derniers mois ? premier point >= 15 ans en arrière ?
    ym_now = today.year * 12 + today.month
    recent = any((int(ym[:4]) * 12 + int(ym[5:7])) >= ym_now - 3 for ym in levels)
    first_ok = any(ym <= f"{today.year - 15:04d}-12" for ym in levels)
    if not levels or not recent or not first_ok:
        # fetch profond (range max = profondeur réelle de l'ETF) dans le threadpool
        chart = await run_in_threadpool(_yahoo_chart, yahoo, "max", "1mo")
        if chart and chart.get("points"):
            conn = db()
            try:
                for dstr, close in chart["points"]:
                    if len(dstr) >= 7 and close is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO index_levels (key, ym, level) VALUES (?,?,?)",
                            (mkey, dstr[:7], close),
                        )
                conn.commit()
            finally:
                conn.close()
            levels = {dstr[:7]: close for dstr, close in chart["points"] if close is not None}
    blocks = mc.make_blocks(levels)
    if len(blocks) < 5:
        return JSONResponse(
            {"detail": "Série de rendements indisponible (réseau ou profondeur) — réessayez plus tard"},
            status_code=502,
        )
    shallow = len(blocks) < 60  # < ~6 ans de fenêtres : résultats indicatifs
    seed_used = seed if seed >= 0 else None
    res = await run_in_threadpool(
        mc.simulate, principal, savings_month * 12, expenses_month * 12,
        pension_month * 12, r_pct, i_pct, s_pct, max_years, blocks,
        n_sims, seed_used,
    )
    _audit(u["username"], "Monte-Carlo FIRE", f"{n_sims} sims x {max_years} ans "
           f"({bench_row['key']}, {len(blocks)} blocs)")
    year0 = datetime.now().year
    return {
        "year0": year0,
        "index": bench_row["key"], "index_name": bench_row["name"],
        "shallow": shallow,
        "series": {"months": len(levels), "blocks": len(blocks)},
        "n_sims": res["n_sims"], "seed_used": res["seed_used"],
        "horizons": [{"year": year0 + h["t"], "t": h["t"], "success_pct": h["success_pct"]}
                     for h in res["horizons"]],
        "p50_capital_end": res["p50_capital_end"],
        "median_exhaustion_t": res["median_exhaustion_t"],
        "exhausted_pct": res["exhausted_pct"],
    }


@app.get("/api/tax-estimate")
async def tax_estimate(account_id: int, request: Request, year: int = 2026,
                       opt: str = "", sub: int = -1):
    """Estimation fiscale « si liquidation aujourd'hui » d'un actif
    (v2026.09.031) : appelle le moteur PUR src/tax (règles FR/LU versionnées)
    avec le profil de l'actif — coût effectif, dernière valorisation,
    date d'ouverture, enveloppe, pays fiscal. Réponse = breakdown complet
    (lignes à ids de règles auditable, warnings/assumptions explicites).
    v2026.09.032 : les hypothèses du foyer viennent des settings du membre
    (GET/PUT /api/settings) ; opt=2op|3cn et sub=0|1 surchargent la
    simulation pour CET appel (options barème / détention substantielle LU)."""
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND owner=?",
            (account_id, u["username"]),
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
        if not (row["tax_country"] or "").strip():
            return JSONResponse(
                {"detail": "Pays fiscal non renseigné — définissez-le dans l'actif"},
                status_code=400,
            )
        latest = _latest_valuations(conn).get(account_id)
        if latest is None:
            return JSONResponse(
                {"detail": "Aucune valorisation — impossible d'estimer"},
                status_code=400,
            )
        cost = row["cost_basis"] or 0.0
        txn = _txn_summary(conn).get(account_id)
        if txn and txn["has_tx"]:
            cost = txn["cost"]
        s = _get_settings(conn, u["username"])
        if opt not in ("", "2op", "3cn"):
            return JSONResponse({"detail": "Option invalide (2op|3cn)"}, status_code=400)
        try:
            res = tax_compute(TaxInput(
                country=(row["tax_country"] or "").strip().lower(),
                asset_class=row["asset_class"],
                wrapper=row["wrapper"] or "",
                open_date=row["open_date"] or "",
                acquisition_cost=round(cost, 2),
                current_value=round(latest["value"], 2),
                year=year,
                tmi_lu=s["tax_tmi_lu"] / 100.0,
                tmi_fr=s["tax_tmi_fr"] / 100.0,
                married=bool(s["tax_married"]),
                av_primes_under_150k=bool(s["tax_av_150k"]),
                substantial_holding=(bool(sub) if sub in (0, 1)
                                     else bool(s["tax_substantial"])),
                progressive=opt,
            ))
        except KeyError as exc:  # année fiscale non versionnée
            return JSONResponse({"detail": str(exc)}, status_code=400)
        out = {
            "account_id": account_id,
            "currency": row["currency"] or "EUR",
            "regime": res.regime,
            "ruleset_version": res.ruleset_version,
            "gross_gain": res.gross_gain,
            "losses_applied": res.losses_applied,
            "taxable_gain": res.taxable_gain,
            "income_tax": res.income_tax,
            "social_contributions": res.social_contributions,
            "extra_tax": res.extra_tax,
            "estimated_net_gain": res.estimated_net_gain,
            "lines": [{"id": l.id, "kind": l.kind, "amount": l.amount,
                       "pct": l.pct} for l in res.lines],
            "warnings": list(res.warnings),
            "assumptions": list(res.assumptions),
        }
        if row["currency"] not in (None, "EUR"):
            # estimation en devise native : la conversion fiscale réelle des
            # opérations historiques s'opère au taux de chaque date
            out["warnings"].insert(0, "FX_DEVISE_NON_EUR")
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- settings
# Hypothèses fiscales du foyer (v2026.09.032) : stockées PAR MEMBRE dans la
# table settings (base principale pour les standard, coffre pour les
# protected — le routage de db() suffit). Valeurs en % pour les taux, 0/1
# pour les booléens ; les défauts du moteur s'appliquent si absentes.
TAX_SETTING_DEFAULTS = {
    "tax_tmi_lu": 42.8,   # taux marginal LU % — barème spéculation/participations
    "tax_tmi_fr": 0.0,    # taux marginal FR % — 0 = non renseigné (options barème refusées)
    "tax_married": 0.0,   # imposition collective (abattements doublés)
    "tax_av_150k": 1.0,   # AV FR : primes ≤ 150 k€ (strate 7,5 %)
    "tax_substantial": 0.0,  # LU : détention ≥ 10 % (demi-taux au lieu de l'exonération)
}
TAX_SETTING_KEYS = tuple(TAX_SETTING_DEFAULTS)


def _get_settings(conn: sqlite3.Connection, username: str) -> dict:
    """Réglages résolus (stockés sinon défauts documentés) : fiscal + FIRE."""
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE member=?", (username,)
    ).fetchall()
    out = dict(ALL_SETTING_DEFAULTS)
    for r in rows:
        if r["key"] in out:
            out[r["key"]] = r["value"]
    return out


FIRE_SETTING_DEFAULTS = {
    "fire_return": 5.0,      # rendement nominal annuel % des investissements
    "fire_inflation": 2.0,   # inflation annuelle % (indexe épargne/dépenses/rentes)
    "fire_swr": 4.0,         # taux de retrait soutenable % (règle des 4 %)
    "fire_birthyear": 0.0,   # année de naissance (0 = inconnue — pas d'âge affiché)
}
FIRE_SETTING_RANGES = {
    "fire_return": (-5.0, 25.0),
    "fire_inflation": (0.0, 15.0),
    "fire_swr": (0.1, 25.0),
    "fire_birthyear": (1900.0, 2100.0),
}
ALL_SETTING_DEFAULTS = {**TAX_SETTING_DEFAULTS, **FIRE_SETTING_DEFAULTS}


class SettingsIn(BaseModel):
    tax_tmi_lu: float | None = None
    tax_tmi_fr: float | None = None
    tax_married: int | None = None
    tax_av_150k: int | None = None
    tax_substantial: int | None = None
    fire_return: float | None = None
    fire_inflation: float | None = None
    fire_swr: float | None = None
    fire_birthyear: float | None = None


@app.get("/api/settings")
async def get_settings(request: Request):
    u = _need(request)
    conn = db()
    try:
        out = _get_settings(conn, u["username"])
    finally:
        conn.close()
    return out


@app.put("/api/settings")
async def put_settings(body: SettingsIn, request: Request):
    u = _need(request)
    vals = {
        "tax_tmi_lu": body.tax_tmi_lu,
        "tax_tmi_fr": body.tax_tmi_fr,
        "tax_married": body.tax_married,
        "tax_av_150k": body.tax_av_150k,
        "tax_substantial": body.tax_substantial,
        "fire_return": body.fire_return,
        "fire_inflation": body.fire_inflation,
        "fire_swr": body.fire_swr,
        "fire_birthyear": body.fire_birthyear,
    }
    for key, v in vals.items():
        if v is None:
            continue
        if key in FIRE_SETTING_RANGES:
            lo, hi = FIRE_SETTING_RANGES[key]
            if v < lo or v > hi:
                return JSONResponse({"detail": f"Valeur {key} invalide ({lo}-{hi})"}, status_code=400)
        elif key in ("tax_tmi_lu", "tax_tmi_fr"):
            if v < 0 or v > 100:
                return JSONResponse({"detail": "Taux marginal invalide (0-100 %)"}, status_code=400)
        elif v not in (0, 1):
            return JSONResponse({"detail": "Valeur invalide (0 ou 1)"}, status_code=400)
    conn = db()
    try:
        for key, v in vals.items():
            if v is None:
                continue
            conn.execute(
                "INSERT INTO settings (member, key, value) VALUES (?,?,?)"
                " ON CONFLICT(member, key) DO UPDATE SET value=excluded.value",
                (u["username"], key, float(v)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


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
    wrapper: str | None = None  # enveloppe fiscale pea|av|cto (v2026.09.027)
    tax_country: str = ""  # pays fiscal fr|lu|'' (v2026.09.031)
    loan_principal: float = 0  # capital restant dû du crédit lié (immo, v2026.09.033)
    loan_rate: float = 0  # taux nominal annuel % du crédit lié (v2026.09.033)
    loan_monthly: float = 0  # mensualité hors assurance du crédit lié (v2026.09.033)
    area_m2: float | None = None  # surface du bien (immo, v2026.09.060)
    price_m2: float | None = None  # prix de référence €/m² du secteur (v2026.09.060)


# enveloppe fiscale d'un actif : elle détermine la PV nette (règles FR/LU à
# venir — v026 roadmap). Classes autorisées par enveloppe : PEA/CTO = titres
# (bourse), AV = contrats d'assurance (bourse ou épargne fonds euros).
_WRAPPER_ALLOW = {"pea": ("bourse",), "cto": ("bourse",), "av": ("bourse", "epargne")}


def _estate_meta_err(area_m2: float | None, price_m2: float | None,
                     asset_class: str) -> str | None:
    """Garde v2026.09.060 : surface / prix au m² réservés aux biens immo."""
    for v, what in ((area_m2, "Surface"), (price_m2, "Prix au m²")):
        if v is not None:
            if asset_class != "immobilier":
                return "La surface / le prix au m² est réservé aux biens immobiliers"
            try:
                if float(v) <= 0:
                    return f"{what} invalide"
            except (TypeError, ValueError):
                return f"{what} invalide"
    return None


def _wrapper_err(wrapper: str | None, asset_class: str) -> str | None:
    if wrapper is None:
        return None
    if wrapper not in _WRAPPER_ALLOW:
        return "Enveloppe fiscale invalide"
    if asset_class not in _WRAPPER_ALLOW[wrapper]:
        return "Enveloppe incompatible avec cette classe d'actif"
    return None


def _tax_country_err(tax_country: str | None) -> str | None:
    if tax_country is not None and tax_country not in ("", "fr", "lu"):
        return "Pays fiscal invalide (fr, lu ou vide)"
    return None


# Crédit lié à l'actif (v2026.09.033) : seuls les biens immobiliers portent un
# prêt — le « conteneur reste l'actif » (option ③-a validée Fred).
def _loan_err(principal: float, rate: float, monthly: float,
              asset_class: str) -> str | None:
    if principal < 0 or rate < 0 or monthly < 0:
        return "Montants du crédit invalides (négatifs)"
    if rate > 100:
        return "Taux du crédit invalide (> 100 %)"
    if (principal > 0 or rate > 0 or monthly > 0) and asset_class != "immobilier":
        return "Un crédit ne peut être lié qu'à un actif immobilier"
    return None


@app.post("/api/accounts")
async def create_account(body: AccountIn, request: Request):
    u = _need(request)
    if not body.name.strip():
        return JSONResponse({"detail": "Nom requis"}, status_code=400)
    if body.asset_class not in CLASS_KEYS:
        return JSONResponse({"detail": "Classe d'actif invalide"}, status_code=400)
    if body.asset_class == "crowdfunding":
        # classe 100 % alimentée par le module Crowdfunding (décision Fred 2026-09-08)
        return JSONResponse({"detail": "La classe Crowdfunding est gérée par le module — suivez vos projets dans la section Crowdfunding"}, status_code=400)
    mode = body.valuation_mode if body.valuation_mode in ("manual", "auto") else "manual"
    # les actifs auto sont valorisés en EUR (cours converti au refresh)
    ccy = "EUR" if mode == "auto" else (body.currency or "EUR").upper()
    if ccy not in FX_SUPPORTED:
        return JSONResponse({"detail": "Devise non supportée"}, status_code=400)
    if body.fees_pct is not None and body.fees_pct < 0:
        return JSONResponse({"detail": "Frais annuels invalides"}, status_code=400)
    werr = _wrapper_err(body.wrapper, body.asset_class)
    if werr:
        return JSONResponse({"detail": werr}, status_code=400)
    terr = _tax_country_err(body.tax_country)
    if terr:
        return JSONResponse({"detail": terr}, status_code=400)
    lerr = _loan_err(body.loan_principal, body.loan_rate, body.loan_monthly,
                    body.asset_class)
    if lerr:
        return JSONResponse({"detail": lerr}, status_code=400)
    emerr = _estate_meta_err(body.area_m2, body.price_m2, body.asset_class)
    if emerr:
        return JSONResponse({"detail": emerr}, status_code=400)
    conn = db()
    cur = conn.execute(
        "INSERT INTO accounts (owner, name, asset_class, institution, currency, fx_override, cost_basis, fees_pct, wrapper, tax_country, loan_principal, loan_rate, loan_monthly, open_date, notes, active,"
        " valuation_mode, symbol, quantity, area_m2, price_m2) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (u["username"], body.name.strip(), body.asset_class, body.institution.strip(), ccy,
         round(body.fx_override, 6) if body.fx_override else None, body.cost_basis or 0,
         round(body.fees_pct, 4) if body.fees_pct is not None else None,
         body.wrapper, body.tax_country or "",
         round(body.loan_principal, 2) if body.loan_principal else 0,
         round(body.loan_rate, 4) if body.loan_rate else 0,
         round(body.loan_monthly, 2) if body.loan_monthly else 0,
         body.open_date, body.notes.strip(), body.active,
         mode,
         body.symbol.strip().upper(), body.quantity or 0,
         round(body.area_m2, 2) if body.area_m2 is not None else None,
         round(body.price_m2, 2) if body.price_m2 is not None else None),
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
    # module Crédits (v2026.09.056) : un crédit saisi sur la fiche d'un bien
    # (champs legacy loan_*, conservés pour compatibilité) est matérialisé
    # dans le module Crédits — le compte ne porte plus le passif
    if body.asset_class == "immobilier" and (body.loan_principal or 0) > 0:
        d0 = body.open_date or date.today().isoformat()
        conn.execute(
            "INSERT INTO loans (owner, name, loan_type, lender, currency,"
            " principal_initial, principal_remaining, rate_annual,"
            " monthly_payment, insurance_monthly, start_date, account_id,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,?,?, datetime('now'), datetime('now'))",
            (u["username"], body.name.strip(), "immo", body.institution.strip(),
             ccy, round(body.loan_principal, 2), round(body.loan_principal, 2),
             round(body.loan_rate, 4) if body.loan_rate else 0,
             round(body.loan_monthly, 2) if body.loan_monthly else 0,
             d0, aid),
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
    if crowdfund.is_cf_account(conn, aid):
        conn.close()
        return JSONResponse({"detail": "Actif géré par le module Crowdfunding (valeur calculée)"}, status_code=400)
    mode = body.valuation_mode if body.valuation_mode in ("manual", "auto") else "manual"
    ccy = "EUR" if mode == "auto" else (body.currency or "EUR").upper()
    if ccy not in FX_SUPPORTED:
        conn.close()
        return JSONResponse({"detail": "Devise non supportée"}, status_code=400)
    werr = _wrapper_err(body.wrapper, body.asset_class)
    if werr:
        conn.close()
        return JSONResponse({"detail": werr}, status_code=400)
    terr = _tax_country_err(body.tax_country)
    if terr:
        conn.close()
        return JSONResponse({"detail": terr}, status_code=400)
    lerr = _loan_err(body.loan_principal, body.loan_rate, body.loan_monthly,
                    body.asset_class)
    if lerr:
        conn.close()
        return JSONResponse({"detail": lerr}, status_code=400)
    emerr = _estate_meta_err(body.area_m2, body.price_m2, body.asset_class)
    if emerr:
        conn.close()
        return JSONResponse({"detail": emerr}, status_code=400)
    conn.execute(
        "UPDATE accounts SET name=?, asset_class=?, institution=?, currency=?, fx_override=?, cost_basis=?, fees_pct=?, wrapper=?, tax_country=?, loan_principal=?, loan_rate=?, loan_monthly=?, open_date=?, notes=?,"
        " active=?, valuation_mode=?, symbol=?, quantity=?, updated_at=datetime('now') WHERE id=?",
        (body.name.strip(), body.asset_class, body.institution.strip(), ccy,
         round(body.fx_override, 6) if body.fx_override else None, body.cost_basis or 0,
         round(body.fees_pct, 4) if body.fees_pct is not None else None,
         body.wrapper, body.tax_country or "",
         round(body.loan_principal, 2) if body.loan_principal else 0,
         round(body.loan_rate, 4) if body.loan_rate else 0,
         round(body.loan_monthly, 2) if body.loan_monthly else 0,
         body.open_date, body.notes.strip(), body.active,
         mode,
         body.symbol.strip().upper(), body.quantity or 0, aid),
    )
    # v2026.09.060 — surface / prix au m² : mis à jour SEULEMENT si le client
    # les envoie (l'UI antérieure n'a pas ces champs → ne jamais les effacer)
    b_u = body.model_dump(exclude_unset=True)
    if "area_m2" in b_u or "price_m2" in b_u:
        conn.execute(
            "UPDATE accounts SET area_m2=?, price_m2=?, updated_at=datetime('now')"
            " WHERE id=?",
            (round(b_u["area_m2"], 2) if b_u.get("area_m2") is not None else None,
             round(b_u["price_m2"], 2) if b_u.get("price_m2") is not None else None,
             aid),
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
    if crowdfund.is_cf_account(conn, aid):
        conn.close()
        return JSONResponse({"detail": "Actif géré par le module Crowdfunding — retirez la plateforme dans la section Crowdfunding"}, status_code=400)
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
        fxr = fx.lookup(conn, ccy, d or date.today().isoformat(), None)
        if fxr is None:
            return None
        out["price_eur"] = px["price"] / fxr["rate"]
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
    sur la DERNIÈRE valorisation de chaque mois (historique réel) — plusieurs
    valorisations dans un même mois (refresh auto, captures) ne comptent
    qu'une fois : une par mois, sinon le cumul serait surestimé."""
    pct = row["fees_pct"]
    if pct is None or pct <= 0:
        return None
    # une ligne par mois : dernière val_date du mois (ex-aequo même jour ->
    # MAX(id) gagne, convention du modèle financier)
    vals = conn.execute(
        "SELECT val_date, value FROM ("
        " SELECT val_date, value, ROW_NUMBER() OVER ("
        "   PARTITION BY substr(val_date,1,7) ORDER BY val_date DESC, id DESC) rn"
        " FROM valuations WHERE account_id=? AND val_date <= date('now'))"
        " WHERE rn = 1 ORDER BY val_date", (row["id"],)
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
    if crowdfund.is_cf_account(conn, aid):
        conn.close()
        return JSONResponse({"detail": "Valorisation gérée par le module Crowdfunding"}, status_code=400)
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
FX_META = {
    "EUR": {"symbol": "€", "label": "Euro"},
    "USD": {"symbol": "$", "label": "US Dollar"},
    "CHF": {"symbol": "CHF", "label": "Franc suisse"},
    "GBP": {"symbol": "£", "label": "Livre sterling"},
    "JPY": {"symbol": "¥", "label": "Yen japonais"},
    "CAD": {"symbol": "C$", "label": "Dollar canadien"},
    "AUD": {"symbol": "A$", "label": "Dollar australien"},
}
@app.post("/api/fx/refresh")
async def fx_refresh(request: Request):
    """Met à jour les taux de change (BCE). Appel réseau dans le threadpool
    (fetch sans connexion — les écritures restent dans le handler)."""
    u = _need(request)
    try:
        rates = await run_in_threadpool(fx.fetch_daily, YAHOO_UA)
    except Exception:
        rates = []
    if not rates:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    conn = db()
    try:
        n, day = fx.store_daily(conn, rates)
    finally:
        conn.close()
    if n == 0:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    _audit(u["username"], "Mise à jour des taux", f"{n} devises")
    return {"updated": n, "asof": day}
@app.post("/api/fx/history")
async def fx_history_backfill(request: Request):
    """Backfill idempotent des fins de mois BCE (conversions historiques
    exactes au lieu du repli 'taux le plus ancien'). ~8 Mo téléchargés une
    fois ; INSERT OR REPLACE, aucune donnée existante touchée."""
    u = _need(request)
    try:
        rates = await run_in_threadpool(fx.fetch_hist, YAHOO_UA)
    except Exception:
        rates = []
    if not rates:
        return JSONResponse({"detail": "BCE injoignable — réessayez plus tard"}, status_code=502)
    conn = db()
    try:
        months = fx.store_hist(conn, rates)
    finally:
        conn.close()
    _audit(u["username"], "Taux historiques", f"backfill {months} mois")
    return {"months": months, "currencies": FX_SUPPORTED[1:]}


# ---------------------------------------------------------------- synthèse
@app.get("/api/summary")
async def summary(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family), member or None)
    wc, args = _owner_clause(owners)
    latest = _latest_valuations(conn)
    txns = _txn_summary(conn)
    rows = conn.execute(f"SELECT * FROM accounts WHERE active=1 AND {wc}", args).fetchall()
    by_class = {k: {"key": k, "value": 0.0, "cost": 0.0, "count": 0} for k in CLASS_KEYS}
    total_value = total_cost = 0.0
    total_debt = 0.0
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
        fxr = fx.lookup(conn, ccy, lv["date"], r["fx_override"])
        if fxr is None:
            fx_missing.append(r["name"])  # actif non convertible → exclu des totaux EUR
            continue
        fx_applied = True
        value_eur = lv["value"] / fxr["rate"] if ccy != "EUR" else lv["value"]
        cost_eur = cost / fxr["rate"] if (cost and ccy != "EUR") else cost
        c = by_class[r["asset_class"]]
        c["value"] += value_eur
        c["count"] += 1
        if cost_eur:
            c["cost"] += cost_eur
        total_value += value_eur
        total_cost += cost_eur
        if fxr.get("date"):
            fx_dates.add(fxr["date"])
        if asof is None or lv["date"] > asof:
            asof = lv["date"]
    # module Crédits (v2026.09.056) : le passif = somme des restants dus des
    # crédits actifs (table loans) — les colonnes loan_* legacy ont été
    # migrées au boot puis neutralisées (le compte immobilier ne déduit plus)
    debt = _loans_debt(conn, owners)
    total_debt = debt["total_eur"]
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
    net_worth = round(total_value - total_debt, 2)
    fx_asof = max(fx_dates) if fx_dates else None
    return {
        "total_value": round(total_value, 2),
        "total_debt": round(total_debt, 2),  # passifs (crédits suivis, v2026.09.056)
        "debt": {
            "total_eur": round(debt["total_eur"], 2),
            "per_type": {k: round(v, 2) for k, v in debt["per_type"].items()},
            "part_pct": round(total_debt / total_value * 100, 2) if total_value else None,
            "fx_missing": debt["fx_missing"],
        },
        "net_worth": net_worth,  # patrimoine net = actifs − passifs
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


# ---------------------------------------------------------------- module Crédits (v2026.09.056)
# Passifs suivis par type (🏠 immo / 🚗 auto / 🛒 conso) — table `loans`,
# moteur pur src/loans.py (amortissement français calculé à la demande).
# Le restant dû est DÉCLARÉ (source de vérité) ; `recompute` propose la
# valeur théorique de l'échéancier sans jamais l'appliquer d'office.
# Les champs legacy accounts.loan_* (v033) sont migrés au boot puis
# neutralisés ; la création d'un bien avec loan_principal > 0 matérialise
# un crédit (compatibilité anciens clients).

class LoanIn(BaseModel):
    name: str
    loan_type: str = "conso"
    lender: str = ""
    currency: str = "EUR"
    principal_initial: float = 0
    principal_remaining: float = 0
    rate_annual: float = 0
    monthly_payment: float = 0
    insurance_monthly: float = 0
    start_date: str | None = None
    account_id: int | None = None
    notes: str = ""
    active: int = 1


def _loan_err2(p: dict) -> str | None:
    """Validation partagée d'un crédit du module (v2026.09.056)."""
    if not (p.get("name") or "").strip():
        return "Nom requis"
    if (p.get("loan_type") or "conso") not in loans.LOAN_TYPES:
        return "Type de crédit invalide"
    if (p.get("currency") or "EUR").upper() not in FX_SUPPORTED:
        return "Devise non supportée"
    for k, msg in (
        ("principal_initial", "Capital initial invalide (négatif)"),
        ("principal_remaining", "Capital restant invalide (négatif)"),
        ("rate_annual", "Taux invalide (négatif)"),
        ("monthly_payment", "Mensualité invalide (négative)"),
        ("insurance_monthly", "Assurance mensuelle invalide (négative)"),
    ):
        if (p.get(k) or 0) < 0:
            return msg
    if (p.get("rate_annual") or 0) > 100:
        return "Taux invalide (> 100 %)"
    rem, M, r = ((p.get("principal_remaining") or 0),
                 (p.get("monthly_payment") or 0), (p.get("rate_annual") or 0))
    if rem > 0 and M <= 0:
        return "Mensualité requise (capital restant > 0)"
    if rem > 0 and r > 0 and M <= rem * r / 100 / 12:
        return "La mensualité ne couvre pas les intérêts du premier mois"
    return None


def _loan_link_err(conn: sqlite3.Connection, owner: str,
                   account_id: int | None) -> str | None:
    """Le crédit ne peut être lié qu'à un bien immobilier du propriétaire."""
    if account_id is None:
        return None
    row = conn.execute(
        "SELECT asset_class FROM accounts WHERE id=? AND owner=?",
        (account_id, owner),
    ).fetchone()
    if row is None:
        return "Bien immobilier introuvable"
    if row["asset_class"] != "immobilier":
        return "Le crédit ne peut être lié qu'à un bien immobilier"
    return None


def _loans_debt(conn: sqlite3.Connection, owners: list[str]) -> dict:
    """Passif agrégé des crédits actifs (EUR, taux BCE ≤ aujourd'hui) :
    {total_eur, per_type, fx_missing} — utilisé par /api/summary."""
    wc = "l.owner IN (%s)" % ",".join("?" * len(owners))
    today = date.today().isoformat()
    out = {"total_eur": 0.0,
           "per_type": {t: 0.0 for t in loans.LOAN_TYPES}, "fx_missing": []}
    rows = conn.execute(
        "SELECT l.name, l.loan_type, l.currency, l.principal_remaining FROM loans l"
        f" WHERE l.active=1 AND {wc}", owners).fetchall()
    for r in rows:
        ccy = r["currency"] or "EUR"
        fxr = fx.lookup(conn, ccy, today, None)
        if fxr is None:
            out["fx_missing"].append(r["name"])
            continue
        rem_eur = (r["principal_remaining"] / fxr["rate"] if ccy != "EUR"
                   else r["principal_remaining"])
        out["total_eur"] += rem_eur
        key = r["loan_type"] if r["loan_type"] in out["per_type"] else "conso"
        out["per_type"][key] += rem_eur
    return out


def _loan_computed(row: sqlite3.Row) -> dict | None:
    """Projections d'un crédit (fin estimée, intérêts restants, 12 prochains
    mois) — calcul unique src/loans.amortize (aucun JS dupliqué)."""
    rem, M, r = ((row["principal_remaining"] or 0), (row["monthly_payment"] or 0),
                 (row["rate_annual"] or 0))
    if not row["active"] or rem <= 0 or M <= 0:
        return None
    am = loans.amortize(rem, r, M)
    if am is None:
        return {"months_left": None, "end_date": None,
                "interests_left": None, "next_year_capital": None,
                "next_year_interests": None, "never": True}
    n = am["months_left"]
    y = date.today().year + (date.today().month - 1 + n) // 12
    mo = (date.today().month - 1 + n) % 12 + 1
    return {"months_left": n, "end_date": f"{y:04d}-{mo:02d}-01",
            "interests_left": am["interests_left"],
            "next_year_capital": am["next_year_capital"],
            "next_year_interests": am["next_year_interests"], "never": False}


@app.get("/api/loans")
async def list_loans(request: Request, family: int = 0, member: str = ""):
    """Crédits du propriétaire (scope famille/membre comme /api/accounts) +
    totaux en EUR par type. Le passif = restants dus DÉCLARÉS, convertis au
    taux BCE le plus récent ≤ aujourd'hui (fx_missing listé, jamais muet)."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "l.owner IN (%s)" % ",".join("?" * len(owners))
        rows = conn.execute(
            "SELECT l.*, a.name AS account_name FROM loans l"
            " LEFT JOIN accounts a ON a.id=l.account_id"
            f" WHERE l.active=1 AND {wc} ORDER BY l.principal_remaining DESC",
            owners).fetchall()
        today = date.today().isoformat()
        totals = {"total_eur": 0.0,
                  "per_type": {t: 0.0 for t in loans.LOAN_TYPES}, "fx_missing": []}
        out = []
        for r in rows:
            p = {k: r[k] for k in r.keys()}
            p["computed"] = _loan_computed(r)
            fxr = fx.lookup(conn, p["currency"] or "EUR", today, None)
            if fxr is None:
                p["eur_remaining"] = None
                totals["fx_missing"].append(r["name"])
            else:
                p["eur_remaining"] = round(
                    (p["principal_remaining"] / fxr["rate"]
                     if (p["currency"] or "EUR") != "EUR" else p["principal_remaining"]), 2)
                if p["active"]:
                    totals["total_eur"] += p["eur_remaining"]
                    key = p["loan_type"] if p["loan_type"] in totals["per_type"] else "conso"
                    totals["per_type"][key] += p["eur_remaining"]
            out.append(p)
    finally:
        conn.close()
    return {"loans": out, "totals": totals}


@app.get("/api/loans/curve")
async def loans_curve(request: Request, family: int = 0, member: str = ""):
    """Courbes du capital restant dû (échéancier théorique) par prêt actif —
    charts Crédits (v2026.09.062). Devise native convertie en EUR (taux BCE
    ≤ aujourd'hui) ; un prêt sans taux de change est omis et listé."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        d = loans.plan_curve(conn, owners)
        today = date.today().isoformat()
        missing = []
        kept = []
        for s in d["series"]:
            if (s["currency"] or "EUR") != "EUR":
                fxr = fx.lookup(conn, s["currency"], today, None)
                if fxr is None:
                    missing.append(s["name"])
                    continue
                s["values"] = [round(v / fxr["rate"], 2) for v in s["values"]]
            kept.append(s)
        d["series"] = kept
        d["fx_missing"] = missing
        return d
    finally:
        conn.close()


@app.post("/api/loans")
async def create_loan(body: LoanIn, request: Request):
    u = _need(request)
    lerr = _loan_err2(body.model_dump())
    if lerr:
        return JSONResponse({"detail": lerr}, status_code=400)
    conn = db()
    try:
        lerr = _loan_link_err(conn, u["username"], body.account_id)
        if lerr:
            return JSONResponse({"detail": lerr}, status_code=400)
        ccy = (body.currency or "EUR").upper()
        cur = conn.execute(
            "INSERT INTO loans (owner, name, loan_type, lender, currency,"
            " principal_initial, principal_remaining, rate_annual,"
            " monthly_payment, insurance_monthly, start_date, account_id,"
            " notes, active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))",
            (u["username"], body.name.strip(), body.loan_type, body.lender.strip(),
             ccy, round(body.principal_initial or 0, 2),
             round(body.principal_remaining or 0, 2),
             round(body.rate_annual or 0, 4),
             round(body.monthly_payment or 0, 2),
             round(body.insurance_monthly or 0, 2),
             body.start_date, body.account_id, body.notes.strip(),
             1 if body.active else 0),
        )
        lid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Création de crédit", f"#{lid} {body.name.strip()}")
    return {"id": lid}


@app.put("/api/loans/{lid}")
async def update_loan(lid: int, body: LoanIn, request: Request):
    u = _need(request)
    lerr = _loan_err2(body.model_dump())
    if lerr:
        return JSONResponse({"detail": lerr}, status_code=400)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM loans WHERE id=? AND owner=?", (lid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Crédit introuvable"}, status_code=404)
        lerr = _loan_link_err(conn, u["username"], body.account_id)
        if lerr:
            return JSONResponse({"detail": lerr}, status_code=400)
        ccy = (body.currency or "EUR").upper()
        conn.execute(
            "UPDATE loans SET name=?, loan_type=?, lender=?, currency=?,"
            " principal_initial=?, principal_remaining=?, rate_annual=?,"
            " monthly_payment=?, insurance_monthly=?, start_date=?,"
            " account_id=?, notes=?, active=?, updated_at=datetime('now')"
            " WHERE id=? AND owner=?",
            (body.name.strip(), body.loan_type, body.lender.strip(), ccy,
             round(body.principal_initial or 0, 2),
             round(body.principal_remaining or 0, 2),
             round(body.rate_annual or 0, 4),
             round(body.monthly_payment or 0, 2),
             round(body.insurance_monthly or 0, 2),
             body.start_date, body.account_id, body.notes.strip(),
             1 if body.active else 0, lid, u["username"]),
        )
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Modification de crédit", f"#{lid} {body.name.strip()}")
    return {"ok": True}


@app.delete("/api/loans/{lid}")
async def delete_loan(lid: int, request: Request):
    """Suppression douce (active=0) : un crédit migré reste traçable et un
    compte lié n'est jamais détruit. Simple et sûr (décision design)."""
    u = _need(request)
    conn = db()
    try:
        cur = conn.execute(
            "UPDATE loans SET active=0, updated_at=datetime('now')"
            " WHERE id=? AND owner=?", (lid, u["username"]))
        if cur.rowcount == 0:
            return JSONResponse({"detail": "Crédit introuvable"}, status_code=404)
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Suppression de crédit", f"#{lid}")
    return {"ok": True}


@app.get("/api/loans/{lid}/schedule")
async def loan_schedule(lid: int, request: Request, months: int = 480):
    """Échéancier mensuel déterministe (amortissement français) depuis le
    restant déclaré — la courbe UI (v057) consommera cette route."""
    if not (12 <= months <= 480):
        return JSONResponse({"detail": "Horizon invalide (12-480 mois)"}, status_code=400)
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id, principal_remaining, rate_annual, monthly_payment"
            " FROM loans WHERE id=? AND owner=?", (lid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Crédit introuvable"}, status_code=404)
        if (row["principal_remaining"] or 0) <= 0:
            return {"months_left": 0, "rows": []}
        am = loans.amortize(row["principal_remaining"], row["rate_annual"] or 0,
                            row["monthly_payment"] or 0, months)
        if am is None:
            return JSONResponse(
                {"detail": "Mensualité trop faible — le crédit ne s'amortit jamais"},
                status_code=400)
    finally:
        conn.close()
    return {"months_left": am["months_left"], "rows": am["rows"]}


@app.post("/api/loans/{lid}/recompute")
async def loan_recompute(lid: int, request: Request):
    """Valeur théorique du restant au jour J (mensualités régulières depuis
    la date de départ) + écart vs le restant déclaré — l'utilisateur choisit
    d'appliquer ou non (jamais d'écriture ici)."""
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id, principal_remaining FROM loans WHERE id=? AND owner=?",
            (lid, u["username"]),
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Crédit introuvable"}, status_code=404)
        theo = loans.theoretical_remaining(conn, lid)
        if theo is None:
            return JSONResponse(
                {"detail": "Recalcul impossible — vérifiez la date de départ et la mensualité"},
                status_code=400)
        declared = row["principal_remaining"] or 0
    finally:
        conn.close()
    _audit(u["username"], "Recalcul de crédit", f"#{lid} théorique {theo} vs {declared}")
    return {"declared_remaining": round(declared, 2),
            "theoretical_remaining": theo,
            "delta": round(theo - declared, 2)}


# ---------------------------------------------------------------- module Locations & TCO (v2026.09.058)
# Suivi locatif par contrat (locataire, loyer, dépôt, début/fin) + coût total
# de possession (TCO) par fiche (bien immo / véhicule hors patrimoine). Un
# encaissement MATÉRIALISE une op income (source_id loc:enc:<id>) ; les
# dépenses imputées = lien vers une op expense existante (une op = un objet).

class LocContractIn(BaseModel):
    account_id: int | None = None
    tenant: str = ""
    rent_monthly: float = 0
    deposit: float = 0
    start_date: str = ""
    end_date: str | None = None
    active: int = 1
    notes: str = ""


class LocPaymentIn(BaseModel):
    contract_id: int = 0
    op_date: str = ""
    amount: float = 0
    month: str = ""
    cash_account_id: int | None = None
    notes: str = ""


class TcoItemIn(BaseModel):
    kind: str = ""
    label: str = ""
    account_id: int | None = None
    loan_id: int | None = None
    purchase_date: str | None = None
    purchase_price: float | None = None
    resale_value: float | None = None  # valeur résiduelle (argus, v2026.09.060)
    resale_date: str | None = None
    active: int = 1
    notes: str = ""


class TcoImputeIn(BaseModel):
    transaction_id: int = 0
    item_id: int = 0
    category: str = ""


def _loc_account_summary(conn, latest: dict | None, acc_row) -> dict:
    """Payload léger d'un bien immo pour l'overview Locations (valeur +
    devise gérées comme /api/accounts, sans tout le payload de compte)."""
    p = {k: acc_row[k] for k in acc_row.keys()}
    p["last_value"] = latest["value"] if latest else None
    p["last_val_date"] = latest["date"] if latest else None
    # estimation indicative immo (v2026.09.060) — proposée, jamais appliquée
    p["estimated"] = None
    if p.get("area_m2") and p.get("price_m2"):
        p["estimated"] = round(p["area_m2"] * p["price_m2"], 2)
    if latest and (acc_row["currency"] or "EUR") != "EUR":
        fxr = fx.lookup(conn, acc_row["currency"], latest["date"], None)
        p["value_eur"] = round(latest["value"] / fxr["rate"], 2) if fxr else None
    else:
        p["value_eur"] = latest["value"] if latest else None
    return p


@app.get("/api/loc/overview")
async def loc_overview(request: Request, family: int = 0, member: str = ""):
    """Par bien immobilier : contrat actif, loyers attendus/perçus (12 m et
    total), occupation, coûts 12 m et rendements brut/net/net-financier."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "a.owner IN (%s)" % ",".join("?" * len(owners))
        accs = conn.execute(
            "SELECT * FROM accounts a WHERE a.asset_class='immobilier'"
            f" AND a.active=1 AND {wc} ORDER BY a.name", owners).fetchall()
        latest = _latest_valuations(conn)
        out = []
        for a in accs:
            o = a["owner"]
            pay = estate.contract_payload  # noqa
            entry = _loc_account_summary(conn, latest.get(a["id"]), a)
            entry["owner"] = o
            entry["contracts"] = [
                {k: c[k] for k in c.keys()} | {"account_name": a["name"]}
                for c in conn.execute(
                    "SELECT * FROM loc_contracts WHERE owner=? AND account_id=?"
                    " ORDER BY start_date DESC", (o, a["id"])).fetchall()
            ]
            first = min((c["start_date"][:7] for c in entry["contracts"]), default=None)
            today = estate._today_ym()
            f12 = estate._ym_add(today, -11)
            exp12 = est_exp = {}
            per12 = est_per = {}
            if first:
                exp12 = estate.expected_by_month(conn, o, a["id"], f12, today)
                est_exp = estate.expected_by_month(conn, o, a["id"], first, today)
                per12 = estate.perceived_by_month(conn, o, a["id"], f12, today)
                est_per = estate.perceived_by_month(conn, o, a["id"], first, today)
            entry["expected_12m"] = round(sum(exp12.values()), 2)
            entry["expected_total"] = round(sum(est_exp.values()), 2)
            entry["perceived_12m"] = round(sum(per12.values()), 2)
            entry["perceived_total"] = round(sum(est_per.values()), 2)
            entry["occupancy"] = estate.occupancy(conn, o, a["id"])
            entry["costs_12m"] = estate._imputed_window(conn, o, a["id"],
                                                        estate._ym_add(today, -11))
            entry["credit_12m"] = estate.credit_paid_12m(conn, a["id"])
            val = entry.get("value_eur")
            if val:
                entry["yield_brut"] = round(entry["perceived_12m"] / val * 100, 2)
                entry["yield_net"] = round(
                    (entry["perceived_12m"] - entry["costs_12m"]) / val * 100, 2)
                entry["yield_net_fin"] = round(
                    (entry["perceived_12m"] - entry["costs_12m"]
                     - entry["credit_12m"]) / val * 100, 2)
            else:
                entry["yield_brut"] = entry["yield_net"] = entry["yield_net_fin"] = None
            # encaissements récents (détail 12 derniers mois pour l'UI)
            entry["payments_12m"] = [
                {k: p[k] for k in p.keys()} for p in conn.execute(
                    "SELECT p.*, c.tenant FROM loc_payments p"
                    " JOIN loc_contracts c ON c.id=p.contract_id"
                    " WHERE c.owner=? AND c.account_id=? AND p.month>=?"
                    " ORDER BY p.month DESC LIMIT 400", (o, a["id"], f12)).fetchall()
            ]
            out.append(entry)
    finally:
        conn.close()
    return {"properties": out}


@app.get("/api/loc/contracts")
async def list_loc_contracts(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "c.owner IN (%s)" % ",".join("?" * len(owners))
        rows = conn.execute(
            "SELECT c.*, a.name AS account_name FROM loc_contracts c"
            " JOIN accounts a ON a.id=c.account_id"
            f" WHERE {wc} ORDER BY c.start_date DESC", owners).fetchall()
        out = [estate.contract_payload(r, conn) for r in rows]
    finally:
        conn.close()
    return {"contracts": out}


@app.post("/api/loc/contracts")
async def create_loc_contract(body: LocContractIn, request: Request):
    u = _need(request)
    b = body.model_dump()
    conn = db()
    try:
        err = estate.contract_err(conn, u["username"], b)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        cur = conn.execute(
            "INSERT INTO loc_contracts (owner, account_id, tenant, rent_monthly,"
            " deposit, start_date, end_date, active, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (u["username"], b["account_id"], b["tenant"].strip(),
             round(b["rent_monthly"], 2), round(b["deposit"] or 0, 2),
             b["start_date"], b.get("end_date"), 1 if b["active"] else 0,
             b.get("notes", "").strip()),
        )
        cid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Création de contrat de location", f"#{cid} {b['tenant'].strip()}")
    return {"id": cid}


@app.put("/api/loc/contracts/{cid}")
async def update_loc_contract(cid: int, body: LocContractIn, request: Request):
    u = _need(request)
    b = body.model_dump()
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM loc_contracts WHERE id=? AND owner=?", (cid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Contrat introuvable"}, status_code=404)
        err = estate.contract_err(conn, u["username"], b, cid)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        conn.execute(
            "UPDATE loc_contracts SET account_id=?, tenant=?, rent_monthly=?,"
            " deposit=?, start_date=?, end_date=?, active=?, notes=? WHERE id=?",
            (b["account_id"], b["tenant"].strip(), round(b["rent_monthly"], 2),
             round(b["deposit"] or 0, 2), b["start_date"], b.get("end_date"),
             1 if b["active"] else 0, b.get("notes", "").strip(), cid),
        )
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Modification de contrat de location", f"#{cid}")
    return {"ok": True}


@app.delete("/api/loc/contracts/{cid}")
async def delete_loc_contract(cid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM loc_contracts WHERE id=? AND owner=?", (cid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Contrat introuvable"}, status_code=404)
        n = conn.execute("SELECT COUNT(*) AS n FROM loc_payments WHERE contract_id=?",
                         (cid,)).fetchone()["n"]
        if n:
            conn.execute("UPDATE loc_contracts SET active=0 WHERE id=?", (cid,))
        else:
            conn.execute("DELETE FROM loc_contracts WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Suppression de contrat de location", f"#{cid}")
    return {"ok": True}


@app.get("/api/loc/payments")
async def list_loc_payments(request: Request, contract_id: int = 0, year: str = ""):
    u = _need(request)
    conn = db()
    try:
        c = conn.execute(
            "SELECT owner FROM loc_contracts WHERE id=?", (contract_id,)).fetchone()
        if c is None or c["owner"] != u["username"]:
            return JSONResponse({"detail": "Contrat introuvable"}, status_code=404)
        rows = estate.payments_for(conn, contract_id, year or None)
        out = []
        for r in rows:
            p = {k: r[k] for k in r.keys()}
            if p["transaction_id"]:
                tx = conn.execute(
                    "SELECT kind FROM transactions WHERE id=?",
                    (p["transaction_id"],)).fetchone()
                p["tx_ok"] = bool(tx)
            else:
                p["tx_ok"] = False
            out.append(p)
    finally:
        conn.close()
    return {"payments": out}


@app.post("/api/loc/payments")
async def create_loc_payment(body: LocPaymentIn, request: Request):
    u = _need(request)
    b = body.model_dump()
    conn = db()
    try:
        err = estate.pay_err(conn, u["username"], b)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        c = conn.execute(
            "SELECT c.id, c.tenant, a.name AS acc_name FROM loc_contracts c"
            " JOIN accounts a ON a.id=c.account_id WHERE c.id=?",
            (b["contract_id"],)).fetchone()
        cur = conn.execute(
            "INSERT INTO loc_payments (owner, contract_id, op_date, amount,"
            " month, notes) VALUES (?,?,?,?,?,?)",
            (u["username"], b["contract_id"], b["op_date"], round(b["amount"], 2),
             b["month"], b.get("notes", "").strip()),
        )
        pid = cur.lastrowid
        # matérialisation : l'encaissement EST une op income sur le compte choisi
        note = f"Loyer {b['month']} — {c['acc_name']} ({c['tenant']})"
        tcur = conn.execute(
            "INSERT INTO transactions (account_id, op_date, kind, amount, note,"
            " source_id) VALUES (?,?,?,?,?,?)",
            (b["cash_account_id"], b["op_date"], "income", round(b["amount"], 2),
             note, f"loc:enc:{pid}"),
        )
        conn.execute("UPDATE loc_payments SET transaction_id=? WHERE id=?",
                     (tcur.lastrowid, pid))
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Encaissement de loyer", f"#{pid} {b['month']} {b['amount']} €")
    return {"id": pid}


@app.delete("/api/loc/payments/{pid}")
async def delete_loc_payment(pid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id, transaction_id FROM loc_payments WHERE id=? AND owner=?",
            (pid, u["username"])).fetchone()
        if row is None:
            return JSONResponse({"detail": "Encaissement introuvable"}, status_code=404)
        if row["transaction_id"]:
            conn.execute(
                "DELETE FROM transactions WHERE id=? AND source_id=?",
                (row["transaction_id"], f"loc:enc:{pid}"),
            )
        conn.execute("DELETE FROM loc_payments WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Suppression d'encaissement de loyer", f"#{pid}")
    return {"ok": True}


@app.get("/api/tco/items")
async def list_tco_items(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "i.owner IN (%s)" % ",".join("?" * len(owners))
        rows = conn.execute(
            "SELECT i.*, a.name AS account_name, l.name AS loan_name"
            " FROM tco_items i LEFT JOIN accounts a ON a.id=i.account_id"
            " LEFT JOIN loans l ON l.id=i.loan_id"
            f" WHERE i.active=1 AND {wc} ORDER BY i.kind, i.label", owners).fetchall()
        out = []
        for r in rows:
            p = {k: r[k] for k in r.keys()}
            p["imputations_count"] = conn.execute(
                "SELECT COUNT(*) AS n FROM tco_imputations WHERE item_id=?",
                (r["id"],)).fetchone()["n"]
            out.append(p)
    finally:
        conn.close()
    return {"items": out}


@app.post("/api/tco/items")
async def create_tco_item(body: TcoItemIn, request: Request):
    u = _need(request)
    b = body.model_dump()
    conn = db()
    try:
        err = estate.item_err(conn, u["username"], b)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        cur = conn.execute(
            "INSERT INTO tco_items (owner, kind, label, account_id, loan_id,"
            " purchase_date, purchase_price, resale_value, resale_date, active, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (u["username"], b["kind"], b["label"].strip(), b.get("account_id"),
             b.get("loan_id"), b.get("purchase_date"),
             round(b["purchase_price"], 2) if b.get("purchase_price") is not None else None,
             round(b["resale_value"], 2) if b.get("resale_value") is not None else None,
             b.get("resale_date"),
             1 if b["active"] else 0, b.get("notes", "").strip()),
        )
        iid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Création de fiche de coûts", f"#{iid} {b['label'].strip()}")
    return {"id": iid}


@app.put("/api/tco/items/{iid}")
async def update_tco_item(iid: int, body: TcoItemIn, request: Request):
    u = _need(request)
    b = body.model_dump()
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM tco_items WHERE id=? AND owner=?", (iid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Fiche introuvable"}, status_code=404)
        err = estate.item_err(conn, u["username"], b, iid)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        conn.execute(
            "UPDATE tco_items SET kind=?, label=?, account_id=?, loan_id=?,"
            " purchase_date=?, purchase_price=?, active=?, notes=? WHERE id=?",
            (b["kind"], b["label"].strip(), b.get("account_id"), b.get("loan_id"),
             b.get("purchase_date"),
             round(b["purchase_price"], 2) if b.get("purchase_price") is not None else None,
             1 if b["active"] else 0, b.get("notes", "").strip(), iid),
        )
        # v2026.09.060 — valeur résiduelle (argus) : mise à jour seulement si
        # le client l'envoie (les UIs antérieures n'ont pas ces champs)
        b_u = body.model_dump(exclude_unset=True)
        if "resale_value" in b_u or "resale_date" in b_u:
            conn.execute(
                "UPDATE tco_items SET resale_value=?, resale_date=? WHERE id=?",
                (round(b_u["resale_value"], 2)
                 if b_u.get("resale_value") is not None else None,
                 b_u.get("resale_date"), iid),
            )
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Modification de fiche de coûts", f"#{iid}")
    return {"ok": True}


@app.delete("/api/tco/items/{iid}")
async def delete_tco_item(iid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM tco_items WHERE id=? AND owner=?", (iid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Fiche introuvable"}, status_code=404)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tco_imputations WHERE item_id=?",
            (iid,)).fetchone()["n"]
        if n:
            conn.execute("UPDATE tco_items SET active=0 WHERE id=?", (iid,))
        else:
            conn.execute("DELETE FROM tco_items WHERE id=?", (iid,))
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Suppression de fiche de coûts", f"#{iid}")
    return {"ok": True}


@app.get("/api/tco/overview")
async def tco_overview(request: Request, family: int = 0, member: str = ""):
    """Coûts par objet : fiches véhicules + biens immo (fiche créée ou non —
    le crédit du bien est lu via loans.account_id, les imputations via sa
    fiche). Véhicule = acquisition cash + crédit versé + imputations."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "owner IN (%s)" % ",".join("?" * len(owners))
        latest = _latest_valuations(conn)
        items = conn.execute(
            "SELECT * FROM tco_items WHERE active=1 AND " + wc +
            " ORDER BY kind, label", owners).fetchall()
        accs = conn.execute(
            "SELECT * FROM accounts WHERE asset_class='immobilier' AND active=1"
            " AND " + wc + " ORDER BY name", owners).fetchall()
        out = []
        for it in items:
            p = {k: it[k] for k in it.keys()}
            acc = None
            if it["account_id"]:
                acc = next((x for x in accs if x["id"] == it["account_id"]), None)
            p["value"] = latest.get(it["account_id"], {}).get("value") if it["account_id"] else None
            p["account_name"] = acc["name"] if acc else None
            costs = estate.item_costs(conn, it)
            p["costs"] = costs
            rv = it["resale_value"] if "resale_value" in it.keys() else None
            rd = it["resale_date"] if "resale_date" in it.keys() else None
            p["resale_value"] = rv
            p["resale_date"] = rd
            rmonths, rstale = estate.resale_meta(rd)
            p["resale_months"] = rmonths
            p["resale_stale"] = rstale
            p["net_to_date"] = (round(costs["total_to_date"] - rv, 2)
                                if rv is not None and costs.get("total_to_date") is not None
                                else None)
            out.append(p)
        # biens immo sans fiche : crédit seul (coûts imputés = aucun)
        for a in accs:
            if any(o["account_id"] == a["id"] for o in items if o["kind"] == "immo"):
                continue
            ghost = {"id": -a["id"], "owner": a["owner"], "kind": "immo",
                     "label": a["name"], "account_id": a["id"], "loan_id": None,
                     "purchase_date": None, "purchase_price": None, "active": 1,
                     "notes": ""}
            costs = estate.item_costs(conn, ghost)
            out.append({
                "id": None, "kind": "immo", "label": a["name"], "owner": a["owner"],
                "account_id": a["id"], "account_name": a["name"], "loan_id": None,
                "purchase_date": None, "purchase_price": None,
                "resale_value": None, "resale_date": None,
                "resale_months": None, "resale_stale": False, "net_to_date": None,
                "notes": "",
                "value": latest.get(a["id"], {}).get("value"),
                "costs": costs,
            })
        for e in out:
            e["owner"] = e.get("owner") or u["username"]
    finally:
        conn.close()
    return {"items": out}


@app.get("/api/tco/curve")
async def tco_curve(request: Request, family: int = 0, member: str = "",
                    months: int = 120):
    """Coût total cumulé par fiche (imputations + crédit versé), mois par mois —
    charts Coûts (v2026.09.062). Mêmes entités que /api/tco/overview ; chaque
    point = estate.item_costs(asof=1er du mois) — aucune logique dupliquée."""
    if not (12 <= months <= 240):
        return JSONResponse({"detail": "Horizon invalide (12-240 mois)"},
                            status_code=400)
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "owner IN (%s)" % ",".join("?" * len(owners))
        items = conn.execute(
            "SELECT * FROM tco_items WHERE active=1 AND " + wc +
            " ORDER BY kind, label", owners).fetchall()
        accs = conn.execute(
            "SELECT * FROM accounts WHERE asset_class='immobilier' AND active=1"
            " AND " + wc + " ORDER BY name", owners).fetchall()
        ents = []  # (key, name, kind, row)
        for it in items:
            ents.append((it["id"], it["label"], it["kind"], it))
        for a in accs:  # biens sans fiche : même ghost que l'overview
            if any(o["account_id"] == a["id"] for o in items
                   if o["kind"] == "immo"):
                continue
            ghost = {"id": -a["id"], "owner": a["owner"], "kind": "immo",
                     "label": a["name"], "account_id": a["id"], "loan_id": None,
                     "purchase_date": None, "purchase_price": None,
                     "active": 1, "notes": ""}
            ents.append((-a["id"], a["name"], "immo", ghost))
        y, mo = date.today().year, date.today().month
        labels = []
        for k in range(months - 1, -1, -1):
            yy, mm = y, mo - k
            while mm <= 0:
                mm += 12
                yy -= 1
            labels.append(f"{yy:04d}-{mm:02d}")
        series = []
        for key, name, kind, row in ents:
            values = []
            for ym in labels:
                try:
                    # fin de mois : toutes les échéances/imputations du mois
                    # comptent au point du mois (cohérent avec les KPI à date)
                    d0 = date(int(ym[:4]), int(ym[5:7]), 1)
                    d1 = (date(d0.year + (d0.month == 12), d0.month % 12 + 1, 1)
                          - timedelta(days=1))
                    c = estate.item_costs(conn, row, asof=d1)
                    values.append(c.get("total_to_date"))
                except Exception:
                    values.append(None)
            series.append({"key": str(key), "name": name, "kind": kind,
                           "values": values})
        return {"labels": labels, "series": series}
    finally:
        conn.close()


@app.get("/api/tco/imputations")
async def list_tco_imputations(request: Request, unmapped: int = 0):
    """Imputations de l'owner ; ?unmapped=1 = dépenses non encore imputées
    (assistant UI) — limité à 500 pour rester léger."""
    u = _need(request)
    conn = db()
    try:
        if unmapped:
            rows = conn.execute(
                "SELECT t.id, t.op_date, t.amount, t.note, a.name AS account_name"
                " FROM transactions t JOIN accounts a ON a.id=t.account_id"
                " WHERE t.kind='expense' AND a.owner=? AND NOT EXISTS"
                " (SELECT 1 FROM tco_imputations i WHERE i.transaction_id=t.id)"
                " ORDER BY t.op_date DESC LIMIT 500", (u["username"],)).fetchall()
            return {"unmapped": [dict(r) for r in rows]}
        rows = conn.execute(
            "SELECT i.*, t.op_date, t.amount, t.note, a.name AS account_name,"
            " it.label AS item_label FROM tco_imputations i"
            " JOIN transactions t ON t.id=i.transaction_id"
            " JOIN accounts a ON a.id=t.account_id"
            " JOIN tco_items it ON it.id=i.item_id"
            " WHERE i.owner=? ORDER BY t.op_date DESC LIMIT 500",
            (u["username"],)).fetchall()
        return {"imputations": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/tco/impute")
async def create_tco_impute(body: TcoImputeIn, request: Request):
    u = _need(request)
    conn = db()
    try:
        err = estate.impute_err(conn, u["username"], body.transaction_id,
                                body.item_id, body.category)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        conn.execute(
            "INSERT OR REPLACE INTO tco_imputations (transaction_id, owner,"
            " item_id, category) VALUES (?,?,?,?)",
            (body.transaction_id, u["username"], body.item_id, body.category),
        )
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Imputation de dépense", f"tx {body.transaction_id} → #{body.item_id}")
    return {"ok": True}


@app.delete("/api/tco/impute/{tid}")
async def delete_tco_impute(tid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        cur = conn.execute(
            "DELETE FROM tco_imputations WHERE transaction_id=? AND owner=?",
            (tid, u["username"]))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse({"detail": "Imputation introuvable"}, status_code=404)
    finally:
        conn.close()
    _audit(u["username"], "Retrait d'imputation de dépense", f"tx {tid}")
    return {"ok": True}


# ---------------------------------------------------------------- investissements (v2026.09.054)
# Pages « 📈 Actions » (PEA/CTO) & « 🛡️ Assurance vie » — aucun changement de
# schéma : tout est lu depuis accounts/positions/dividend_events/prices/
# transactions/valuations. La VALEUR d'un compte reste sa dernière
# valorisation (source de vérité, comme le dashboard) ; les lignes titres
# sont une décomposition informative (cours = cache `prices`, jamais réseau
# au rendu).

def _inv_flows(conn: sqlite3.Connection, owners: list[str],
               wrappers: tuple[str, ...]) -> dict[int, dict]:
    """Flux par compte (deposits/income in, withdrawals out, retraits YTD)
    + dividendes enregistrés (total & YTD) — scope owners + wrappers."""
    wc, args = _owner_clause(owners)
    wl = ",".join("?" * len(wrappers))
    out: dict[int, dict] = {}
    rows = conn.execute(
        f"SELECT t.account_id AS aid,"
        " COALESCE(SUM(CASE WHEN t.kind IN ('deposit','income')"
        " THEN t.amount ELSE 0 END),0) AS inflow,"
        " COALESCE(SUM(CASE WHEN t.kind='withdrawal' THEN t.amount ELSE 0 END),0)"
        " AS outflow,"
        " COALESCE(SUM(CASE WHEN t.kind='withdrawal' AND t.op_date>=?"
        " THEN t.amount ELSE 0 END),0) AS withdrawn_ytd"
        " FROM transactions t JOIN accounts a ON a.id=t.account_id"
        f" WHERE a.active=1 AND a.wrapper IN ({wl}) AND {wc}"
        " GROUP BY t.account_id",
        [date.today().strftime("%Y-01-01"), *wrappers, *args],
    ).fetchall()
    for r in rows:
        d = dict(r)
        d["dividends_total"] = d["dividends_ytd"] = 0.0
        out[r["aid"]] = d
    dyear = date.today().strftime("%Y-01-01")
    divs = conn.execute(
        f"SELECT a.id AS aid, d.ex_date, COALESCE(p.quantity,0) AS qty, d.per_share"
        " FROM dividend_events d JOIN positions p ON p.id=d.position_id"
        " JOIN accounts a ON a.id=p.account_id"
        f" WHERE a.active=1 AND a.wrapper IN ({wl}) AND {wc}",
        [*wrappers, *args],
    ).fetchall()
    for r in divs:
        amt = round((r["qty"] or 0) * (r["per_share"] or 0), 2)
        d = out.setdefault(r["aid"], {
            "inflow": 0.0, "outflow": 0.0, "withdrawn_ytd": 0.0,
            "dividends_total": 0.0, "dividends_ytd": 0.0})
        d["dividends_total"] = round(d["dividends_total"] + amt, 2)
        if r["ex_date"] >= dyear:
            d["dividends_ytd"] = round(d["dividends_ytd"] + amt, 2)
    return out


def _inv_row(conn: sqlite3.Connection, r: sqlite3.Row,
             latest: dict, txns: dict, flows: dict, fx_missing: list,
             with_positions: bool) -> dict | None:
    """Compte converti en EUR (mêmes règles que /api/summary) + lignes."""
    lv = latest.get(r["id"])
    if lv is None:
        return None
    t = txns.get(r["id"])
    cost = t["cost"] if t else (r["cost_basis"] or 0.0)
    ccy = r["currency"] or "EUR"
    fxr = fx.lookup(conn, ccy, lv["date"], r["fx_override"])
    if fxr is None:
        fx_missing.append(r["name"])
        return None
    value_eur = lv["value"] / fxr["rate"] if ccy != "EUR" else lv["value"]
    cost_eur = cost / fxr["rate"] if (cost and ccy != "EUR") else cost
    fl = flows.get(r["id"]) or {}
    row = {
        "id": r["id"], "name": r["name"], "institution": r["institution"] or "",
        "currency": ccy, "open_date": r["open_date"],
        "value": round(value_eur, 2),
        "cost": round(cost_eur, 2) if cost_eur else None,
        "gain": None, "gain_pct": None,
        "last_val_date": lv["date"], "last_val_source": lv["source"],
        "inflow": round(fl.get("inflow", 0.0) / fxr["rate"], 2) if ccy != "EUR"
        else round(fl.get("inflow", 0.0), 2),
        "outflow": round(fl.get("outflow", 0.0) / fxr["rate"], 2) if ccy != "EUR"
        else round(fl.get("outflow", 0.0), 2),
        "withdrawn_ytd": round(fl.get("withdrawn_ytd", 0.0) / fxr["rate"], 2)
        if ccy != "EUR" else round(fl.get("withdrawn_ytd", 0.0), 2),
        "dividends_total": round(fl.get("dividends_total", 0.0) / fxr["rate"], 2)
        if ccy != "EUR" else round(fl.get("dividends_total", 0.0), 2),
        "dividends_ytd": round(fl.get("dividends_ytd", 0.0) / fxr["rate"], 2)
        if ccy != "EUR" else round(fl.get("dividends_ytd", 0.0), 2),
    }
    if row["cost"]:
        row["gain"] = round(value_eur - row["cost"], 2)
        row["gain_pct"] = round(row["gain"] / row["cost"] * 100, 2) \
            if row["cost"] else None
    if with_positions:
        row["positions"] = _positions_payload(conn, r["id"])
    return row


def _inv_totals(accounts: list[dict]) -> dict:
    value = round(sum(a["value"] for a in accounts), 2)
    cost = round(sum(a["cost"] or 0 for a in accounts), 2)
    gain = round(value - cost, 2) if cost else None
    return {
        "value": value, "cost": cost or None,
        "gain": gain,
        "gain_pct": round(gain / cost * 100, 2) if gain is not None and cost else None,
        "dividends_total": round(sum(a["dividends_total"] for a in accounts), 2),
        "dividends_ytd": round(sum(a["dividends_ytd"] for a in accounts), 2),
        "withdrawn_ytd": round(sum(a["withdrawn_ytd"] for a in accounts), 2),
        "count": len(accounts),
    }


@app.get("/api/actions/overview")
async def actions_overview(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family), member or None)
    latest = _latest_valuations(conn)
    txns = _txn_summary(conn)
    flows = _inv_flows(conn, owners, ("pea", "cto"))
    fx_missing: list[str] = []
    wrappers_out = []
    for key in ("pea", "cto"):
        accs = []
        rows = conn.execute(
            "SELECT * FROM accounts WHERE active=1 AND wrapper=?"
            " AND asset_class='bourse' AND owner IN (%s)" % ",".join("?" * len(owners)),
            [key, *owners]).fetchall()
        for r in rows:
            row = _inv_row(conn, r, latest, txns, flows, fx_missing, True)
            if row:
                accs.append(row)
        accs.sort(key=lambda a: a["value"], reverse=True)
        wrappers_out.append({
            "key": key, "label": "PEA" if key == "pea" else "CTO",
            "accounts": accs, **{k: v for k, v in _inv_totals(accs).items()
                                 if k != "count"},
        })
    all_accs = [a for w in wrappers_out for a in w["accounts"]]
    net = _inv_totals(all_accs)
    conn.close()
    return {"net": net, "wrappers": wrappers_out, "fx_missing": fx_missing}


@app.get("/api/av/overview")
async def av_overview(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, bool(family), member or None)
    latest = _latest_valuations(conn)
    txns = _txn_summary(conn)
    flows = _inv_flows(conn, owners, ("av",))
    fx_missing: list[str] = []
    contracts = []
    rows = conn.execute(
        "SELECT * FROM accounts WHERE active=1 AND wrapper='av'"
        " AND owner IN (%s)" % ",".join("?" * len(owners)),
        [*owners]).fetchall()
    for r in rows:
        row = _inv_row(conn, r, latest, txns, flows, fx_missing, False)
        if row:
            row["kind_funds"] = "euro" if r["asset_class"] == "epargne" else "uc"
            contracts.append(row)
    contracts.sort(key=lambda a: a["value"], reverse=True)
    net = _inv_totals(contracts)
    conn.close()
    return {"net": net, "contracts": contracts, "fx_missing": fx_missing}


@app.post("/api/actions/refresh")
async def actions_refresh(request: Request):
    """Cours frais (Yahoo, une fois par symbole) pour les LIGNES TITRES des
    PEA/CTO du scope → cache `prices` (jamais de valorisation auto ici :
    la valeur du compte reste celle de sa dernière valuation)."""
    u = _need(request)
    conn = db()
    owners = _visible_owners(conn, u, False, None)
    syms = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT p.symbol FROM positions p JOIN accounts a"
        " ON a.id=p.account_id WHERE p.active=1 AND a.active=1"
        " AND a.wrapper IN ('pea','cto') AND a.owner IN (%s)"
        % ",".join("?" * len(owners)), [*owners]).fetchall()]
    updated, failed = [], []

    async def one(symbol: str) -> None:
        q = await run_in_threadpool(fetch_quote, symbol, "bourse")
        if not q or not q.get("price"):
            failed.append({"symbol": symbol, "error": "cours introuvable"})
            return
        conn.execute(
            "INSERT OR REPLACE INTO prices (symbol, price, currency, ts)"
            " VALUES (?,?,?,?)",
            (symbol, q["price"], q.get("currency", ""),
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        updated.append({"symbol": symbol, "price": q["price"],
                        "currency": q.get("currency", "")})

    for s in sorted(syms):
        await one(s)
    conn.close()
    return {"updated": updated, "failed": failed}



@app.get("/api/history")
async def history(request: Request, months: int = 60, family: int = 0, member: str = "",
                  ids: str = ""):
    u = _need(request)
    months = max(6, min(months, 240))
    conn = db()
    owners = _visible_owners(conn, u, bool(family), member or None)
    wc, args = _owner_clause(owners)
    # v2026.09.060 — ?ids=1,2,3 : série SOMME des comptes demandés (courbes
    # des pages dédiées) ; sans ids = agrégation par classe (comportement
    # historique strictement inchangé)
    id_set: set[int] | None = None
    if ids.strip():
        try:
            id_set = {int(x) for x in ids.split(",") if x.strip()}
        except ValueError:
            id_set = set()
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
    page_values = [] if id_set is not None else None
    while d <= today:
        labels.append(d.strftime("%Y-%m"))
        end_str = f"{d.strftime('%Y-%m')}-{calendar.monthrange(d.year, d.month)[1]:02d}"
        msum = 0.0
        page_sum = 0.0 if id_set is not None else None
        monthly = {k: 0.0 for k in CLASS_KEYS}
        for r in rows:
            if not r["active"]:
                continue
            if id_set is not None and r["id"] not in id_set:
                continue
            if r["open_date"] and r["open_date"][:10] > end_str:
                continue
            if r["close_date"] and r["close_date"][:10] < end_str:
                continue
            val = 0.0
            ccy = r["currency"] or "EUR"
            fxr = None
            for vd, vv in by_acc.get(r["id"], []):
                if vd <= end_str:
                    val = vv
                else:
                    break
            if val and ccy != "EUR":
                # taux BCE au plus proche ≤ fin de mois (sinon override manuel)
                fxr = fx.lookup(conn, ccy, end_str, r["fx_override"])
                if fxr is None:
                    val = 0.0  # actif non convertible : exclu de ce mois (approximation assumée)
                else:
                    val = val / fxr["rate"]
            # une classe peut porter PLUSIEURS comptes (ex. crowdfunding auto) :
            # la série est la SOMME par mois — jamais une valeur par compte
            monthly[r["asset_class"]] += val
            msum += val
            if page_sum is not None:
                page_sum += val
        for k in CLASS_KEYS:
            series[k].append(round(monthly[k], 2))
        totals.append(round(msum, 2))
        if page_values is not None:
            page_values.append(round(page_sum or 0.0, 2))
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    conn.close()
    if page_values is not None:
        return {
            "labels": labels,
            "values": page_values,
            "current": page_values[-1] if page_values else 0,
        }
    return {
        "labels": labels,
        "series": series,
        "totals": totals,
        "current": totals[-1] if totals else 0,
    }


# ---------------------------------------------------------------- évolution
def _snap_value(conn, r, by_acc, d_iso):
    """Valeur d'un actif à la date d_iso : dernière valorisation ≤ date (0
    si aucune), convertie en EUR — mêmes conventions que /api/history."""
    val = 0.0
    for vd, vv in by_acc.get(r["id"], []):
        if vd <= d_iso:
            val = vv
        else:
            break
    if val and (r["currency"] or "EUR") != "EUR":
        fxr = fx.lookup(conn, r["currency"], d_iso, r["fx_override"])
        val = 0.0 if fxr is None else val / fxr["rate"]
    return val


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


@app.get("/api/evolution")
async def evolution(request: Request, months: int = 12, family: int = 0, member: str = ""):
    """Pourquoi le patrimoine change : décomposition additive par mois
    (Flux = dépôts − retraits − dépenses, Revenus = opérations income,
    Effet marché = résidu) + snapshots annuels par classe (dernière
    valorisation de décembre, année courante incluse partielle)."""
    u = _need(request)
    months = max(3, min(months, 60))
    conn = db()
    owners = _visible_owners(conn, u, bool(family), member or None)
    wc, args = _owner_clause(owners)
    rows = conn.execute(
        f"SELECT id, name, asset_class, currency, fx_override, open_date, close_date, active"
        f" FROM accounts WHERE {wc}", args
    ).fetchall()
    by_acc: dict[int, list[tuple[str, float]]] = {}
    for v in conn.execute(
        f"SELECT v.account_id, v.val_date, v.value FROM valuations v"
        f" JOIN accounts a ON a.id = v.account_id WHERE {wc} ORDER BY v.val_date, v.id", args
    ):
        by_acc.setdefault(v["account_id"], []).append((v["val_date"][:10], v["value"]))
    sums: dict[tuple[int, str], dict[str, float]] = {}
    for t in conn.execute(
        f"SELECT t.account_id, substr(t.op_date, 1, 7) ym, t.kind, t.amount FROM transactions t"
        f" JOIN accounts a ON a.id = t.account_id WHERE {wc}", args
    ):
        d = sums.setdefault((t["account_id"], t["ym"]), {"deposit": 0.0, "withdrawal": 0.0, "income": 0.0, "expense": 0.0})
        d[t["kind"]] = d.get(t["kind"], 0.0) + t["amount"]

    today = date.today()
    cur_y, cur_m = today.year, today.month
    # --- snapshots annuels (dernière valo de décembre ; année courante partielle)
    years: list[int] = []
    annual: list[dict] = []
    first_y = min((int(v[0][:4]) for vs in by_acc.values() for v in vs), default=None)
    if first_y is not None:
        years = list(range(first_y, cur_y + 1))
        for y in years:
            end = today if y == cur_y else date(y, 12, 31)
            end_str = end.isoformat()
            by_class = {k: 0.0 for k in CLASS_KEYS}
            tot = 0.0
            for r in rows:
                if not r["active"]:
                    continue
                if r["open_date"] and r["open_date"][:10] > end_str:
                    continue
                if r["close_date"] and r["close_date"][:10] < end_str:
                    continue
                val = _snap_value(conn, r, by_acc, end_str)
                if val:
                    by_class[r["asset_class"]] = round(by_class[r["asset_class"]] + val, 2)
                    tot += val
            annual.append({"year": y, "by_class": by_class, "total": round(tot, 2)})

    # --- drivers mensuels (fenêtre glissante, mois courant inclus)
    d0 = date(cur_y, cur_m, 1)
    for _ in range(months - 1):
        d0 = date(d0.year - (d0.month == 1), 12 if d0.month == 1 else d0.month - 1, 1)
    ym_prev = (d0.year - (d0.month == 1), 12 if d0.month == 1 else d0.month - 1)
    prev_end = _month_end(*ym_prev)
    months_out: list[dict] = []
    d = d0
    while d <= today:
        end = today if (d.year, d.month) == (cur_y, cur_m) else _month_end(d.year, d.month)
        end_str, prev_str = end.isoformat(), prev_end.isoformat()
        ym = d.strftime("%Y-%m")
        classes: dict[str, dict[str, float]] = {}
        tot = {"dv": 0.0, "flux": 0.0, "revenus": 0.0, "marche": 0.0, "depenses": 0.0}
        acts = []
        for r in rows:
            if not r["active"]:
                continue
            if r["open_date"] and r["open_date"][:10] > end_str:
                continue
            if r["close_date"] and r["close_date"][:10] < end_str:
                continue
            sm = sums.get((r["id"], ym)) or {}
            dep, wd = sm.get("deposit", 0.0), sm.get("withdrawal", 0.0)
            exp, inc = sm.get("expense", 0.0), sm.get("income", 0.0)
            v0, v1 = _snap_value(conn, r, by_acc, prev_str), _snap_value(conn, r, by_acc, end_str)
            dv = round(v1 - v0, 2)
            flux = round(dep - wd - exp, 2)
            rev = round(inc, 2)
            marche = round(dv - flux - rev, 2)
            if not (dv or flux or rev):
                continue  # actif immobile ce mois : rien à expliquer
            c = classes.setdefault(r["asset_class"], {"dv": 0.0, "flux": 0.0, "revenus": 0.0, "marche": 0.0})
            for k, vv in (("dv", dv), ("flux", flux), ("revenus", rev)):
                c[k] = round(c[k] + vv, 2)
            tot["dv"] = round(tot["dv"] + dv, 2)
            tot["flux"] = round(tot["flux"] + flux, 2)
            tot["revenus"] = round(tot["revenus"] + rev, 2)
            tot["depenses"] = round(tot["depenses"] + exp, 2)
            acts.append({"id": r["id"], "name": r["name"], "cls": r["asset_class"],
                         "dv": dv, "flux": flux, "revenus": rev, "marche": marche})
        for c in classes.values():
            c["marche"] = round(c["dv"] - c["flux"] - c["revenus"], 2)
        tot["marche"] = round(tot["dv"] - tot["flux"] - tot["revenus"], 2)
        months_out.append({"ym": ym, "total": tot, "classes": classes, "acts": acts})
        prev_end = end
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    conn.close()
    return {"years": years, "current_year": cur_y, "annual": annual, "months": months_out}


# ---------------------------------------------------------------- divers
@app.get("/api/version")
async def version():
    # disclaimer optionnel de la page de login (env DISCLAIMER, lu à la
    # demande : configurable par déploiement sans redémarrage)
    return {"version": VERSION, "disclaimer": (os.environ.get("DISCLAIMER") or "").strip() or None}


@app.get("/api/export")
async def export(request: Request):
    u = _need(request)
    conn = db()
    try:
        data = transfer.export_data(conn, u["username"], VERSION)
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
        data = transfer.export_data(conn, u["username"], VERSION)
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
    conn = db()
    try:
        err = transfer.do_import(conn, u["username"], data)
    finally:
        conn.close()
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
    conn = db()
    try:
        err = transfer.do_import(conn, u["username"], body)
    finally:
        conn.close()
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    _audit(u["username"], "Import JSON", f"{len(body['accounts'])} actifs")
    return {"ok": True}


class TxCsvIn(BaseModel):
    account_id: int
    default_kind: str = "deposit"
    csv_text: str


@app.post("/api/transactions/import-csv")
async def import_tx_csv(body: TxCsvIn, request: Request):
    """Importe un CSV bancaire (opérations) dans un actif appartenant à l'utilisateur.
    Colonnes d'en-tête : date + libellé + montant (ou débit/crédit).
    Montant négatif = type inversé (dépôt↔retrait, revenu↔dépense). Doublons ignorés.
    Le parsing/l'insertion vivent dans src/transfer.py."""
    u = _need(request)
    if body.default_kind not in transfer.TX_KINDS:
        return JSONResponse({"detail": "Type inconnu"}, status_code=400)
    conn = db()
    try:
        acc = conn.execute(
            "SELECT id, name FROM accounts WHERE id=? AND owner=?", (body.account_id, u["username"])
        ).fetchone()
        if acc is None:
            return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
        try:
            res = transfer.import_tx_csv(conn, body.account_id, body.default_kind, body.csv_text)
        except transfer.TransferError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
    finally:
        conn.close()
    _audit(u["username"], "Import CSV d'opérations", f"#{body.account_id} +{res['inserted']} "
            f"({res['skipped']} doublons, {res['invalid']} invalides)")
    return {"inserted": res["inserted"], "skipped": res["skipped"],
            "invalid": res["invalid"], "errors": res["errors"]}
@app.get("/api/export/csv/{kind}")
async def export_csv(kind: str, request: Request):
    """Export CSV UTF-8 (BOM pour Excel) de ses propres données, par type.
    Le contenu localisé est produit par src/transfer.py."""
    u = _need(request)
    spec = transfer.CSV_KINDS.get(kind)
    if spec is None:
        return JSONResponse({"detail": "Type inconnu (accounts|transactions|valuations|rules)"}, status_code=404)
    fname, sql = spec
    conn = db()
    try:
        rows = conn.execute(sql, (u["username"],)).fetchall()
    finally:
        conn.close()
    body = transfer.csv_content(rows, request.headers.get("accept-language", ""))
    _audit(u["username"], f"Export CSV ({fname})", f"{len(rows)} lignes")
    return Response(
        body,
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
                            kind: str | None = None, limit: int = 300,
                            member: str = ""):
    u = _need(request)
    limit = max(1, min(limit, 1000))
    conn = db()
    owner = _member_target(conn, u, member) if member else u["username"]
    try:
        where, args = ["a.owner=?"], [owner]
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
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["signed"] = round(r["amount"] * KIND_SIGN.get(r["kind"], 1), 2)
            d["kind_label"] = KIND_LABELS.get(r["kind"], r["kind"])
            out.append(d)
    finally:
        conn.close()
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
    if crowdfund.is_cf_account(conn, body.account_id):
        conn.close()
        return JSONResponse({"detail": "Opérations gérées par le module Crowdfunding"}, status_code=400)
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
    if (row["source_id"] or "").startswith("cf:"):
        conn.close()
        return JSONResponse({"detail": "Opération gérée par le module Crowdfunding"}, status_code=400)
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
    kind: str = "income"  # v2026.09.028 : income | expense (règles de dépenses)


def _freq_months(freq: str, months_int: int) -> int:
    return {"monthly": 1, "quarterly": 3, "yearly": 12}.get(freq, max(1, months_int or 1))


@app.get("/api/income-rules")
async def list_rules(request: Request, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owner = _member_target(conn, u, member) if member else u["username"]
        rows = conn.execute(
            "SELECT r.*, a.name AS account_name FROM income_rules r JOIN accounts a ON a.id=r.account_id"
            " WHERE a.owner=? ORDER BY r.next_date, r.label", (owner,)
        ).fetchall()
    finally:
        conn.close()
    return {"rules": [dict(r) for r in rows]}


@app.post("/api/income-rules")
async def add_rule(body: RuleIn, request: Request):
    u = _need(request)
    if body.amount <= 0 or not body.label.strip():
        return JSONResponse({"detail": "Libellé ou montant invalide"}, status_code=400)
    if body.kind not in ("income", "expense"):
        return JSONResponse({"detail": "Type de règle invalide"}, status_code=400)
    conn = db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE id=? AND owner=?", (body.account_id, u["username"])
    ).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"detail": "Actif introuvable"}, status_code=404)
    cur = conn.execute(
        "INSERT INTO income_rules (account_id, label, amount, freq, months_int, next_date, active, kind)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (body.account_id, body.label.strip(), round(body.amount, 2), body.freq,
         body.months_int, body.next_date[:10], body.active, body.kind),
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
    if body.kind not in ("income", "expense"):
        conn.close()
        return JSONResponse({"detail": "Type de règle invalide"}, status_code=400)
    conn.execute(
        "UPDATE income_rules SET account_id=?, label=?, amount=?, freq=?, months_int=?, next_date=?,"
        " active=?, kind=? WHERE id=?",
        (body.account_id, body.label.strip(), round(body.amount, 2), body.freq, body.months_int,
         body.next_date[:10], body.active, body.kind, rid),
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
async def income_calendar(request: Request, months: int = 12, member: str = ""):
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    try:
        owner = _member_target(conn, u, member) if member else u["username"]
        rows = conn.execute(
            "SELECT r.*, a.name AS account_name, a.asset_class FROM income_rules r"
            " JOIN accounts a ON a.id=r.account_id WHERE r.active=1 AND a.owner=? ORDER BY r.label",
            (owner,),
        ).fetchall()
    finally:
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
                "kind": r["kind"],
            })
            d = _adv(d)
            n += 1
    out.sort(key=lambda x: (x["ym"], -x["amount"]))
    return {"calendar": out, "end_ym": end.strftime("%Y-%m")}


@app.get("/api/income-actual")
async def income_actual(request: Request, months: int = 12, member: str = ""):
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    try:
        owner = _member_target(conn, u, member) if member else u["username"]
        rows = conn.execute(
            "SELECT substr(t.op_date,1,7) ym, SUM(t.amount) total FROM transactions t"
            " JOIN accounts a ON a.id=t.account_id"
            " WHERE t.kind='income' AND a.owner=? GROUP BY ym ORDER BY ym DESC LIMIT ?",
            (owner, months),
        ).fetchall()
    finally:
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


@app.get("/api/cashflow")
async def cashflow(request: Request, months: int = 12, member: str = ""):
    """Projection de trésorerie : règles récurrentes (revenus ET dépenses)
    sur les mois à venir, solde cumulé à partir de la trésorerie réelle
    (dernière valorisation des comptes de classe « comptes »)."""
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    owner = _member_target(conn, u, member) if member else u["username"]
    try:
        rules = conn.execute(
            "SELECT r.* FROM income_rules r JOIN accounts a ON a.id=r.account_id"
            " WHERE r.active=1 AND a.owner=? ORDER BY r.label", (owner,),
        ).fetchall()
        bal_row = conn.execute(
            "SELECT COALESCE(SUM(v.value), 0) FROM valuations v"
            " WHERE v.id IN (SELECT MAX(id) FROM valuations"
            "  WHERE account_id IN (SELECT id FROM accounts WHERE owner=? AND asset_class='comptes')"
            "  GROUP BY account_id)", (owner,),
        ).fetchone()
    finally:
        conn.close()
    start = round(bal_row[0] or 0.0, 2)
    today = date.today()
    labels = []
    for k in range(months):
        y = today.year + (today.month - 1 + k) // 12
        m = (today.month - 1 + k) % 12 + 1
        labels.append(f"{y:04d}-{m:02d}")
    last_day = lambda dt: calendar.monthrange(dt.year, dt.month)[1]
    in_m = {ym: 0.0 for ym in labels}
    out_m = {ym: 0.0 for ym in labels}
    end_ym = labels[-1]
    for r in rules:
        step = _freq_months(r["freq"], r["months_int"])
        d = date.fromisoformat(r["next_date"][:10])
        day0 = min(d.day, 28)
        n = 0
        while d.strftime("%Y-%m") <= end_ym and n < 400:
            ym = d.strftime("%Y-%m")
            if ym in in_m:
                if (r["kind"] or "income") == "expense":
                    out_m[ym] += r["amount"]
                else:
                    in_m[ym] += r["amount"]
            y2 = d.year + (d.month - 1 + step) // 12
            m2 = (d.month - 1 + step) % 12 + 1
            d = date(y2, m2, min(day0, last_day(date(y2, m2, 1))))
            n += 1
    ins, outs, nets, bals = [], [], [], []
    bal = start
    for ym in labels:
        i, o = round(in_m[ym], 2), round(out_m[ym], 2)
        net = round(i - o, 2)
        bal = round(bal + net, 2)
        ins.append(i); outs.append(o); nets.append(net); bals.append(bal)
    return {"starting_balance": start, "labels": labels,
            "in": ins, "out": outs, "net": nets, "balance": bals}


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
        fxr = fx.lookup(conn, ccy_q, today, None)
        if fxr is None:
            try:  # taux manquant : un seul appel BCE, puis échec propre
                rates = await run_in_threadpool(fx.fetch_daily, YAHOO_UA)
                for cc2, day, rate in rates:
                    if cc2 in FX_SUPPORTED and day <= today:
                        conn.execute(
                            "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source)"
                            " VALUES (?,?,?, 'ecb')", (cc2, day, rate),
                        )
                conn.commit()
                fxr = fx.lookup(conn, ccy_q, today, None)
            except Exception:
                fxr = None
        if fxr is None:
            return None, "taux de change indisponible (BCE)"
        return q["price"] / fxr["rate"], None

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
@app.get("/api/benchmarks")
async def benchmarks(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        latest = _latest_valuations(conn)
        today = date.today()
        start = bench.start_ym(conn, owners, today)
        need = bench.needs(conn, start, False, today)
        if need:
            charts = await run_in_threadpool(bench.fetch_charts, need, _yahoo_chart)
            bench.store_levels(conn, start, charts)
        return bench.build(conn, owners, latest, start, today)
    finally:
        conn.close()
@app.post("/api/refresh-benchmarks")
async def refresh_benchmarks(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        today = date.today()
        start = bench.start_ym(conn, owners, today)
        need = bench.needs(conn, start, True, today)
        if need:
            charts = await run_in_threadpool(bench.fetch_charts, need, _yahoo_chart)
            bench.store_levels(conn, start, charts)
    finally:
        conn.close()
    return await benchmarks(request, family=family, member=member)
# ================================================================ module Crowdfunding
# Suivi projet-par-projet des plateformes (Bricks.co, La Première Brique) —
# v2026.09.046. Métier dans src/crowdfund.py ; le patrimoine de chaque plateforme
# est matérialisé dans des comptes-auto (classe crowdfunding) par
# refresh_integration → dashboard/évolution/historique sans double saisie.

class CfProjectIn(BaseModel):
    platform: str = "bricks"
    name: str = ""
    city: str = ""
    invested: float = 0
    rate: float = 0
    duration_months: int = 0
    start_date: str | None = None
    expected_end_date: str | None = None
    actual_end_date: str | None = None
    status: str = "en_cours"
    repaid_capital: float = 0
    interest_received: float = 0
    interest_net: float = 0
    interest_remaining: float = 0
    interest_remaining_net: float = 0
    real_rate: float = 0
    contract_type: str = ""
    valuation: float = 0
    reinvested_from: int | None = None
    notes: str = ""


class CfPlatformIn(BaseModel):
    platform: str
    balance: float | None = None
    deposited: float | None = None
    invested_value: float | None = None


class CfImportIn(BaseModel):
    b64: str = ""


class CfSyncBodyIn(BaseModel):
    captures: list = []


def _cf_check(body: CfProjectIn) -> str | None:
    if not body.name.strip():
        return "Nom requis"
    if body.platform not in crowdfund.CF_PLATFORMS:
        return "Plateforme inconnue"
    if body.status not in crowdfund.CF_STATUS_KEYS:
        return "Statut inconnu"
    return None


def _cf_row_to_project(conn, pid, owner: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM cf_projects WHERE id=? AND owner=?", (pid, owner)
    ).fetchone()
    if row is None:
        return None
    return crowdfund.project_computed(row)


@app.get("/api/cf/summary")
async def cf_summary(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return crowdfund.summary_agg(conn, owners)
    finally:
        conn.close()


@app.get("/api/cf/projects")
async def cf_list_projects(request: Request, platform: str = "", status: str = "",
                           family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return {"projects": crowdfund.project_extras(
            conn, owners, platform or None, status or None)}
    finally:
        conn.close()


@app.post("/api/cf/projects")
async def cf_create_project(body: CfProjectIn, request: Request):
    u = _need(request)
    err = _cf_check(body)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    conn = db()
    try:
        cur = conn.execute(
            """INSERT INTO cf_projects
               (owner, platform, name, city, invested, rate, duration_months,
                start_date, expected_end_date, actual_end_date, status, repaid_capital,
                interest_received, interest_net, interest_remaining,
                interest_remaining_net, real_rate, valuation, contract_type,
                reinvested_from, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["username"], body.platform, body.name.strip(), body.city.strip(),
             body.invested, body.rate, body.duration_months, body.start_date,
             body.expected_end_date, body.actual_end_date, body.status,
             body.repaid_capital, body.interest_received, body.interest_net,
             body.interest_remaining, body.interest_remaining_net, body.real_rate,
             body.valuation, body.contract_type, body.reinvested_from,
             body.notes.strip(), crowdfund.now_iso(), crowdfund.now_iso()),
        )
        pid = cur.lastrowid
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
        out = _cf_row_to_project(conn, pid, u["username"])
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : projet créé", f"#{pid} {body.name.strip()}")
    return out or {"ok": True}


@app.put("/api/cf/projects/{pid}")
async def cf_update_project(pid: int, body: CfProjectIn, request: Request):
    u = _need(request)
    err = _cf_check(body)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM cf_projects WHERE id=? AND owner=?", (pid, u["username"])
        ).fetchone()
        if row is None:
            return JSONResponse({"detail": "Projet introuvable"}, status_code=404)
        conn.execute(
            """UPDATE cf_projects SET platform=?, name=?, city=?, invested=?, rate=?,
               duration_months=?, start_date=?, expected_end_date=?, actual_end_date=?,
               status=?, repaid_capital=?, interest_received=?, interest_net=?,
               interest_remaining=?, interest_remaining_net=?, real_rate=?,
               contract_type=?, valuation=?, reinvested_from=?, notes=?, updated_at=?
               WHERE id=?""",
            (body.platform, body.name.strip(), body.city.strip(), body.invested,
             body.rate, body.duration_months, body.start_date, body.expected_end_date,
             body.actual_end_date, body.status, body.repaid_capital,
             body.interest_received, body.interest_net, body.interest_remaining,
             body.interest_remaining_net, body.real_rate, body.contract_type,
             body.valuation, body.reinvested_from, body.notes.strip(),
             crowdfund.now_iso(), pid),
        )
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
        out = _cf_row_to_project(conn, pid, u["username"])
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : projet modifié", f"#{pid}")
    return out or {"ok": True}


@app.delete("/api/cf/projects/{pid}")
async def cf_delete_project(pid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        # neutraliser les liens de réinvestissement, puis cascade des opérations
        conn.execute("UPDATE cf_projects SET reinvested_from=NULL WHERE reinvested_from=?", (pid,))
        cur = conn.execute(
            "DELETE FROM cf_projects WHERE id=? AND owner=?", (pid, u["username"])
        )
        if cur.rowcount == 0:
            return JSONResponse({"detail": "Projet introuvable"}, status_code=404)
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : projet supprimé", f"#{pid}")
    return {"ok": True}


@app.get("/api/cf/operations")
async def cf_list_operations(request: Request, platform: str = "",
                             project_id: int = 0, q: str = "", type: str = "",
                             limit: int = 200, offset: int = 0,
                             family: int = 0, member: str = ""):
    u = _need(request)
    limit = max(1, min(limit, 1000))
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc, args = crowdfund._wc(owners)
        conds, params = ["o." + wc], list(args)
        if platform:
            conds.append("o.platform=?")
            params.append(platform)
        if project_id:
            conds.append("o.project_id=?")
            params.append(project_id)
        if type:
            conds.append("o.type LIKE ?")
            params.append(f"%{type}%")
        if q:
            conds.append("(o.type LIKE ? OR o.details LIKE ? OR COALESCE(p.name,'') LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
        where = " WHERE " + " AND ".join(conds)
        total = conn.execute(
            f"SELECT COUNT(*) c FROM cf_operations o LEFT JOIN cf_projects p ON p.id=o.project_id{where}",
            params).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT o.*, p.name AS project_name FROM cf_operations o
                LEFT JOIN cf_projects p ON p.id=o.project_id{where}
                ORDER BY o.op_date DESC, o.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["extra"] = json.loads(d["extra"] or "{}")
            except Exception:
                d["extra"] = {}
            items.append(d)
    finally:
        conn.close()
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@app.get("/api/cf/operations/stats")
async def cf_ops_stats(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc, args = crowdfund._wc(owners)
        rows = conn.execute(
            f"""SELECT o.platform, o.project_id, p.name AS project_name,
                       SUM(CASE WHEN o.amount >= 0 THEN o.amount ELSE 0 END) AS inc,
                       SUM(CASE WHEN o.amount < 0 THEN -o.amount ELSE 0 END) AS out,
                       COUNT(*) AS n
                FROM cf_operations o LEFT JOIN cf_projects p ON p.id=o.project_id
                WHERE o.status IN ('Validée', 'Réussi') AND o.{wc}
                GROUP BY o.platform, o.project_id""", args).fetchall()
        by_platform: dict[str, dict] = {}
        by_project = []
        total_in = total_out = 0.0
        for r in rows:
            inc, out = r["inc"] or 0.0, r["out"] or 0.0
            total_in += inc
            total_out += out
            bp = by_platform.setdefault(r["platform"], {
                "label": crowdfund.CF_PLATFORMS.get(r["platform"], r["platform"]),
                "in": 0.0, "out": 0.0, "n": 0})
            bp["in"] += inc
            bp["out"] += out
            bp["n"] += r["n"]
            if r["project_id"]:
                by_project.append({"project_id": r["project_id"],
                                   "project_name": r["project_name"],
                                   "in": inc, "out": out, "n": r["n"]})
        by_project.sort(key=lambda x: -(x["out"] + x["in"]))
    finally:
        conn.close()
    return {"total_in": round(total_in, 2), "total_out": round(total_out, 2),
            "net": round(total_in - total_out, 2),
            "by_platform": list(by_platform.values()), "by_project": by_project}


@app.post("/api/cf/import-xlsx")
async def cf_import_xlsx(body: CfImportIn, request: Request):
    """Importe un export xlsx (Bricks.co ou La Première Brique) — body base64."""
    u = _need(request)
    import base64 as _b64
    try:
        raw = _b64.b64decode(body.b64 or "")
    except Exception:
        return JSONResponse({"detail": "Fichier illisible (base64 invalide)"}, status_code=400)
    if not raw:
        return JSONResponse({"detail": "Fichier vide"}, status_code=400)
    try:
        platform, ops = crowdfund.parse_xlsx(raw)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    conn = db()
    try:
        summary = crowdfund.import_operations(conn, u["username"], platform, ops)
        crowdfund.sync_indicators_from_ops(conn, u["username"])
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : import xlsx", platform)
    return {"ok": True, "platform": platform,
            "platform_label": crowdfund.CF_PLATFORMS[platform], **summary}


@app.get("/api/cf/platforms")
async def cf_list_platforms(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc, args = crowdfund._wc(owners)
        rows = conn.execute(f"SELECT * FROM cf_platforms WHERE {wc} ORDER BY platform", args).fetchall()
    finally:
        conn.close()
    return {"platforms": [dict(r) for r in rows]}


@app.put("/api/cf/platforms")
async def cf_update_platform(body: CfPlatformIn, request: Request):
    u = _need(request)
    if body.platform not in crowdfund.CF_PLATFORMS:
        return JSONResponse({"detail": "Plateforme inconnue"}, status_code=400)
    conn = db()
    try:
        cur = conn.execute(
            "SELECT * FROM cf_platforms WHERE owner=? AND platform=?",
            (u["username"], body.platform)).fetchone()
        vals = {
            "balance": body.balance if body.balance is not None else (cur["balance"] if cur else 0),
            "deposited": body.deposited if body.deposited is not None else (cur["deposited"] if cur else 0),
            # LPB : valeur dans les projets toujours calculée (auto)
            "invested_value": (body.invested_value if body.invested_value is not None
                               else (cur["invested_value"] if cur else 0)) if body.platform == "bricks" else 0,
        }
        conn.execute(
            """INSERT INTO cf_platforms (owner, platform, balance, deposited, invested_value, updated_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(owner, platform) DO UPDATE SET
               balance=?, deposited=?, invested_value=?, updated_at=?""",
            (u["username"], body.platform, vals["balance"], vals["deposited"],
             vals["invested_value"], crowdfund.now_iso(),
             vals["balance"], vals["deposited"], vals["invested_value"], crowdfund.now_iso()))
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : plateforme modifiée", body.platform)
    return {"ok": True}


@app.delete("/api/cf/platforms/{platform}")
async def cf_delete_platform(platform: str, request: Request):
    u = _need(request)
    if platform not in crowdfund.CF_PLATFORMS:
        return JSONResponse({"detail": "Plateforme inconnue"}, status_code=400)
    conn = db()
    try:
        crowdfund.remove_platform(conn, u["username"], platform)
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : plateforme supprimée", platform)
    return {"ok": True}


@app.get("/api/cf/curve")
async def cf_curve(request: Request, family: int = 0, member: str = ""):
    """Encours de la créance (capital encore dû) par plateforme — charts
    Crowdfunding (v2026.09.062), reconstruit des cf_operations datées :
    souscription (montant < 0) → encours + ; opération positive de type
    revenu (règle du module, ne rembourse pas le capital) → sans effet ;
    toute autre opération positive d'un projet (remboursement, revente) →
    encours − (plancher 0). Les ops sans projet ni plateforme suivie sont
    ignorées. Série en EUR, cumul fin de mois."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        ow = ",".join("?" * len(owners))
        pfs = conn.execute(
            "SELECT platform, account_id FROM cf_platforms"
            " WHERE owner IN (" + ow + ") AND account_id IS NOT NULL",
            owners).fetchall()
        pf2acc = {r["platform"]: r["account_id"] for r in pfs}
        if not pf2acc:
            return {"labels": [], "series": []}
        names = {}
        for r in conn.execute(
                "SELECT id, name FROM accounts WHERE id IN (%s)"
                % ",".join("?" * len(set(pf2acc.values()))),
                list(set(pf2acc.values()))).fetchall():
            names[r["id"]] = r["name"]
        ops = conn.execute(
            "SELECT platform, op_date, amount, type FROM cf_operations"
            " WHERE owner IN (" + ow + ") AND project_id IS NOT NULL"
            " AND status IN ('Validée','Réussi') ORDER BY op_date, id",
            owners).fetchall()
        run: dict[int, float] = {}
        per: dict[int, dict] = {}
        for o in ops:
            aid = pf2acc.get(o["platform"])
            if aid is None:
                continue
            amt = o["amount"] or 0.0
            delta = 0.0
            if amt < 0:
                delta = -amt
            elif amt > 0:
                typ = (o["type"] or "").lower()
                if not any(k in typ for k in ("revenu", "intérêt", "interet",
                                              "interest", "dividende")):
                    delta = -amt
            if delta:
                run[aid] = max(0.0, run.get(aid, 0.0) + delta)
                per.setdefault(aid, {})[o["op_date"][:7]] = round(run[aid], 2)
        # un mois sans mouvement de capital garde l'encours du mois précédent
        for aid, m in per.items():
            keys = sorted(m)
            if len(keys) < 2:
                continue
            cur = m[keys[0]]
            y, mo = int(keys[0][:4]), int(keys[0][5:7])
            y1, mo1 = int(keys[-1][:4]), int(keys[-1][5:7])
            while (y, mo) <= (y1, mo1):
                ym = f"{y:04d}-{mo:02d}"
                cur = m.get(ym, cur)
                if ym not in m:
                    m[ym] = cur
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
        labels = sorted({ym for m in per.values() for ym in m})
        series = [
            {"key": str(aid), "name": names.get(aid, str(aid)),
             "values": [per.get(aid, {}).get(ym, 0.0) for ym in labels]}
            for aid in per
        ]
        return {"labels": labels, "series": series}
    finally:
        conn.close()


@app.get("/api/cf/overview")
async def cf_overview(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return crowdfund.overview_rows(conn, owners)
    finally:
        conn.close()


@app.post("/api/cf/refresh")
async def cf_refresh(request: Request):
    """Recadre les comptes-auto du module (après un import xlsx ou une passe
    de l'extension) : valeur actuelle + série fin-de-mois + dépôt initial."""
    u = _need(request)
    conn = db()
    try:
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/cf/export")
async def cf_export(request: Request):
    """Sauvegarde JSON du module (projets + opérations + plateformes)."""
    u = _need(request)
    conn = db()
    try:
        return crowdfund.export_payload(conn, u["username"])
    finally:
        conn.close()


@app.post("/api/cf/import")
async def cf_import(request: Request):
    u = _need(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "JSON invalide"}, status_code=400)
    conn = db()
    try:
        err = crowdfund.do_cf_import(conn, u["username"], body)
        if err:
            return JSONResponse({"detail": err}, status_code=400)
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crowdfunding : restauration",
           f"{len(body.get('projects') or [])} projets")
    return {"ok": True}


@app.get("/api/cf/sync/report")
async def cf_sync_report(request: Request):
    """Dernier rapport de synchronisation (extension navigateur)."""
    u = _need_main(request)  # cookie OU jeton scope 'crowdfund' (filtre dans _me)
    conn = db_main()
    try:
        row = conn.execute(
            "SELECT data, created_at FROM cf_reports WHERE owner=?", (u["username"],)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": True, "report": None}
    try:
        data = json.loads(row["data"])
    except Exception:
        data = {}
    data["created_at"] = row["created_at"]
    return {"ok": True, "report": data}


@app.post("/api/cf/sync/ingest")
async def cf_sync_ingest(request: Request):
    """Reçoit les captures de l'extension (Bearer token scope 'crowdfund'),
    enrichit les projets et produit le rapport de conformité site vs exports."""
    u = _need_main(request)
    # jeton API scope 'crowdfund' uniquement (l'ingestion ne passe jamais par un
    # cookie de session — sqlite3.Row n'a pas de .get() → indexation par try)
    try:
        token_scope = u["token_scope"]
    except Exception:
        token_scope = None
    if token_scope != "crowdfund":
        return JSONResponse({"detail": "Jeton à portée 'crowdfund' requis"},
                            status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"detail": "JSON invalide"}, status_code=400)
    conn = db_main()
    try:
        try:
            res = crowdfund.run_ingest(conn, u["username"], data.get("captures") or [])
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        crowdfund.refresh_integration(conn, u["username"])
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, **res}


# ------------------------------------------------------------- module Crypto (v2026.09.050)


@app.get("/api/cw/wallets")
async def cw_wallets_list(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return {"wallets": crypto.wallets_rows(conn, owners)}
    finally:
        conn.close()


@app.post("/api/cw/wallets")
async def cw_wallets_add(request: Request):
    """Ajoute un wallet non-custodial (adresse EVM publique) puis lance son
    premier rafraîchissement complet (scan + historique)."""
    u = _need(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "JSON invalide"}, status_code=400)
    label = (body.get("label") or "").strip()
    address = (body.get("address") or "").strip()
    conn = db()
    try:
        try:
            w = crypto.add_wallet(conn, u["username"], label, address)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        conn.commit()
        if not w["demo"]:
            if not crypto.refresh_claimed(u["username"]):
                return JSONResponse({"detail": "Rafraîchissement déjà en cours"},
                                    status_code=409)
            try:
                try:
                    report = crypto.refresh_wallet(conn, u["username"], w["id"])
                except ValueError as e:
                    return JSONResponse({"detail": str(e)}, status_code=400)
                conn.commit()
            finally:
                crypto.refresh_release(u["username"])
        else:
            report = {"demo": True}
        wid = w["id"]
    finally:
        conn.close()
    _audit(u["username"], "Module crypto : wallet ajouté",
           f"#{wid} {label} ({address[:10]}…)")
    return {"ok": True, "wallet": wid, "refresh": report}


@app.delete("/api/cw/wallets/{wid}")
async def cw_wallets_delete(wid: int, request: Request):
    u = _need(request)
    conn = db()
    try:
        try:
            ok = crypto.remove_wallet(conn, u["username"], wid)
        except Exception as e:
            return JSONResponse({"detail": str(e)[:200]}, status_code=400)
        if not ok:
            return JSONResponse({"detail": "Wallet introuvable"}, status_code=404)
        conn.commit()
    finally:
        conn.close()
    _audit(u["username"], "Module crypto : wallet supprimé", f"#{wid}")
    return {"ok": True}


@app.post("/api/cw/refresh")
async def cw_refresh(request: Request):
    """Rafraîchit un wallet (ou tous ceux du compte) : scan → transferts →
    prix → série → intégration. Synchrone (le premier passage peut durer
    quelques minutes) ; 409 si déjà en cours."""
    u = _need(request)
    wid = None
    try:
        body = await request.json()
        wid = body.get("wallet_id")
    except Exception:
        pass  # corps absent = tous les wallets
    if not crypto.refresh_claimed(u["username"]):
        return JSONResponse({"detail": "Rafraîchissement déjà en cours"},
                            status_code=409)
    conn = db()
    try:
        try:
            if wid:
                report = crypto.refresh_wallet(conn, u["username"], int(wid))
            else:
                report = {"wallets": [crypto.refresh_wallet(
                    conn, u["username"], r["id"])
                    for r in conn.execute(
                        "SELECT id FROM cw_wallets WHERE owner=?",
                        (u["username"],)).fetchall()]}
            conn.commit()
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        except Exception as e:
            conn.rollback()
            return JSONResponse({"detail": str(e)[:300]}, status_code=500)
    finally:
        crypto.refresh_release(u["username"])
        conn.close()
    _audit(u["username"], "Module crypto : rafraîchissement", f"wallet={wid or 'tous'}")
    return {"ok": True, "report": report}


@app.get("/api/cw/refresh/status")
async def cw_refresh_status(request: Request):
    u = _need(request)
    return {"owner": u["username"], **crypto.STATE.get(u["username"],
                                                       {"state": "idle"})}


@app.get("/api/cw/curve")
async def cw_curve(request: Request, family: int = 0, member: str = ""):
    """Évolution mensuelle de la valeur des wallets, par compte — charts
    Crypto (v2026.09.062). Point du mois = dernier snapshot cw_history du
    mois (somme de tous les jetons du wallet), converti en EUR au taux BCE
    ≤ date du snapshot (None si taux indisponible). Les wallets sans
    historique ne tracent rien."""
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        wc = "a.owner IN (%s)" % ",".join("?" * len(owners))
        wrows = conn.execute(
            "SELECT w.id, w.account_id, a.name AS account_name"
            " FROM cw_wallets w JOIN accounts a ON a.id=w.account_id"
            " WHERE " + wc, owners).fetchall()
        if not wrows:
            return {"labels": [], "series": []}
        wids = [w["id"] for w in wrows]
        wq = ",".join("?" * len(wids))
        # dernier snapshot par (wallet, mois)
        sm = conn.execute(
            "SELECT wallet_id, substr(date,1,7) ym, MAX(date) md"
            " FROM cw_history WHERE wallet_id IN (" + wq + ")"
            " GROUP BY wallet_id, substr(date,1,7)", wids).fetchall()
        pts = {(r["wallet_id"], r["ym"]): r["md"] for r in sm}
        # sommes par (wallet, date de snapshot)
        md_dates = sorted({d for d in pts.values()})
        vals: dict[tuple, float] = {}
        for d in md_dates:
            for r in conn.execute(
                    "SELECT wallet_id, SUM(value_usd) v FROM cw_history"
                    " WHERE date=? AND wallet_id IN (" + wq + ")"
                    " GROUP BY wallet_id", [d] + wids).fetchall():
                vals[(r["wallet_id"], d)] = r["v"] or 0.0
        labels = sorted({ym for _, ym in pts})
        byacc: dict[int, dict] = {}
        for w in wrows:
            e = byacc.setdefault(w["account_id"], {"name": w["account_name"],
                                                   "wallets": []})
            e["wallets"].append(w["id"])
        series = []
        for aid, e in byacc.items():
            values = []
            for ym in labels:
                tot = 0.0
                ok = False
                md = None
                for wid in e["wallets"]:
                    d0 = pts.get((wid, ym))
                    if not d0:
                        continue
                    md = d0 if md is None else max(md, d0)
                    if (wid, d0) in vals:
                        tot += vals[(wid, d0)]
                        ok = True
                if not ok:
                    values.append(None)
                    continue
                eur = crypto._usd_to_eur(conn, tot, md)
                values.append(round(eur, 2) if eur is not None else None)
            series.append({"key": str(aid), "name": e["name"],
                           "values": values})
        return {"labels": labels, "series": series}
    finally:
        conn.close()


@app.get("/api/cw/overview")
async def cw_overview(request: Request, family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return crypto.overview_rows(conn, owners)
    finally:
        conn.close()


@app.get("/api/cw/tokens")
async def cw_tokens(request: Request, wallet_id: int = 0,
                    family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return {"tokens": crypto.tokens_rows(conn, owners,
                                             wallet_id or None)}
    finally:
        conn.close()


@app.get("/api/cw/history")
async def cw_history(request: Request, wallet_id: int = 0,
                     family: int = 0, member: str = ""):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family), member or None)
        return {"points": crypto.monthly_history(conn, owners,
                                                 wallet_id or None)}
    finally:
        conn.close()


@app.get("/manifest.webmanifest")
async def manifest_pwa():
    return JSONResponse(
        {
            "name": "Patrimony — Data Sovereignty",
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

# module Crypto (v2026.09.050) : rafraîchissement automatique des wallets
# non-custodial — rattrapage au boot si > 24 h, puis boucle quotidienne à
# 06:00 Europe/Paris. Désactivable (PAT_CRYPTO_AUTO=0). Ne démarre JAMAIS de
# réseau dans les tests (aucun wallet en base → sommeil).
if os.environ.get("PAT_CRYPTO_AUTO", "1") != "0":
    threading.Thread(
        target=crypto.auto_loop, args=(str(DB_PATH),), daemon=True,
        name="pat-crypto-auto", kwargs={"max_age_h": float(
            os.environ.get("PAT_CRYPTO_MAX_AGE_H", "24"))},
    ).start()

