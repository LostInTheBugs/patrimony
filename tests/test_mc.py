"""Monte-Carlo FIRE par bootstrap (v2026.09.042) :
- fenêtres glissantes de 12 mois calendaires (make_blocks) ;
- fire.simulate(returns=chemin constant) ≡ simulate(returns=None) — la
  trajectoire bootstrapée ne change pas le moteur déterministe ;
- déterminisme : même seed → même résultat ; réussite décroissante avec
  l'horizon ; dépenses nulles → 100 % ; retraits écrasants → épuisement ;
- route /api/fire/montecarlo : 401/400, fetch mocké (série synthétique),
  cache mc:<key> réutilisé sans re-fetch.
"""
import datetime
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-mc-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app
import src.fire as fire
import src.mc as mc

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
    _login(c, user=name, pwd=PWD)
    return c


# ---------------------------------------------------------------- make_blocks

def test_make_blocks_fenetres_12_rendements():
    """Une fenêtre de 12 mois = 12 rendements mensuels = [ym, ym+12 mois) ;
    elle ne compte que si ses DEUX extrémités existent."""
    lvl = {"2020-01": 100.0, "2021-01": 112.0,   # fenêtre 2020-01 → 2021-01 : +12 %
           "2020-12": 110.0}                      # 2020-12 → 2021-12 absent : rien
    blocks = mc.make_blocks(lvl)
    assert len(blocks) == 1 and round(blocks[0], 6) == 0.12
    # extrémité manquante → fenêtre ignorée
    lvl2 = {"2020-01": 100.0, "2020-12": 110.0}
    assert mc.make_blocks(lvl2) == []


def test_make_blocks_rendement_croissant_reel():
    lvl = {}
    for n in range(300):  # 25 ans de mois
        y, m = divmod(n + 2001 * 12, 12)
        lvl[f"{y:04d}-{m + 1:02d}"] = 100.0 * (1.005 ** n)
    blocks = mc.make_blocks(lvl)
    assert len(blocks) == 300 - 12  # toutes les fenêtres de 12 mois
    assert abs(blocks[0] - (1.005 ** 12 - 1.0)) < 0.002  # ~6,17 %/an


# ---------------------------------------------------------------- simulate

def test_fire_simulate_returns_constant_equivaut_defaut():
    args = dict(principal=200_000, savings_year=12_000, expenses_year=30_000,
                pension_year=0, return_pct=5.0, inflation_pct=2.0,
                swr_pct=4.0, max_years=40)
    base = fire.simulate(**args)
    path = [0.05] * 40  # 5 % nominal chaque année
    alt = fire.simulate(**args, returns=path)
    assert alt["rows"] == base["rows"]
    assert alt["exhausted"] == base["exhausted"] and alt["fire"] == base["fire"]


def test_mc_deterministe_et_monotone(admin_c):
    blocks = [0.05 + 0.10 * (i % 5) for i in range(120)]  # 120 fenêtres connues
    a = mc.simulate(principal=200_000, savings_year=12_000, expenses_year=30_000,
                    pension_year=0, return_pct=5.0, inflation_pct=2.0,
                    swr_pct=4.0, max_years=50, blocks=blocks, n_sims=300, seed=42)
    b = mc.simulate(principal=200_000, savings_year=12_000, expenses_year=30_000,
                    pension_year=0, return_pct=5.0, inflation_pct=2.0,
                    swr_pct=4.0, max_years=50, blocks=blocks, n_sims=300, seed=42)
    assert a == b  # même seed → résultat identique
    assert a["seed_used"] == 42 and a["n_sims"] == 300
    pcts = [h["success_pct"] for h in a["horizons"]]
    assert pcts == sorted(pcts, reverse=True)  # monotone décroissant
    assert 0 <= a["exhausted_pct"] <= 100
    # dépenses nulles → toujours 100 % et jamais épuisé
    ok = mc.simulate(principal=10_000, savings_year=0, expenses_year=0,
                     pension_year=0, return_pct=5.0, inflation_pct=2.0,
                     swr_pct=4.0, max_years=30, blocks=[0.05, -0.2, 0.3],
                     n_sims=100, seed=1)
    assert all(h["success_pct"] == 100.0 for h in ok["horizons"])
    assert ok["exhausted_pct"] == 0.0
    # retraits écrasants → échec quasi certain, capital médian nul
    # swr très élevé : la cible (dépenses/swr) est atteinte d'emblée → phase
    # retraite immédiate → les retraits écrasent le capital (test unitaire pur,
    # hors plages de la route)
    bad = mc.simulate(principal=100_000, savings_year=0,
                      expenses_year=80_000, pension_year=0, return_pct=5.0,
                      inflation_pct=2.0, swr_pct=80.0, max_years=30,
                      blocks=[0.02, -0.10, 0.05], n_sims=200, seed=7)
    assert bad["exhausted_pct"] > 50
    assert bad["p50_capital_end"] == 0.0
    assert bad["median_exhaustion_t"] is not None


