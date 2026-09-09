"""Tests v2026.09.064 — simulateurs élargis (demande Fred 2026-09-09) :
projection classique (intérêts composés mensuels, série nominale/réelle) et
rente potentielle (certaine / à vie / perpétuelle). Moteur pur src/sim +
routes /api/sim/project et /api/sim/rente (membres dédiés, préfixe `sv`).

Formules vérifiées indépendamment :
- (1 + r_m)^12 = 1 + r  (taux mensuel équivalent au taux annuel)
- rente certaine : PMT = P·r_m / (1 − (1 + r_m)^(−n)), paiements fin de mois
- perpétuelle : PMT = P·r_m (intérêts seuls, capital intact)
- vie : PMT = P·swr/12 (règle du taux soutenable)
"""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["PAT_CRYPTO_AUTO"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import src.app as app  # noqa: E402

from src import sim  # noqa: E402

PWD = "sim-pass-2026-long"


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


# ---------------- moteur pur ----------------

def test_project_compound_only():
    """10 000 € à 10 %/an pendant 10 ans, sans versement : 10 000 × 1,1^10."""
    out = sim.project(10_000, 0, 10.0, 2.0, 10)
    assert out["labels"] == list(range(11))
    assert out["nominal"][0] == 10_000.0
    assert abs(out["final_nominal"] - 10_000.0 * 1.1 ** 10) < 1.0
    assert abs(out["final_nominal"] - 25_937.42) < 0.5
    assert out["total_pmts"] == 10_000.0
    assert abs(out["interest"] - (out["final_nominal"] - 10_000.0)) < 0.01
    # la série réelle déflate le nominal par l'inflation
    i_m = (1.02 ** (1 / 12)) - 1
    assert abs(out["reel"][10] - out["nominal"][10] / ((1 + i_m) ** 120)) < 0.5
    assert out["final_reel"] < out["final_nominal"]


def test_project_with_pmt_zero_return():
    """100 €/mois à 0 % sur 1 an : 1 200 € versés, aucun intérêt."""
    out = sim.project(0, 100, 0.0, 0.0, 1)
    assert out["labels"] == [0, 1]
    assert out["nominal"][1] == 1_200.0
    assert out["invested"][1] == 1_200.0
    assert out["interest"] == 0.0
    assert out["final_reel"] == 1_200.0
    # avec 2 % d'inflation, le pouvoir d'achat au bout d'un an est déflaté
    out2 = sim.project(0, 100, 0.0, 2.0, 1)
    assert abs(out2["final_reel"] - 1_200.0 / 1.02) < 0.5


def test_project_monthly_compounding_consistency():
    """La capitalisation mensuelle d'un dépôt unique équivaut au taux annuel."""
    out = sim.project(5_000, 0, 6.0, 0.0, 3)
    assert abs(out["final_nominal"] - 5_000.0 * 1.06 ** 3) < 0.5


def test_rente_years_formula():
    """Rente certaine : PMT = P·r_m/(1−(1+r_m)^−n), capital épuisé au terme."""
    # r = 0 : PMT = P/n mois
    out0 = sim.rente(120_000, "years", 0.0, 0.0, 10, 4.0)
    assert abs(out0["pmt_month"] - 1_000.0) < 0.01
    assert out0["capital"][-1] == 0.0
    assert out0["pmt_year"] == 12_000.0
    # r = 5 %
    out = sim.rente(120_000, "years", 5.0, 2.0, 10, 4.0)
    r_m = (1.05 ** (1 / 12)) - 1
    expected = 120_000 * r_m / (1 - (1 + r_m) ** -120)
    assert abs(out["pmt_month"] - expected) < 0.05
    assert abs(out["capital"][-1]) < 1.0  # capital épuisé au terme
    # pouvoir d'achat au terme : déflaté de l'inflation sur 120 mois
    i_m = (1.02 ** (1 / 12)) - 1
    assert abs(out["pmt_month_real_end"] - expected / ((1 + i_m) ** 120)) < 0.05
    assert out["pmt_month_real_end"] < out["pmt_month"]


def test_rente_perp_and_life():
    """Perpétuelle : les intérêts seuls (capital stable). Vie : P·swr/12."""
    out = sim.rente(100_000, "perp", 4.0, 2.0, 30, 4.0)
    r_m = (1.04 ** (1 / 12)) - 1
    assert abs(out["pmt_month"] - 100_000 * r_m) < 0.05
    assert abs(out["cap_end"] - 100_000) < 10.0  # capital intact
    assert abs(out["total_payout"] - out["pmt_month"] * 360) < 10.0
    out2 = sim.rente(100_000, "life", 4.0, 2.0, 30, 4.0)
    assert abs(out2["pmt_month"] - 100_000 * 4.0 / 100 / 12) < 0.01
    assert len(out2["capital"]) == 31  # année 0 + 30 ans


# ---------------- routes ----------------

def test_sim_routes_auth_and_validations():
    c = TestClient(app.app)
    r = c.get("/api/sim/project?principal=10000&years=10")
    assert r.status_code == 401
    _login(c)
    r = c.get("/api/sim/project")
    assert r.status_code == 400  # principal requis
    r = c.get("/api/sim/project?principal=10000&years=0")
    assert r.status_code == 400
    r = c.get("/api/sim/rente?principal=1000&mode=nope")
    assert r.status_code == 400
    r = c.get("/api/sim/rente?principal=1000&mode=life&swr_pct=99")
    assert r.status_code == 400


def test_sim_project_route():
    c = TestClient(app.app)
    tag = "sv_project"
    _mk_member(c, tag)
    r = c.get("/api/sim/project?principal=10000&pmt_month=100"
              "&return_pct=6&inflation_pct=2&years=15")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["year0"] >= 2026
    assert d["labels"] == list(range(d["year0"], d["year0"] + 16))
    assert len(d["nominal"]) == 16
    # cohérence : dernier point = moteur pur sur les mêmes paramètres
    ref = sim.project(10_000, 100, 6.0, 2.0, 15)
    assert d["final_nominal"] == ref["final_nominal"]
    assert d["interest"] == ref["interest"]
    # les défauts rendement/inflation viennent des settings fire_* du membre
    r = c.get("/api/sim/project?principal=10000&years=10")
    assert r.status_code == 200


def test_sim_rente_route_defaults():
    c = TestClient(app.app)
    tag = "sv_rente"
    _mk_member(c, tag)
    r = c.get("/api/sim/rente?principal=200000&mode=years&years=20")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "years"
    assert len(d["capital"]) == 21
    assert d["labels"][0] == d["year0"]
    assert d["pmt_month"] > 0
    # mode life sans swr : défaut des settings fire_* (4 % standard)
    r = c.get("/api/sim/rente?principal=200000&mode=life")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "life"
