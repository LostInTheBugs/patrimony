"""Tests du module Locations & TCO (v2026.09.058) : table loc_*/tco_* +
routes /api/loc/* et /api/tco/*.

Design claude/design-locations-tco-2026.md : contrats par bien loué
(attendu/perçu/occupation/rendements), encaissements MATÉRIALISÉS en op
income (source_id loc:enc:<id>), fiches TCO (immo lié au compte — crédit
auto-détecté ; véhicules hors patrimoine — acquisition cash + crédit versé),
imputations = lien d'UNE op expense vers UNE fiche.

Isolation : membres dédiés par test (préfixe `es` — base partagée par toute
la suite, cf. conventions).
"""

import base64
import os
import sqlite3
import tempfile
from datetime import date


def _b64(x: bytes) -> str:
    return base64.b64encode(x).decode()

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["PAT_CRYPTO_AUTO"] = "0"

from fastapi.testclient import TestClient

import src.app as app
import src.estate as estate
import src.transfer as transfer

PWD = "estate-pass-2026-long"


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y += (m - 1 + k) // 12
    m = (m - 1 + k) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(c, tag, mode="standard"):
    c.post("/api/auth/logout")
    _login(c)  # la gestion famille est réservée à l'admin
    r = c.post("/api/family", json={"username": tag, "password": PWD,
                                    "display_name": tag, "mode": mode})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, tag, PWD)
    return tag


def _mk_asset(c, name="Bien loué", cls="immobilier", value=172000,
              odate="2021-03-01", cost=145000, **kw):
    body = {"name": name, "asset_class": cls, "currency": "EUR",
            "cost_basis": cost, "open_date": odate, "initial_value": value,
            "valuation_mode": "manual"}
    body.update(kw)
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_cash(c, name="Courant"):
    return _mk_asset(c, name=name, cls="comptes", value=4200, odate="2023-09-01",
                     cost=4200)


