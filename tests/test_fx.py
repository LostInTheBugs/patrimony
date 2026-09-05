"""Tests multi-devises (v2026.09.020) : conversion EUR dans synthèse,
historique et benchmarks ; override manuel ; taux BCE seedés (aucun réseau) ;
parser XML BCE ; refresh mocké ; devise invalide rejetée.

Règle : rate = unités de devise pour 1 EUR → EUR = valeur / rate.
"""
import datetime
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-fx-")
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


def _member(admin_c, name):
    r = admin_c.post("/api/family", json={"username": name, "password": PWD, "mode": "standard"})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, name, PWD)
    return c


def _seed_rates(dates: dict[str, dict[str, float]], source="ecb"):
    """dates: {day: {ccy: rate}} — taux BCE « unités par EUR »."""
    conn = app.db_main()
    try:
        for day, ccs in dates.items():
            for ccy, rate in ccs.items():
                conn.execute(
                    "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source) VALUES (?,?,?,?)",
                    (ccy, day, rate, source),
                )
        conn.commit()
    finally:
        conn.close()


def _mk_acc(c, name, cls="comptes", currency="EUR", fx_override=None):
    body = {"name": name, "asset_class": cls, "currency": currency}
    if fx_override is not None:
        body["fx_override"] = fx_override
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_fx_summary_conversion_and_override(admin_c):
    c = _member(admin_c, "fx-sum")
    _seed_rates({"2026-03-15": {"USD": 1.08, "CHF": 0.94}})
    eur = _mk_acc(c, "Compte EUR", "comptes", cost_basis=1000) if False else None
    a_eur = _mk_acc(c, "Compte EUR", "comptes")
    c.post(f"/api/accounts/{a_eur}/valuation", json={"value": 1000, "val_date": "2026-03-20"})
    a_usd = _mk_acc(c, "PEA USD", "bourse", currency="USD")
    c.post(f"/api/accounts/{a_usd}/valuation", json={"value": 10800, "val_date": "2026-03-20"})
    # taux BCE du 2026-03-15 : 1 EUR = 1.08 USD → 10800 USD = 10 000 EUR
    s = c.get("/api/summary").json()
    assert s["total_value"] == round(1000 + 10800 / 1.08, 2) == 11000.0
    assert s["fx_asof"] == "2026-03-15" and s["fx_missing"] == []
    assert {x["key"]: x for x in s["classes"]}["bourse"]["value"] == 10000.0
    # payload : fx attaché à l'actif USD
    acc = {x["id"]: x for x in c.get("/api/accounts").json()["accounts"]}
    assert acc[a_usd]["currency"] == "USD" and acc[a_usd]["fx"]["rate"] == 1.08
    assert acc[a_usd]["fx"]["value_eur"] == 10000.0
    assert acc[a_eur]["fx"] is None and acc[a_eur]["currency"] == "EUR"
    # override manuel prime sur la BCE : 1 EUR = 1.10 USD
    r = c.put(f"/api/accounts/{a_usd}", json={"name": "PEA USD", "asset_class": "bourse",
                                              "currency": "USD", "fx_override": 1.10})
    assert r.status_code == 200
    s = c.get("/api/summary").json()
    assert s["total_value"] == round(1000 + 10800 / 1.10, 2) == round(10818.18, 2)
    assert s["fx_asof"] is None  # override → pas de date BCE
    acc = {x["id"]: x for x in c.get("/api/accounts").json()["accounts"]}
    assert acc[a_usd]["fx"]["source"] == "manual" and acc[a_usd]["fx"]["stale"] is False
    # taux manquant → actif exclu des totaux + signalé
    a_gbp = _mk_acc(c, "Compte GBP", "epargne", currency="GBP")
    c.post(f"/api/accounts/{a_gbp}/valuation", json={"value": 100, "val_date": "2026-03-20"})
    s = c.get("/api/summary").json()
    assert s["fx_missing"] == ["Compte GBP"] and s["nb_accounts"] == 2
    assert s["total_value"] == round(10818.18, 2)


