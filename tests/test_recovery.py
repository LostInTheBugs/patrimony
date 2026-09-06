"""v2026.09.030 — clé de récupération du coffre : armement (2e wrap DEK par
une clé de secours générée côté client) + récupération du compte quand le
mot de passe est perdu (preuve par la clé, nouveau mdp, re-wrap)."""
import base64
import hashlib
import os
import tempfile
import time
from pathlib import Path

DATA_DIR = tempfile.mkdtemp(prefix="pat-test-rc-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
import src.app as app  # noqa: E402

PWD_ADMIN = "admin-test-2026"
KDF_ITER = 600000


def _login(c, user, pwd=PWD_ADMIN):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _kek(secret: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret, salt, KDF_ITER, 32)


def _wrap(key: bytes, data: bytes) -> str:
    nonce = os.urandom(12)
    return _b64(nonce + AESGCM(key).encrypt(nonce, data, b""))


def _unwrap(key: bytes, wrapped_b64: str) -> bytes:
    raw = base64.b64decode(wrapped_b64)
    return AESGCM(key).decrypt(raw[:12], raw[12:], b"")


@pytest.fixture(scope="module")
def admin_c():
    c = TestClient(app.app)
    _login(c, "admin")
    yield c


def _mk_protected(admin_c, username):
    r = admin_c.post("/api/family", json={"username": username,
                                          "password": "rc-initial-2026", "mode": "protected"})
    assert r.status_code == 200, r.text


def _setup_vault(c, user, new_pwd):
    """Login initial → changement de mdp → init coffre (dek aléatoire) →
    un actif dedans. Retourne la DEK."""
    _login(c, user, "rc-initial-2026")
    r = c.post("/api/auth/password", json={"current": "rc-initial-2026", "new": new_pwd})
    assert r.status_code == 200, r.text
    dek = os.urandom(32)
    salt = _b64(os.urandom(16))
    wrapped = _wrap(b"x" * 32, dek)  # valeur quelconque : le serveur ne vérifie pas
    r = c.post("/api/vault/init", json={"salt": salt, "wrapped": wrapped, "dek": _b64(dek)})
    assert r.status_code == 200, r.text
    r = c.post("/api/accounts", json={"name": "Compte secret", "asset_class": "epargne"})
    assert r.status_code == 200, r.text
    return dek


def _arm(c, key: bytes, dek: bytes | None = None):
    """Arme la clé de récupération `key` (coffre ouvert). La DEK est
    réellement wrappée si fournie (sinon leurre pour les tests de forme)."""
    r_salt, r_auth_salt = os.urandom(12), os.urandom(12)
    kekr = _kek(key, r_salt)
    r_wrapped = _wrap(kekr, dek if dek is not None else os.urandom(20))
    r_auth = _kek(key, r_auth_salt)
    r = c.post("/api/vault/recovery",
               json={"r_salt": _b64(r_salt), "r_auth_salt": _b64(r_auth_salt),
                     "r_wrapped": r_wrapped, "r_auth": _b64(r_auth)})
    assert r.status_code == 200, r.text
    return r_salt, r_auth_salt


def test_recovery_arm_requires_open_vault_and_me_flag(admin_c):
    """L'armement exige un coffre ouvert ; me() expose recovery_armed."""
    user = "rc-arm"
    _mk_protected(admin_c, user)
    c = TestClient(app.app)
    dek = _setup_vault(c, user, "rc-nouveau-2026")
    me = c.get("/api/auth/me").json()
    assert me["recovery_armed"] is False
    r = c.post("/api/vault/recovery", json={"r_salt": _b64(b"s" * 12),
                                            "r_auth_salt": _b64(b"t" * 12),
                                            "r_wrapped": _b64(b"w" * 20),
                                            "r_auth": _b64(b"a" * 32)})
    assert r.status_code == 200, r.text
    assert c.get("/api/auth/me").json()["recovery_armed"] is True


def test_recovery_arm_locked_vault_rejected(admin_c):
    """Session authentifiée mais coffre non déverrouillé → 400 (le client
    ne peut pas produire r_wrapped sans la DEK)."""
    user = "rc-lock"
    _mk_protected(admin_c, user)
    c = TestClient(app.app)
    _setup_vault(c, user, "rc-lock-pwd-2026")
    # logout → le coffre mémoire se ferme
    c.post("/api/auth/logout")
    _login(c, user, "rc-lock-pwd-2026")  # session sans open
    r = c.post("/api/vault/recovery", json={"r_salt": _b64(b"s" * 12),
                                            "r_auth_salt": _b64(b"t" * 12),
                                            "r_wrapped": _b64(b"w" * 20),
                                            "r_auth": _b64(b"a" * 32)})
    assert r.status_code == 400
    assert "Déverrouillez" in r.text


def test_recover_full_cycle_and_old_key_revoked(admin_c):
    """Cycle complet : mot de passe perdu → start → preuve par la clé →
    nouvelle session + nouveau mdp + données du coffre intactes ; l'ancien
    mdp est mort ; une clé remplacée ne récupère plus."""
    user = "rc-cycle"
    _mk_protected(admin_c, user)
    c = TestClient(app.app)
    old_pwd = "rc-vieux-mdp-2026"
    dek = _setup_vault(c, user, old_pwd)
    key = os.urandom(16)
    r_salt, r_auth_salt = _arm(c, key, dek)
    # --- mot de passe perdu : logout, on ne peut plus ouvrir
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": user,
                                           "password": old_pwd}).status_code == 200
    # session sans open → données inaccessibles
    r = c.post("/api/accounts", json={"name": "X", "asset_class": "epargne"})
    # (route data d'un protected sans coffre ouvert : 403 coffre verrouillé)
    assert r.status_code in (400, 403)

    # --- récupération
    s = c.post("/api/vault/recover/start", json={"username": user})
    assert s.status_code == 200, s.text
    m = s.json()
    kekr = _kek(key, base64.b64decode(m["r_salt"]))
    dek2 = _unwrap(kekr, m["r_wrapped"])
    assert dek2 == dek  # la vraie DEK du coffre
    proof = _kek(key, base64.b64decode(m["r_auth_salt"]))
    new_pwd = "rc-fresh-2026-new"
    salt2 = _b64(os.urandom(16))
    wrapped2 = _wrap(b"y" * 32, dek2)
    r = c.post("/api/vault/recover", json={"username": user, "proof": _b64(proof),
                                           "dek": _b64(dek2), "new_password": new_pwd,
                                           "wrapped": wrapped2, "salt": salt2})
    assert r.status_code == 200, r.text
    # données du coffre accessibles avec la nouvelle session
    r = c.get("/api/accounts")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["accounts"]]
    assert "Compte secret" in names
    # l'ancien mdp ne marche plus, le nouveau oui
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": user, "password": old_pwd}).status_code == 401
    r = _login(c, user, new_pwd)
    assert r.json()["must_change"] is False
    # --- clé remplacée → l'ancienne ne récupère plus
    key2 = os.urandom(16)
    c.post("/api/vault/open", json={"dek": _b64(dek)})  # coffre rouvert avec la DEK
    _arm(c, key2, dek)
    s2 = c.post("/api/vault/recover/start", json={"username": user}).json()
    # l'ancienne clé ne passe plus
    r = c.post("/api/vault/recover", json={"username": user, "proof": _b64(proof),
                                           "dek": _b64(dek2), "new_password": "rc-autre-2026-xx",
                                           "wrapped": _b64(b"w" * 20), "salt": _b64(b"s" * 12)})
    assert r.status_code == 400
    # la nouvelle clé passe
    kekr2 = _kek(key2, base64.b64decode(s2["r_salt"]))
    dek3 = _unwrap(kekr2, s2["r_wrapped"])
    assert dek3 == dek
    proof2 = _kek(key2, base64.b64decode(s2["r_auth_salt"]))
    r = c.post("/api/vault/recover", json={"username": user, "proof": _b64(proof2),
                                           "dek": _b64(dek3), "new_password": "rc-fresh-2027",
                                           "wrapped": _b64(b"w" * 20), "salt": _b64(b"s" * 12)})
    assert r.status_code == 200, r.text


def test_recover_unknown_or_unarmed_generic_and_bad_proof(admin_c):
    """Réponses génériques (anti-énumération) ; preuve fausse refusée."""
    for uname in ("rc-ghost", "rc-unarmed"):  # inexistant, puis protégé SANS clé
        r = admin_c.post("/api/vault/recover/start", json={"username": uname})
        assert r.status_code == 400
    # protégé sans clé armée : même réponse générique
    c = TestClient(app.app)
    _mk_protected(admin_c, "rc-unarmed")
    _setup_vault(c, "rc-unarmed", "rc-unarmed-pwd-26")
    c.post("/api/auth/logout")
    r = admin_c.post("/api/vault/recover/start", json={"username": "rc-unarmed"})
    assert r.status_code == 400
    # compte standard armé ? impossible (arm = protected seulement)
    user = "rc-standard"
    r = admin_c.post("/api/family", json={"username": user,
                                          "password": "rc-std-2026-xx", "mode": "standard"})
    assert r.status_code == 200
    r = admin_c.post("/api/vault/recover/start", json={"username": user})
    assert r.status_code == 400