def _mk_contract(c, account_id, tenant="M. Dupont", rent=1250, start="2024-01-01",
                 **kw):
    body = {"account_id": account_id, "tenant": tenant, "rent_monthly": rent,
            "deposit": 2500, "start_date": start, "active": 1, "notes": ""}
    body.update(kw)
    r = c.post("/api/loc/contracts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_loan_auto(c, name="Prêt auto", remaining=20000, monthly=1000,
                  start=None, rate=0):
    body = {"name": name, "loan_type": "auto", "lender": "Banque",
            "currency": "EUR", "principal_initial": remaining,
            "principal_remaining": remaining, "rate_annual": rate,
            "monthly_payment": monthly,
            "start_date": start or _ym_add(_ym(date.today()), -13) + "-01",
            "account_id": None}
    r = c.post("/api/loans", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_expense(c, cash_id, d, amount, note="Dépense"):
    r = c.post("/api/transactions", json={"account_id": cash_id, "op_date": d,
                                          "kind": "expense", "amount": amount,
                                          "note": note})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _raw_conn():
    conn = sqlite3.connect(app.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------- contrats

def test_contract_crud_clash_and_delete():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_c1")
    aid = _mk_asset(c)
    # création
    cid = _mk_contract(c, aid)
    # second contrat actif sur le même bien → refusé
    r = c.post("/api/loc/contracts", json={"account_id": aid, "tenant": "M. X",
                                           "rent_monthly": 1000,
                                           "start_date": "2025-01-01"})
    assert r.status_code == 400
    # clôture du premier → le second devient possible
    r = c.put(f"/api/loc/contracts/{cid}",
              json={"account_id": aid, "tenant": "M. Dupont", "rent_monthly": 1250,
                    "deposit": 2500, "start_date": "2024-01-01",
                    "end_date": "2025-12-31", "active": 0, "notes": ""})
    assert r.status_code == 200, r.text
    r = c.post("/api/loc/contracts", json={"account_id": aid, "tenant": "M. X",
                                           "rent_monthly": 1000,
                                           "start_date": "2026-01-01"})
    assert r.status_code == 200, r.text
    # DELETE : sans encaissement → suppression dure
    r = c.delete(f"/api/loc/contracts/{cid}")
    assert r.status_code == 200
    r = c.get(f"/api/loc/contracts")
    assert len(r.json()["contracts"]) == 1  # seul le 2e contrat reste


def test_contract_guards():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_c2")
    aid = _mk_asset(c)
    cash = _mk_cash(c)
    base = {"account_id": aid, "tenant": "M. A", "rent_monthly": 800,
            "start_date": "2024-01-01"}
    for patch_body, in ((dict(base, tenant="  "),), (dict(base, rent_monthly=0),),
                        (dict(base, start_date="2024-13-01"),),
                        (dict(base, start_date="2024-01-01", end_date="2023-01-01"),)):
        r = c.post("/api/loc/contracts", json=patch_body)
        assert r.status_code == 400, patch_body
    # compte non immobilier → refusé
    r = c.post("/api/loc/contracts", json=dict(base, account_id=cash))
    assert r.status_code == 400
    # bien d'un autre membre → introuvable
    c.post("/api/auth/logout")
    _mk_member(c, "es_c2b")
    r = c.post("/api/loc/contracts", json=base)
    assert r.status_code == 400


# ---------------------------------------------------------------- encaissements

def test_payment_materializes_income_and_delete_removes_it():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_p1")
    aid = _mk_asset(c)
    cash = _mk_cash(c)
    cid = _mk_contract(c, aid, start="2026-01-01")
    r = c.post("/api/loc/payments", json={"contract_id": cid, "op_date": "2026-01-03",
                                          "amount": 1250, "month": "2026-01",
                                          "cash_account_id": cash})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    conn = _raw_conn()
    p = conn.execute("SELECT * FROM loc_payments WHERE id=?", (pid,)).fetchone()
    assert p["transaction_id"] is not None
    tx = conn.execute("SELECT * FROM transactions WHERE id=?",
                      (p["transaction_id"],)).fetchone()
    assert tx["kind"] == "income" and tx["amount"] == 1250
    assert tx["account_id"] == cash
    assert tx["source_id"] == f"loc:enc:{pid}"
    assert "Loyer 2026-01" in tx["note"]
    conn.close()
    # suppression de l'encaissement → l'op income liée disparaît aussi
    r = c.delete(f"/api/loc/payments/{pid}")
    assert r.status_code == 200
    conn = _raw_conn()
    gone = conn.execute("SELECT COUNT(*) n FROM transactions WHERE id=?",
                        (p["transaction_id"],)).fetchone()["n"]
    assert gone == 0
    conn.close()


def test_payment_guards():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_p2")
    aid = _mk_asset(c)
    cash = _mk_cash(c)
    cid = _mk_contract(c, aid)
    base = {"contract_id": cid, "op_date": "2026-03-03", "amount": 1250,
            "month": "2026-03", "cash_account_id": cash}
    for bad in (dict(base, amount=0), dict(base, month="2026-3"),
                dict(base, op_date="hier"), dict(base, cash_account_id=999999),
                dict(base, contract_id=999999)):
        r = c.post("/api/loc/payments", json=bad)
        assert r.status_code == 400, bad
    # contrat d'un autre membre → 404
    c.post("/api/auth/logout")
    _mk_member(c, "es_p2b")
    r = c.post("/api/loc/payments", json=base)
    assert r.status_code == 400
    r = c.get(f"/api/loc/payments?contract_id={cid}")
    assert r.status_code == 404


# ---------------------------------------------------------------- overview Locations

def test_loc_overview_exact():
    c = TestClient(app.app)
    _login(c)
    es = _mk_member(c, "es_o1")
    aid = _mk_asset(c, value=150000, odate="2023-06-01")
    cid = _mk_contract(c, aid, rent=1000, start="2024-01-01")
    # 10 encaissements dans la fenêtre glissante (mois courant −10 → −1)
    for k in range(10):
        m = _ym_add(_ym_add(_ym(date.today()), -10), k)
        r = c.post("/api/loc/payments", json={"contract_id": cid, "op_date": m + "-03",
                                              "amount": 1000, "month": m,
                                              "cash_account_id": _mk_cash(c)})
        assert r.status_code == 200, r.text
    ov = c.get("/api/loc/overview").json()["properties"]
    assert len(ov) == 1
    p = ov[0]
    today = _ym(date.today())
    # attendu 12 m : 12 × 1000 (contrat actif toute l'année)
    assert p["expected_12m"] == 12000.0
    # perçu 12 m : 10 × 1000 (les encaissements sont dans la fenêtre)
    assert p["perceived_12m"] == 10000.0
    # attendu total depuis le début du contrat
    n_month_total = 0
    m = "2024-01"
    while m <= today:
        n_month_total += 1
        m = _ym_add(m, 1)
    assert p["expected_total"] == round(n_month_total * 1000, 2)
    # occupation 100 % depuis le 1er contrat (le contrat couvre tout)
    occ = p["occupancy"]
    assert occ["pct"] == 100.0
    # valeur = 150 000 → rendement brut = 10 000/150 000
    assert p["yield_brut"] == round(10000 / 150000 * 100, 2)
    # pas de coûts imputés ni de crédit → net = brut
    assert p["costs_12m"] == 0.0 and p["credit_12m"] == 0.0
    assert p["yield_net"] == p["yield_brut"]
    assert p["yield_net_fin"] == p["yield_brut"]
    # scope membre : la vue ne montre que les biens du membre (admin requis)
    c.post("/api/auth/logout")
    _login(c)
    r = c.get(f"/api/loc/overview?member={es}")
    assert r.status_code == 200


# ---------------------------------------------------------------- TCO véhicule (coût complet cash)

def test_tco_vehicle_full_cost():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_v1")
    cash = _mk_cash(c)
    loan = _mk_loan_auto(c, start=_ym_add(_ym(date.today()), -13) + "-01")
    # 20 000 € financés sur 20 mois à 0 % → apport 10 000 sur un achat à 30 000
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "Tesla",
                                       "loan_id": loan, "purchase_date": "2025-07-15",
                                       "purchase_price": 30000})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    # une dépense imputée (carburant)
    tid = _mk_expense(c, cash, "2026-02-10", 150, "Essence")
    r = c.post("/api/tco/impute", json={"transaction_id": tid, "item_id": iid,
                                        "category": "fuel"})
    assert r.status_code == 200, r.text
    ov = c.get("/api/tco/overview").json()["items"]
    it = next(x for x in ov if x["id"] == iid)
    costs = it["costs"]
    cur = _ym(date.today())
    paid = estate._months_between(_ym_add(cur, -13), cur)  # échéances ≤ aujourd'hui
    assert costs["credit"]["months_paid"] == paid
    # acquisition = apport 10 000 + mensualités versées (0 % → tout en capital)
    assert costs["acquisition_to_date"] == round(10000 + 1000 * paid, 2)
    # usage = imputation
    assert costs["usage_to_date"] == 150.0
    assert costs["by_cat"] == {"fuel": 150.0}
    assert costs["total_to_date"] == round(10000 + 1000 * paid + 150, 2)
    # au terme du crédit : apport + total du crédit (20 000) + usage
    assert costs["total_at_term"] == round(30000 + 150, 2)
    # lissage : total ÷ mois depuis purchase_date
    span = estate._months_between("2025-07", cur) + 1
    assert costs["months_owned"] == span
    assert costs["per_month"] == round(costs["total_to_date"] / span, 2)
    # par année : imputations de 2026 + crédit payé 2025/2026 (ventilé)
    y2026 = costs["by_year"].get("2026", {})
    assert y2026["imputations"] == 150.0
    # l'apport (10 000) n'appartient à aucune année : Σ années = total − apport
    assert sum(v["total"] for v in costs["by_year"].values()) \
        == round(costs["total_to_date"] - 10000, 2)


