"""Tests v2026.09.062 — courbes « partout » (step ② des charts, demande Fred) :
crédits (capital restant dû théorique), TCO (coût cumulé rétroactif),
crypto (snapshots mensuels), crowdfunding (encours de la créance).

Design claude/design-charts-partout-2026.md. Toutes les routes curve
retournent {labels: [ym], series: [{key, name, …, values}]} — zéro calcul
côté UI. Isolation : membres dédiés par test (préfixe `cv`)."""

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

from fastapi.testclient import TestClient  # noqa: E402

import src.app as app  # noqa: E402

PWD = "curve-pass-2026-long"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(c, tag):
    c.post("/api/auth/logout")
    _login(c)
    r = c.post("/api/family", json={"username": tag, "password": PWD,
                                    "display_name": tag, "mode": "standard"})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, tag, PWD)
    return tag


def _mk_asset(c, name, cls="immobilier", value=172000, odate="2021-03-01",
              cost=145000, **kw):
    body = {"name": name, "asset_class": cls, "currency": "EUR",
            "cost_basis": cost, "open_date": odate, "initial_value": value,
            "valuation_mode": "manual"}
    body.update(kw)
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_loan(c, name, ptype="immo", principal=92000, monthly=520, rate=2.8,
             start="2021-03-01", currency="EUR", account_id=None):
    r = c.post("/api/loans", json={
        "name": name, "loan_type": ptype, "lender": "Banque",
        "currency": currency, "principal_initial": principal,
        "principal_remaining": principal, "rate_annual": rate,
        "monthly_payment": monthly, "insurance_monthly": 0,
        "start_date": start, "account_id": account_id, "notes": "", "active": 1})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_item(c, kind="vehicle", label="Voiture", **kw):
    body = {"kind": kind, "label": label, "purchase_date": "2024-02-15",
            "purchase_price": 40000, "loan_id": None}
    body.update(kw)
    r = c.post("/api/tco/items", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_expense(c, cash_id, d, amount, note="Dépense"):
    r = c.post("/api/transactions", json={"account_id": cash_id, "op_date": d,
                                          "kind": "expense", "amount": amount,
                                          "note": note})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _impute(c, tx, item, category="maintenance"):
    r = c.post("/api/tco/impute", json={"transaction_id": tx, "item_id": item,
                                        "category": category})
    assert r.status_code == 200, r.text


def _raw_conn():
    conn = sqlite3.connect(app.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ------------------------------------------------------------- crédits

def test_loans_curve_theoretical_remaining():
    c = TestClient(app.app)
    _login(c)
    tag = _mk_member(c, "cv_lo")
    _mk_loan(c, "Prêt appart", "immo", principal=92000, monthly=520, rate=2.8,
             start="2021-03-01")
    _mk_loan(c, "Prêt Tesla", "auto", principal=30000, monthly=884, rate=3.9,
             start="2024-03-01")
    r = c.get("/api/loans/curve")
    assert r.status_code == 200
    d = r.json()
    assert d["fx_missing"] == []
    assert d["labels"][0] == "2021-03"
    assert len(d["series"]) == 2
    immo = next(s for s in d["series"] if s["loan_type"] == "immo")
    auto = next(s for s in d["series"] if s["loan_type"] == "auto")
    # premier point = capital initial au mois de souscription, puis décroît
    # régulièrement jusqu'à 0 au terme (aucune remontée)
    for s in (immo, auto):
        nz = [i for i, v in enumerate(s["values"]) if v]
        assert s["values"][nz[0]] == (92000.0 if s is immo else 30000.0)
        assert s["values"][-1] == 0.0
        # décroissance régulière à partir du premier mois non nul
        for i in range(nz[0], len(s["values"]) - 1):
            assert s["values"][i] >= s["values"][i + 1], i
    # l'auto démarre plus tard (0 avant 2024-03) et se termine avant l'immo
    assert auto["values"][0] == 0.0
    i24 = d["labels"].index("2024-03")
    assert auto["values"][i24] == 30000.0
    assert max(immo["values"]) > max(auto["values"])


def test_loans_curve_fx_missing_omits_series():
    c = TestClient(app.app)
    _login(c)
    tag = _mk_member(c, "cv_lx")
    _mk_loan(c, "Prêt USD", "conso", principal=10000, monthly=300, rate=1,
             start="2024-01-01", currency="USD")
    r = c.get("/api/loans/curve")
    d = r.json()
    assert d["series"] == []
    assert d["fx_missing"] == ["Prêt USD"]


# ------------------------------------------------------------- TCO

def test_tco_curve_retrospective_monthly():
    c = TestClient(app.app)
    _login(c)
    tag = _mk_member(c, "cv_tc")
    cash = _mk_asset(c, "Courant", cls="comptes", value=8000,
                     odate="2023-01-01", cost=8000)
    lid = _mk_loan(c, "Prêt T", "auto", principal=30000, monthly=1000, rate=0,
                   start="2024-03-01", account_id=None)
    iid = _mk_item(c, kind="vehicle", label="Voiture T", purchase_date="2024-02-15",
                   purchase_price=40000, loan_id=lid)
    tx = _mk_expense(c, cash, "2025-06-10", 500)
    _impute(c, tx, iid, "maintenance")
    r = c.get("/api/tco/curve", params={"months": 60})
    assert r.status_code == 200
    d = r.json()
    s = next(x for x in d["series"] if x["kind"] == "vehicle")
    L = d["labels"]
    assert s["values"][L.index("2024-01")] == 0.0        # avant l'achat
    assert s["values"][L.index("2024-02")] == 10000.0    # apport (40 000−30 000)
    assert s["values"][L.index("2024-03")] == 10000.0    # 1re échéance en avril
    # l'imputation du 2025-06-10 n'apparaît qu'à partir du point juin
    assert s["values"][L.index("2025-05")] < s["values"][L.index("2025-06")]
    assert round(s["values"][L.index("2025-06")]
                 - s["values"][L.index("2025-05")], 2) == 1500.0
    # aucun trou, et le dernier point = le KPI à date (même fonction métier)
    assert all(v is not None for v in s["values"])
    ov = c.get("/api/tco/overview").json()
    ref = next(x for x in ov["items"] if x["id"] == iid)
    assert s["values"][-1] == ref["costs"]["total_to_date"]


# ------------------------------------------------------------- crypto

def test_cw_curve_monthly_snapshots_usd():
    c = TestClient(app.app)
    _login(c)
    tag = _mk_member(c, "cv_cw")
    conn = _raw_conn()
    try:
        aid = conn.execute(
            "INSERT INTO accounts (owner, name, asset_class, currency)"
            " VALUES (?, 'Ledger', 'crypto', 'EUR')", (tag,)).lastrowid
        cur = conn.execute(
            "INSERT INTO cw_wallets (owner, label, address, chain, watch_only,"
            " account_id) VALUES (?, 'Ledger main', '0xabc', 'eth', 1, ?)",
            (tag, aid))
        wid = cur.lastrowid
        for d0, v in (("2026-08-28", 1000.0), ("2026-08-30", 1200.0),
                      ("2026-09-05", 1300.0)):
            conn.execute(
                "INSERT INTO cw_history (wallet_id, owner, date, value_usd,"
                " cost_usd) VALUES (?, ?, ?, ?, ?)", (wid, tag, d0, v, v))
        conn.commit()
    finally:
        conn.close()
    r = c.get("/api/cw/curve")
    assert r.status_code == 200
    d = r.json()
    assert d["labels"] == ["2026-08", "2026-09"]
    assert len(d["series"]) == 1
    s = d["series"][0]
    assert s["key"] == str(aid)
    # dernier snapshot du mois (le 30, pas le 28) — USD, devise de la page
    assert s["values"][0] == 1200.0
    assert s["values"][1] == 1300.0


# ------------------------------------------------------------- crowdfunding

def test_cf_curve_encours_reconstructed():
    c = TestClient(app.app)
    _login(c)
    tag = _mk_member(c, "cv_cf")
    # le compte crowdfunding est géré par le module (jamais par /api/accounts)
    conn = _raw_conn()
    try:
        aid = conn.execute(
            "INSERT INTO accounts (owner, name, asset_class, currency)"
            " VALUES (?, 'Bricks', 'crowdfunding', 'EUR')", (tag,)).lastrowid
        conn.execute(
            "INSERT INTO cf_platforms (owner, platform, account_id, balance,"
            " deposited, invested_value, updated_at)"
            " VALUES (?, 'bricks', ?, 0, 1400, 0, '2026-09-09T00:00:00+00:00')",
            (tag, aid))
        pid = conn.execute(
            "INSERT INTO cf_projects (owner, platform, name, city, invested, rate,"
            " duration_months, start_date, status, repaid_capital, interest_received,"
            " interest_net, interest_remaining, interest_remaining_net, real_rate,"
            " valuation, created_at, updated_at) VALUES (?, 'bricks', 'Projet 1',"
            " '', 800, 9.0, 24, '2024-01-10', 'en_cours', 0, 0, 0, 0, 0, 0, 0,"
            " '2024-01-10T00:00:00+00:00', '2024-01-10T00:00:00+00:00')",
            (tag,)).lastrowid
        ops = [
            ("a1", "2024-01-10", "Achat de bricks", "Validée", -500),
            ("a2", "2024-02-05", "Achat de bricks", "Validée", -300),
            ("a3", "2024-03-01", "Revenus reversés", "Validée", 40.0),
            ("a4", "2024-04-01", "Remboursement de capital", "Validée", 200.0),
            ("a5", "2024-05-01", "Achat de bricks", "En attente", -1000.0),
        ]
        for src, d0, typ, st, amt in ops:
            conn.execute(
                "INSERT INTO cf_operations (owner, platform, source_id, op_date,"
                " type, status, project_id, amount, details, contract_type, extra,"
                " created_at) VALUES (?, 'bricks', ?, ?, ?, ?, ?, ?, ?, '', '',"
                " '2024-01-10T00:00:00+00:00')",
                (tag, src, d0, typ, st, pid, amt, typ))
        conn.commit()
    finally:
        conn.close()
    r = c.get("/api/cf/curve")
    assert r.status_code == 200
    d = r.json()
    assert d["labels"] == ["2024-01", "2024-02", "2024-03", "2024-04"]
    assert len(d["series"]) == 1
    s = d["series"][0]
    assert s["key"] == str(aid)
    # souscriptions + ; revenus sans effet ; remboursement capital − ; plancher
    assert s["values"] == [500.0, 800.0, 800.0, 600.0]
