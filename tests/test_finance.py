"""Tests métier des CALCULS financiers (v2026.09.019) : synthèse (dernière
valorisation par actif, coût effectif, gains, parts de classes), historique
mensuel (report de la dernière valeur connue, fenêtres d'ouverture), et
benchmarks (annualisation, simulation de flux, livret synthétique, perf
utilisateur). Valeurs exactes épinglées — toute régression de calcul casse ici.

Chaque membre de test est isolé : la synthèse/l'historique sont scopés owner.
index_levels est seedé (monde sans réseau) : la logique de _fetch_bench_levels
saute l'appel HTTP dès que >= 2 mois existent à partir du start_ym.
"""
import datetime
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-fin-")
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


def _mk_member(admin_c, name):
    r = admin_c.post("/api/family", json={"username": name, "password": PWD, "mode": "standard"})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, name, PWD)
    return c


def _mk_acc(c, name, cls="comptes", **kw):
    body = {"name": name, "asset_class": cls, **kw}
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _val(c, aid, d, value):
    r = c.post(f"/api/accounts/{aid}/valuation", json={"value": value, "val_date": d})
    assert r.status_code == 200, r.text


def _sql_tx(account_id, op_date, kind, amount, note=""):
    """Insertion SQL directe (les routes API transactions ont été remplacées
    par l'import CSV ; ici on teste les CALCULS, pas la saisie)."""
    conn = app.db_main()
    try:
        conn.execute(
            "INSERT INTO transactions (account_id, op_date, kind, amount, note) VALUES (?,?,?,?,?)",
            (account_id, op_date, kind, amount, note),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- synthèse
def test_summary_latest_valuation_classes_gains(admin_c):
    c = _mk_member(admin_c, "fin-sum")
    a1 = _mk_acc(c, "Compte courant", "comptes", cost_basis=1000)
    _val(c, a1, "2025-01-15", 900)
    _val(c, a1, "2025-06-15", 1500)
    _val(c, a1, "2026-03-10", 1600)
    _val(c, a1, "2026-03-10", 1625)  # même jour : la PLUS RÉCENTE gagne (max id)
    a2 = _mk_acc(c, "Livret", "epargne", cost_basis=500)
    _val(c, a2, "2026-02-01", 520)
    a3 = _mk_acc(c, "PEA sans valeur")  # aucune valorisation → exclu des totaux
    a4 = _mk_acc(c, "Clôturé", "bourse", cost_basis=300)
    _val(c, a4, "2026-01-01", 9000)
    conn = app.db_main()  # clôture APRÈS valorisation (une val. exige active=1)
    try:
        conn.execute("UPDATE accounts SET active=0 WHERE id=?", (a4,))
        conn.commit()
    finally:
        conn.close()
    a5 = _mk_acc(c, "Crypto sans coût", "crypto")  # valeur sans coût → comptée, gain None
    _val(c, a5, "2026-04-01", 700)

    s = c.get("/api/summary").json()
    assert s["total_value"] == round(1625 + 520 + 700, 2) == 2845.0
    assert s["total_cost"] == round(1000 + 500, 2) == 1500.0
    assert s["gain"] == 1345.0
    assert s["gain_pct"] == round(1345 / 1500 * 100, 2) == 89.67
    assert s["asof"] == "2026-04-01"  # date max des valorisations retenues
    assert s["nb_accounts"] == 3  # a3 sans valeur et a4 inactif exclus

    cls = {x["key"]: x for x in s["classes"]}
    assert cls["comptes"]["value"] == 1625.0 and cls["comptes"]["cost"] == 1000.0
    assert cls["comptes"]["gain"] == 625.0 and cls["comptes"]["gain_pct"] == 62.5
    assert cls["comptes"]["share_pct"] == round(1625 / 2845 * 100, 1) == 57.1
    assert cls["crypto"]["gain"] is None and cls["crypto"]["gain_pct"] is None
    assert cls["crypto"]["value"] == 700.0 and cls["crypto"]["cost"] == 0.0
    assert "bourse" not in cls  # inactif invisible

    accs = {x["id"]: x for x in c.get("/api/accounts").json()["accounts"]}
    assert accs[a1]["last_value"] == 1625.0 and accs[a1]["last_val_date"] == "2026-03-10"
    assert accs[a3]["last_value"] is None and accs[a3]["gain"] is None
    assert accs[a4]["last_value"] == 9000.0  # liste actifs : visible (fermeture assumée)


def test_txn_cost_overrides_cost_basis_and_kind_semantics(admin_c):
    c = _mk_member(admin_c, "fin-cost")
    a = _mk_acc(c, "PEA Boursorama", "bourse", cost_basis=100000)
    _sql_tx(a, "2025-01-10", "deposit", 20000)
    _sql_tx(a, "2025-02-10", "deposit", 10000)
    _sql_tx(a, "2025-03-10", "income", 500)
    _sql_tx(a, "2025-04-10", "withdrawal", 3000)
    _sql_tx(a, "2025-05-10", "expense", 120)  # dépense ≠ retrait : ne réduit PAS le coût
    _val(c, a, "2026-06-15", 40000)

    acc = c.get("/api/accounts").json()["accounts"][0]
    # coût = entrées (dépôts + revenus) − retraits ; cost_basis ignoré dès qu'il y a des opérations
    assert acc["cost_effective"] == round(20000 + 10000 + 500 - 3000, 2) == 27500.0
    assert acc["cost_from_tx"] is True
    assert acc["txn_count"] == 5
    assert acc["income_received"] == 500.0
    assert acc["gain"] == 12500.0
    assert acc["gain_pct"] == round(12500 / 27500 * 100, 2) == 45.45

    s = c.get("/api/summary").json()
    assert s["total_cost"] == 27500.0 and s["gain"] == 12500.0
    assert s["classes"][0]["gain_pct"] == 45.45

    # sans opérations : le cost_basis manuel fait foi
    b = _mk_acc(c, "Compte courant", "comptes", cost_basis=7000)
    _val(c, b, "2026-01-01", 7000)
    acc2 = {x["id"]: x for x in c.get("/api/accounts").json()["accounts"]}[b]
    assert acc2["cost_effective"] == 7000.0 and acc2["cost_from_tx"] is False
    assert acc2["gain"] == 0.0 and acc2["gain_pct"] == 0.0


# ---------------------------------------------------------------- historique
def test_history_monthly_carry_and_open_window(admin_c):
    c = _mk_member(admin_c, "fin-hist")
    a1 = _mk_acc(c, "Livret", "epargne", open_date="2023-01-10")
    _val(c, a1, "2023-01-15", 100)
    _val(c, a1, "2023-03-20", 300)
    _val(c, a1, "2024-02-05", 250)
    _val(c, a1, "2024-02-05", 270)  # même jour : 270 retenu
    _val(c, a1, "2026-01-15", 350)
    a2 = _mk_acc(c, "Crypto", "crypto", open_date="2025-09-01")
    _val(c, a2, "2025-09-15", 1000)
    _val(c, a2, "2026-01-10", 900)

    h = c.get("/api/history?months=60").json()
    labels = h["labels"]
    assert len(labels) == 60
    assert labels[0] <= "2023-01"  # fenêtre de 60 mois couvre tout le scénario
    i = {m: k for k, m in enumerate(labels)}
    assert h["totals"][i["2022-12"]] == 0.0  # actif pas encore ouvert
    assert h["totals"][i["2023-01"]] == 100.0  # valeur d'ouverture
    assert h["totals"][i["2023-02"]] == 100.0  # report de la dernière connue
    assert h["totals"][i["2023-03"]] == 300.0
    assert h["totals"][i["2024-02"]] == 270.0  # même jour → dernière saisie
    assert h["totals"][i["2025-08"]] == 270.0  # crypto pas encore ouverte
    assert h["totals"][i["2025-09"]] == 270.0 + 1000.0
    assert h["totals"][i["2026-01"]] == 350.0 + 900.0
    assert h["current"] == h["totals"][-1] == 350.0 + 900.0  # report jusqu'à aujourd'hui
    ep = h["series"]["epargne"]
    assert ep[i["2023-01"]] == 100.0 and ep[i["2024-02"]] == 270.0 and ep[i["2026-01"]] == 350.0
    assert h["series"]["crypto"][i["2025-08"]] == 0.0 and h["series"]["crypto"][i["2025-09"]] == 1000.0
    assert len(ep) == len(labels) == 60


def test_history_empty_and_clamp(admin_c):
    c = _mk_member(admin_c, "fin-void")
    h6 = c.get("/api/history?months=1").json()  # clamp bas : 6
    assert len(h6["labels"]) == 6 and all(x == 0.0 for x in h6["totals"]) and h6["current"] == 0
    h240 = c.get("/api/history?months=9999").json()  # clamp haut : 240
    assert len(h240["labels"]) == 240
    s = c.get("/api/summary").json()
    assert s["total_value"] == 0.0 and s["total_cost"] == 0.0 and s["gain"] is None
    assert s["classes"] == [] and s["asof"] is None


# ---------------------------------------------------------------- benchmarks
def test_benchmarks_math_exact(admin_c):
    """Annualisation, simulation de flux, livret composé, perf utilisateur —
    valeurs épinglées dans un monde seedé sans réseau (levels géométriques)."""
    today = datetime.date.today()
    conn = app.db_main()
    try:
        for key, growth in (("cac", 1.01), ("iwda", 1.005), ("nasdaq", 1.02),
                            ("sp500", 1.0), ("stoxx", 1.003)):
            k = 0
            d = datetime.date(2023, 1, 1)
            while d <= today:
                conn.execute(
                    "INSERT OR REPLACE INTO index_levels (key, ym, level) VALUES (?,?,?)",
                    (key, d.strftime("%Y-%m"), 100.0 * growth ** k),
                )
                k += 1
                d = datetime.date(d.year + d.month // 12, d.month % 12 + 1, 1)
        conn.commit()
    finally:
        conn.close()

    c = _mk_member(admin_c, "fin-bench")
    a = _mk_acc(c, "Compte", "comptes")
    _sql_tx(a, "2023-01-10", "deposit", 1000)
    _sql_tx(a, "2025-06-05", "deposit", 1000)
    _sql_tx(a, "2025-07-01", "income", 50)  # revenu : PAS un flux investi
    _val(c, a, "2026-06-15", 2600)

    b = c.get("/api/benchmarks").json()
    u = b["user"]
    assert u["deposited"] == 2000.0 and u["net"] == 2000.0  # l'income est exclu
    assert u["value"] == 2600.0 and u["gain"] == 600.0
    assert u["first_ym"] == "2023-01"
    # annualisation utilisateur : même formule que la route (approximation débit en début)
    days = max(1, (today - datetime.date(2023, 1, 10)).days)
    assert u["annualized"] == round(((2600 / 2000) ** (365 / days) - 1) * 100, 2)

    bm = {x["key"]: x for x in b["benchmarks"]}
    # annualisé = croissance mensuelle composée (indépendant de la fenêtre) :
    assert bm["cac"]["annualized"] == round((1.01 ** 12 - 1) * 100, 2) == 12.68
    assert bm["iwda"]["annualized"] == round((1.005 ** 12 - 1) * 100, 2) == 6.17
    assert bm["nasdaq"]["annualized"] == round((1.02 ** 12 - 1) * 100, 2) == 26.82
    assert bm["sp500"]["annualized"] == 0.0  # niveau plat → 0 %
    # livret A : synthétique, composé mensuel à partir de annual_pct (2.2 seedé)
    assert bm["livret"]["annualized"] == round(((1 + 2.2 / 100 / 12) ** 12 - 1) * 100, 2) == 2.22
    # simulation : chaque flux investi aux niveaux du benchmark
    K = (today.year - 2023) * 12 + (today.month - 1)  # mois écoulés depuis 2023-01
    last_cac = 100.0 * 1.01 ** K
    sv = round(1000.0 * (last_cac / 100.0) + 1000.0 * (last_cac / (100.0 * 1.01 ** 29)), 2)
    assert bm["cac"]["sim_value"] == sv
    assert bm["cac"]["sim_gain"] == round(sv - 2000.0, 2)
    assert bm["iwda"]["sim_value"] is not None and bm["iwda"]["sim_gain"] is not None
    # classement : tri par annualisé décroissant (None en dernier)
    anns = [x["annualized"] for x in b["benchmarks"]]
    assert anns == sorted([a for a in anns if a is not None], reverse=True) + \
        [a for a in anns if a is None]
