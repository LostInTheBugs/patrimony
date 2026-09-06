"""Tests hypothèses fiscales (settings) + câblage estimation (v2026.09.032).

Les hypothèses du foyer (taux marginaux, imposition collective, AV 150 k€,
détention substantielle LU) vivent dans la table settings PAR MEMBRE — base
principale pour les standard, coffre pour les protected (routage db()).
La route /api/tax-estimate les applique ; opt/sub surchargent par appel.
Inclut le round-trip export/import JSON : tax_country ET settings survivent
(le bug : l'import accounts omettait tax_country → perte silencieuse)."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pat-settings-")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import src.app as app  # noqa: E402


def _login(c, user, pwd):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r


def _mk_user(c, username, mode="standard", pwd="member-pass-2026-long"):
    r = c.post("/api/family", json={"username": username, "password": pwd, "mode": mode})
    assert r.status_code == 200, r.text


def test_settings_defaults_puis_crud():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    r = c.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    assert d["tax_tmi_lu"] == 42.8 and d["tax_tmi_fr"] == 0.0
    assert d["tax_married"] == 0 and d["tax_av_150k"] == 1 and d["tax_substantial"] == 0
    r = c.put("/api/settings", json={"tax_tmi_lu": 39.0, "tax_tmi_fr": 30.0,
                                     "tax_married": 1, "tax_av_150k": 0,
                                     "tax_substantial": 1})
    assert r.status_code == 200, r.text
    d = c.get("/api/settings").json()
    assert d["tax_tmi_lu"] == 39.0 and d["tax_tmi_fr"] == 30.0
    assert d["tax_married"] == 1 and d["tax_av_150k"] == 0 and d["tax_substantial"] == 1


def test_settings_validations_et_isolation():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    assert c.put("/api/settings", json={"tax_tmi_lu": 150}).status_code == 400
    assert c.put("/api/settings", json={"tax_tmi_fr": -5}).status_code == 400
    assert c.put("/api/settings", json={"tax_married": 2}).status_code == 400
    _mk_user(c, "set-iso", pwd="member-pass-2026-long")
    c.post("/api/auth/logout")
    _login(c, "set-iso", "member-pass-2026-long")
    d = c.get("/api/settings").json()
    assert d["tax_married"] == 0  # les réglages d'admin ne fuient pas
    c.put("/api/settings", json={"tax_married": 1})
    c.post("/api/auth/logout")
    _login(c, "admin", "admin-test-2026")
    assert c.get("/api/settings").json()["tax_married"] == 1


def _mk_asset(c, name, cls, country, wrapper, cost, value, odate, ccy="EUR"):
    r = c.post("/api/accounts", json={
        "name": name, "asset_class": cls, "currency": ccy, "cost_basis": cost,
        "open_date": odate, "wrapper": wrapper, "tax_country": country,
        "initial_value": value})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_estimation_applique_les_settings_et_les_surcharges():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    # état settings propre (les tests précédents partagent l'admin)
    c.put("/api/settings", json={"tax_tmi_lu": 42.8, "tax_tmi_fr": 0.0,
                                 "tax_married": 0, "tax_av_150k": 1,
                                 "tax_substantial": 0})
    # LU titres exo par défaut ; sub=1 → demi-taux (settings substantial=0)
    # PV 80 000 > abattement 50 k€ : la participation devient imposable
    aid = _mk_asset(c, "LU exo", "bourse", "lu", "cto", 100000, 180000, "2019-04-10")
    r = c.get(f"/api/tax-estimate?account_id={aid}")
    assert r.json()["taxable_gain"] == 0.0  # exonération < 10 % / > 6 mois
    r = c.get(f"/api/tax-estimate?account_id={aid}&sub=1")
    j = r.json()
    assert j["regime"] == "titres" and j["taxable_gain"] > 0
    # demi-taux plafonné 21,4 % sur (80 000 − 50 000) = 30 000 → 6 420
    assert abs(j["income_tax"] - 6420.0) < 0.01
    # marié (settings) → abattement 100 k€ : PV 80 000 < abattement → 0 d'IR
    c.put("/api/settings", json={"tax_married": 1})
    r = c.get(f"/api/tax-estimate?account_id={aid}&sub=1")
    assert r.json()["income_tax"] == 0.0
    # tmi_lu réglé à 30 % : la spéculation < 6 mois l'utilise
    from datetime import date, timedelta
    c.put("/api/settings", json={"tax_married": 0, "tax_tmi_lu": 30.0})
    rec = (date.today() - timedelta(days=30)).isoformat()  # toujours < 6 mois
    aid2 = _mk_asset(c, "LU spécul", "bourse", "lu", "cto", 100000, 142800,
                     odate=rec)
    j = c.get(f"/api/tax-estimate?account_id={aid2}").json()
    # 30 % + fonds 7 % + CADEP 1,4 % sur 42 800
    assert abs(j["income_tax"] - 12840.0) < 0.01
    assert abs(j["extra_tax"] - 898.80) < 0.01
    assert abs(j["social_contributions"] - 599.20) < 0.01
    # CTO FR : option 2OP sans tmi_fr → refus propre (not_estimated)
    aid3 = _mk_asset(c, "CTO FR", "bourse", "fr", "cto", 100000, 142800, "2020-01-10")
    j = c.get(f"/api/tax-estimate?account_id={aid3}&opt=2op").json()
    assert j["regime"] == "not_estimated"
    assert "PROGRESSIVE_NO_TMI" in j["warnings"]
    # option 2OP avec tmi_fr 30 % : titres après 2018 → pas d'abattement durée
    c.put("/api/settings", json={"tax_tmi_fr": 30.0})
    j = c.get(f"/api/tax-estimate?account_id={aid3}&opt=2op").json()
    assert abs(j["income_tax"] - 12840.0) < 0.01  # 42 800 × 30 %
    assert abs(j["social_contributions"] - 7960.80) < 0.01  # 18,6 % inchangés
    # opt invalide → 400
    assert c.get(f"/api/tax-estimate?account_id={aid3}&opt=9zz").status_code == 400


def test_settings_protected_vivant_dans_le_coffre():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    _mk_user(c, "set-vault", mode="protected", pwd="vault-pass-2026-long")
    # mdp initial jetable : must_change → changer puis init coffre
    r = c.post("/api/auth/login", json={"username": "set-vault", "password": "vault-pass-2026-long"})
    assert r.status_code == 200
    r = c.post("/api/auth/password", json={
        "current": "vault-pass-2026-long", "new": "vault-pass-2026-long"})
    assert r.status_code == 200, r.text
    # init coffre (le flux front envoie dek wrapped par le mdp + sel)
    import base64 as _b64
    r = c.post("/api/vault/init", json={
        "salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==", "dek": _b64.b64encode(b"k" * 32).decode()})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, "set-vault", "vault-pass-2026-long")
    r = c.post("/api/vault/open", json={
        "dek": _b64.b64encode(b"k" * 32).decode()})
    assert r.status_code == 200, r.text
    c.put("/api/settings", json={"tax_tmi_lu": 33.0})
    assert c.get("/api/settings").json()["tax_tmi_lu"] == 33.0
    # la base principale ne contient AUCUN settings du protected
    import sqlite3
    conn = sqlite3.connect(os.path.join(app.DATA_DIR, "app.db"))
    n = conn.execute("SELECT COUNT(*) FROM settings WHERE member='set-vault'").fetchone()[0]
    conn.close()
    assert n == 0


def test_export_import_preserve_tax_country_et_settings():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    c.put("/api/settings", json={"tax_tmi_lu": 41.0, "tax_married": 1})
    aid = _mk_asset(c, "CTO round-trip", "bourse", "fr", "cto", 50000, 60000, "2021-01-01")
    exp = c.get("/api/export").json()
    acc = next(a for a in exp["accounts"] if a["id"] == aid)
    assert acc["tax_country"] == "fr"
    assert {"key": "tax_tmi_lu", "value": 41.0} in exp["settings"]
    # wipe + restore
    c.delete(f"/api/accounts/{aid}")
    r = c.post("/api/import", json=exp)
    assert r.status_code == 200, r.text
    accs = c.get("/api/accounts").json()["accounts"]
    back = next(a for a in accs if a["name"] == "CTO round-trip")
    assert back["tax_country"] == "fr"  # le bug v031 : perdu à l'import
    assert c.get("/api/settings").json()["tax_tmi_lu"] == 41.0
