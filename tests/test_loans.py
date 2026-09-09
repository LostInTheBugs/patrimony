"""Tests du module Crédits (v2026.09.056) : table loans + routes /api/loans*.

Remplace les tests v033 du « crédit lié » (accounts.loan_*, neutralisés) :
le passif vit désormais dans `loans` (restant DÉCLARÉ, échéancier calculé à
la demande par src/loans.py — amortissement français), migré au boot depuis
les colonnes legacy puis remis à zéro sur le compte.

Isolation : chaque test utilise un membre dédié (usernames préfixés `lo` —
la base est partagée par toute la suite, cf. conventions des tests).
"""

import math
import os
import sqlite3
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient

import src.app as app
import src.loans as loans
import src.transfer as transfer

PWD = "loan-pass-2026-long"


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def _mk_member(c, tag, mode="standard"):
    r = c.post("/api/family", json={"username": tag, "password": PWD,
                                    "display_name": tag, "mode": mode})
    assert r.status_code == 200, r.text
    c.post("/api/auth/logout")
    _login(c, tag, PWD)
    return tag


def _mk_asset(c, name="Bien", cls="immobilier", value=172000, odate="2021-03-01",
              cost=80000, **kw):
    body = {"name": name, "asset_class": cls, "currency": "EUR",
            "cost_basis": cost, "open_date": odate, "initial_value": value,
            "valuation_mode": "manual"}
    body.update(kw)
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_loan(c, name="Prêt maison", remaining=92000, rate=2.8, monthly=520,
             account_id=None, **kw):
    body = {"name": name, "loan_type": "immo", "lender": "Banque",
            "currency": "EUR", "principal_initial": remaining,
            "principal_remaining": remaining, "rate_annual": rate,
            "monthly_payment": monthly, "start_date": "2021-03-01",
            "account_id": account_id}
    body.update(kw)
    r = c.post("/api/loans", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _summary(c):
    r = c.get("/api/summary")
    assert r.status_code == 200, r.text
    return r.json()


def _raw_conn():
    """Connexion SQLite directe sur la base partagée (écritures legacy)."""
    conn = sqlite3.connect(app.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # même contrat que db() (cascade)
    return conn


def _sub_months(d: date, n: int) -> date:
    """d − n mois (jour forcé à 1 puis re-ajusté — jamais de 29/30/31)."""
    y = d.year + (d.month - 1 - n) // 12
    m = (d.month - 1 - n) % 12 + 1
    return date(y, m, 1)


def _get_loan_ids(c):
    r = c.get("/api/loans")
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()["loans"]]


# ---------------------------------------------------------------- migration legacy

def test_migrate_legacy_creates_loan_and_zeroes():
    c = TestClient(app.app)
    _login(c)
    lo = _mk_member(c, "lo_mig1")
    aid = _mk_asset(c)  # compte immo SANS crédit côté API
    conn = _raw_conn()
    conn.execute(
        "UPDATE accounts SET loan_principal=92000, loan_rate=2.8,"
        " loan_monthly=520 WHERE id=?", (aid,))
    conn.commit()
    n = loans.migrate_legacy(conn)
    conn.commit()
    assert n == 1
    row = conn.execute("SELECT * FROM loans WHERE owner=?", (lo,)).fetchone()
    assert row is not None
    assert row["name"] == "Bien" and row["loan_type"] == "immo"
    assert row["principal_remaining"] == 92000 and row["rate_annual"] == 2.8
    assert row["monthly_payment"] == 520 and row["account_id"] == aid
    acc = conn.execute("SELECT loan_principal, loan_rate, loan_monthly"
                       " FROM accounts WHERE id=?", (aid,)).fetchone()
    assert (acc["loan_principal"], acc["loan_rate"], acc["loan_monthly"]) == (0, 0, 0)
    # idempotence : second passage ne crée rien (colonnes neutralisées)
    conn.close()
    conn = _raw_conn()
    assert loans.migrate_legacy(conn) == 0
    conn.commit()
    conn.close()
    s = _summary(c)  # le passif migré alimente le dashboard
    assert s["total_debt"] == 92000


def test_migrate_legacy_skips_linked_and_non_immo():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_mig2")
    aid = _mk_asset(c)  # crédit créé côté module (déjà lié)
    _mk_loan(c, account_id=aid)
    conn = _raw_conn()
    # un compte bourse « maquillé » en legacy (les champs ne sont validés que
    # pour l'immo côté API — écriture directe pour le scénario)
    b = c.post("/api/accounts", json={"name": "Bourse", "asset_class": "bourse",
                                      "currency": "EUR", "cost_basis": 100,
                                      "open_date": "2021-03-01"})
    assert b.status_code == 200
    conn.execute("UPDATE accounts SET loan_principal=5000 WHERE id=?",
                 (b.json()["id"],))
    conn.commit()
    assert loans.migrate_legacy(conn) == 0  # déjà lié + non-immo ignorés
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- dashboard (summary)

def test_summary_debt_and_net_worth():
    c = TestClient(app.app)
    _login(c)
    lo = _mk_member(c, "lo_sum1")
    aid = _mk_asset(c, value=172000)
    _mk_loan(c, account_id=aid)
    s = _summary(c)
    assert s["total_value"] == 172000
    assert s["total_debt"] == 92000
    assert s["net_worth"] == 80000
    assert s["debt"]["total_eur"] == 92000
    assert s["debt"]["per_type"]["immo"] == 92000
    assert abs(s["debt"]["part_pct"] - 53.49) < 0.01
    assert s["debt"]["fx_missing"] == []
    assert lo  # usage du membre (isolation)


def test_summary_no_loan_no_debt():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_sum2")
    _mk_asset(c, value=172000)  # bien SANS crédit
    s = _summary(c)
    assert s["total_debt"] == 0 and s["debt"]["total_eur"] == 0
    assert s["net_worth"] == 172000
    assert s["debt"]["per_type"] == {"immo": 0.0, "auto": 0.0, "conso": 0.0}


def test_summary_debt_multiccy_and_fx_missing():
    c = TestClient(app.app)
    _login(c)
    lo = _mk_member(c, "lo_fx1")
    _mk_asset(c, value=100000)  # valeur EUR quelconque
    _mk_loan(c, name="Prêt USD", remaining=10000, rate=3.0, monthly=300,
             currency="USD")
    # taux posé AUJOURD'HUI : max(rate_date ≤ jour) → vainqueur déterministe
    # quel que soit l'historique seedé par les autres fichiers (test_fx…)
    conn = _raw_conn()
    conn.execute("INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source)"
                 " VALUES ('USD', ?, 1.05, 'ecb')", (date.today().isoformat(),))
    conn.commit()
    conn.close()
    s = _summary(c)
    assert abs(s["total_debt"] - 10000 / 1.05) < 0.01
    assert abs(s["debt"]["per_type"]["immo"] - 10000 / 1.05) < 0.01
    assert s["debt"]["fx_missing"] == []
    r = c.get("/api/loans")
    loan = r.json()["loans"][0]
    assert abs(loan["eur_remaining"] - 10000 / 1.05) < 0.01
    assert lo  # usage du membre (isolation)
    # devise SANS aucun taux dans la base partagée (CAD n'est seedé par
    # aucun autre fichier — vérifié par grep sur la suite) : exclue des
    # totaux + listée (jamais muet)
    c.post("/api/auth/logout")
    _login(c)
    _mk_member(c, "lo_fx2")
    _mk_loan(c, name="Prêt CAD", remaining=5000, rate=2.0, monthly=200,
             currency="CAD")
    s2 = _summary(c)
    assert s2["total_debt"] == 0 and s2["debt"]["fx_missing"] == ["Prêt CAD"]
    r2 = c.get("/api/loans")
    assert r2.json()["totals"]["fx_missing"] == ["Prêt CAD"]
    cad = r2.json()["loans"][0]
    assert cad["eur_remaining"] is None


# ---------------------------------------------------------------- CRUD + validation

def test_loan_validation_errors():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_val1")
    _mk_asset(c, value=100000)  # immo pour le test de lien
    good = {"name": "Prêt", "loan_type": "immo", "currency": "EUR",
            "principal_initial": 50000, "principal_remaining": 45000,
            "rate_annual": 2.5, "monthly_payment": 400,
            "start_date": "2023-01-01"}
    cases = [
        ({**good, "name": "  "}, "Nom requis"),
        ({**good, "loan_type": "maison"}, "Type de crédit invalide"),
        ({**good, "currency": "BTC"}, "Devise non supportée"),
        ({**good, "principal_remaining": -1}, "négatif"),
        ({**good, "rate_annual": 101}, "Taux invalide"),
        ({**good, "rate_annual": 150}, "Taux invalide"),
        ({**good, "monthly_payment": 0}, "Mensualité requise"),
        ({**good, "monthly_payment": 50, "rate_annual": 2.5}, "intérêts du premier mois"),
    ]
    for body, frag in cases:
        r = c.post("/api/loans", json=body)
        assert r.status_code == 400, (body, r.text)
        assert frag.lower() in r.json()["detail"].lower(), (body, r.text)
    # lien : bien immobilier requis
    r = c.post("/api/loans", json={**good, "account_id": 999999})
    assert r.status_code == 400 and "introuvable" in r.json()["detail"]
    r = c.post("/api/loans", json={**good, "account_id": 1})
    # (le compte 1 appartient à l'admin d'un autre fichier → introuvable pour ce membre)
    assert r.status_code in (400, 404)
    assert "immobilier" in r.json()["detail"] or "introuvable" in r.json()["detail"]
    bours = c.post("/api/accounts", json={"name": "CTO", "asset_class": "bourse",
                                          "currency": "EUR", "cost_basis": 100,
                                          "open_date": "2021-03-01"})
    assert bours.status_code == 200
    r = c.post("/api/loans", json={**good, "account_id": bours.json()["id"]})
    assert r.status_code == 400
    assert "bien immobilier" in r.json()["detail"]


def test_loan_crud_soft_delete_reactivate():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_crud1")
    aid = _mk_asset(c)
    lid = _mk_loan(c, account_id=aid)
    r = c.put(f"/api/loans/{lid}", json={"name": "Prêt renégocié", "loan_type": "immo",
                                         "currency": "EUR", "principal_initial": 92000,
                                         "principal_remaining": 80000,
                                         "rate_annual": 2.5, "monthly_payment": 500,
                                         "start_date": "2021-03-01", "account_id": aid,
                                         "active": 1})
    assert r.status_code == 200, r.text
    row = c.get("/api/loans").json()["loans"][0]
    assert row["name"] == "Prêt renégocié"
    assert row["principal_remaining"] == 80000 and row["rate_annual"] == 2.5
    assert row["account_name"] == "Bien"
    assert row["computed"]["months_left"] is not None
    # suppression douce : disparaît de la liste, le passif tombe à zéro
    r = c.delete(f"/api/loans/{lid}")
    assert r.status_code == 200 and c.get("/api/loans").json()["loans"] == []
    s = _summary(c)
    assert s["total_debt"] == 0
    # réactivation par PUT (active=1) — la ligne soft-deleted reste connue
    r = c.put(f"/api/loans/{lid}", json={"name": "Prêt renégocié", "loan_type": "immo",
                                         "currency": "EUR", "principal_initial": 92000,
                                         "principal_remaining": 80000,
                                         "rate_annual": 2.5, "monthly_payment": 500,
                                         "start_date": "2021-03-01", "account_id": aid,
                                         "active": 1})
    assert r.status_code == 200
    assert len(_get_loan_ids(c)) == 1
    # 404 sur un crédit d'un autre propriétaire
    c.post("/api/auth/logout")
    _login(c)
    _mk_member(c, "lo_crud2")
    assert c.delete(f"/api/loans/{lid}").status_code == 404
    assert c.put(f"/api/loans/{lid}", json={"name": "x", "loan_type": "conso",
                                            "principal_initial": 1,
                                            "principal_remaining": 1,
                                            "monthly_payment": 10}).status_code == 404


def test_delete_account_unlinks_loan():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_unlink")
    aid = _mk_asset(c)
    lid = _mk_loan(c, account_id=aid)
    r = c.delete(f"/api/accounts/{aid}")
    assert r.status_code == 200, r.text
    loans_ = c.get("/api/loans").json()["loans"]
    assert len(loans_) == 1 and loans_[0]["id"] == lid
    assert loans_[0]["account_id"] is None and loans_[0]["account_name"] is None
    s = _summary(c)
    assert s["total_debt"] == 92000  # le crédit survit à la vente du bien


# ---------------------------------------------------------------- échéancier & recalcul

def test_amortize_math_pure():
    am = loans.amortize(100000, 3.0, 1000)
    assert am is not None
    # n = ceil(log(M/(M−P0·r)) / log(1+r))
    n = math.ceil(math.log(1000 / (1000 - 100000 * 0.0025)) / math.log(1.0025))
    assert am["months_left"] == n
    assert am["rows"][-1]["remaining"] == 0
    # chaque échéance intermédiaire paie exactement M ; le capital total est
    # remboursé (tolérance arrondis d'affichage)
    assert all(abs(round(r["interest"] + r["principal"], 2) - 1000) < 0.01
               for r in am["rows"][:-1])
    assert abs(sum(r["principal"] for r in am["rows"]) - 100000) < 0.05
    assert am["interests_left"] > 0
    assert am["next_year_capital"] > 0 and am["next_year_interests"] > 0
    # taux nul : n = ceil(P0/M), aucun intérêt
    am0 = loans.amortize(12000, 0.0, 1000)
    assert am0 is not None
    assert am0["months_left"] == 12 and am0["interests_left"] == 0
    # mensualité ≤ intérêts du 1er mois → jamais amorti
    assert loans.amortize(100000, 2.8, 100) is None
    assert loans.amortize(100000, 2.8, 0) is None
    # plafond de lignes détaillées
    amc = loans.amortize(100000, 3.0, 1000, months=24)
    assert amc is not None
    assert len(amc["rows"]) == 24 and amc["months_left"] == n


def test_schedule_endpoint():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_sched")
    lid = _mk_loan(c, remaining=100000, rate=3.0, monthly=1000)
    r = c.get(f"/api/loans/{lid}/schedule")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == body["months_left"] > 0
    assert body["rows"][-1]["remaining"] == 0
    # cohérence : les échéances intermédiaires valent la mensualité
    assert all(abs(round(x["interest"] + x["principal"], 2) - 1000) < 0.01
               for x in body["rows"][:-1])
    # horizon réduit / invalide
    r12 = c.get(f"/api/loans/{lid}/schedule?months=12").json()
    assert len(r12["rows"]) == 12 and r12["months_left"] == body["months_left"]
    assert c.get(f"/api/loans/{lid}/schedule?months=6").status_code == 400
    # mensualité trop faible → 400 explicite (le POST refuse déjà ce crédit ;
    # insertion directe pour couvrir la garde de l'échéancier)
    conn = _raw_conn()
    cur = conn.execute(
        "INSERT INTO loans (owner, name, loan_type, currency, principal_initial,"
        " principal_remaining, rate_annual, monthly_payment)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("lo_sched", "Jamais", "immo", "EUR", 100000, 100000, 2.8, 100))
    conn.commit()
    conn.close()
    bad = cur.lastrowid
    r = c.get(f"/api/loans/{bad}/schedule")
    assert r.status_code == 400 and "jamais" in r.json()["detail"]
    # remboursé : échéancier vide
    paid = _mk_loan(c, name="Soldé", remaining=0, rate=0, monthly=0)
    assert c.get(f"/api/loans/{paid}/schedule").json() == {"months_left": 0, "rows": []}
    # 404 autre propriétaire
    c.post("/api/auth/logout")
    _login(c)
    _mk_member(c, "lo_sched2")
    assert c.get(f"/api/loans/{lid}/schedule").status_code == 404


def test_recompute_endpoint():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_rec")
    # 12 échéances échues garanties : départ = 1er jour du mois d'il y a 12
    # mois → la 12e échéance tombe le 1er du mois courant (toujours passée),
    # la 13e au 1er du mois suivant (toujours future)
    start = _sub_months(date.today(), 12).isoformat()
    lid = _mk_loan(c, name="Prêt recalcul", remaining=100000, rate=3.0,
                   monthly=1000, principal_initial=100000, start_date=start)
    r = c.post(f"/api/loans/{lid}/recompute")
    assert r.status_code == 200, r.text
    body = r.json()
    # formule indépendante de l'annuité : restant après k échéances
    k, r0 = 12, 0.0025
    expected = 100000 * (1 + r0) ** k - 1000 * (((1 + r0) ** k - 1) / r0)
    assert body["declared_remaining"] == 100000
    assert abs(body["theoretical_remaining"] - expected) < 0.01
    assert body["delta"] == round(expected - 100000, 2)
    # écart positif : l'utilisateur applique ou non (jamais écrit ici)
    assert body["theoretical_remaining"] < 100000
    # sans date de départ → 400
    nod = _mk_loan(c, name="Sans date", remaining=50000, rate=2.0,
                   monthly=300, start_date=None)
    assert c.post(f"/api/loans/{nod}/recompute").status_code == 400


# ---------------------------------------------------------------- scopes & sauvegarde

def test_loans_family_and_member_scope():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_fam1")
    _mk_loan(c, name="Prêt A", remaining=60000, rate=2.0, monthly=300)
    c.post("/api/auth/logout")
    _login(c)
    _mk_member(c, "lo_fam2")
    _mk_loan(c, name="Prêt B", remaining=40000, rate=1.5, monthly=250)
    c.post("/api/auth/logout")
    _login(c)  # admin
    # vue membre : uniquement le membre ciblé
    r = c.get("/api/loans?member=lo_fam1")
    assert [x["name"] for x in r.json()["loans"]] == ["Prêt A"]
    assert r.json()["totals"]["total_eur"] == 60000
    # vue famille : les deux membres du scénario + agrégation cohérente avec
    # TOUTES les lignes visibles (les autres membres du fichier partagent la
    # base — l'invariant exact = totaux ≡ Σ des eur_remaining renvoyés)
    r = c.get("/api/loans?family=1")
    body = r.json()
    fam = [x for x in body["loans"] if x["owner"] in ("lo_fam1", "lo_fam2")]
    names = {x["name"] for x in fam}
    assert names == {"Prêt A", "Prêt B"}
    assert abs(sum(x["eur_remaining"] or 0 for x in body["loans"])
               - body["totals"]["total_eur"]) < 0.01
    assert abs(sum(x["eur_remaining"] or 0 for x in body["loans"]
                   if x["loan_type"] == "immo")
               - body["totals"]["per_type"]["immo"]) < 0.01
    # member inconnu → 404 indistinguable
    assert c.get("/api/loans?member=nobody").status_code == 404
    # un membre ne voit que ses propres crédits (pas de param member)
    c.post("/api/auth/logout")
    _login(c, "lo_fam1", PWD)
    r = c.get("/api/loans")
    assert [x["name"] for x in r.json()["loans"]] == ["Prêt A"]
    s = _summary(c)
    assert s["total_debt"] == 60000
    # family=1 d'un membre ≠ admin : ignoré (vue soi-même, sémantique
    # _visible_owners commune à toutes les routes)
    r = c.get("/api/loans?family=1")
    assert r.status_code == 200
    assert [x["name"] for x in r.json()["loans"]] == ["Prêt A"]


def test_export_import_roundtrip_loans():
    c = TestClient(app.app)
    _login(c)
    lo = _mk_member(c, "lo_xport")
    aid = _mk_asset(c)
    lid = _mk_loan(c, account_id=aid, name="Prêt export")
    conn = _raw_conn()
    payload = transfer.export_data(conn, lo, "2026.09.056-test")
    assert any(x["id"] == lid for x in payload["loans"])
    # vieux fichier (sans section loans) : import OK et crédits effacés
    legacy = {k: v for k, v in payload.items() if k != "loans"}
    assert transfer.do_import(conn, lo, legacy) is None
    conn.commit()
    assert c.get("/api/loans").json()["loans"] == []
    # round-trip complet : restauration id + lien compte
    assert transfer.do_import(conn, lo, payload) is None
    conn.commit()
    conn.close()
    rows = c.get("/api/loans").json()["loans"]
    assert len(rows) == 1 and rows[0]["id"] == lid
    assert rows[0]["account_id"] == aid and rows[0]["name"] == "Prêt export"
    s = _summary(c)
    assert s["total_debt"] == 92000


# ---------------------------------------------------------------- payload comptes (v2026.09.057)

def test_account_payload_linked_loan():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_pay1")
    aid = _mk_asset(c)  # bien SANS crédit
    r = c.get("/api/accounts")
    acc = [x for x in r.json()["accounts"] if x["id"] == aid][0]
    assert acc["loan"] is None
    lid = _mk_loan(c, account_id=aid)
    r = c.get("/api/accounts")
    acc = [x for x in r.json()["accounts"] if x["id"] == aid][0]
    assert acc["loan"] is not None
    assert acc["loan"]["id"] == lid
    assert acc["loan"]["principal_remaining"] == 92000
    assert acc["loan"]["currency"] == "EUR" and acc["loan"]["name"] == "Prêt maison"
    # suppression douce du crédit → le compte n'expose plus de lien
    c.delete(f"/api/loans/{lid}")
    acc = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
    assert acc["loan"] is None


# ---------------------------------------------------------------- compatibilité legacy (API comptes)

def test_post_account_legacy_loan_materializes():
    c = TestClient(app.app)
    _login(c)
    _mk_member(c, "lo_legacy")
    # ancien client : création d'un bien avec les champs loan_* (v033)
    r = c.post("/api/accounts", json={"name": "Maison", "asset_class": "immobilier",
                                      "currency": "EUR", "cost_basis": 150000,
                                      "open_date": "2022-06-01", "initial_value": 200000,
                                      "loan_principal": 92000, "loan_rate": 2.8,
                                      "loan_monthly": 520})
    assert r.status_code == 200, r.text
    rows = c.get("/api/loans").json()["loans"]
    assert len(rows) == 1
    loan = rows[0]
    assert loan["name"] == "Maison" and loan["loan_type"] == "immo"
    assert loan["principal_remaining"] == 92000
    assert loan["rate_annual"] == 2.8 and loan["monthly_payment"] == 520
    assert loan["account_id"] == r.json()["id"]
    s = _summary(c)
    assert s["total_debt"] == 92000
    assert s["net_worth"] == 200000 - 92000