def test_tco_vehicle_guards():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_v2")
    cash = _mk_cash(c)
    aid = _mk_asset(c)  # immo d'un autre usage
    # véhicule avec un crédit immo → refusé
    r = c.post("/api/loans", json={"name": "Prêt immo", "loan_type": "immo",
                                   "currency": "EUR", "principal_initial": 50000,
                                   "principal_remaining": 50000, "rate_annual": 2,
                                   "monthly_payment": 400,
                                   "start_date": "2024-01-01", "account_id": aid})
    immo_loan = r.json()["id"]
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "Auto",
                                       "loan_id": immo_loan})
    assert r.status_code == 400
    # fiche immo sur un compte non immo → refusé ; label vide → refusé
    r = c.post("/api/tco/items", json={"kind": "immo", "label": "Bien",
                                       "account_id": cash})
    assert r.status_code == 400
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "  "})
    assert r.status_code == 400
    # deux fiches immo pour le même bien → refusé
    r = c.post("/api/tco/items", json={"kind": "immo", "label": "Fiche",
                                       "account_id": aid})
    assert r.status_code == 200, r.text
    r = c.post("/api/tco/items", json={"kind": "immo", "label": "Fiche 2",
                                       "account_id": aid})
    assert r.status_code == 400


# ---------------------------------------------------------------- TCO immo (crédit auto-détecté, ghost)

