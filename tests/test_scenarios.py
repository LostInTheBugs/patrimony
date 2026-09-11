"""Scénarios de bout en bout — validation fonctionnelle « maturité v1 ».

Volontairement des PARCOURS complets (plusieurs modules enchaînés sur un
portefeuille réaliste) plutôt que des tests unitaires par module :

  1. chaîne de liquidation : brut → dettes → PV → pertes → fiscalité → net
  2. déménagement fiscal FR → LU sur un MÊME actif
  3. compte multi-devise : conversion BCE + actif exclu si taux manquant
  4. immobilier + crédit : dette au dashboard, échéancier, recalcul doux
  5. AV ≥ 8 ans vs < 8 ans (7,5 % vs 12,8 % — PS 17,2 % inchangés)
  6. PEA ≥ 5 ans vs < 5 ans (IR exonéré vs PFU à la clôture)
  7. CTO en moins-value (aucun impôt, perte affichée, warning explicite)
  8. membre standard vs protégé (asymétrie admin : lecture / reset / jetons)
  9. export → destruction → restauration (aller-retour complet par l'API)

Les cas dorés recopient les règles FR-2026 (PS 18,6 %, PFU IR 12,8 %,
AV 7,5/17,2 %, exo LU titres) — même source que tests/test_tax_engine.py.
Le détail fin des barèmes (immo : forfaits/abattements) reste couvert par
ses tests dédiés ; ici on vérifie l'INTÉGRATION et la COHÉRENCE.

Hors périmètre v1 (déjà couverts par leurs suites dédiées) : scan wallets
crypto (test_crypto), cycle de vie du coffre (test_recovery), flux profonds
Locations/TCO (test_estate) et crowdfunding (test_crowdfund).
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="pat-scen-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"
os.environ["PAT_CRYPTO_AUTO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app


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
              cost=100000.0, value=142800.0, open_date="2020-01-10",
              currency="EUR", fx_override=None):
    body = {
        "name": name, "asset_class": cls, "cost_basis": cost,
        "open_date": open_date, "wrapper": wrapper, "tax_country": country,
        "initial_value": value, "currency": currency,
    }
    if fx_override is not None:
        body["fx_override"] = fx_override
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _est(c, aid):
    r = c.get(f"/api/tax-estimate?account_id={aid}")
    assert r.status_code == 200, r.text
    return r.json()


def _mk_loan(c, name, account_id, initial, remaining, rate=2.5,
             monthly=700.0, start="2016-09-06", loan_type="immo"):
    r = c.post("/api/loans", json={
        "name": name, "loan_type": loan_type, "principal_initial": initial,
        "principal_remaining": remaining, "rate_annual": rate,
        "monthly_payment": monthly, "start_date": start,
        "account_id": account_id,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


admin_c = None


def _admin():
    global admin_c
    if admin_c is None:
        admin_c = TestClient(app.app)
        _login(admin_c, "admin", "admin-test-2026")
    return admin_c


# ---------------------------------------------------------- 1. chaîne de
# liquidation : brut → dettes → PV → pertes → fiscalité → net
def test_scenario_liquidation_chaine_complete():
    c = _mk_member(_admin(), "sc-liq")
    pea = _mk_asset(c, "PEA", wrapper="pea",
                    cost=20000.0, value=26500.0, open_date="2021-01-10")
    cto = _mk_asset(c, "CTO", wrapper="cto",
                    cost=12000.0, value=15800.0, open_date="2019-03-01")
    cto_mv = _mk_asset(c, "CTO en moins-value", wrapper="cto",
                       cost=5000.0, value=4200.0, open_date="2022-01-01")
    av = _mk_asset(c, "AV", cls="epargne", wrapper="av",
                   cost=25000.0, value=30500.0, open_date="2016-01-10")
    cash = _mk_asset(c, "Compte courant", cls="comptes",
                     cost=8500.0, value=8500.0)
    livret = _mk_asset(c, "Livret A", cls="epargne",
                       cost=15800.0, value=15800.0)
    crypto = _mk_asset(c, "Crypto", cls="crypto",
                       cost=3000.0, value=6400.0, open_date="2021-05-01")
    immo = _mk_asset(c, "Appartement", cls="immobilier",
                     cost=145000.0, value=172000.0, open_date="2021-03-01")
    _mk_loan(c, "Prêt appartement", immo, initial=92000.0, remaining=92000.0)

    # --- brut & dettes (dashboard) ---
    s = c.get("/api/summary").json()
    assert s["total_value"] == pytest.approx(279700.0, abs=0.01)
    assert s["total_debt"] == pytest.approx(92000.0, abs=0.01)
    assert s["net_worth"] == pytest.approx(279700.0 - 92000.0, abs=0.01)
    assert s["debt"]["part_pct"] == pytest.approx(
        92000.0 / 279700.0 * 100, abs=0.01)

    # --- PV (cas dorés FR-2026) ---
    t_pea = _est(c, pea)
    assert t_pea["regime"] == "pea" and t_pea["ruleset_version"] == "FR-2026"
    assert t_pea["income_tax"] == 0.0
    assert t_pea["social_contributions"] == pytest.approx(6500 * 0.186, abs=0.01)

    t_cto = _est(c, cto)
    assert t_cto["income_tax"] == pytest.approx(3800 * 0.128, abs=0.01)
    assert t_cto["social_contributions"] == pytest.approx(3800 * 0.186, abs=0.01)

    t_av = _est(c, av)
    assert t_av["income_tax"] == pytest.approx(5500 * 0.075, abs=0.01)
    assert t_av["social_contributions"] == pytest.approx(5500 * 0.172, abs=0.01)

    t_crypto = _est(c, crypto)
    assert t_crypto["income_tax"] == pytest.approx(3400 * 0.128, abs=0.01)
    assert t_crypto["social_contributions"] == pytest.approx(3400 * 0.186, abs=0.01)

    # --- pertes : moins-value → aucun impôt, perte affichée, warning ---
    t_mv = _est(c, cto_mv)
    assert t_mv["income_tax"] == 0.0 and t_mv["social_contributions"] == 0.0
    assert t_mv["estimated_net_gain"] == pytest.approx(-800.0, abs=0.01)
    assert "NEGATIVE_GAIN" in t_mv["warnings"]

    # --- hors périmètre : jamais silencieux ---
    assert _est(c, cash)["regime"] == "not_estimated"
    assert "NOT_ESTIMATED" in _est(c, cash)["warnings"]
    # épargne réglementée sans enveloppe : « hors plus-value » par conception
    t_livret = _est(c, livret)
    assert t_livret["regime"] == "not_estimated"
    assert "NOT_ESTIMATED" in t_livret["warnings"]

    # --- intégration immo (barèmes fins couverts par TestImmoFr) ---
    t_immo = _est(c, immo)
    assert t_immo["regime"] == "immo"
    assert t_immo["gross_gain"] == pytest.approx(27000.0, abs=0.01)

    # --- cohérence interne de CHAQUE estimation ---
    #   net = brut − moins-values + forfaits (kind=allowance, ex. frais/travaux
    #   immo) − impôts — équation vérifiée sur les 6 régimes, pertes incluses
    for t in (t_pea, t_cto, t_av, t_crypto, t_mv, t_immo):
        allowance = sum(l["amount"] for l in t["lines"]
                        if l["kind"] == "allowance")
        assert t["estimated_net_gain"] == pytest.approx(
            t["gross_gain"] - t["losses_applied"] + allowance
            - t["income_tax"] - t["social_contributions"] - t["extra_tax"],
            abs=0.02), t["regime"]
    # hors forfaits immo : base taxable = brut − moins-values appliquées
    for t in (t_pea, t_cto, t_av, t_crypto):
        assert t["taxable_gain"] == pytest.approx(
            t["gross_gain"] - t["losses_applied"], abs=0.02), t["regime"]
    # moins-value : base taxable ramenée à 0 (le warning porte la perte)
    assert t_mv["taxable_gain"] == pytest.approx(0.0, abs=0.01)

    # --- chaîne nette : brut − dettes − fiscalité → patrimoine net liquide ---
    taxes = sum(t["income_tax"] + t["social_contributions"] + t["extra_tax"]
                for t in (t_pea, t_cto, t_av, t_crypto, t_mv, t_immo))
    goldens = 1209.0 + 1193.2 + 1358.5 + 1067.6  # PEA, CTO, AV, crypto
    assert taxes >= goldens  # + la fiscalité immo (non figée ici)
    net_apres = s["net_worth"] - taxes
    assert 0 < net_apres < s["net_worth"]
    # les gains du portefeuille couvrent l'impôt estimé
    assert taxes < s["gain"]


# ---------------------------------------------------------- 2. déménagement
# fiscal FR → LU sur le même actif
def test_scenario_demenagement_fr_lu():
    c = _mk_member(_admin(), "sc-mig")
    aid = _mk_asset(c, "Titres migrés", wrapper=None,
                    cost=100000.0, value=142800.0, open_date="2020-01-10")

    d_fr = _est(c, aid)
    assert d_fr["ruleset_version"] == "FR-2026"
    assert d_fr["regime"] == "cto"
    assert d_fr["income_tax"] == pytest.approx(42800 * 0.128, abs=0.01)
    assert d_fr["social_contributions"] == pytest.approx(42800 * 0.186, abs=0.01)

    # le même actif passe au Luxembourg (déménagement du membre)
    acc = next(a for a in c.get("/api/accounts").json()["accounts"]
               if a["id"] == aid)
    payload = {k: acc[k] for k in (
        "name", "asset_class", "institution", "currency", "cost_basis",
        "fx_override", "open_date", "notes", "active", "valuation_mode",
        "symbol", "quantity", "wrapper") if k in acc}
    payload["tax_country"] = "lu"
    r = c.put(f"/api/accounts/{aid}", json=payload)
    assert r.status_code == 200, r.text

    d_lu = _est(c, aid)
    assert d_lu["ruleset_version"] == "LU-2026"
    assert d_lu["income_tax"] == 0.0
    assert d_lu["social_contributions"] == 0.0
    assert d_lu["estimated_net_gain"] == pytest.approx(42800.0, abs=0.01)
    assert "LU_TITRES_EXO_10PCT_6MOIS" in [l["id"] for l in d_lu["lines"]]
    assert "ASSUME_SUBSTANTIAL_NO" in d_lu["assumptions"]


# ---------------------------------------------------------- 3. compte
# multi-devise : conversion BCE, exclusion si taux manquant, override manuel
def test_scenario_multi_devise():
    c = _mk_member(_admin(), "sc-fx")
    chf = _mk_asset(c, "Compte CHF", cls="comptes",
                    cost=10000.0, value=10500.0, open_date="2026-06-01",
                    currency="CHF")

    # taux BCE posé comme le ferait /api/fx/refresh (1 EUR = 0,95 CHF).
    # La conversion utilise le taux AU JOUR DE LA VALORISATION — la
    # valorisation initiale porte la DATE D'OUVERTURE du compte.
    conn = app.db()
    conn.execute(
        "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source)"
        " VALUES (?,?,?,?)", ("CHF", "2026-06-01", 0.95, "ecb"))
    conn.commit()
    conn.close()

    s = c.get("/api/summary").json()
    assert "Compte CHF" not in s["fx_missing"]
    assert s["fx_applied"] is True
    assert s["fx_asof"] == "2026-06-01"
    assert s["total_value"] == pytest.approx(10500.0 / 0.95, abs=0.02)

    # second actif dans une devise SANS aucun taux en base → listé, exclu des
    # totaux (jamais muet) — CAD n'est seedé par aucun fichier de tests
    # (même convention que test_loans) ; la purge rend le scénario déterministe
    conn = app.db()
    conn.execute("DELETE FROM fx_rates WHERE ccy='CAD'")
    conn.commit()
    conn.close()
    _mk_asset(c, "Compte CAD", cls="comptes",
              cost=50000.0, value=55000.0, currency="CAD")
    s2 = c.get("/api/summary").json()
    assert "Compte CAD" in s2["fx_missing"]
    assert s2["total_value"] == pytest.approx(10500.0 / 0.95, abs=0.02)

    # override manuel prioritaire sur tout (dépannage d'un taux manquant)
    _mk_asset(c, "Compte USD", cls="comptes",
              cost=1000.0, value=1100.0, currency="USD", fx_override=0.90)
    s3 = c.get("/api/summary").json()
    assert "Compte USD" not in s3["fx_missing"]
    assert s3["total_value"] == pytest.approx(10500.0 / 0.95 + 1100.0 / 0.90,
                                              abs=0.02)


# ---------------------------------------------------------- 4. immobilier
# + crédit : dette au dashboard, échéancier cohérent, recalcul non intrusif
def test_scenario_immobilier_credit():
    c = _mk_member(_admin(), "sc-immo")
    immo = _mk_asset(c, "Appartement R4", cls="immobilier",
                     cost=200000.0, value=350000.0, open_date="2016-09-06")
    lid = _mk_loan(c, "Prêt R4", immo, initial=150000.0, remaining=140000.0)

    s = c.get("/api/summary").json()
    assert s["total_debt"] == pytest.approx(140000.0, abs=0.01)
    assert s["net_worth"] == pytest.approx(350000.0 - 140000.0, abs=0.01)
    assert s["debt"]["per_type"]["immo"] == pytest.approx(140000.0, abs=0.01)

    # équité visible sur la ligne Actifs (payload `loan` du compte lié)
    acc = next(a for a in c.get("/api/accounts").json()["accounts"]
               if a["id"] == immo)
    assert acc["loan"] is not None
    assert acc["loan"]["principal_remaining"] == pytest.approx(140000.0)

    # échéancier : mensualité = capital + intérêts, restant strictement
    # décroissant — la DERNIÈRE échéance est partielle (solde éteint)
    sch = c.get(f"/api/loans/{lid}/schedule").json()
    assert sch["months_left"] == len(sch["rows"]) > 0
    rows = sch["rows"]
    for row in rows[:-1]:
        assert row["interest"] + row["principal"] == pytest.approx(700.0, abs=0.02)
    last = rows[-1]
    assert last["interest"] + last["principal"] <= 700.0 + 0.02
    assert last["remaining"] == pytest.approx(0.0, abs=0.02)
    assert rows[0]["remaining"] == pytest.approx(140000.0 - rows[0]["principal"],
                                                 abs=0.02)
    remaining = [r["remaining"] for r in rows]
    assert all(b < a for a, b in zip(remaining, remaining[1:]))

    # recalcul : PROPOSE la valeur théorique, ne l'applique JAMAIS d'office
    rc = c.post(f"/api/loans/{lid}/recompute").json()
    assert rc["declared_remaining"] == pytest.approx(140000.0)
    assert rc["delta"] == pytest.approx(rc["theoretical_remaining"]
                                        - rc["declared_remaining"], abs=0.02)
    loans = c.get("/api/loans").json()["loans"]
    assert next(l for l in loans if l["id"] == lid)["principal_remaining"] \
        == pytest.approx(140000.0)


# ---------------------------------------------------------- 5. AV : ≥ 8 ans
# vs < 8 ans — même gain, fiscalité plus douce après 8 ans
def test_scenario_av_seuil_8_ans():
    c = _mk_member(_admin(), "sc-av")
    av8 = _mk_asset(c, "AV ancienne", cls="epargne", wrapper="av",
                    cost=25000.0, value=30500.0, open_date="2016-01-10")
    av_new = _mk_asset(c, "AV récente", cls="epargne", wrapper="av",
                       cost=25000.0, value=30500.0, open_date="2024-06-01")

    t8 = _est(c, av8)
    assert t8["income_tax"] == pytest.approx(5500 * 0.075, abs=0.01)
    assert t8["social_contributions"] == pytest.approx(5500 * 0.172, abs=0.01)
    assert "ASSUME_AV_PRIMES_150K" in t8["assumptions"]

    t_new = _est(c, av_new)
    assert t_new["income_tax"] == pytest.approx(5500 * 0.128, abs=0.01)
    assert t_new["social_contributions"] == pytest.approx(5500 * 0.172, abs=0.01)

    assert t8["income_tax"] < t_new["income_tax"]  # 7,5 % vs 12,8 %
    assert t8["estimated_net_gain"] > t_new["estimated_net_gain"]


# ---------------------------------------------------------- 6. PEA : ≥ 5 ans
# vs < 5 ans — IR exonéré vs PFU à la clôture anticipée
def test_scenario_pea_seuil_5_ans():
    c = _mk_member(_admin(), "sc-pea")
    pea_old = _mk_asset(c, "PEA ancien", wrapper="pea",
                        cost=100000.0, value=142800.0, open_date="2018-01-10")
    pea_new = _mk_asset(c, "PEA récent", wrapper="pea",
                        cost=100000.0, value=142800.0, open_date="2024-01-10")

    t_old = _est(c, pea_old)
    assert t_old["income_tax"] == 0.0
    assert t_old["social_contributions"] == pytest.approx(42800 * 0.186, abs=0.01)

    t_new = _est(c, pea_new)
    assert t_new["income_tax"] == pytest.approx(42800 * 0.128, abs=0.01)
    assert t_new["social_contributions"] == pytest.approx(42800 * 0.186, abs=0.01)

    assert t_old["estimated_net_gain"] > t_new["estimated_net_gain"]


# ---------------------------------------------------------- 7. CTO en
# moins-value : aucun impôt, perte affichée, warning explicite
def test_scenario_cto_moins_value():
    c = _mk_member(_admin(), "sc-loss")
    aid = _mk_asset(c, "CTO perdant", wrapper="cto",
                    cost=30000.0, value=24000.0, open_date="2021-06-01")
    t = _est(c, aid)
    assert t["income_tax"] == 0.0
    assert t["social_contributions"] == 0.0
    assert t["estimated_net_gain"] == pytest.approx(-6000.0, abs=0.01)
    assert "NEGATIVE_GAIN" in t["warnings"]


# ---------------------------------------------------------- 8. membre
# standard vs protégé : asymétrie admin (lecture / reset / jetons)
def test_scenario_membres_standard_vs_protege():
    admin = _admin()
    _mk_member(admin, "sc-std")
    assert admin.get("/api/summary?member=sc-std").status_code == 200

    r = admin.post("/api/family", json={
        "username": "sc-prot", "password": "ta-pwd-2026-long",
        "mode": "protected"})
    assert r.status_code == 200, r.text

    # admin : rien à lire (indistinguable d'un inconnu), rien à réinitialiser
    assert admin.get("/api/summary?member=sc-prot").status_code == 404
    r = admin.post("/api/family/sc-prot/reset-password",
                   json={"password": "nouveau-pwd-2026"})
    assert r.status_code == 403
    assert "par conception" in r.json()["detail"]

    fam = {m["username"]: m for m in admin.get("/api/family").json()["members"]}
    assert fam["sc-std"]["mode"] == "standard"
    assert fam["sc-prot"]["mode"] == "protected"

    # le membre protégé : connexion OK, données scellées tant que le coffre
    # n'est pas ouvert, jetons inaccessibles
    cp = TestClient(app.app)
    _login(cp, "sc-prot", "ta-pwd-2026-long")
    r = cp.get("/api/accounts")
    assert r.status_code == 403
    assert r.json().get("code") == "vault_locked"
    assert cp.get("/api/summary").status_code == 403
    assert cp.post("/api/tokens", json={"name": "t"}).status_code == 403

    # suppression (destruction du coffre incluse) — le seul droit admin
    assert admin.delete("/api/family/sc-prot").status_code == 200
    fam2 = [m["username"] for m in admin.get("/api/family").json()["members"]]
    assert "sc-prot" not in fam2


# ---------------------------------------------------------- 9. export →
# destruction → restauration : aller-retour complet par l'API
def test_scenario_export_destruction_restauration():
    c = _mk_member(_admin(), "sc-restore")
    a1 = _mk_asset(c, "Compte titres", wrapper="cto",
                   cost=10000.0, value=12000.0, open_date="2021-01-10")
    a2 = _mk_asset(c, "Livret", cls="epargne",
                   cost=5000.0, value=5200.0)
    a3 = _mk_asset(c, "Immeuble", cls="immobilier",
                   cost=150000.0, value=180000.0, open_date="2018-01-01")
    lid = _mk_loan(c, "Prêt immeuble", a3, initial=100000.0, remaining=90000.0)

    assert c.post("/api/transactions", json={
        "account_id": a1, "op_date": "2024-05-01", "kind": "deposit",
        "amount": 10000.0, "note": "apport"}).status_code == 200
    assert c.post("/api/income-rules", json={
        "account_id": a2, "label": "Intérêts", "amount": 8.0,
        "freq": "monthly", "next_date": "2026-10-01"}).status_code == 200
    assert c.put("/api/settings", json={
        "tax_tmi_fr": 30.0, "tax_married": 1}).status_code == 200

    snap = c.get("/api/export").json()
    counts = {k: len(snap[k]) for k in
              ("accounts", "valuations", "transactions", "income_rules",
               "loans")}
    assert counts == {"accounts": 3, "valuations": 3, "transactions": 1,
                      "income_rules": 1, "loans": 1}
    avant = c.get("/api/summary").json()
    avant_settings = c.get("/api/settings").json()

    # destruction (par l'API, comme le ferait un utilisateur)
    for a in c.get("/api/accounts").json()["accounts"]:
        assert c.delete(f"/api/accounts/{a['id']}").status_code == 200
    assert c.delete(f"/api/loans/{lid}").status_code == 200
    vide = c.get("/api/summary").json()
    assert vide["nb_accounts"] == 0 and vide["total_value"] == 0.0
    assert c.get("/api/loans").json()["loans"] == []

    # restauration : remplacement total, transactionnel
    assert c.post("/api/import", json=snap).status_code == 200
    apres = c.get("/api/summary").json()
    assert apres["nb_accounts"] == avant["nb_accounts"] == 3
    assert apres["total_value"] == pytest.approx(avant["total_value"], abs=0.01)
    assert apres["net_worth"] == pytest.approx(avant["net_worth"], abs=0.01)
    assert len(c.get("/api/loans").json()["loans"]) == 1
    assert len(c.get("/api/transactions").json()["transactions"]) == 1
    assert c.get("/api/settings").json()["tax_tmi_fr"] \
        == avant_settings["tax_tmi_fr"] == 30.0

    # la fiscalité refonctionne après restauration (gain 2 000 → PFU 31,4 %)
    t = _est(c, a1)
    assert t["income_tax"] == pytest.approx(2000 * 0.128, abs=0.01)
    assert t["social_contributions"] == pytest.approx(2000 * 0.186, abs=0.01)
