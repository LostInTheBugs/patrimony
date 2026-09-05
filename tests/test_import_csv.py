"""Tests d'import CSV d'opérations (v2026.09.015) : parsing FR/Excel, signe,
débit/crédit, doublons, isolation par propriétaire, erreurs par ligne.

Même environnement que test_app.py (posé avant l'import de src.app).
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-csv-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app

PWD = "member-pass-2026"

CSV_FR = (
    "Date;Libellé;Montant\n"
    "05/01/2026;Versement initial;1 000,00\n"
    '12/01/2026;Achat "part 1" (ETF);-250,50\n'
    "20/01/2026;Frais de garde;-3,49\n"
    "13/02/2026;Revenu;25,00\n"
)


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r


def _mk_acc(c, name):
    r = c.post("/api/accounts", json={"name": name, "asset_class": "comptes"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tx_of(admin_c, aid):
    ops = admin_c.get("/api/transactions").json()["transactions"]
    return {t["note"]: t for t in ops if t["account_id"] == aid}


@pytest.fixture(scope="module")
def admin_c():
    c = TestClient(app.app)
    _login(c)
    yield c


def test_csv_fr_parse_sign_and_dup(admin_c):
    aid = _mk_acc(admin_c, "CSV fr")
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": CSV_FR,
    })
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["inserted"], j["skipped"], j["invalid"]) == (4, 0, 0)
    txs = _tx_of(admin_c, aid)
    assert len(txs) == 4
    assert txs["Versement initial"]["amount"] == 1000.0
    assert txs["Versement initial"]["kind"] == "deposit"
    assert txs['Achat "part 1" (ETF)']["amount"] == 250.5
    assert txs['Achat "part 1" (ETF)']["kind"] == "withdrawal"  # négatif → type inversé
    assert txs["Frais de garde"]["amount"] == 3.49
    assert txs["Frais de garde"]["kind"] == "withdrawal"  # négatif + défaut deposit → retrait
    assert txs["Revenu"]["kind"] == "deposit"  # positif + type par défaut
    # re-import du même fichier → tout doublon, aucune insertion
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": CSV_FR,
    })
    j = r.json()
    assert (j["inserted"], j["skipped"]) == (0, 4)


def test_csv_debit_credit_columns(admin_c):
    aid = _mk_acc(admin_c, "CSV debit credit")
    csv_dc = "Date;Description;Débit;Crédit\n01/03/2026;Virement recu;;1 234,56\n02/03/2026;Prel;89,90;\n"
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "income", "csv_text": csv_dc,
    })
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2
    txs = _tx_of(admin_c, aid)
    assert txs["Virement recu"]["amount"] == 1234.56 and txs["Virement recu"]["kind"] == "income"
    assert txs["Prel"]["amount"] == 89.9 and txs["Prel"]["kind"] == "expense"  # crédit < débit


def test_csv_errors_per_row_and_ownership(admin_c):
    aid = _mk_acc(admin_c, "CSV mixte")
    csv_ok = "date,note,amount\n2026-04-01,bonne ligne,\"12,34\"\n32/13/2026,mauvaise date,5.00\n2026-04-02,montant vide,\n"
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": csv_ok,
    })
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["inserted"], j["invalid"], j["skipped"]) == (1, 2, 0)
    assert any("ligne" in e for e in j["errors"])
    assert len(j["errors"]) <= 5
    # en-tête incompréhensible → 400
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": "foo;bar\n1;2\n",
    })
    assert r.status_code == 400
    # tout invalide → 400 avec raison
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": "date;montant\nzz;zz\n",
    })
    assert r.status_code == 400 and "Aucune ligne importée" in r.json()["detail"]
    # isolation : l'admin ne peut pas importer dans l'actif d'un membre
    assert admin_c.post("/api/family", json={
        "username": "sec-csv-m1", "password": PWD, "mode": "standard",
    }).status_code == 200
    mb = TestClient(app.app)
    _login(mb, "sec-csv-m1", PWD)
    mid = _mk_acc(mb, "Actif membre")
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": mid, "default_kind": "deposit", "csv_text": CSV_FR,
    })
    assert r.status_code == 404
    # actif inconnu → 404 ; non authentifié → 401 ; type invalide → 400
    assert admin_c.post("/api/transactions/import-csv", json={
        "account_id": 99999, "default_kind": "deposit", "csv_text": CSV_FR,
    }).status_code == 404
    assert TestClient(app.app).post("/api/transactions/import-csv", json={
        "account_id": 1, "default_kind": "deposit", "csv_text": CSV_FR,
    }).status_code == 401
    assert admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "arbitrage", "csv_text": CSV_FR,
    }).status_code == 400


def test_csv_duplicates_within_batch_and_iso_dates(admin_c):
    aid = _mk_acc(admin_c, "CSV dedup")
    csv = "Date;Libellé;Montant\n2026-05-01;A;10\n2026-05-01;B;20\n2026-05-01;A;10\n"
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": csv,
    })
    j = r.json()
    assert (j["inserted"], j["skipped"]) == (2, 1)
    # doublon entre un fichier et un ajout manuel existant (note identique)
    admin_c.post("/api/transactions", json={
        "account_id": aid, "kind": "deposit", "op_date": "2026-05-02", "amount": 30, "note": "C",
    })
    r = admin_c.post("/api/transactions/import-csv", json={
        "account_id": aid, "default_kind": "deposit", "csv_text": "Date;Libellé;Montant\n2026-05-02;C;30\n",
    })
    j = r.json()
    assert (j["inserted"], j["skipped"]) == (0, 1)
