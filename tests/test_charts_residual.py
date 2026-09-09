"""Tests v2026.09.060 — graphiques par page (filtre ids de /api/history) +
valeurs résiduelles (argus véhicule) + estimation immo (surface × prix/m²).

Design claude/design-charts-residual-2026.md : saisie MANUELLE annuelle de
l'argus (net_to_date = cash-out − valeur résiduelle, badge stale > 12 mois) ;
estimation immo toujours PROPOSÉE (jamais une valuation) ; ?ids= série SOMME
des comptes demandés (l'UI des pages dédiées), sans ids = comportement
historique inchangé.

Isolation : membres dédiés par test (préfixe `cr` — base partagée par toute
la suite, cf. conventions).
"""

import base64
import os
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

PWD = "residual-pass-2026-long"


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


def _acct(c, aid):
    r = c.get("/api/accounts")
    assert r.status_code == 200
    return next(a for a in r.json()["accounts"] if a["id"] == aid)


def _mk_item(c, kind="vehicle", label="Voiture", **kw):
    body = {"kind": kind, "label": label, "purchase_date": "2024-02-15",
            "purchase_price": 40000}
    body.update(kw)
    r = c.post("/api/tco/items", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tco(c):
    r = c.get("/api/tco/overview")
    assert r.status_code == 200
    return r.json()["items"]


def _item(c, iid):
    return next(i for i in _tco(c) if i["id"] == iid)


def test_account_area_price_stored_and_estimated():
    c = TestClient(app.app)
    tag = _mk_member(c, "cr_meta")
    aid = _mk_asset(c, "Bien m²", value=170000)
    # PUT sans les champs (formulaire antérieur) → les valeurs sont CONSERVÉES
    body = {"name": "Bien m²", "asset_class": "immobilier", "institution": "",
            "currency": "EUR", "cost_basis": 145000, "fx_override": None,
            "open_date": "2021-03-01", "notes": "", "active": 1,
            "valuation_mode": "manual", "symbol": "", "quantity": 0,
            "wrapper": None, "tax_country": "", "loan_principal": 0,
            "loan_rate": 0, "loan_monthly": 0}
    r = c.put(f"/api/accounts/{aid}", json=body)
    assert r.status_code == 200, r.text
    p = _acct(c, aid)
    assert p["area_m2"] is None and p["price_m2"] is None
    # saisie : surface + prix au m² → estimation exposée (PAS une valuation)
    r = c.put(f"/api/accounts/{aid}", json={**body, "area_m2": 65,
                                            "price_m2": 2800})
    assert r.status_code == 200, r.text
    p = _acct(c, aid)
    assert p["area_m2"] == 65 and p["price_m2"] == 2800
    assert p["estimated"] == 182000.0
    assert p["last_value"] != 182000.0  # l'estimation ne valorise PAS d'office
    # modification : un seul champ suffit (le formulaire complet est envoyé)
    r = c.put(f"/api/accounts/{aid}", json={**body, "area_m2": 70,
                                            "price_m2": 3000})
    assert r.status_code == 200, r.text
    p = _acct(c, aid)
    assert p["estimated"] == 210000.0
    # effacement explicite
    r = c.put(f"/api/accounts/{aid}", json={**body, "area_m2": None,
                                            "price_m2": None})
    assert r.status_code == 200, r.text
    assert _acct(c, aid)["estimated"] is None


def test_account_area_price_guards():
    c = TestClient(app.app)
    _mk_member(c, "cr_guards")
    # réservé à la classe immobilier
    r = c.post("/api/accounts", json={"name": "Livret", "asset_class": "epargne",
                                      "area_m2": 50})
    assert r.status_code == 400 and "immobiliers" in r.json()["detail"]
    # valeurs non positives refusées
    r = c.post("/api/accounts", json={"name": "Bien", "asset_class": "immobilier",
                                      "area_m2": -5})
    assert r.status_code == 400
    r = c.post("/api/accounts", json={"name": "Bien", "asset_class": "immobilier",
                                      "area_m2": 65, "price_m2": 0})
    assert r.status_code == 400
    r = c.post("/api/accounts", json={"name": "Bien", "asset_class": "immobilier",
                                      "price_m2": "abc"})
    assert r.status_code == 422  # rejet Pydantic (type), avant la garde


def test_history_ids_filter():
    c = TestClient(app.app)
    _mk_member(c, "cr_hist")
    today = date.today().isoformat()
    a = _mk_asset(c, "Livret A", cls="epargne", value=5000)
    b = _mk_asset(c, "Livret B", cls="epargne", value=3000)
    x = _mk_asset(c, "Livret X", cls="epargne", value=999)
    for aid, v in ((a, 5000), (b, 3000), (x, 999)):
        r = c.post(f"/api/accounts/{aid}/valuation",
                   json={"value": v, "val_date": today})
        assert r.status_code == 200, r.text
    # filtre : somme des comptes DEMANDÉS uniquement (dernier point = courant)
    r = c.get(f"/api/history?ids={a},{b}&months=6")
    assert r.status_code == 200
    d = r.json()
    assert "values" in d and "series" not in d  # contrat page dédiée
    assert d["current"] == 8000.0
    assert d["values"][-1] == 8000.0
    assert len(d["labels"]) == 6
    # un seul compte
    r = c.get(f"/api/history?ids={x}&months=6")
    assert r.json()["current"] == 999.0
    # ids inconnus / invalides → 200 avec une courbe vide (jamais d'erreur)
    r = c.get(f"/api/history?ids={a},424242&months=6")
    assert r.status_code == 200 and r.json()["current"] == 5000.0
    r = c.get("/api/history?ids=abc&months=6")
    assert r.status_code == 200 and r.json()["current"] == 0
    # sans ids : contrat historique inchangé (agrégation par classe)
    r = c.get("/api/history?months=6")
    d = r.json()
    assert "series" in d and "totals" in d
    assert d["current"] == 8999.0  # les 3 livrets


def test_resale_stored_net_stale():
    c = TestClient(app.app)
    _mk_member(c, "cr_resale")
    iid = _mk_item(c, label="Tesla", resale_value=28000,
                   resale_date="2025-06-30")
    it = _item(c, iid)
    assert it["resale_value"] == 28000
    assert it["resale_date"] == "2025-06-30"
    # 15 mois au 2026-09 → rappel doux + coût net = 40 000 − 28 000
    assert it["resale_months"] == 15
    assert it["resale_stale"] is True
    assert it["costs"]["total_to_date"] == 40000.0
    assert it["net_to_date"] == 12000.0
    # estimation récente → pas de stale, net inchangé dans sa logique
    iid2 = _mk_item(c, label="Clio", resale_value=9000,
                    resale_date="2026-08-01")
    it2 = _item(c, iid2)
    assert it2["resale_months"] == 1 and it2["resale_stale"] is False
    assert it2["net_to_date"] == 31000.0
    # PUT sans les champs resale (UI antérieure) → valeur CONSERVÉE
    body = {"kind": "vehicle", "label": "Tesla", "account_id": None,
            "loan_id": None, "purchase_date": "2024-02-15",
            "purchase_price": 40000, "active": 1, "notes": ""}
    r = c.put(f"/api/tco/items/{iid}", json=body)
    assert r.status_code == 200, r.text
    assert _item(c, iid)["resale_value"] == 28000
    # effacement explicite → net disparaît
    r = c.put(f"/api/tco/items/{iid}",
              json={**body, "resale_value": None, "resale_date": None})
    assert r.status_code == 200, r.text
    it = _item(c, iid)
    assert it["resale_value"] is None and it["net_to_date"] is None


def test_resale_guards():
    c = TestClient(app.app)
    _mk_member(c, "cr_guards2")
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "X",
                                       "resale_value": -1})
    assert r.status_code == 400
    r = c.post("/api/tco/items", json={"kind": "vehicle", "label": "X",
                                       "resale_date": "2024-13-01"})
    assert r.status_code == 400
    # valeur résiduelle sur une fiche immo : autorisée (éventuelle revente)
    # mais la garde de fiche existante s'applique — on reste sur la forme
    aid = _mk_asset(c, "Bien revente")
    r = c.post("/api/tco/items", json={"kind": "immo", "label": "Bien",
                                       "account_id": aid,
                                       "resale_value": 250000})
    assert r.status_code == 200, r.text


