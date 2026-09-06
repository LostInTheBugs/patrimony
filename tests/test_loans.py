"""Tests crédit lié à l'actif immobilier (v2026.09.033).

Option ③-a validée par Fred : le passif vit DANS l'actif (aucune classe
« dettes »). Colonnes accounts.loan_principal/loan_rate/loan_monthly,
réservées à la classe immobilier ; /api/summary expose total_debt et
net_worth (= total_value − total_debt, valeur ajoutée sans rien casser :
0 crédit → net_worth == total_value).

Isolation : chaque test utilise un membre dédié (le mode est figé à la
création ; les summaries sont scopés par owner).
"""

import os
import sqlite3
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient

import src.app as app

PWD = "loan-pass-2026-long"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(c, tag, mode="standard"):
    """L'admin crée un membre dédié (isolation des tests) puis s'y connecte."""
    r = c.post("/api/family", json={"username": tag, "password": PWD,
                                    "display_name": tag, "mode": mode})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, tag, PWD)
    return tag


def _mk_asset(c, name, cls, wrapper=None, cost=100000, value=172000,
              odate="2021-03-01", **kw):
    body = {"name": name, "asset_class": cls, "currency": "EUR",
            "cost_basis": cost, "open_date": odate, "wrapper": wrapper,
            "initial_value": value}
    body.update(kw)
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _loan_put(c, aid, principal, rate=0.0, monthly=0.0):
    r = c.put(f"/api/accounts/{aid}", json={"name": "Bien", "asset_class": "immobilier",
                                            "currency": "EUR", "cost_basis": 80000,
                                            "open_date": "2021-03-01",
                                            "loan_principal": principal,
                                            "loan_rate": rate, "loan_monthly": monthly})
    assert r.status_code == 200, r.text


def test_loan_reserve_immobilier_et_validation():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "loan-v1")
    # un crédit sur une classe non immo → 400
    r = c.post("/api/accounts", json={"name": "CTO", "asset_class": "bourse",
                                      "currency": "EUR", "cost_basis": 1000,
                                      "open_date": "2020-01-01", "initial_value": 1200,
                                      "loan_principal": 50000, "loan_rate": 2.5,
                                      "loan_monthly": 300})
    assert r.status_code == 400
    assert "immobilier" in r.json()["detail"]
    # montants négatifs → 400 ; taux absurde → 400
    aid = _mk_asset(c, "Immo", "immobilier", value=200000)
    r = c.put(f"/api/accounts/{aid}", json={"name": "Immo", "asset_class": "immobilier",
                                            "currency": "EUR", "cost_basis": 80000,
                                            "open_date": "2021-03-01",
                                            "loan_principal": -1})
    assert r.status_code == 400
    r = c.put(f"/api/accounts/{aid}", json={"name": "Immo", "asset_class": "immobilier",
                                            "currency": "EUR", "cost_basis": 80000,
                                            "open_date": "2021-03-01",
                                            "loan_principal": 90000, "loan_rate": 250})
    assert r.status_code == 400


def test_loan_crud_et_summary_net():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "loan-v2")
    aid = _mk_asset(c, "Appartement", "immobilier", cost=80000, value=200000)
    _loan_put(c, aid, 90000, 2.8, 520)
    accs = c.get("/api/accounts").json()["accounts"]
    a = next(x for x in accs if x["id"] == aid)
    assert (a["loan_principal"], a["loan_rate"], a["loan_monthly"]) == (90000.0, 2.8, 520.0)
    # summary : net = valeur − crédit, passif exposé, total_value brut
    s = c.get("/api/summary").json()
    assert s["total_value"] == 200000.0
    assert s["total_debt"] == 90000.0
    assert s["net_worth"] == 110000.0
    # effacer le crédit (principal 0) → champs à 0, net = brut
    _loan_put(c, aid, 0)
    s = c.get("/api/summary").json()
    assert s["total_debt"] == 0.0 and s["net_worth"] == s["total_value"] == 200000.0


def test_loan_multi_devises_converti_comme_la_valo():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "loan-v3")
    aid = _mk_asset(c, "Bien CH", "immobilier", currency="CHF", cost=100000,
                    value=200000, fx_override=1.05)
    r = c.put(f"/api/accounts/{aid}", json={"name": "Bien CH",
                                            "asset_class": "immobilier",
                                            "currency": "CHF", "cost_basis": 100000,
                                            "open_date": "2021-03-01",
                                            "fx_override": 1.05,
                                            "loan_principal": 90000})
    assert r.status_code == 200, r.text
    s = c.get("/api/summary").json()
    # 90 000 CHF / 1.05 = 85 714.29 € ; valeur 200 000 CHF / 1.05 = 190 476.19 €
    assert abs(s["total_debt"] - 85714.29) < 0.01
    assert abs(s["net_worth"] - (190476.19 - 85714.29)) < 0.01


def test_loan_roundtrip_export_import():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "loan-v4")
    aid = _mk_asset(c, "Immo export", "immobilier", value=200000)
    _loan_put(c, aid, 60000, 1.9, 350)
    exp = c.get("/api/export").json()
    acc = next(x for x in exp["accounts"] if x["id"] == aid)
    assert acc["loan_principal"] == 60000.0
    # purge + ré-import (restauration complète de SES données)
    r = c.post("/api/import", json=exp)
    assert r.status_code == 200, r.text
    accs = c.get("/api/accounts").json()["accounts"]
    a = next(x for x in accs if x["id"] == aid)
    assert (a["loan_principal"], a["loan_rate"], a["loan_monthly"]) == (60000.0, 1.9, 350.0)


def test_loan_vault_protected_rien_en_clair():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "loan-v5", mode="protected")
    # membre protégé : mdp personnel exigé puis coffre init
    r = c.post("/api/auth/password", json={"current": PWD, "new": PWD})
    assert r.status_code == 200, r.text
    import base64 as _b64
    r = c.post("/api/vault/init", json={"salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==",
                                        "dek": _b64.b64encode(b"k" * 32).decode()})
    assert r.status_code == 200, r.text
    # ses actifs naissent dans le coffre : crédit stocké et restitué
    aid = _mk_asset(c, "Immo coffre", "immobilier", value=200000)
    _loan_put(c, aid, 45000, 2.2, 260)
    accs = c.get("/api/accounts").json()["accounts"]
    a = next(x for x in accs if x["id"] == aid)
    assert (a["loan_principal"], a["loan_rate"], a["loan_monthly"]) == (45000.0, 2.2, 260.0)
    # white-box : la base principale (claire) ne contient ni le compte ni la dette
    main = sqlite3.connect(app.DB_PATH)
    rows = main.execute("SELECT COUNT(*) FROM accounts WHERE owner='loan-v5'").fetchone()[0]
    main.close()
    assert rows == 0
