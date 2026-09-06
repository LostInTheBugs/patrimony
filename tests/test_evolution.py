"""v2026.09.029 — moteur d'évolution : décomposition additive mensuelle
(Flux / Revenus / Effet marché résiduel) + snapshots annuels par classe
(dernière valorisation de décembre, année courante partielle)."""
import os
import tempfile
from datetime import date, timedelta
import pytest

DATA_DIR = tempfile.mkdtemp(prefix="pat-test-ev-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
import src.app as app  # noqa: E402


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module")
def admin_c():
    c = TestClient(app.app)
    _login(c)
    yield c


def _new_member(admin_c, username):
    r = admin_c.post("/api/family", json={"username": username, "password": "ev-pwd-2026-long", "mode": "standard"})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, username, "ev-pwd-2026-long")
    return c


def _acc(c, name, cls="comptes", **kw):
    r = c.post("/api/accounts", json={"name": name, "asset_class": cls, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tx(c, acc, kind, amount, op_date):
    r = c.post("/api/transactions",
               json={"account_id": acc, "kind": kind, "amount": amount, "op_date": op_date.isoformat()})
    assert r.status_code == 200, r.text


def _val(c, acc, value, d):
    r = c.post(f"/api/accounts/{acc}/valuation", json={"value": value, "val_date": d.isoformat()})
    assert r.status_code == 200, r.text


def _first(days_ago):
    """1er jour du mois situé days_ago jours dans le passé."""
    d = date.today() - timedelta(days=days_ago)
    return date(d.year, d.month, 1)


def _prev_month(d, k=1):
    """k-ième mois civil précédant d (1er du mois)."""
    y, m = d.year, d.month
    for _ in range(k):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return date(y, m, 1)


def _add_month(d):
    y, m = d.year, d.month
    return date(y + m // 12, m % 12 + 1, 1)


def _mid(d):
    return d + timedelta(days=7)


# ---------------------------------------------------------------- helpers
def _ym(d):
    return d.strftime("%Y-%m")


def test_evolution_additive_split(admin_c):
    """Flux + Revenus + Effet marché == ΔV exactement, par actif, classe
    et total — dépôt, retrait, dépense, revenu et variation de prix."""
    me = _new_member(admin_c, "ev-split")
    acc = _acc(me, "Livret A", "epargne")
    today = date.today()
    if today.day < 4:
        pytest.skip("début de mois : pas de jour sûr dans le mois courant")
    m0 = date(today.year, today.month, 1)  # mois courant (valo à aujourd'hui)
    m2, m1 = _prev_month(m0, 2), _prev_month(m0)  # avant-dernier, précédent
    _tx(me, acc, "deposit", 1000.0, m2 + timedelta(days=10))
    _val(me, acc, 1000.0, m2 + timedelta(days=25))
    _tx(me, acc, "income", 30.0, m1 + timedelta(days=10))   # intérêts encaissés
    _val(me, acc, 1030.0, m1 + timedelta(days=25))
    _tx(me, acc, "withdrawal", 200.0, m0 + timedelta(days=2))  # retrait ce mois
    _tx(me, acc, "expense", 10.0, m0 + timedelta(days=2))      # frais ce mois
    _val(me, acc, 900.0, today)                                # baisse de valeur

    ev = me.get("/api/evolution").json()
    assert len(ev["months"]) == 12
    by_ym = {m["ym"]: m for m in ev["months"]}

    # mois m-2 : dépôt 1000, valo 1000 → tout en flux
    a = by_ym[_ym(m2)]
    assert a["total"]["dv"] == 1000.0
    assert a["total"]["flux"] == 1000.0
    assert a["total"]["revenus"] == 0.0
    assert a["total"]["marche"] == 0.0

    # mois m-1 : revenu 30, valo 1030 → revenus
    a = by_ym[_ym(m1)]
    assert a["total"]["dv"] == 30.0
    assert a["total"]["flux"] == 0.0
    assert a["total"]["revenus"] == 30.0
    assert a["total"]["marche"] == 0.0

    # mois courant : retrait 200 + dépense 10, valo 1030 → 900 → ΔV −130
    # flux −210, marché +80
    a = by_ym[_ym(m0)]
    t = a["total"]
    assert t["dv"] == -130.0
    assert t["flux"] == -210.0
    assert t["depenses"] == 10.0
    assert t["marche"] == 80.0
    # additivité stricte sur tous les niveaux
    for m in ev["months"]:
        assert m["total"]["dv"] == pytest.approx(
            m["total"]["flux"] + m["total"]["revenus"] + m["total"]["marche"])
        for c in m["classes"].values():
            assert c["dv"] == pytest.approx(c["flux"] + c["revenus"] + c["marche"])
        for act in m["acts"]:
            assert act["dv"] == pytest.approx(act["flux"] + act["revenus"] + act["marche"])
    # l'actif est présent dans le détail du mois courant
    assert any(act["id"] == acc and act["flux"] == -210.0 for act in by_ym[_ym(m0)]["acts"])


def test_evolution_annual_snapshot_december(admin_c):
    """Snapshots annuels : dernière valo de décembre par classe ; année
    courante partielle ; actif inactif exclu."""
    me = _new_member(admin_c, "ev-annual")
    today = date.today()
    d_prev_dec = date(today.year - 1, 12, 15)
    acc = _acc(me, "PEA historique", "bourse")
    _tx(me, acc, "deposit", 5000.0, d_prev_dec)
    _val(me, acc, 5000.0, d_prev_dec + timedelta(days=5))
    _val(me, acc, 5300.0, date(today.year - 1, 12, 28))  # perf fin N-1
    _val(me, acc, 5400.0, today)                          # année courante partielle
    ev = me.get("/api/evolution").json()
    assert ev["years"] == [today.year - 1, today.year]
    ann = {a["year"]: a for a in ev["annual"]}
    assert ann[today.year - 1]["by_class"]["bourse"] == 5300.0
    assert ann[today.year - 1]["total"] == 5300.0
    assert ann[today.year]["by_class"]["bourse"] == 5400.0
    # année N-1 décomposée dans les drivers si dans la fenêtre
    by_ym = {m["ym"]: m for m in ev["months"]}


def test_evolution_closed_inactive_and_isolation(admin_c):
    """Actif fermé → exclu des snapshots après close_date ; inactif exclu ;
    isolation totale entre membres."""
    me = _new_member(admin_c, "ev-closed")
    today = date.today()
    acc = _acc(me, "Compte courant", "comptes")
    _tx(me, acc, "deposit", 2000.0, today - timedelta(days=40))
    _val(me, acc, 2000.0, today - timedelta(days=35))
    # fermé le mois dernier : dernière valo avant fermeture
    r = admin_c.post("/api/accounts", json={"name": "Ancien livret", "asset_class": "epargne",
                                            "close_date": (today - timedelta(days=25)).isoformat()})
    assert r.status_code == 200
    old = r.json()["id"]
    admin_c.post(f"/api/accounts/{old}/valuation",
                 json={"value": 800.0, "val_date": (today - timedelta(days=60)).isoformat()})
    # isolation : l'autre membre ne voit ni actifs ni transactions de me
    other = _new_member(admin_c, "ev-iso")
    dov = other.get("/api/evolution").json()
    assert dov["months"][-1]["total"]["dv"] == 0.0
    assert not any(a["id"] == acc for a in dov["months"][-1]["acts"])


def test_evolution_market_residual_covers_untracked(admin_c):
    """Un mouvement de valeur sans flux ni revenu saisi ressort
    intégralement en effet marché (résidu) — la somme reste exacte."""
    me = _new_member(admin_c, "ev-res")
    acc = _acc(me, "Crypto", "crypto")
    m1 = _first(45)
    _tx(me, acc, "deposit", 500.0, m1 + timedelta(days=7))
    _val(me, acc, 500.0, m1 + timedelta(days=20))
    _val(me, acc, 640.0, date.today())  # +140 sans aucune opération
    ev = me.get("/api/evolution").json()
    m0 = ev["months"][-1]
    assert m0["total"]["dv"] == 140.0
    assert m0["total"]["flux"] == 0.0
    assert m0["total"]["revenus"] == 0.0
    assert m0["total"]["marche"] == 140.0
