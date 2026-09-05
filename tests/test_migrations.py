"""Tests de migration de base (revue externe 2026-09-05) : boot sur des bases
d'anciennes versions — colonnes ajoutées, backfills, purge des orphelins.
init_db() est idempotent et s'exécute au boot ; ces tests le rejouent sur des
répertoires DATA_DIR jetables (monkeypatch des globals du module).
"""
import os
import sqlite3
import tempfile

# Protège les exécutions isolées : sans env, l'import de src.app créerait
# ./data/app.db dans le repo (init_db() s'exécute à l'import).
_env_tmp = tempfile.mkdtemp(prefix="patrimony-mig-env-")
os.environ["DATA_DIR"] = _env_tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"

import pytest

import src.app as app

PWD = "member-pass-2026"


@pytest.fixture()
def scratch_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="patrimony-mig-")
    monkeypatch.setattr(app, "DATA_DIR", __import__("pathlib").Path(d))
    monkeypatch.setattr(app, "DB_PATH", __import__("pathlib").Path(d) / "app.db")
    return d


def _mkdb(path: str, script: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(script)
    c.commit()
    c.close()


def test_migrate_v010_to_v012(scratch_dir):
    """Base v010 : vaults SANS canary, comptes clairs orphelins d'un protected,
    données admin/membres normales → après boot : canary ajouté (vide), purge
    des orphelins, rien d'autre touché."""
    _mkdb(os.path.join(scratch_dir, "app.db"), """
    CREATE TABLE users (username TEXT PRIMARY KEY, password TEXT NOT NULL,
      display_name TEXT DEFAULT '', role TEXT DEFAULT 'member', mode TEXT DEFAULT 'standard',
      must_change INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
    CREATE TABLE sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL);
    CREATE TABLE vaults (username TEXT PRIMARY KEY, salt TEXT NOT NULL, wrapped TEXT NOT NULL,
      blob TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now')));
    INSERT INTO users VALUES ('admin','x','Admin','admin','standard',0,datetime('now'));
    INSERT INTO users VALUES ('sec-legacy','x','Membre','member','standard',0,datetime('now'));
    INSERT INTO users VALUES ('prot-legacy','x','Protégé','member','protected',1,datetime('now'));
    INSERT INTO vaults VALUES ('prot-legacy','c2FsdA==','d3JhcHBlZA==','c2VjcmV0LWJsb2I=',datetime('now'));
    """)
    # comptes : schéma v010 complet (owner inclus)
    c = sqlite3.connect(os.path.join(scratch_dir, "app.db"))
    c.executescript("""
    CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL, asset_class TEXT NOT NULL, institution TEXT DEFAULT '',
      currency TEXT DEFAULT 'EUR', valuation_mode TEXT DEFAULT 'manual', cost_basis REAL DEFAULT 0,
      open_date TEXT, close_date TEXT, notes TEXT DEFAULT '', active INTEGER DEFAULT 1,
      created_at TEXT DEFAULT (datetime('now')), updated_at TEXT,
      symbol TEXT DEFAULT '', quantity REAL DEFAULT 0);
    INSERT INTO accounts (owner, name, asset_class) VALUES ('admin','Compte admin','comptes');
    INSERT INTO accounts (owner, name, asset_class) VALUES ('sec-legacy','Compte membre','epargne');
    INSERT INTO accounts (owner, name, asset_class) VALUES ('prot-legacy','Orphelin clair','crypto');
    """)
    c.commit()
    c.close()

    app.init_db()

    c = sqlite3.connect(os.path.join(scratch_dir, "app.db"))
    c.row_factory = sqlite3.Row
    cols = [r["name"] for r in c.execute("PRAGMA table_info(vaults)").fetchall()]
    assert "canary" in cols
    canary = c.execute("SELECT canary FROM vaults WHERE username='prot-legacy'").fetchone()["canary"]
    assert canary == ""
    owners = [r["owner"] for r in c.execute("SELECT owner FROM accounts").fetchall()]
    assert sorted(owners) == ["admin", "sec-legacy"]  # l'orphelin clair a été purgé
    blob = c.execute("SELECT blob FROM vaults WHERE username='prot-legacy'").fetchone()["blob"]
    assert blob == "c2VjcmV0LWJsb2I="  # le coffre chiffré, lui, est intact
    c.close()


def test_migrate_ancient_schema_backfills(scratch_dir):
    """Base très ancienne : users (username, password) et accounts sans
    owner/symbol/quantity → les colonnes sont ajoutées, l'utilisateur unique
    est promu admin et les comptes orphelins lui sont rattachés."""
    _mkdb(os.path.join(scratch_dir, "app.db"), """
    CREATE TABLE users (username TEXT PRIMARY KEY, password TEXT NOT NULL);
    INSERT INTO users VALUES ('admin','x');
    CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      asset_class TEXT NOT NULL, cost_basis REAL DEFAULT 0);
    INSERT INTO accounts (name, asset_class) VALUES ('Ancien compte','epargne');
    """)

    app.init_db()

    c = sqlite3.connect(os.path.join(scratch_dir, "app.db"))
    c.row_factory = sqlite3.Row
    role = c.execute("SELECT role, mode, display_name FROM users WHERE username='admin'").fetchone()
    assert role["role"] == "admin" and role["mode"] == "standard"
    owner = c.execute("SELECT owner FROM accounts WHERE name='Ancien compte'").fetchone()["owner"]
    assert owner == "admin"  # backfill vers l'admin configuré (mono-utilisateur d'origine)
    cols = [r["name"] for r in c.execute("PRAGMA table_info(accounts)").fetchall()]
    for col in ("owner", "symbol", "quantity"):
        assert col in cols
    c.close()


def test_fresh_boot_seeds_admin_and_stays_idempotent(scratch_dir):
    app.init_db()
    app.init_db()  # re-boot : aucune erreur, données intactes
    c = sqlite3.connect(os.path.join(scratch_dir, "app.db"))
    n = c.execute("SELECT COUNT(*) FROM users WHERE username='admin' AND role='admin'").fetchone()[0]
    assert n == 1
    c.close()
