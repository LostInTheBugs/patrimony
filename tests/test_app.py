"""Tests API Patrimony (v2026.09.011).

L'environnement DOIT être posé avant l'import de src.app (init_db() s'exécute
à l'import). Les tests partagent une base temporaire unique ; chaque test
utilise des comptes au nom unique. Les coffres ouverts sont purgés entre
les tests (autouse fixture).
"""
import base64
import os
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="patrimony-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"  # auto-lock piloté en white-box dans les tests

import pytest
from fastapi.testclient import TestClient

import src.app as app
from src.app import _hash, _verify

DEK = base64.b64encode(b"\x11" * 32).decode()
DEK_WRONG = base64.b64encode(b"\x22" * 32).decode()
PWD = "member-pass-2026"


@pytest.fixture(autouse=True)
def _clean_state():
    app._LOGIN_FAILS.clear()
    # referme les coffres restés ouverts par un test précédent
    for uname, v in list(app._VAULTS.items()):
        if v["conn"] is not None:
            try:
                v["conn"]._hard_close()
            except Exception:
                pass
        app._VAULTS.pop(uname, None)
    yield


def _login(client: TestClient, username: str, password: str, expected: int = 200):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == expected, r.text
    return r


def _logout(client: TestClient):
    client.post("/api/auth/logout")
    client.cookies.clear()


def _make_member(admin_c: TestClient, username: str, mode: str = "standard"):
    r = admin_c.post("/api/family", json={
        "username": username, "display_name": username,
        "password": PWD, "mode": mode,
    })
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- unitaires

def test_hash_verify_roundtrip():
    stored = _hash("s3cret-passphrase")
    assert stored.startswith("pbkdf2$")
    assert _verify("s3cret-passphrase", stored)
    assert not _verify("wrong-passphrase", stored)
    assert not _verify("x", "not-a-valid-stored-hash")


# ---------------------------------------------------------------- auth/login

def test_login_ok_and_wrong():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    assert c.cookies.get("pat_session")
    _logout(c)
    _login(c, "admin", "bad-password", expected=401)


def test_login_unknown_user_rejected():
    c = TestClient(app.app)
    r = _login(c, "ghost-unknown-user", "whatever-password", expected=401)
    assert r.json()["detail"] == "Identifiants invalides"


def test_login_rate_limit_per_user():
    app.LOGIN_MAX_FAILS = 3
    app.LOGIN_MAX_FAILS_USER = 3
    try:
        c = TestClient(app.app)
        uname = "ratelimit-ghost"
        for _ in range(3):
            _login(c, uname, "wrong-password", expected=401)
        r = _login(c, uname, "wrong-password", expected=429)
        assert r.json()["code"] == "rate_limited"
        # la levée du blocage (fenêtre écoulée) débloque le compte
        app._LOGIN_FAILS.clear()
        _login(c, uname, "wrong-password", expected=401)
    finally:
        app.LOGIN_MAX_FAILS = 5
        app.LOGIN_MAX_FAILS_USER = 10


def test_password_min_length_enforced():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    # change_password d'un membre standard : trop court → 400
    _make_member(c, "minlen-user")
    _logout(c)
    _login(c, "minlen-user", PWD)
    r = c.post("/api/auth/password", json={"current": PWD, "new": "short"})
    assert r.status_code == 400
    # création famille : trop court → 400
    _logout(c)
    _login(c, "admin", "admin-test-2026")
    r = c.post("/api/family", json={"username": "minlen-2", "password": "short", "mode": "standard"})
    assert r.status_code == 400


