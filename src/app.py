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
from src import transfer
from src import vault
from src.schema import schema_data
from src import bench

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
        if name == "Appartement locatif":
            # crédit lié au bien (démo v2026.09.033) : équité = valeur − restant
            conn.execute(
                "UPDATE accounts SET loan_principal=92000, loan_rate=2.8, loan_monthly=520 WHERE id=?",
                (aid,),
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


# enveloppe fiscale d'un actif : elle détermine la PV nette (règles FR/LU à
# venir — v026 roadmap). Classes autorisées par enveloppe : PEA/CTO = titres
# (bourse), AV = contrats d'assurance (bourse ou épargne fonds euros).
_WRAPPER_ALLOW = {"pea": ("bourse",), "cto": ("bourse",), "av": ("bourse", "epargne")}


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
    conn = db()
    cur = conn.execute(
        "INSERT INTO accounts (owner, name, asset_class, institution, currency, fx_override, cost_basis, fees_pct, wrapper, tax_country, loan_principal, loan_rate, loan_monthly, open_date, notes, active,"
        " valuation_mode, symbol, quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (u["username"], body.name.strip(), body.asset_class, body.institution.strip(), ccy,
         round(body.fx_override, 6) if body.fx_override else None, body.cost_basis or 0,
         round(body.fees_pct, 4) if body.fees_pct is not None else None,
         body.wrapper, body.tax_country or "",
         round(body.loan_principal, 2) if body.loan_principal else 0,
         round(body.loan_rate, 4) if body.loan_rate else 0,
         round(body.loan_monthly, 2) if body.loan_monthly else 0,
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
        # crédit lié (v2026.09.033) : passif converti au même taux que la valo
        if r["asset_class"] == "immobilier" and r["loan_principal"]:
            debt_eur = r["loan_principal"] / fxr["rate"] if ccy != "EUR" else r["loan_principal"]
            total_debt += debt_eur
        if fxr.get("date"):
            fx_dates.add(fxr["date"])
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
    net_worth = round(total_value - total_debt, 2)
    fx_asof = max(fx_dates) if fx_dates else None
    return {
        "total_value": round(total_value, 2),
        "total_debt": round(total_debt, 2),  # passifs (crédits liés, v2026.09.033)
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
async def evolution(request: Request, months: int = 12, family: int = 0):
    """Pourquoi le patrimoine change : décomposition additive par mois
    (Flux = dépôts − retraits − dépenses, Revenus = opérations income,
    Effet marché = résidu) + snapshots annuels par classe (dernière
    valorisation de décembre, année courante incluse partielle)."""
    u = _need(request)
    months = max(3, min(months, 60))
    conn = db()
    owners = _visible_owners(conn, u, bool(family))
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
    kind: str = "income"  # v2026.09.028 : income | expense (règles de dépenses)


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
                "kind": r["kind"],
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


@app.get("/api/cashflow")
async def cashflow(request: Request, months: int = 12):
    """Projection de trésorerie : règles récurrentes (revenus ET dépenses)
    sur les mois à venir, solde cumulé à partir de la trésorerie réelle
    (dernière valorisation des comptes de classe « comptes »)."""
    u = _need(request)
    months = max(3, min(months, 36))
    conn = db()
    try:
        rules = conn.execute(
            "SELECT r.* FROM income_rules r JOIN accounts a ON a.id=r.account_id"
            " WHERE r.active=1 AND a.owner=? ORDER BY r.label", (u["username"],),
        ).fetchall()
        bal_row = conn.execute(
            "SELECT COALESCE(SUM(v.value), 0) FROM valuations v"
            " WHERE v.id IN (SELECT MAX(id) FROM valuations"
            "  WHERE account_id IN (SELECT id FROM accounts WHERE owner=? AND asset_class='comptes')"
            "  GROUP BY account_id)", (u["username"],),
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
async def benchmarks(request: Request, family: int = 0):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family))
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
async def refresh_benchmarks(request: Request, family: int = 0):
    u = _need(request)
    conn = db()
    try:
        owners = _visible_owners(conn, u, bool(family))
        today = date.today()
        start = bench.start_ym(conn, owners, today)
        need = bench.needs(conn, start, True, today)
        if need:
            charts = await run_in_threadpool(bench.fetch_charts, need, _yahoo_chart)
            bench.store_levels(conn, start, charts)
    finally:
        conn.close()
    return await benchmarks(request, family=family)
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