def test_resale_meta_unit():
    t = date(2026, 9, 9)
    assert estate.resale_meta(None) == (None, False)
    assert estate.resale_meta("2025-06-30", today=t) == (15, True)
    assert estate.resale_meta("2026-08-01", today=t) == (1, False)
    assert estate.resale_meta("2026-12-01", today=t) == (0, False)  # futur borné
    assert estate.resale_meta("pas-une-date", today=t) == (None, False)


def test_apply_estimation_flow():
    """Appliquer l'estimation = valuation manuelle datée du jour (le mécanisme
    de la page Actifs), jamais une application automatique."""
    c = TestClient(app.app)
    _mk_member(c, "cr_apply")
    aid = _mk_asset(c, "Bien est.", value=170000)
    body = {"name": "Bien est.", "asset_class": "immobilier", "institution": "",
            "currency": "EUR", "cost_basis": 145000, "fx_override": None,
            "open_date": "2021-03-01", "notes": "", "active": 1,
            "valuation_mode": "manual", "symbol": "", "quantity": 0,
            "wrapper": None, "tax_country": "", "loan_principal": 0,
            "loan_rate": 0, "loan_monthly": 0,
            "area_m2": 65, "price_m2": 2800}
    assert c.put(f"/api/accounts/{aid}", json=body).status_code == 200
    est = _acct(c, aid)["estimated"]
    r = c.post(f"/api/accounts/{aid}/valuation",
               json={"value": est, "note": "Estimation indicative (65 m² × 2 800 €/m²)"})
    assert r.status_code == 200, r.text
    assert _acct(c, aid)["last_value"] == est


def test_ghost_and_overview_shapes():
    """Les nouveaux champs ne cassent aucune forme existante (fiche immo liée
    et bien sans fiche = ghost, avec les champs resale à None)."""
    c = TestClient(app.app)
    _mk_member(c, "cr_shapes")
    aid = _mk_asset(c, "Bien lié", value=172000)
    aid2 = _mk_asset(c, "Bien sans fiche", value=120000)
    iid = _mk_item(c, kind="immo", label="Bien lié", account_id=aid)
    it = _item(c, iid)
    assert it["resale_value"] is None and it["net_to_date"] is None
    assert it["resale_stale"] is False
    ghosts = [i for i in _tco(c) if i["id"] is None]
    assert len(ghosts) == 1 and ghosts[0]["account_id"] == aid2
    g = ghosts[0]
    assert "resale_value" in g and "net_to_date" in g and g["net_to_date"] is None