def test_tco_immo_ghost_shows_credit():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "es_i1")
    aid = _mk_asset(c)
    r = c.post("/api/loans", json={"name": "Prêt bien", "loan_type": "immo",
                                   "currency": "EUR", "principal_initial": 50000,
                                   "principal_remaining": 50000, "rate_annual": 3,
                                   "monthly_payment": 600,
                                   "start_date": "2025-01-01", "account_id": aid})
    assert r.status_code == 200, r.text
    # PAS de fiche : le bien apparaît avec son crédit (imputations 0)
    ov = c.get("/api/tco/overview").json()["items"]
    ghost = next(x for x in ov if x["kind"] == "immo" and x["account_id"] == aid)
    assert ghost["id"] is None
    assert ghost["costs"]["credit"] is not None
    assert ghost["costs"]["credit_to_date"] > 0
    assert ghost["costs"]["usage_to_date"] == 0.0
    assert ghost["costs"]["by_cat"] == {}
    # création de la fiche → id présent, coûts identiques (crédit + 0 imputation)
    r = c.post("/api/tco/items", json={"kind": "immo", "label": "Bien loué",
                                       "account_id": aid})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    ov = c.get("/api/tco/overview").json()["items"]
    fic = next(x for x in ov if x["id"] == iid)
    assert fic["costs"]["total_to_date"] == ghost["costs"]["total_to_date"]
    # DELETE fiche sans imputation → dure ; avec imputation → soft
    r = c.delete(f"/api/tco/items/{iid}")
    assert r.status_code == 200
    conn = _raw_conn()
    assert conn.execute("SELECT COUNT(*) n FROM tco_items WHERE id=?",
                        (iid,)).fetchone()["n"] == 0
    conn.close()


# ---------------------------------------------------------------- imputations

def test_impute_guards_and_unmapped():
    c = TestClient(app.app)
    _login(c)
    es = _mk_member(c, "es_m1")
    cash = _mk_cash(c)
    loan = _mk_loan_auto(c)
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "Auto",
                                       "loan_id": loan})
    iid = r.json()["id"]
    income_tx = _mk_expense(c, cash, "2026-01-05", 50)
    conn = _raw_conn()
    conn.execute("UPDATE transactions SET kind='income' WHERE id=?", (income_tx,))
    conn.commit()
    conn.close()
    # une op income → refusée
    r = c.post("/api/tco/impute", json={"transaction_id": income_tx,
                                        "item_id": iid, "category": "fuel"})
    assert r.status_code == 400
    # catégorie invalide / fiche d'un autre membre / op d'un autre membre
    tid = _mk_expense(c, cash, "2026-02-05", 80)
    r = c.post("/api/tco/impute", json={"transaction_id": tid, "item_id": iid,
                                        "category": "work"})  # work = immo uniquement
    assert r.status_code == 400
    r = c.post("/api/tco/impute", json={"transaction_id": tid, "item_id": iid,
                                        "category": "fuel"})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _mk_member(c, "es_m1b")
    r = c.post("/api/tco/impute", json={"transaction_id": tid, "item_id": iid,
                                        "category": "fuel"})
    assert r.status_code == 400  # l'op n'est pas à ce membre
    # retrait
    c.post("/api/auth/logout")
    _login(c, es, PWD)
    r = c.delete(f"/api/tco/impute/{tid}")
    assert r.status_code == 200
    r = c.delete(f"/api/tco/impute/{tid}")
    assert r.status_code == 404
    # liste unmapped : l'income n'y est JAMAIS (non imputable) ; tid, après
    # retrait de son imputation, redevient une dépense non imputée → listée
    unm = c.get("/api/tco/imputations?unmapped=1").json()["unmapped"]
    ids = [x["id"] for x in unm]
    assert income_tx not in ids
    assert tid in ids


