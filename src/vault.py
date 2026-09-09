"""Coffres chiffrés des comptes protégés (extrait de src/app.py, v2026.09.036).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI — les connexions
à la base principale sont TOUJOURS passées en paramètre (main) par l'appelant
(middleware ou route), qui en garde la responsabilité (ouverture/fermeture).
Les messages d'erreur sont émis en FR (source de vérité lisible) — le
middleware HTTP de src/app.py les traduit selon Accept-Language.

Règles de conception (héritées des versions v010→v030, à préserver) :
- État MONO-PROCESS : ne pas lancer uvicorn avec --workers > 1 ni
  multi-réplicas (les coffres ouverts vivent dans la mémoire du process).
- La DEK n'existe qu'en mémoire ; le serveur ne stocke que sel + wrapped +
  blob (AES-256-GCM) — l'admin ne peut rien déchiffrer.
- flush : snapshot `Connection.serialize()` (jamais de clair sur le disque ;
  repli fichier temporaire nettoyé en finally si le SQLite du runtime ne
  supporte pas la désérialisation).
- close() des connexions coffre est neutralisé (les handlers ferment leur
  connexion en fin de route) ; la fermeture réelle = _hard_close().
- check_same_thread=False : la base :memory: vit au-delà du handler qui l'a
  créée — SQLite en mode serialized + accès gardés par GUARD.
"""

import base64
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.schema import schema_data
from src.loans import migrate_legacy

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))

VAULT_IDLE_MIN = int(os.environ.get("VAULT_IDLE_MIN", "30"))

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - dépendance requise (requirements.txt)
    AESGCM = None

# username -> coffre ouvert {conn, dek, sessions:{token: dernier usage (monotonic)}}
VAULTS: dict[str, dict] = {}
GUARD = threading.Lock()

_CANARY_PT = b"patrimony-vault-key-canary-v1"


def _aesgcm():
    """AESGCM chargé (dépendance requise) — garde explicite pour l'analyse
    statique : les appels ne sont atteignables qu'après mem_new/mem_from_blob
    qui refusent de tourner sans cryptography."""
    if AESGCM is None:
        raise RuntimeError("cryptography manquante (pip install cryptography)")
    return AESGCM


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


class VaultConn(sqlite3.Connection):
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


class VaultError(Exception):
    """Échec d'une opération coffre. Le message (FR, traduit par le middleware
    HTTP) est la réponse JSON `detail` ; `audit` signale les échecs qui doivent
    être journalisés (« Échec de récupération ») — les erreurs de format des
    matériaux ne le sont pas (anti-bruit)."""

    def __init__(self, message: str, audit: bool = False):
        super().__init__(message)
        self.audit = audit


# ---------------------------------------------------------------- canary DEK

def canary(dek: bytes) -> str:
    """Valeur témoin chiffrée par la DEK : permet de vérifier une clé fournie
    SANS déchiffrer tout le blob (open à chaud)."""
    nonce = secrets.token_bytes(12)
    ct = _aesgcm()(dek).encrypt(nonce, _CANARY_PT, None)
    return b64e(nonce + ct)


def check_canary(dek: bytes, canary_b64: str) -> bool:
    if not canary_b64:
        return True  # coffre hérité (pré-v011) : la preuve est le déchiffrement du blob à froid
    try:
        raw = b64d(canary_b64)
        _aesgcm()(dek).decrypt(raw[:12], raw[12:], None)
        return True
    except Exception:
        return False


def store_canary(main: sqlite3.Connection, username: str, canary_b64: str) -> None:
    main.execute("UPDATE vaults SET canary=? WHERE username=?", (canary_b64, username))
    main.commit()


# ---------------------------------------------------------------- bases mémoire

