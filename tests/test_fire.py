"""Tests simulateur FIRE déterministe (v2026.09.034) : moteur pur src/fire
(module séparé, aucune I/O) + route /api/fire/simulate (défauts depuis les
settings fire_*, plages, sensibilité) + persistance des hypothèses."""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import src.app as app  # noqa: E402
from src import fire  # noqa: E402

c = TestClient(app.app)

PWD = "fire-pwd-2026-long"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(c, tag):
    """L'admin crée un membre dédié (isolation des tests) puis s'y connecte."""
    r = c.post("/api/family", json={"username": tag, "password": PWD,
                                    "display_name": tag, "mode": "standard"})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, tag, PWD)
    return tag


def test_moteur_accumulation_pure():
    # 200 000 €, 1 000 €/mois d'épargne, 5 % nominal, 2 % inflation,
    # dépenses 2 000 €/mois, swr 4 % → FIRE quand capital >= 600 k€ (réel)
    out = fire.simulate(200_000, 12_000, 24_000, 0, 5.0, 2.0, 4.0, max_years=60)
    assert out["real_return_pct"] == 2.94  # (1.05/1.02 − 1)
    assert out["net_expenses_year0"] == 24_000
    assert out["fire"] is not None and out["fire"]["t"] > 0
    assert not out["exhausted"] and out["retired"]
    # le point FIRE : capital ≥ cible (dépenses indexées / swr)
    fr = out["fire"]["t"]
    row = [r for r in out["rows"] if r["t"] == fr][0]
    assert row["retired"]
    assert row["capital"] >= row["target"]
    # croissance de la 1re année : 200 000 × 1.05 + 12 000
    assert out["rows"][0]["capital"] == round(200_000 * 1.05 + 12_000, 2)


def test_moteur_deja_independant_et_epuise():
    # rentes ≥ dépenses → indépendant immédiat (t=0), retrait net négatif
    out = fire.simulate(100_000, 0, 24_000, 25_000, 5.0, 2.0, 4.0, max_years=30)
    assert out["fire"] == {"t": 0, "capital": 100_000.0}
    assert out["retired"]
    # swr 10 % > rendement réel 2,94 % → le capital s'érode jusqu'à épuisement
    out2 = fire.simulate(1_000_000, 0, 100_000, 0, 5.0, 2.0, 10.0, max_years=60)
    assert out2["exhausted"]
    last = out2["rows"][-1]
    assert last["capital"] == 0.0


def test_moteur_jamais_fire():
    # épargne nulle, capital loin de la cible → pas de FIRE sur l'horizon
    out = fire.simulate(10_000, 0, 24_000, 0, 5.0, 2.0, 4.0, max_years=25)
    assert out["fire"] is None and not out["retired"]
    assert len(out["rows"]) == 25


def test_sensibilite_ordres():
    # rendement plus haut → FIRE plus tôt (jamais plus tard)
    sens = fire.sensitivity(200_000, 12_000, 24_000, 0, 5.0, 2.0, 4.0, max_years=60)
    ts = [s["fire_t"] for s in sens]
    assert ts[0] >= ts[1] >= ts[2]  # 3 % ≥ 5 % ≥ 7 %
    assert sens[1]["return_pct"] == 5.0


def test_route_defauts_settings_et_validation():
    _login(c)
    # défauts du moteur (settings vides) : 5 % / 2 % / 4 %
    r = c.get("/api/fire/simulate",
              params={"principal": 200_000, "savings_month": 1000, "expenses_month": 2000})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["year0"] >= 2026 and j["real_return_pct"] == 2.94
    assert j["fire"] and j["fire"]["year"] > j["year0"]
    assert len(j["rows"]) >= 1 and j["rows"][0]["year"] == j["year0"] + 1
    assert len(j["sensitivity"]) == 3 and j["sensitivity"][1]["return_pct"] == 5.0
    # plages : rendement absurde
    r2 = c.get("/api/fire/simulate", params={"principal": 1, "savings_month": 1,
                                             "expenses_month": 1, "return_pct": 40})
    assert r2.status_code == 400
    # montants négatifs
    r3 = c.get("/api/fire/simulate", params={"principal": -5, "savings_month": 1,
                                             "expenses_month": 1})
    assert r3.status_code == 400
    # settings fire_* persistés → utilisés comme défauts
    r4 = c.put("/api/settings", json={"fire_return": 3.0, "fire_inflation": 1.5,
                                      "fire_swr": 5.0, "fire_birthyear": 1975})
    assert r4.status_code == 200, r4.text
    r5 = c.get("/api/settings")
    assert r5.status_code == 200
    s = r5.json()
    assert s["fire_return"] == 3.0 and s["fire_birthyear"] == 1975
    assert s["tax_tmi_lu"] == 42.8  # défauts fiscaux intacts
    r6 = c.get("/api/fire/simulate",
               params={"principal": 200_000, "savings_month": 1000, "expenses_month": 2000})
    assert r6.status_code == 200
    j6 = r6.json()
    assert j6["sensitivity"][1]["return_pct"] == 3.0  # défaut = setting stocké
    # surcharge explicite par requête
    r7 = c.get("/api/fire/simulate",
               params={"principal": 200_000, "savings_month": 1000, "expenses_month": 2000,
                       "return_pct": 7.0})
    assert r7.json()["sensitivity"][1]["return_pct"] == 7.0
    # plage de swr stockée
    r8 = c.put("/api/settings", json={"fire_swr": 30})
    assert r8.status_code == 400


def test_route_isolee_par_membre():
    # les settings d'un membre ne débordent pas sur un autre
    _login(c)
    c.put("/api/settings", json={"fire_return": 3.0})
    _mk_member(c, "fire-iso")  # logout + login du membre dédié
    r = c.get("/api/settings")
    assert r.status_code == 200, r.text
    assert r.json()["fire_return"] == 5.0  # défaut (rien stocké pour ce membre)
