"""Tests de la route GET /api/tax-estimate (v2026.09.031).

Le moteur pur est testé dans test_tax_engine.py ; ici : câblage route —
profil de l'actif (coût effectif, valo, pays, wrapper), erreurs propres,
isolation par membre.
"""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pat-taxapi-")
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


def _mk_member(admin_c, username, mode="standard"):
    r = admin_c.post("/api/family", json={
        "username": username,
        "password": "ta-pwd-2026-long",
        "mode": mode,
    })
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, username, "ta-pwd-2026-long")
    return c


def _mk_asset(c, name, cls="bourse", country="fr", wrapper=None,
              cost=100000.0, value=142800.0, open_date="2020-01-10"):
    r = c.post("/api/accounts", json={
        "name": name, "asset_class": cls, "cost_basis": cost,
        "open_date": open_date, "wrapper": wrapper, "tax_country": country,
        "initial_value": value,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


# la fixture admin duplique le pattern des autres fichiers de tests :
# l'admin réel est défini par l'env (1er import alphabétique partagé)
def _setup_admin():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    return c


admin_c = None


def test_estimation_cto_fr_complete():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    c = _mk_member(admin_c, "ta-cto")
    aid = _mk_asset(c, "CTO test", wrapper="cto")
    r = c.get(f"/api/tax-estimate?account_id={aid}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["regime"] == "cto"
    assert d["ruleset_version"] == "FR-2026"
    assert d["gross_gain"] == 42800.0
    assert d["income_tax"] == round(42800 * 0.128, 2)
    assert d["estimated_net_gain"] == round(42800 * (1 - 0.314), 2)
    ids = [l["id"] for l in d["lines"]]
    assert "FR_CTO_PFU_IR_2026" in ids
    assert any(l["pct"] == 18.6 for l in d["lines"])


def test_estimation_pea_exonere_avec_warning():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    c = _mk_member(admin_c, "ta-pea")
    aid = _mk_asset(c, "PEA test", wrapper="pea", open_date="2016-01-10")
    d = c.get(f"/api/tax-estimate?account_id={aid}").json()
    assert d["income_tax"] == 0.0
    assert "FR_PEA_PS_HISTORICAL" in d["warnings"]


def test_erreurs_propres():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    c = _mk_member(admin_c, "ta-err")
    # pays non renseigné → 400 avec message
    aid = _mk_asset(c, "Sans pays", country="")
    r = c.get(f"/api/tax-estimate?account_id={aid}")
    assert r.status_code == 400
    assert "Pays fiscal" in r.json()["detail"]
    # aucune valorisation → 400
    r2 = c.post("/api/accounts", json={
        "name": "Sans valo", "asset_class": "bourse", "cost_basis": 1000,
        "tax_country": "fr", "wrapper": "cto", "open_date": "2020-01-01",
    })
    aid2 = r2.json()["id"]
    r3 = c.get(f"/api/tax-estimate?account_id={aid2}")
    assert r3.status_code == 400
    # année fiscale non versionnée → 400
    r4 = c.get(f"/api/tax-estimate?account_id={aid}&year=2025")
    assert r4.status_code == 400
    # classe hors périmètre → 200 regime not_estimated (jamais silencieux)
    aid5 = _mk_asset(c, "Or", cls="metaux", cost=1000.0, value=1500.0)
    d5 = c.get(f"/api/tax-estimate?account_id={aid5}").json()
    assert d5["regime"] == "not_estimated"
    assert "NOT_ESTIMATED" in d5["warnings"]
    # actif inexistant → 404
    assert c.get("/api/tax-estimate?account_id=99999").status_code == 404


def test_isolation_entre_membres():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    a = _mk_member(admin_c, "ta-iso-a")
    b = _mk_member(admin_c, "ta-iso-b")
    aid = _mk_asset(a, "Actif de A")
    r = b.get(f"/api/tax-estimate?account_id={aid}")
    assert r.status_code == 404


def test_pays_invalide_refuse_400():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    c = _mk_member(admin_c, "ta-pays")
    r = c.post("/api/accounts", json={
        "name": "Pays faux", "asset_class": "bourse",
        "tax_country": "de", "open_date": "2020-01-10",
    })
    assert r.status_code == 400


def test_actif_non_eur_warning_devise():
    global admin_c
    if admin_c is None:
        admin_c = _setup_admin()
    c = _mk_member(admin_c, "ta-usd")
    r = c.post("/api/accounts", json={
        "name": "CTO USD", "asset_class": "bourse", "currency": "USD",
        "cost_basis": 100000, "tax_country": "fr", "wrapper": "cto",
        "open_date": "2020-01-10", "initial_value": 142800,
    })
    aid = r.json()["id"]
    d = c.get(f"/api/tax-estimate?account_id={aid}").json()
    assert "FX_DEVISE_NON_EUR" in d["warnings"]