def mem_new(dek: bytes) -> sqlite3.Connection:
    """Base mémoire vide d'un coffre (schéma de données complet)."""
    # check_same_thread=False : la connexion vit au-delà du handler qui l'a
    # créée et sert les requêtes suivantes (worker du pool différent sous
    # TestClient/anyio) — SQLite est compilé en mode serialized et les
    # accès concurrents sont déjà gardés par GUARD.
    conn = sqlite3.connect(":memory:", factory=VaultConn, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema_data(conn)
    # clôt la transaction éventuelle du seed sans marquer dirty (le backup vers
    # une destination en transaction échoue : « destination database is in use »)
    sqlite3.Connection.commit(conn)
    return conn


def mem_from_blob(dek: bytes, blob_b64: str) -> sqlite3.Connection:
    """Base mémoire d'un coffre déchiffrée depuis vaults.blob.
    Jamais de clair sur le disque : deserialize() reste en mémoire ; repli
    fichier temporaire uniquement si le SQLite du runtime ne le supporte pas
    (nettoyé en finally)."""
    conn = mem_new(dek)
    if not blob_b64:
        return conn
    nonce_ct = b64d(blob_b64)
    raw = _aesgcm()(dek).decrypt(nonce_ct[:12], nonce_ct[12:], None)
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
    schema_data(conn)
    # module Crédits (v2026.09.056) : migration des crédits liés legacy du
    # coffre (accounts.loan_* → loans). Commit immédiat si des lignes ont été
    # créées : le flush du middleware ne doit pas les annuler (rollback)
    if migrate_legacy(conn):
        conn.commit()  # marque le coffre sale → le blob chiffré est réécrit
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------- persistance

def flush(username: str, v: dict, main: sqlite3.Connection) -> None:
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
    blob = nonce + _aesgcm()(v["dek"]).encrypt(nonce, raw, None)
    main.execute(
        "UPDATE vaults SET blob=?, updated_at=datetime('now') WHERE username=?",
        (b64e(blob), username),
    )
    main.commit()
    conn._vault_dirty = False


def gc(username: str, v: dict, main: sqlite3.Connection) -> None:
    """Purge les sessions expirées OU inactives (auto-lock) ; ferme le coffre
    si plus aucune session."""
    if not v["sessions"]:
        if v["conn"] is not None:
            flush(username, v, main)
            try:
                v["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass
            v["conn"] = None
        VAULTS.pop(username, None)
        return
    now = datetime.now(timezone.utc).isoformat()
    idle_cut = time.monotonic() - VAULT_IDLE_MIN * 60 if VAULT_IDLE_MIN > 0 else 0
    for tok, last in list(v["sessions"].items()):
        if idle_cut and last < idle_cut:
            v["sessions"].pop(tok, None)  # inactivité → verrouillage
        elif not main.execute(
            "SELECT 1 FROM sessions WHERE token=? AND expires_at>?", (tok, now)
        ).fetchone():
            v["sessions"].pop(tok, None)  # session expirée (TTL 30 j)
    if not v["sessions"]:
        gc(username, v, main)


# ---------------------------------------------------------------- état (sessions mémoire)

def register(username: str, conn: sqlite3.Connection, dek: bytes, token: str) -> None:
    """Ouvre/remplace le coffre en mémoire pour une session (init, open à
    froid, récupération). Un éventuel coffre précédent est fermé."""
    with GUARD:
        old = VAULTS.get(username)
        if old is not None and old["conn"] is not None:
            try:
                old["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass
        VAULTS[username] = {"conn": conn, "dek": dek,
                            "sessions": {token: time.monotonic()}, "username": username}


def unregister(username: str) -> None:
    """Ferme et retire le coffre mémoire (suppression de membre)."""
    with GUARD:
        v = VAULTS.pop(username, None)
        if v is not None and v["conn"] is not None:
            try:
                v["conn"]._hard_close()
            except sqlite3.ProgrammingError:
                pass


def active(username: str, token: str) -> bool:
    """Le coffre est-il ouvert pour cette session ? (aucune écriture)."""
    v = VAULTS.get(username)
    return v is not None and v["conn"] is not None and token in v["sessions"]


def copy_rows(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, where: str, args: tuple) -> None:
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


# ---------------------------------------------------------------- matériaux publics

def public_materials(main: sqlite3.Connection, username: str) -> dict | None:
    """salt + wrapped + état de la clé de récupération (login/me)."""
    row = main.execute(
        "SELECT salt, wrapped, r_auth FROM vaults WHERE username=?", (username,)
    ).fetchone()
    if row is None:
        return None
    return {"salt": row["salt"], "wrapped": row["wrapped"],
            "recovery_armed": bool(row["r_auth"])}


def recovery_materials(main: sqlite3.Connection, username: str) -> dict | None:
    """Matériaux publics de la clé de récupération (recover/start) — None si
    le compte n'existe pas / n'est pas protégé / n'a pas de clé armée."""
    row = main.execute("SELECT mode FROM users WHERE username=?", (username,)).fetchone()
    if row is None or row["mode"] != "protected":
        return None
    v = main.execute(
        "SELECT r_salt, r_auth_salt, r_wrapped FROM vaults WHERE username=?", (username,)
    ).fetchone()
    if v is None or not v["r_auth_salt"]:
        return None
    return {"r_salt": v["r_salt"], "r_auth_salt": v["r_auth_salt"], "r_wrapped": v["r_wrapped"]}


# ---------------------------------------------------------------- cycles de vie

def init_vault(main: sqlite3.Connection, username: str, salt: str, wrapped: str,
               dek: bytes, token: str) -> None:
    """Initialise le coffre d'un compte protégé : base mémoire + transfert des
    données claires + ligne vaults + ouverture pour la session. L'effacement
    du clair et l'audit restent à la charge de la route."""
    if main.execute("SELECT 1 FROM vaults WHERE username=?", (username,)).fetchone():
        raise VaultError("Coffre déjà initialisé")
    mem = mem_new(dek)
    copy_rows(main, mem, "accounts", "owner=?", (username,))
    copy_rows(main, mem, "valuations",
              "account_id IN (SELECT id FROM accounts WHERE owner=?)", (username,))
    copy_rows(main, mem, "transactions",
              "account_id IN (SELECT id FROM accounts WHERE owner=?)", (username,))
    copy_rows(main, mem, "income_rules",
              "account_id IN (SELECT id FROM accounts WHERE owner=?)", (username,))
    copy_rows(main, mem, "positions",
              "account_id IN (SELECT id FROM accounts WHERE owner=?)", (username,))
    copy_rows(main, mem, "dividend_events",
              "position_id IN (SELECT p.id FROM positions p"
              " JOIN accounts a ON a.id=p.account_id WHERE a.owner=?)", (username,))
    # module Crowdfunding (v2026.09.046) : tables scopées par owner
    copy_rows(main, mem, "cf_platforms", "owner=?", (username,))
    copy_rows(main, mem, "cf_projects", "owner=?", (username,))
    copy_rows(main, mem, "cf_operations", "owner=?", (username,))
    copy_rows(main, mem, "cf_reports", "owner=?", (username,))
    # module Crédits (v2026.09.056) : crédits liés legacy migrés ici même
    copy_rows(main, mem, "loans", "owner=?", (username,))
    migrate_legacy(mem)
    # module Crypto (v2026.09.050) : wallets + transferts + séries + scans
    copy_rows(main, mem, "cw_wallets", "owner=?", (username,))
    copy_rows(main, mem, "cw_transfers", "owner=?", (username,))
    copy_rows(main, mem, "cw_history", "owner=?", (username,))
    copy_rows(main, mem, "cw_scans", "owner=?", (username,))
    copy_rows(main, mem, "settings", "member=?", (username,))
    register(username, mem, dek, token)
    # la ligne vaults doit exister avant le flush du blob
    main.execute(
        "INSERT INTO vaults (username, salt, wrapped, canary, blob) VALUES (?,?,?,?,'')",
        (username, salt, wrapped, canary(dek)),
    )
    main.commit()
    mem.commit()  # marque le coffre sale → le flush du middleware l'écrit chiffré


def open_vault(main: sqlite3.Connection, username: str, dek: bytes, token: str) -> None:
    """Ouvre le coffre pour la session (à froid : déchiffrement réel + canary ;
    à chaud : canary seul). Rétro-arme le canary des coffres hérités."""
    vault = main.execute(
        "SELECT salt, wrapped, blob, canary FROM vaults WHERE username=?", (username,)
    ).fetchone()
    if vault is None:
        raise VaultError("Coffre non initialisé")
    with GUARD:
        v = VAULTS.get(username)
        if v is None:
            # open à froid : la clé est prouvée par le déchiffrement réel du blob
            # (et par le canary s'il est déjà armé)
            if not check_canary(dek, vault["canary"] or ""):
                raise VaultError("Clé de coffre invalide")
            try:
                mem = mem_from_blob(dek, vault["blob"])
            except Exception:
                raise VaultError("Clé de coffre invalide") from None
            VAULTS[username] = {"conn": mem, "dek": dek,
                                "sessions": {token: time.monotonic()}, "username": username}
            if not vault["canary"]:
                # rétro-armement : la DEK vient d'être prouvée par le blob — les
                # prochains opens (même à chaud) la vérifieront via le canary
                try:
                    store_canary(main, username, canary(dek))
                except Exception:
                    pass
        else:
            # open à chaud : vérifier la DEK fournie même si le coffre est déjà
            # en cache (sinon n'importe quelle clé ouvrirait une session)
            if not check_canary(dek, vault["canary"] or ""):
                raise VaultError("Clé de coffre invalide")
            v["sessions"][token] = time.monotonic()


def arm_recovery(main: sqlite3.Connection, username: str, token: str,
                 r_salt: str, r_auth_salt: str, r_wrapped: str, r_auth: str) -> None:
    """Arme (ou remplace) la clé de récupération. Exige une session au coffre
    OUVERT : seul le détenteur de la DEK peut produire r_wrapped, et l'ancienne
    clé est invalidée d'un coup (UPDATE)."""
    if not active(username, token):
        raise VaultError("Déverrouillez d'abord le coffre")
    for label, raw, lo in (("r_salt", r_salt, 8), ("r_auth_salt", r_auth_salt, 8),
                           ("r_wrapped", r_wrapped, 16), ("r_auth", r_auth, 16)):
        try:
            if len(b64d(raw)) < lo:
                raise ValueError
        except Exception:
            raise VaultError(f"Champ {label} invalide") from None
    main.execute(
        "UPDATE vaults SET r_salt=?, r_auth_salt=?, r_wrapped=?, r_auth=?,"
        " updated_at=datetime('now') WHERE username=?",
        (r_salt, r_auth_salt, r_wrapped, r_auth, username),
    )
    main.commit()


def prepare_recover(main: sqlite3.Connection, username: str, proof_b64: str,
                    dek_b64: str, new_password: str, wrapped: str, salt: str,
                    min_password_len: int) -> dict:
    """Mot de passe oublié — phase de PREUVE : clé de récupération (preuve
    hmac), règles du nouveau mot de passe, puis DEK prouvée par le
    déchiffrement réel (canary + blob). Retourne l'état coffre {conn, dek} ;
    la route crée ensuite la session, met à jour users/vaults, pose le cookie
    et audite le succès. L'ancien mdp n'est pas requis (perdu par définition)."""
    vault = None
    row = main.execute("SELECT mode FROM users WHERE username=?", (username,)).fetchone()
    if row is not None and row["mode"] == "protected":
        vault = main.execute(
            "SELECT r_auth, canary, blob FROM vaults WHERE username=?", (username,)
        ).fetchone()
    if vault is None or not vault["r_auth"]:
        raise VaultError("Récupération impossible")
    try:
        proof, expected = b64d(proof_b64), b64d(vault["r_auth"])
    except Exception:
        raise VaultError("Clé de récupération invalide") from None
    if len(proof) != len(expected) or not secrets.compare_digest(proof, expected):
        raise VaultError("Clé de récupération invalide", audit=True)
    if len(new_password) < min_password_len:
        raise VaultError(f"Mot de passe trop court (min. {min_password_len} caractères)")
    if not wrapped or not salt:
        raise VaultError("Re-chiffrement du coffre requis (wrapped + salt)")
    try:
        dek = b64d(dek_b64)
    except Exception:
        raise VaultError("Clé de coffre invalide") from None
    if len(dek) != 32:
        raise VaultError("Clé de coffre invalide")
    if not check_canary(dek, vault["canary"] or ""):
        raise VaultError("Clé de récupération invalide", audit=True)
    try:
        mem = mem_from_blob(dek, vault["blob"])
    except Exception:
        raise VaultError("Clé de récupération invalide", audit=True) from None
    return {"conn": mem, "dek": dek}


def rewrap(main: sqlite3.Connection, username: str, salt: str, wrapped: str) -> bool:
    """Re-wrap de la DEK sous un nouveau mot de passe (comptes protégés dotés
    d'un coffre). Retourne False si aucune ligne vaults (pas de coffre)."""
    cur = main.execute(
        "UPDATE vaults SET salt=?, wrapped=?, updated_at=datetime('now') WHERE username=?",
        (salt, wrapped, username),
    )
    return cur.rowcount > 0


def has_vault(main: sqlite3.Connection, username: str) -> bool:
    """Le compte dispose-t-il d'un coffre (ligne vaults) ?"""
    return main.execute(
        "SELECT 1 FROM vaults WHERE username=?", (username,)
    ).fetchone() is not None