def test_fx_history_monthly_rates_and_fallback(admin_c):
    c = _member(admin_c, "fx-hist")
    _seed_rates({
        "2026-01-20": {"USD": 1.10},
        "2026-02-10": {"USD": 1.20},
        "2026-02-25": {"USD": 1.05},
    })
    a = _mk_acc(c, "Livret USD", "epargne", currency="USD")
    c.post(f"/api/accounts/{a}/valuation", json={"value": 2100, "val_date": "2026-02-15"})
    # fin janvier : AUCUNE valeur portée (1re val. le 15/02) → 0
    # fin février : 1.05 (dernier taux ≤ 28/02) → 2100/1.05 = 2000
    h = c.get("/api/history?months=60").json()
    labels = h["labels"]
    i = {m: k for k, m in enumerate(labels)}
    assert h["totals"][i["2026-01"]] == 0.0
    assert h["totals"][i["2026-02"]] == round(2100 / 1.05, 2) == 2000.0
    # fallback « taux le plus ancien » : valeur antérieure au 1er taux BCE
    c.post(f"/api/accounts/{a}/valuation", json={"value": 1100, "val_date": "2025-04-15"})
    h2 = c.get("/api/history?months=60").json()
    i2 = {m: k for k, m in enumerate(h2["labels"])}
    assert h2["totals"][i2["2025-04"]] == round(1100 / 1.10, 2) == 1000.0  # aucun ≤ 04/2025 → 1.10
    # valeur sans taux du tout → mois à 0
    b = _mk_acc(c, "Sans taux", "crypto", currency="JPY")
    c.post(f"/api/accounts/{b}/valuation", json={"value": 10000, "val_date": "2026-01-05"})
    h3 = c.get("/api/history?months=60").json()
    i3 = {m: k for k, m in enumerate(h3["labels"])}
    assert h3["totals"][i3["2026-02"]] == 2000.0  # JPY exclu
    assert h3["series"]["crypto"][i3["2026-02"]] == 0.0


def test_fx_benchmarks_and_ecb_parser_and_refresh(admin_c, monkeypatch):
    # parser XML réel (structure BCE)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <Cube><Cube time="2026-09-04">
   <Cube currency="USD" rate="1.0842"/><Cube currency="GBP" rate="0.8456"/>
   <Cube currency="JPY" rate="161.23"/><Cube currency="XXX" rate="12.0"/>
 </Cube></Cube></gesmes:Envelope>"""
    parsed = app._parse_ecb_xml(xml)
    assert ("USD", "2026-09-04", 1.0842) in parsed and ("JPY", "2026-09-04", 161.23) in parsed
    assert len(parsed) == 4  # XXX n'est pas filtré par le parser (filtrage à l'insertion)

    # refresh mocké : insert des taux BCE du jour (fonction SYNCHRONE : le
    # routeur l'exécute dans le threadpool)
    def fake_fetch():
        return parsed
    monkeypatch.setattr(app, "_ecb_fetch_http", fake_fetch)
    r = admin_c.post("/api/fx/refresh")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["updated"] == 3  # USD, GBP, JPY (XXX hors liste supportée)
    conn = app.db_main()
    n = conn.execute("SELECT COUNT(*) c FROM fx_rates WHERE rate_date='2026-09-04'").fetchone()["c"]
    usd = conn.execute("SELECT rate FROM fx_rates WHERE ccy='USD' AND rate_date='2026-09-04'").fetchone()
    conn.close()
    assert n == 3 and usd["rate"] == 1.0842
    # un actif USD valorisé après refresh est converti par la BCE
    c = _member(admin_c, "fx-bench")
    a = _mk_acc(c, "Compte USD", "comptes", currency="USD")
    c.post(f"/api/accounts/{a}/valuation", json={"value": 108.42, "val_date": "2026-09-04"})
    b = c.get("/api/benchmarks").json()
    assert b["user"]["value"] == round(108.42 / 1.0842, 2) == 100.0


def test_fx_account_validation_and_legacy_default(admin_c):
    c = _member(admin_c, "fx-val")
    # devise inconnue → 400 (création comme édition)
    assert c.post("/api/accounts", json={"name": "X", "asset_class": "comptes", "currency": "BTC"}).status_code == 400
    # minuscules normalisées
    aid = c.post("/api/accounts", json={"name": "Y", "asset_class": "comptes", "currency": "usd"}).json()["id"]
    assert c.get("/api/accounts").json()["accounts"][0]["currency"] == "USD"
    assert c.put(f"/api/accounts/{aid}", json={"name": "Y", "asset_class": "comptes", "currency": "EUR"}).status_code == 200
    # devise invalide en édition → 400 et actif intact
    assert c.put(f"/api/accounts/{aid}", json={"name": "Y", "asset_class": "comptes", "currency": "ZZZ"}).status_code == 400
    acc = c.get("/api/accounts").json()["accounts"][0]
    assert acc["currency"] == "EUR" and acc["name"] == "Y"
