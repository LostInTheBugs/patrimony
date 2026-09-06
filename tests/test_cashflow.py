"""v2026.09.028 — cash-flow prévisionnel : règles récurrentes de dépenses
(kind expense) + projection de trésorerie (solde cumulé depuis les comptes
courants)."""
import os
import tempfile
from datetime import date

_tmp = tempfile.mkdtemp(prefix="patrimony-cf-")
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


def _new_member(admin_c, name):
    r = admin_c.post("/api/family", json={"username": name, "password": PWD, "mode": "standard"})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, name, PWD)
    return c


def _acc(c, name, cls="comptes", value=None):
    """Compte sur un membre dédié (classe comptes par défaut) + usernames
    'cf-*' uniques pour la base partagée de la suite."""
    r = c.post("/api/accounts", json={"name": name, "asset_class": cls})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    if value is not None:
        assert c.post(f"/api/accounts/{aid}/valuation",
                      json={"value": value, "val_date": date.today().isoformat()}).status_code == 200
    return aid


def _rule(c, acc, label, amount, kind="income", freq="monthly", next_d=None):
    r = c.post("/api/income-rules", json={
        "account_id": acc, "label": label, "amount": amount, "kind": kind,
        "freq": freq, "next_date": (next_d or date.today()).isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ym_offset(months: int) -> str:
    t = date.today()
    y = t.year + (t.month - 1 + months) // 12
    m = (t.month - 1 + months) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _date_offset(months: int, day: int = 15) -> date:
    t = date.today()
    y = t.year + (t.month - 1 + months) // 12
    m = (t.month - 1 + months) % 12 + 1
    import calendar
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def test_rule_kind_default_validation_and_calendar(admin_c):
    """kind défaut income (rétrocompat), expense accepté, kind invalide 400 ;
    le calendrier liste les deux sens avec leur kind."""
    acc = _acc(admin_c, "Courant CF", value=1000)
    rid = _rule(admin_c, acc, "Loyer", 1200, kind="expense")
    rules = admin_c.get("/api/income-rules").json()["rules"]
    mine = [r for r in rules if r["account_id"] == acc]
    assert any(r["kind"] == "expense" for r in mine)
    # défaut income sans kind
    rid2 = _rule(admin_c, acc, "Salaire", 3000)
    assert next(r for r in admin_c.get("/api/income-rules").json()["rules"]
                if r["id"] == rid2)["kind"] == "income"
    assert admin_c.post("/api/income-rules", json={
        "account_id": acc, "label": "X", "amount": 1, "kind": "toto",
        "next_date": "2026-10-01"}).status_code == 400
    assert admin_c.put(f"/api/income-rules/{rid}", json={
        "account_id": acc, "label": "Loyer", "amount": 1200, "kind": "expense",
        "next_date": "2026-10-01"}).status_code == 200
    assert admin_c.put(f"/api/income-rules/{rid}", json={
        "account_id": acc, "label": "Loyer", "amount": 1200, "kind": "zap",
        "next_date": "2026-10-01"}).status_code == 400
    cal = admin_c.get("/api/income-calendar").json()["calendar"]
    assert any(c["rule_id"] == rid and c["kind"] == "expense" for c in cal)


def test_cashflow_projection_and_isolation(admin_c):
    """Solde de départ = trésorerie (dernière valo des comptes), nets par
    mois in−out, cumul ; isolation par membre."""
    me = _new_member(admin_c, "cf-proj")
    cash = _acc(me, "Courant CF2", value=5000)
    _acc(me, "PEA CF", cls="bourse", value=90000)  # hors trésorerie
    _rule(me, cash, "Salaire", 1000)                     # income monthly
    _rule(me, cash, "Loyer", 400, kind="expense")        # expense monthly
    _rule(me, cash, "Taxe", 300, kind="expense", freq="quarterly")
    d = me.get("/api/cashflow?months=3").json()
    assert d["starting_balance"] == 5000.0
    m0 = d["labels"][0]
    assert d["in"][0] == 1000.0
    assert d["out"][0] == 700.0  # loyer + taxe trimestrielle ce mois-ci
    assert d["net"][0] == 300.0
    assert d["balance"][0] == 5300.0
    assert d["in"][1] == 1000.0 and d["out"][1] == 400.0 and d["balance"][2] == 6500.0
    assert d["labels"][1] == _ym_offset(1) and d["labels"][2] == _ym_offset(2)
    # isolation : un autre membre ne voit ni règles ni solde
    other = _new_member(admin_c, "cf-iso")
    d2 = other.get("/api/cashflow").json()
    assert d2["starting_balance"] == 0.0
    assert all(x == 0.0 for x in d2["in"] + d2["out"])
    assert other.get("/api/income-rules").json()["rules"] == []
    # règle désactivée → exclue
    rid = next(r["id"] for r in me.get("/api/income-rules").json()["rules"]
               if r["label"] == "Loyer")
    me.put(f"/api/income-rules/{rid}", json={
        "account_id": cash, "label": "Loyer", "amount": 400, "kind": "expense",
        "next_date": date.today().isoformat(), "active": 0})
    d3 = me.get("/api/cashflow?months=1").json()
    assert d3["out"][0] == 300.0


def test_cashflow_roundtrip_preserves_kind_and_legacy(admin_c):
    """Export/import : kind expense préservé ; un ancien export sans clé
    kind reste importable (income par défaut)."""
    src = _new_member(admin_c, "cf-src")
    acc = _acc(src, "Courant CF3", value=100)
    rid = _rule(src, acc, "Assurance", 50, kind="expense")
    payload = src.get("/api/export").json()
    assert any(r["id"] == rid and r["kind"] == "expense" for r in payload["income_rules"])
    # legacy : on retire la clé kind (fichiers d'avant v028)
    for r in payload["income_rules"]:
        r.pop("kind", None)
    assert src.delete(f"/api/accounts/{acc}").status_code == 200
    assert src.post("/api/import", json=payload).status_code == 200
    rules = src.get("/api/income-rules").json()["rules"]
    legacy = [r for r in rules if r["label"] == "Assurance"]
    assert legacy and legacy[0]["kind"] == "income"  # défaut pour les vieux exports


def test_cashflow_future_dated_rules(admin_c):
    """Une règle dont la prochaine échéance tombe dans un mois futur ne
    pèse pas sur les mois précédents."""
    me = _new_member(admin_c, "cf-fut")
    acc = _acc(me, "Courant CF4", value=0)
    _rule(me, acc, "Remboursement", 200, kind="expense", next_d=_date_offset(2, 5))
    d = me.get("/api/cashflow?months=3").json()
    assert d["out"][0] == 0.0 and d["out"][1] == 0.0 and d["out"][2] == 200.0
