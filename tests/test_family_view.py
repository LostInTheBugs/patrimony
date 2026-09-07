"""Vue détaillée par membre (v2026.09.043) : ?member= réservé à l'admin sur
les GET de consultation — cible standard uniquement (un compte protected
répond 404 indistinguable d'un compte inexistant) ; sans member, le
comportement historique est inchangé."""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="patmv_")
os.environ["DATA_DIR"] = _TMP
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-password-123"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import app  # noqa: E402


@pytest.fixture()
def admin_c():
    c = TestClient(app.app)
    _login(c, "admin", os.environ["ADMIN_PASSWORD"])
    return c

PWD = "member-password-123"


def _login(c, user, pwd):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(admin_c, name, mode="standard"):
    r = admin_c.post("/api/family", json={"username": name, "password": PWD, "mode": mode})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, name, PWD)
    return c


def _seed_account(c, label, cls="comptes", value=1000.0):
    r = c.post("/api/accounts", json={"name": label, "asset_class": cls,
                                      "valuation_mode": "manual", "cost_basis": 500.0})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    r = c.post(f"/api/accounts/{aid}/valuation", json={"value": value})
    assert r.status_code == 200, r.text
    return aid


def test_admin_consulte_un_membre_standard(admin_c):
    m = _mk_member(admin_c, "vic")
    vic_aid = _seed_account(m, "Compte Vic", "comptes", 1234.5)
    _seed_account(admin_c, "Compte Admin", "comptes", 999.0)

    r = admin_c.get("/api/accounts", params={"member": "vic"})
    assert r.status_code == 200, r.text
    names = [a["name"] for a in r.json()["accounts"]]
    assert names == ["Compte Vic"], names  # jamais les données de l'admin

    r = admin_c.get("/api/summary", params={"member": "vic"})
    assert r.status_code == 200
    j = r.json()
    assert abs(j["total_value"] - 1234.5) < 0.01, j["total_value"]
    # le dash du membre : compte comptes → total_value = 1234,5

    # transactions/income-rules/calendar/actual/cashflow : member supporté
    r = admin_c.get("/api/transactions", params={"member": "vic"})
    assert r.status_code == 200 and "transactions" in r.json()
    r = admin_c.get("/api/income-rules", params={"member": "vic"})
    assert r.status_code == 200
    r = admin_c.get("/api/income-calendar", params={"member": "vic", "months": 6})
    assert r.status_code == 200
    r = admin_c.get("/api/income-actual", params={"member": "vic", "months": 6})
    assert r.status_code == 200
    r = admin_c.get("/api/cashflow", params={"member": "vic", "months": 6})
    assert r.status_code == 200
    r = admin_c.get("/api/history", params={"member": "vic", "months": 6})
    assert r.status_code == 200
    r = admin_c.get("/api/evolution", params={"member": "vic", "months": 6})
    assert r.status_code == 200
    r = admin_c.get("/api/benchmarks", params={"member": "vic"})
    assert r.status_code == 200


def test_admin_never_voit_un_protected(admin_c):
    _mk_member(admin_c, "theo", mode="protected")
    for r in (
        admin_c.get("/api/accounts", params={"member": "theo"}),
        admin_c.get("/api/summary", params={"member": "theo"}),
        admin_c.get("/api/history", params={"member": "theo"}),
    ):
        assert r.status_code == 404, r.status_code
        assert r.json()["detail"] == "Membre introuvable"  # indistinguable
    # membre inexistant : même réponse
    r = admin_c.get("/api/accounts", params={"member": "nobody"})
    assert r.status_code == 404 and r.json()["detail"] == "Membre introuvable"


def test_member_sans_droit_403(admin_c):
    m = _mk_member(admin_c, "vic2")
    _mk_member(admin_c, "vic3")
    r = m.get("/api/accounts", params={"member": "vic3"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Administrateur requis"


def test_sans_member_comportement_inchange(admin_c):
    _seed_account(admin_c, "Mon Compte", "comptes", 42.0)
    r = admin_c.get("/api/accounts")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["accounts"]]
    assert "Mon Compte" in names  # état admin partagé par la suite : filtre
    # l'admin ne peut pas se consulter lui-même via member (404 — cible membre uniquement)
    r = admin_c.get("/api/summary", params={"member": "admin"})
    assert r.status_code == 404


def test_ecriture_ignore_member(admin_c):
    """Les écritures ne sont JAMAIS scopées : ?member= est ignoré (le POST
    crée chez l'appelant). La garde structurelle = aucun POST/PUT/DELETE ne
    lit ce paramètre."""
    m = _mk_member(admin_c, "vic4")
    _seed_account(m, "Compte Vic4", "comptes", 10.0)
    r = admin_c.post("/api/accounts?member=vic4",
                     json={"name": "Ajout Admin", "asset_class": "comptes",
                           "valuation_mode": "manual", "cost_basis": 0.0})
    assert r.status_code == 200, r.text
    # rien n'est apparu chez Vic4
    rv = admin_c.get("/api/accounts", params={"member": "vic4"})
    assert [a["name"] for a in rv.json()["accounts"]] == ["Compte Vic4"]
    # mais chez l'admin (état partagé : filtre)
    ra = admin_c.get("/api/accounts")
    assert "Ajout Admin" in [a["name"] for a in ra.json()["accounts"]]