# ---------------------------------------------------------------- route

def _fake_series(months=308, start=2001):
    """Série mensuelle synthétique croissante (2001-01 → …)."""
    pts = []
    for n in range(months):
        y, m = divmod(start * 12 + n, 12)
        pts.append((f"{y:04d}-{m + 1:02d}-15", round(100.0 * (1.005 ** n), 4)))
    return {"price": pts[-1][1], "currency": "EUR", "points": pts}


def test_route_mc_guards_and_cache(admin_c, monkeypatch):
    anon = TestClient(app.app)
    assert anon.get("/api/fire/montecarlo").status_code == 401
    c = _member(admin_c, "mc-user")
    # n_sims hors plage → 400 avant tout fetch
    r2 = c.get("/api/fire/montecarlo", params={"principal": 200000,
                                               "savings_month": 1000,
                                               "expenses_month": 2500,
                                               "n_sims": 10})
    assert r2.status_code == 400
    calls = {"n": 0}
    real = app._yahoo_chart

    def fake_chart(symbol, rng="1d", interval="1d"):
        calls["n"] += 1
        assert symbol == "IWDA.L" and rng == "max" and interval == "1mo"
        return _fake_series()

    monkeypatch.setattr(app, "_yahoo_chart", fake_chart)
    r3 = c.get("/api/fire/montecarlo", params={"principal": 200000,
                                               "savings_month": 1000,
                                               "expenses_month": 2500,
                                               "inflation_pct": 2.0,
                                               "swr_pct": 4.0,
                                               "return_pct": 5.0,
                                               "max_years": 30,
                                               "n_sims": 100,
                                               "seed": 42})
    assert r3.status_code == 200, r3.text
    j = r3.json()
    assert j["index"] == "iwda" and j["shallow"] is False
    assert j["series"]["blocks"] == 308 - 12
    assert [h["t"] for h in j["horizons"]] == [10, 20, 30]
    assert all(0 <= h["success_pct"] <= 100 for h in j["horizons"])
    assert j["seed_used"] == 42 and j["n_sims"] == 100
    # 2e appel : cache mc:iwda assez profond/récent → AUCUN re-fetch
    r4 = c.get("/api/fire/montecarlo", params={"principal": 200000,
                                               "savings_month": 1000,
                                               "expenses_month": 2500,
                                               "inflation_pct": 2.0,
                                               "swr_pct": 4.0,
                                               "return_pct": 5.0,
                                               "max_years": 30,
                                               "n_sims": 100,
                                               "seed": 42})
    assert r4.status_code == 200 and calls["n"] == 1
    assert r4.json() == j  # même seed → même résultat
    # index inconnu → 400 ; clé réservée mc:iwda présente en base
    assert c.get("/api/fire/montecarlo", params={"index": "nope"}).status_code == 400
    conn = app.db_main()
    n = conn.execute("SELECT COUNT(*) c FROM index_levels WHERE key='mc:iwda'").fetchone()["c"]
    conn.close()
    assert n == 308
    # la série mc:iwda est INVISIBLE du comparateur de benchmarks (clés bench)
    b = c.get("/api/benchmarks").json()
    assert all(bench["key"] != "mc:iwda" for bench in b["benchmarks"])


def test_route_mc_sans_reseau_502(admin_c, monkeypatch):
    c = _member(admin_c, "mc-offline")
    monkeypatch.setattr(app, "_yahoo_chart", lambda *a, **k: None)
    # sp500 : clé mc:sp500 jamais peuplée dans ce process → fetch requis → None
    r = c.get("/api/fire/montecarlo", params={"principal": 200000,
                                              "savings_month": 1000,
                                              "expenses_month": 2500,
                                              "index": "sp500"})
    assert r.status_code == 502