# ---------------------------------------------------------------- famille / export-import / coffre

def test_family_view_and_export_import_roundtrip():
    c = TestClient(app.app)
    _login(c)
    es = _mk_member(c, "es_f1")
    aid = _mk_asset(c, name="Bien fam")
    _mk_contract(c, aid)
    # export → import sur un 2e membre (round-trip des sections estate)
    r = c.get("/api/export")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "estate" in payload and payload["estate"]["loc_contracts"]
    # round-trip sur le MÊME membre (les ids de comptes du payload sont pris)
    r = c.post("/api/import", json=payload)
    assert r.status_code == 200, r.text
    r = c.get("/api/loc/contracts")
    assert len(r.json()["contracts"]) == 1
    # vue admin ?member= : les biens du membre apparaissent dans l'overview
    _login(c)
    ov = c.get(f"/api/loc/overview?member={es}").json()["properties"]
    assert len(ov) == 1 and ov[0]["name"] == "Bien fam"
    # vue famille : les biens de tous les membres standards
    ov = c.get("/api/loc/overview?family=1").json()["properties"]
    assert any(x["name"] == "Bien fam" for x in ov)


def test_vault_init_copies_estate_rows():
    """L'init d'un coffre protected copie les 4 tables estate (copy_rows)."""
    c = TestClient(app.app)
    _login(c)
    es = _mk_member(c, "es_vlt", mode="protected")
    aid = _mk_asset(c, name="Bien vault") if False else None
    # données claires posées en SQL direct (pré-coffre), comme un legacy
    conn = _raw_conn()
    cur = conn.execute(
        "INSERT INTO accounts (owner, name, asset_class, open_date,"
        " valuation_mode) VALUES (?,?,?,?,?)",
        (es, "Bien vault", "immobilier", "2024-01-01", "manual"))
    aid = cur.lastrowid
    lcur = conn.execute(
        "INSERT INTO loans (owner, name, loan_type, principal_initial,"
        " principal_remaining, rate_annual, monthly_payment, start_date,"
        " account_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (es, "Prêt vault", "immo", 50000, 50000, 2, 500, "2024-01-01", aid))
    lid = lcur.lastrowid
    conn.execute(
        "INSERT INTO loc_contracts (owner, account_id, tenant, rent_monthly,"
        " start_date) VALUES (?,?,?,?,?)",
        (es, aid, "M. Vault", 900, "2024-02-01"))
    conn.execute(
        "INSERT INTO tco_items (owner, kind, label, account_id, loan_id)"
        " VALUES (?,?,?,?,?)", (es, "immo", "Bien vault", aid, lid))
    conn.commit()
    conn.close()
    # init du coffre (copie + purge du clair) — handshake chiffré simulé
    dek = os.urandom(32)
    r = c.post("/api/vault/init", json={
        "salt": _b64(os.urandom(16)), "wrapped": _b64(os.urandom(48)),
        "dek": _b64(dek)})
    assert r.status_code == 200, r.text
    # la session courante travaille sur la base mémoire : tout est visible
    r = c.get("/api/loc/contracts")
    assert r.status_code == 200
    assert len(r.json()["contracts"]) == 1
    r = c.get("/api/tco/items")
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    # plus rien en clair sur la base principale
    conn = _raw_conn()
    assert conn.execute("SELECT COUNT(*) n FROM loc_contracts WHERE owner=?",
                        (es,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM tco_items WHERE owner=?",
                        (es,)).fetchone()["n"] == 0
    conn.close()
