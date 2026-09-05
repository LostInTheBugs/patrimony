"""Tests de sécurité HTTP (revue externe 2026-09-05) :
authentification sur chaque endpoint, permissions admin, routes coffre,
validation/rollback de l'import, attributs du cookie de session.

Même environnement que test_app.py (posé avant l'import de src.app).
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-sec-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app

PWD = "member-pass-2026"

# Corps VALIDES : le but est de prouver que l'authentification précède le
# traitement (une requête sans session est rejetée 401 avant toute action).
# logout est volontairement absent : sans cookie il répond 200 par conception.
POST_BODIES = {
    "/api/refresh-prices": None,
    "/api/import": {},
    "/api/vault/open": {"dek": "eHh4"},
    "/api/vault/init": {"salt": "x", "wrapped": "x", "dek": "x"},
    "/api/family": {"username": "unauth-probe", "password": "probe-password-123", "mode": "standard"},
}
DATA_ENDPOINTS = [
    ("get", "/api/accounts"),
    ("get", "/api/summary"),
    ("get", "/api/history"),
    ("get", "/api/benchmarks"),
    ("get", "/api/transactions"),
    ("get", "/api/income-calendar"),
    ("get", "/api/family"),
    ("get", "/api/export"),
    ("post", "/api/refresh-prices"),
    ("post", "/api/import"),
    ("post", "/api/vault/open"),
    ("post", "/api/vault/init"),
    ("post", "/api/family"),
]


@pytest.fixture(autouse=True)
def _clean():
    app._LOGIN_FAILS.clear()
    yield


def _login(c: TestClient, u: str, p: str) -> None:
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text


def test_unauthenticated_requests_rejected():
    c = TestClient(app.app)
    for method, path in DATA_ENDPOINTS:
        kwargs = {}
        if method == "post":
            body = POST_BODIES.get(path, {})
            if body is not None:
                kwargs["json"] = body
        r = getattr(c, method)(path, **kwargs)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_login_cookie_flags():
    c = TestClient(app.app)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin-test-2026"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie


def test_member_cannot_use_admin_endpoints():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    r = admin_c.post("/api/family", json={
        "username": "sec-m1", "password": PWD, "mode": "standard",
    })
    assert r.status_code == 200, r.text

    c = TestClient(app.app)
    _login(c, "sec-m1", PWD)
    assert c.get("/api/family").status_code == 403
    assert c.post("/api/family", json={"username": "x", "password": PWD}).status_code == 403
    assert c.post("/api/family/admin/reset-password", json={"password": PWD}).status_code == 403
    assert c.delete("/api/family/admin").status_code == 403
    # pas d'échappement par l'id : l'admin seul peut lister
    assert c.get("/api/summary?family=1").status_code == 200  # vue famille = soi-même


def test_standard_member_cannot_init_or_open_vault():
    c = TestClient(app.app)
    _login(c, "sec-m1", PWD)
    r = c.post("/api/vault/init", json={"salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==", "dek": "AQ==" * 32})
    assert r.status_code == 400
    r = c.post("/api/vault/open", json={"dek": "AQ==" * 32})
    assert r.status_code == 400


def _full_account(name: str) -> dict:
    return {
        "name": name, "asset_class": "comptes", "institution": "Banque", "currency": "EUR",
        "valuation_mode": "manual", "cost_basis": 100, "open_date": "2026-01-01",
        "close_date": None, "notes": "", "active": 1, "created_at": "2026-01-01T00:00:00",
        "updated_at": None,
    }


def test_import_requires_full_shape_and_rolls_back_on_conflict():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    # shape invalide → 400
    r = admin_c.post("/api/import", json={"app": "patrimony"})
    assert r.status_code == 400
    # import valide qui remplace les données de l'admin
    r = admin_c.post("/api/import", json={
        "app": "patrimony",
        "accounts": [{"id": 9001, **_full_account("Compte admin")}],
        "valuations": [],
    })
    assert r.status_code == 200, r.text
    names = [a["name"] for a in admin_c.get("/api/accounts").json()["accounts"]]
    assert names == ["Compte admin"]

    # conflit d'id avec un actif d'UN AUTRE propriétaire → 400 + rollback complet
    m = TestClient(app.app)
    _login(m, "sec-m1", PWD)
    aid = m.post("/api/accounts", json={"name": "Actif m1", "asset_class": "comptes"}).json()["id"]
    r = admin_c.post("/api/import", json={
        "app": "patrimony",
        "accounts": [{"id": aid, **_full_account("Vol de l'actif de m1")}],
        "valuations": [],
    })
    assert r.status_code == 400  # contrainte PRIMARY KEY → rollback
    # l'admin n'a rien perdu, m1 n'a rien perdu
    names = [a["name"] for a in admin_c.get("/api/accounts").json()["accounts"]]
    assert names == ["Compte admin"]
    names = [a["name"] for a in m.get("/api/accounts").json()["accounts"]]
    assert names == ["Actif m1"]


def test_audit_log_admin_only_and_records_actions():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    # création + login réussi + login raté d'un membre dédié
    uname = "aud-user"
    r = admin_c.post("/api/family", json={"username": uname, "password": PWD, "mode": "standard"})
    assert r.status_code == 200, r.text
    mc = TestClient(app.app)
    _login(mc, uname, PWD)
    mc.post("/api/auth/logout")
    mc2 = TestClient(app.app)
    assert mc2.post("/api/auth/login", json={"username": uname, "password": "wrong-password"}).status_code == 401

    ev = admin_c.get("/api/audit").json()["events"]
    actions = [(e["username"], e["action"]) for e in ev]
    assert (uname, "Connexion") in actions
    assert (uname, "Déconnexion") in actions
    assert (uname, "Échec de connexion") in actions
    assert (admin_c.get("/api/audit").status_code) == 200
    # un membre standard n'a pas accès au journal
    mb = TestClient(app.app)
    _login(mb, "sec-m1", PWD)
    assert mb.get("/api/audit").status_code == 403
    # jamais de montants : aucun détail ne contient « € »
    assert all("€" not in (e["detail"] or "") for e in ev)


DEK = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 octets (client simulé)


def test_audit_hides_protected_member_asset_structure():
    """Un membre protégé ne fuit RIEN dans le journal principal : pas de nom
    d'actif, pas d'événement de données — seuls ses événements d'authentification,
    sans détail."""
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    r = admin_c.post("/api/family", json={
        "username": "prot-aud", "password": PWD, "mode": "protected",
    })
    assert r.status_code == 200, r.text

    # parcours protégé réel : login → mdp → init → open → actif « secret »
    p = TestClient(app.app)
    _login(p, "prot-aud", PWD)
    assert p.post("/api/auth/password", json={"current": PWD, "new": PWD}).status_code == 200
    assert p.post("/api/vault/init", json={
        "salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==", "dek": DEK,
    }).status_code == 200
    assert p.post("/api/vault/open", json={"dek": DEK}).status_code == 200
    r = p.post("/api/accounts", json={
        "name": "Bijou rue des Fleurs", "asset_class": "immobilier", "cost_basis": 9000,
        "initial_value": 9000,
    })
    assert r.status_code == 200, r.text  # écrit dans le coffre
    assert p.get("/api/accounts").json()["accounts"][0]["name"] == "Bijou rue des Fleurs"

    ev = admin_c.get("/api/audit").json()["events"]
    mine = [e for e in ev if e["username"] == "prot-aud"]
    by_action = {e["action"]: e for e in mine}
    # seuls les événements d'authentification du protégé existent
    assert set(by_action) <= {"Connexion", "Ouverture du coffre", "Initialisation du coffre"}
    # AUCUN détail, et aucun nom d'actif nulle part
    assert all((e["detail"] or "") == "" for e in mine)
    assert all("Bijou" not in (e["detail"] or "") and "Fleurs" not in (e["detail"] or "") for e in ev)
    # un compte standard, lui, reste journalisé avec ses noms d'actifs (pas de régression)
    m2 = TestClient(app.app)
    _login(m2, "aud-user", PWD)
    m2.post("/api/accounts", json={"name": "Livret démo visible", "asset_class": "epargne"})
    ev2 = admin_c.get("/api/audit").json()["events"]
    assert any(e["username"] == "aud-user" and "Livret démo visible" in (e["detail"] or "") for e in ev2)


def test_csv_exports_isolated_and_utf8():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    r = admin_c.get("/api/export/csv/accounts")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text
    assert body.startswith("\ufeff")
    assert "Compte admin" in body
    # isolation : sec-m1 (aucun actif) n'exporte que l'en-tête
    m = TestClient(app.app)
    _login(m, "sec-m1", PWD)
    b2 = m.get("/api/export/csv/accounts").text
    assert "Compte admin" not in b2
    assert "name" in b2  # en-tête présent quand même
    # type inconnu → 404 ; non authentifié → 401
    assert m.get("/api/export/csv/nimporte-quoi").status_code == 404
    assert TestClient(app.app).get("/api/export/csv/accounts").status_code == 401