def test_static_assets_served_no_middleware_cost():
    # le middleware ignore les chemins hors /api/ : les assets répondent quand même
    c = TestClient(app.app)
    r = c.get("/logo-mark.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


# ---------------------------------------------------------------- isolation owner

def test_owner_isolation_between_members():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    _make_member(admin_c, "owner-m1")
    _make_member(admin_c, "owner-m2")

    c1 = TestClient(app.app)
    _login(c1, "owner-m1", PWD)
    r = c1.post("/api/accounts", json={
        "name": "Compte M1", "asset_class": "comptes", "institution": "Banque X",
        "cost_basis": 1000, "initial_value": 1000,
    })
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    c2 = TestClient(app.app)
    _login(c2, "owner-m2", PWD)
    accs = c2.get("/api/accounts").json()["accounts"]
    assert all(a["id"] != aid for a in accs)  # M2 ne voit pas l'actif de M1
    # M2 ne peut ni lire ni modifier l'actif de M1
    assert c2.put(f"/api/accounts/{aid}", json={"name": "vol", "asset_class": "comptes"}).status_code == 404
    assert c2.delete(f"/api/accounts/{aid}").status_code == 200  # DELETE silencieux mais sans effet
    accs = c2.get("/api/accounts").json()["accounts"]
    assert accs == []


# ---------------------------------------------------------------- coffre protégé

def _vault_row(username: str):
    m = app.db_main()
    try:
        return m.execute(
            "SELECT blob, canary FROM vaults WHERE username=?", (username,)
        ).fetchone()
    finally:
        m.close()


def _assert_no_plaintext_tmp():
    """Aucun fichier .vault* en clair ne doit traîner dans DATA_DIR (flush et
    open passent par serialize()/deserialize() en mémoire depuis v012)."""
    leftovers = [f for f in os.listdir(_tmp) if f.startswith(".vault")]
    assert leftovers == [], f"fichiers coffre en clair laissés sur le disque: {leftovers}"


def test_protected_vault_cycle():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    _make_member(admin_c, "vault-p1", mode="protected")
    _logout(admin_c)

    # --- 1. login + changement du mot de passe initial (flux réel must_change)
    c1 = TestClient(app.app)
    r = _login(c1, "vault-p1", PWD)
    assert r.json()["must_change"] is True
    assert c1.post("/api/auth/password", json={"current": PWD, "new": PWD}).status_code == 200

    # --- 2. init du coffre : canary stocké, blob chiffré après flush middleware
    r = c1.post("/api/vault/init", json={"salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==", "dek": DEK})
    assert r.status_code == 200, r.text
    row = _vault_row("vault-p1")
    assert row["canary"]  # valeur témoin armée dès l'init
    assert row["blob"]  # flush du middleware en fin de requête d'init
    _assert_no_plaintext_tmp()  # serialize() : aucun .vault_tmp_* en clair

    # --- 3. écriture : les données partent dans la base mémoire du coffre
    r = c1.post("/api/accounts", json={
        "name": "Coffre secret", "asset_class": "crypto", "cost_basis": 5000, "initial_value": 5000,
    })
    assert r.status_code == 200, r.text
    accs = c1.get("/api/accounts").json()["accounts"]
    assert len(accs) == 1

    # isolation : rien en clair dans la base principale (ni comptes ni enfant)
    m = app.db_main()
    n = m.execute("SELECT COUNT(*) c FROM accounts WHERE owner='vault-p1'").fetchone()["c"]
    m.close()
    assert n == 0
    _assert_no_plaintext_tmp()  # après écriture + flush du middleware

    # le blob contient bien une base SQLite chiffrée (déchiffrable avec la DEK)
    row = _vault_row("vault-p1")
    raw = base64.b64decode(row["blob"])
    import src.app as _app  # noqa: F401  (accès _AESGCM ci-dessous)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    clear = AESGCM(base64.b64decode(DEK)).decrypt(raw[:12], raw[12:], None)
    assert clear.startswith(b"SQLite format 3")

    # --- 4. open à chaud avec MAUVAISE DEK (coffre déjà ouvert via c1) → refus
    c2 = TestClient(app.app)
    _login(c2, "vault-p1", PWD)
    r = c2.post("/api/vault/open", json={"dek": DEK_WRONG})
    assert r.status_code == 400
    assert c2.get("/api/accounts").status_code == 403  # session non ajoutée → coffre verrouillé

    # --- 5. open à chaud avec la bonne DEK → accès
    assert c2.post("/api/vault/open", json={"dek": DEK}).status_code == 200
    assert c2.get("/api/accounts").json()["accounts"][0]["name"] == "Coffre secret"

    # --- 6. fermeture complète puis open à froid : mauvaise DEK refusée, bonne acceptée
    _logout(c1)
    _logout(c2)
    assert "vault-p1" not in app._VAULTS
    c3 = TestClient(app.app)
    _login(c3, "vault-p1", PWD)
    assert c3.post("/api/vault/open", json={"dek": DEK_WRONG}).status_code == 400
    assert c3.post("/api/vault/open", json={"dek": DEK}).status_code == 200
    assert c3.get("/api/accounts").json()["accounts"][0]["name"] == "Coffre secret"
    _assert_no_plaintext_tmp()  # open à froid : deserialize en mémoire
    _logout(c3)

    # --- 7. coffre hérité sans canary : rétro-armé au 1er open à froid
    m = app.db_main()
    m.execute("UPDATE vaults SET canary='' WHERE username='vault-p1'")
    m.commit()
    m.close()
    c4 = TestClient(app.app)
    _login(c4, "vault-p1", PWD)
    assert c4.post("/api/vault/open", json={"dek": DEK}).status_code == 200
    assert _vault_row("vault-p1")["canary"]
    assert c4.get("/api/accounts").json()["accounts"][0]["name"] == "Coffre secret"
    _logout(c4)


def test_vault_auto_lock_after_idle():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    _make_member(admin_c, "vault-p2", mode="protected")
    _logout(admin_c)

    c = TestClient(app.app)
    _login(c, "vault-p2", PWD)
    assert c.post("/api/auth/password", json={"current": PWD, "new": PWD}).status_code == 200
    assert c.post("/api/vault/init", json={"salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==", "dek": DEK}).status_code == 200

    # white-box : toutes les sessions inactives > VAULT_IDLE_MIN → coffre fermé
    app.VAULT_IDLE_MIN = 30
    try:
        v = app._VAULTS["vault-p2"]
        tok = next(iter(v["sessions"]))
        v["sessions"][tok] = time.monotonic() - 3600
        app._vault_gc("vault-p2", v)
        assert "vault-p2" not in app._VAULTS
        assert c.get("/api/accounts").status_code == 403  # front → demande de déverrouillage
        # un nouvel open fonctionne après le verrouillage
        assert c.post("/api/vault/open", json={"dek": DEK}).status_code == 200
        assert c.get("/api/accounts").status_code == 200
    finally:
        app.VAULT_IDLE_MIN = 0
        _logout(c)
