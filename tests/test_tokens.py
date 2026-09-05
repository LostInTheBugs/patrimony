"""Tests des jetons API (v2026.09.018) : cycle de vie, auth Bearer, hash au
repos, interdiction aux comptes protégés, révocation."""
import hashlib
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-tok-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app

PWD = "member-pass-2026"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module")
def admin_c():
    c = TestClient(app.app)
    _login(c)
    yield c


def test_token_lifecycle_hash_at_rest_and_bearer_auth(admin_c):
    r = admin_c.post("/api/tokens", json={"name": "ext-test", "expires_days": 90})
    assert r.status_code == 200, r.text
    j = r.json()
    raw = j["token"]
    assert len(raw) >= 40
    assert j["expires_at"] and "T" in j["expires_at"]

    # stocké HACHÉ : le jeton brut n'apparaît jamais en base
    m = app.db_main()
    rows = m.execute("SELECT token_hash, name FROM api_tokens WHERE username='admin'").fetchall()
    m.close()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in rows[0]["token_hash"]

    # liste sans le jeton brut
    lst = admin_c.get("/api/tokens").json()["tokens"]
    assert len(lst) == 1 and "token" not in lst[0]

    # le jeton fonctionne en Bearer sur les données (isolation admin)
    b = TestClient(app.app)
    assert b.get("/api/accounts").status_code == 401  # sans jeton
    hdrs = {"Authorization": "Bearer " + raw}
    assert b.get("/api/accounts", headers=hdrs).status_code == 200  # accès données OK
    aid = b.post("/api/accounts", headers=hdrs, json={"name": "Via jeton", "asset_class": "comptes"}).json()["id"]
    assert aid > 0
    b.post(f"/api/accounts/{aid}/valuation", headers=hdrs, json={"value": 1000})
    # last_used_at a été mis à jour
    lst = admin_c.get("/api/tokens").json()["tokens"]
    assert lst[0]["last_used_at"]

    # révocation → 401
    assert admin_c.delete("/api/tokens/" + str(j["id"])).status_code == 200
    assert b.get("/api/accounts", headers=hdrs).status_code == 401
    # double suppression → 404
    assert admin_c.delete("/api/tokens/" + str(j["id"])).status_code == 404


def test_token_expiration(admin_c):
    r = admin_c.post("/api/tokens", json={"name": "court", "expires_days": 1})
    raw = r.json()["token"]
    # on simule l'expiration directement en base
    m = app.db_main()
    m.execute("UPDATE api_tokens SET expires_at = ? WHERE username='admin'",
              (__import__("datetime").datetime(2020, 1, 1).isoformat(),))
    m.commit()
    m.close()
    b = TestClient(app.app)
    assert b.get("/api/accounts", headers={"Authorization": "Bearer " + raw}).status_code == 401
    # expiration invalide rejetée
    assert admin_c.post("/api/tokens", json={"name": "x", "expires_days": 99999}).status_code == 400
    assert admin_c.post("/api/tokens", json={"name": "x", "expires_days": 0}).status_code == 400


def test_token_forbidden_for_protected_and_isolated(admin_c):
    admin_c.post("/api/family", json={"username": "tok-prot", "password": PWD, "mode": "protected"})
    p = TestClient(app.app)
    _login(p, "tok-prot", PWD)
    assert p.post("/api/tokens", json={"name": "x"}).status_code == 403  # jamais de jeton protégé
    # un membre standard ne voit que SES jetons
    admin_c.post("/api/family", json={"username": "tok-m1", "password": PWD, "mode": "standard"})
    m1 = TestClient(app.app)
    _login(m1, "tok-m1", PWD)
    r = m1.post("/api/tokens", json={"name": "perso"}).json()
    lst = admin_c.get("/api/tokens").json()["tokens"]
    assert all(t["name"] != "perso" for t in lst)  # l'admin ne voit pas le jeton de m1
    assert len(m1.get("/api/tokens").json()["tokens"]) == 1
    # le jeton de m1 est limité à SES données
    b = TestClient(app.app)
    m1.post("/api/accounts", json={"name": "Compte m1", "asset_class": "comptes"})
    acc = b.get("/api/accounts", headers={"Authorization": "Bearer " + r["token"]}).json()["accounts"]
    assert len(acc) == 1 and acc[0]["name"] == "Compte m1"
