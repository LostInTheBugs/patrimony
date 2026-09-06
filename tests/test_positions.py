"""v2026.09.025 — positions (portefeuille multi-lignes bourse) : CRUD,
isolation, valorisation agrégée au refresh, PV brute (PRU), dividendes
(miroir comptable idempotent), frais annuels % (cumul), export/import."""
import os
import tempfile
from datetime import date

_tmp = tempfile.mkdtemp(prefix="patrimony-pos-")
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


def _new_member(admin_c, name):
    r = admin_c.post("/api/family", json={"username": name, "password": PWD, "mode": "standard"})
    assert r.status_code == 200, r.text
    c = TestClient(app.app)
    _login(c, name, PWD)
    return c


def _bourse_auto(c, name, open_date=None):
    body = {"name": name, "asset_class": "bourse", "valuation_mode": "auto", "currency": "EUR"}
    if open_date:
        body["open_date"] = open_date
    r = c.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_legacy_symbol_account_mirrors_first_position(admin_c):
    """Un compte bourse auto créé à l'ancienne (symbole+qty) devient un
    portefeuille à 1 ligne — l'actif reste le conteneur."""
    r = admin_c.post("/api/accounts", json={
        "name": "PEA historique", "asset_class": "bourse", "valuation_mode": "auto",
        "symbol": "IWDA.DE", "quantity": 12.5})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    assert len(acc["positions"]) == 1
    p = acc["positions"][0]
    assert p["symbol"] == "IWDA.DE" and p["quantity"] == 12.5 and p["label"] == "PEA historique"


def test_position_crud_guards_and_ownership(admin_c):
    aid = _bourse_auto(admin_c, "PEA CRUD")
    # classe/mode non autorisés
    rid = admin_c.post("/api/accounts", json={"name": "AV", "asset_class": "epargne"}).json()["id"]
    assert admin_c.post(f"/api/accounts/{rid}/positions",
                        json={"symbol": "X", "quantity": 1}).status_code == 400
    mid = admin_c.post("/api/accounts", json={
        "name": "PEA manuel", "asset_class": "bourse"}).json()["id"]
    assert admin_c.post(f"/api/accounts/{mid}/positions",
                        json={"symbol": "X", "quantity": 1}).status_code == 400
    # validations
    assert admin_c.post(f"/api/accounts/{aid}/positions",
                        json={"symbol": "", "quantity": 1}).status_code == 400
    assert admin_c.post(f"/api/accounts/{aid}/positions",
                        json={"symbol": "AAA", "quantity": 0}).status_code == 400
    assert admin_c.post(f"/api/accounts/{aid}/positions",
                        json={"symbol": "AAA", "quantity": 1, "pru": -2}).status_code == 400
    # création + reflet dans le payload
    r = admin_c.post(f"/api/accounts/{aid}/positions",
                     json={"symbol": "mc.pa", "label": "LVMH", "quantity": 3, "pru": 500})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    p = acc["positions"][0]
    assert p["symbol"] == "MC.PA" and p["label"] == "LVMH" and p["quantity"] == 3 and p["pru"] == 500
    # modification
    assert admin_c.put(f"/api/positions/{pid}",
                       json={"symbol": "MC.PA", "label": "LVMH", "quantity": 4, "pru": 510}).status_code == 200
    # isolation : un autre membre ne voit ni ne touche la ligne
    m = _new_member(admin_c, "pos-iso")
    assert all(x["id"] != aid for x in m.get("/api/accounts").json()["accounts"])
    assert m.put(f"/api/positions/{pid}",
                 json={"symbol": "MC.PA", "label": "", "quantity": 4, "pru": 0}).status_code == 404
    assert m.delete(f"/api/positions/{pid}").status_code == 404
    # suppression
    assert admin_c.delete(f"/api/positions/{pid}").status_code == 200
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    assert acc["positions"] == []


def test_portfolio_payload_gains_weights_and_fees(admin_c):
    aid = _bourse_auto(admin_c, "PEA gains")
    p1 = admin_c.post(f"/api/accounts/{aid}/positions",
                      json={"symbol": "AAA", "quantity": 10, "pru": 10}).json()["id"]
    p2 = admin_c.post(f"/api/accounts/{aid}/positions",
                      json={"symbol": "BBB", "quantity": 5, "pru": 20}).json()["id"]
    # cours en cache (EUR)
    conn = app.db_main()
    try:
        conn.execute("INSERT OR REPLACE INTO prices (symbol, price, currency, ts) VALUES (?,?,?,?)",
                     ("AAA", 12.0, "EUR", "2026-09-06T08:00:00"))
        conn.execute("INSERT OR REPLACE INTO prices (symbol, price, currency, ts) VALUES (?,?,?,?)",
                     ("BBB", 18.0, "EUR", "2026-09-06T08:00:00"))
        conn.commit()
    finally:
        conn.close()
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    by_sym = {p["symbol"]: p for p in acc["positions"]}
    assert by_sym["AAA"]["value_eur"] == 120.0 and by_sym["BBB"]["value_eur"] == 90.0
    assert by_sym["AAA"]["weight_pct"] == pytest.approx(57.14, abs=0.01)
    assert by_sym["BBB"]["weight_pct"] == pytest.approx(42.86, abs=0.01)
    assert by_sym["AAA"]["gain_eur"] == 20.0 and by_sym["AAA"]["gain_pct"] == pytest.approx(20.0)
    assert by_sym["BBB"]["gain_eur"] == -10.0 and by_sym["BBB"]["gain_pct"] == pytest.approx(-10.0)
    assert acc["positions"][0]["portfolio_eur"] == 210.0
    # ligne inactive : hors poids mais liste
    admin_c.put(f"/api/positions/{p2}", json={"symbol": "BBB", "quantity": 5, "pru": 20, "active": 0})
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    assert acc["positions"][1]["active"] is False
    admin_c.put(f"/api/positions/{p2}", json={"symbol": "BBB", "quantity": 5, "pru": 20, "active": 1})


def test_fees_pct_cumul(admin_c):
    r = admin_c.post("/api/accounts", json={
        "name": "AV frais", "asset_class": "epargne", "fees_pct": 1.2,
        "open_date": "2026-01-01"})
    assert r.status_code == 200
    aid = r.json()["id"]
    # plusieurs valorisations DANS un mois : seule la dernière de chaque mois
    # compte (refresh auto/captures quotidiens ne doivent pas sur-comptabiliser)
    for d, v in (("2026-01-15", 9500), ("2026-01-31", 10000),
                 ("2026-02-10", 9000), ("2026-02-28", 10000)):
        assert admin_c.post(f"/api/accounts/{aid}/valuation",
                            json={"value": v, "val_date": d}).status_code == 200
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    assert acc["fees_pct"] == 1.2
    assert acc["fees_paid"]["paid_eur"] == pytest.approx(20.0, abs=0.01)  # (10000+10000)×1.2%/12
    assert acc["fees_paid"]["months"] == 2
    # deux valorisations le MÊME dernier jour : la dernière saisie gagne (MAX(id))
    assert admin_c.post(f"/api/accounts/{aid}/valuation",
                        json={"value": 12000, "val_date": "2026-02-28"}).status_code == 200
    acc = next(a for a in admin_c.get("/api/accounts").json()["accounts"] if a["id"] == aid)
    assert acc["fees_paid"]["paid_eur"] == pytest.approx(22.0, abs=0.01)  # (10000+12000)×1.2%/12
    assert acc["fees_paid"]["months"] == 2
    # refus d'un taux négatif
    assert admin_c.post("/api/accounts", json={
        "name": "AV bad", "asset_class": "epargne", "fees_pct": -1}).status_code == 400


def test_refresh_portfolio_aggregation_and_backfill(admin_c, monkeypatch):
    """Refresh d'un portefeuille 2 lignes : cours dédoublonnés par symbole,
    valeur = Σ, backfill mensuel = Σ des closes (mois communs aux 2 lignes)."""
    m = _new_member(admin_c, "pos-ref")
    today = date.today()
    def mond(y, mo, last_day=True):
        import calendar
        d = calendar.monthrange(y, mo)[1] if last_day else 1
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m2 = today.month - 1 or 12
    y2 = today.year if m2 != 12 else today.year - 1
    m1 = m2 - 1 or 12
    y1 = y2 if m1 != 12 else y2 - 1
    open_ym = f"{y1:04d}-{m1:02d}-01"  # ouverture au mois du point le plus ancien
    aid = _bourse_auto(m, "PEA refresh", open_date=open_ym)
    m.post(f"/api/accounts/{aid}/positions", json={"symbol": "SYM1", "quantity": 10, "pru": 5})
    m.post(f"/api/accounts/{aid}/positions", json={"symbol": "SYM2", "quantity": 4, "pru": 5})

    def fake_quote(symbol, asset_class):
        return {"price": {"SYM1": 11.0, "SYM2": 22.0}[symbol], "currency": "EUR"}
    def fake_chart(symbol, rng, ivl):
        pts = {"SYM1": [(mond(y1, m1), 10.0), (mond(y2, m2), 11.0)],
               "SYM2": [(mond(y1, m1), 20.0), (mond(y2, m2), 22.0)]}
        return {"points": pts[symbol]}
    monkeypatch.setattr(app, "fetch_quote", fake_quote)
    monkeypatch.setattr(app, "_yahoo_chart", fake_chart)

    r = m.post("/api/refresh-prices")
    assert r.status_code == 200, r.text
    st = r.json()["status"]
    assert any(s["id"] == aid and s["value"] == pytest.approx(198.0) for s in st), st  # 10×11+4×22
    conn = app.db_main()
    try:
        rows = conn.execute(
            "SELECT val_date, value FROM valuations WHERE account_id=? ORDER BY val_date",
            (aid,)).fetchall()
    finally:
        conn.close()
    by_d = {x["val_date"]: x["value"] for x in rows}
    assert by_d[mond(y1, m1)] == pytest.approx(10 * 10.0 + 4 * 20.0)   # 180
    assert by_d[mond(y2, m2)] == pytest.approx(10 * 11.0 + 4 * 22.0)   # 198
    assert by_d.get(today.isoformat()) == pytest.approx(198.0)
    # les mois antérieurs à l'ouverture ne sont pas remontés
    assert len(by_d) == 3

    # ligne au cours introuvable → pas de valorisation partielle (honnête)
    conn = app.db_main()
    try:
        conn.execute("DELETE FROM valuations WHERE account_id=?", (aid,))
        conn.commit()
    finally:
        conn.close()
    def fake_quote2(symbol, asset_class):
        return None if symbol == "SYM2" else {"price": 11.0, "currency": "EUR"}
    monkeypatch.setattr(app, "fetch_quote", fake_quote2)
    r = m.post("/api/refresh-prices")
    assert any(s["id"] == aid and "cours" in s["error"] for s in r.json()["status"])


def test_dividend_mirror_sync(admin_c):
    aid = _bourse_auto(admin_c, "PEA div")
    pid = admin_c.post(f"/api/accounts/{aid}/positions",
                       json={"symbol": "TOTF.PA", "quantity": 4, "pru": 60}).json()["id"]
    # création → opération income miroir
    r = admin_c.post(f"/api/positions/{pid}/dividend",
                     json={"ex_date": "2026-06-30", "per_share": 2.5})
    assert r.status_code == 200, r.text
    txs = admin_c.get(f"/api/transactions?account_id={aid}").json()["transactions"]
    assert len(txs) == 1
    tx = txs[0]
    assert tx["amount"] == 10.0 and tx["kind"] == "income" and tx["op_date"] == "2026-06-30"
    assert tx["source_id"] == f"div:{pid}:2026-06-30"
    # montant/action modifié → miroir recalculé (idempotent, pas de doublon)
    admin_c.post(f"/api/positions/{pid}/dividend", json={"ex_date": "2026-06-30", "per_share": 3.0})
    txs = admin_c.get(f"/api/transactions?account_id={aid}").json()["transactions"]
    assert len(txs) == 1 and txs[0]["amount"] == 12.0
    # quantité modifiée sur la ligne → miroir resynchronisé
    admin_c.put(f"/api/positions/{pid}",
                json={"symbol": "TOTF.PA", "quantity": 5, "pru": 60})
    txs = admin_c.get(f"/api/transactions?account_id={aid}").json()["transactions"]
    assert txs[0]["amount"] == 15.0
    # suppression manuelle du miroir → refus (géré par la ligne)
    assert admin_c.delete(f"/api/transactions/{txs[0]['id']}").status_code == 400
    # suppression de l'événement → miroir retiré
    did = admin_c.get(f"/api/accounts").json()
    acc = next(a for a in did["accounts"] if a["id"] == aid)
    ev = acc["positions"][0]["dividends"][0]
    assert ev["amount"] == 15.0
    assert admin_c.delete(f"/api/dividends/{ev['id']}").status_code == 200
    assert admin_c.get(f"/api/transactions?account_id={aid}").json()["transactions"] == []
    # date invalide refusée
    assert admin_c.post(f"/api/positions/{pid}/dividend",
                        json={"ex_date": "2026-13-99", "per_share": 1}).status_code == 400
    # suppression de la ligne → événements et miroirs partis (recréés d'abord)
    admin_c.post(f"/api/positions/{pid}/dividend", json={"ex_date": "2026-06-30", "per_share": 1.0})
    assert admin_c.delete(f"/api/positions/{pid}").status_code == 200
    assert admin_c.get(f"/api/transactions?account_id={aid}").json()["transactions"] == []


def test_export_import_positions_roundtrip(admin_c):
    """Restaurer SON export (usage réel) : positions + événements + miroirs
    reviennent à l'identique."""
    src = _new_member(admin_c, "pos-xsrc")
    aid = _bourse_auto(src, "PEA export")
    pid = src.post(f"/api/accounts/{aid}/positions",
                   json={"symbol": "CSPX.L", "label": "S&P500", "quantity": 2.5, "pru": 400}).json()["id"]
    src.post(f"/api/positions/{pid}/dividend", json={"ex_date": "2026-03-31", "per_share": 1.1})
    payload = src.get("/api/export").json()
    assert len(payload["positions"]) == 1 and len(payload["dividend_events"]) == 1
    # suppression totale puis restauration depuis le payload
    assert src.delete(f"/api/accounts/{aid}").status_code == 200
    assert src.get("/api/accounts").json()["accounts"] == []
    r = src.post("/api/import", json=payload)
    assert r.status_code == 200, r.text
    acc = next(a for a in src.get("/api/accounts").json()["accounts"]
               if a["name"] == "PEA export")
    p = acc["positions"][0]
    assert p["symbol"] == "CSPX.L" and p["quantity"] == 2.5 and p["pru"] == 400
    assert p["dividends"][0]["per_share"] == 1.1 and p["dividends"][0]["ex_date"] == "2026-03-31"
    txs = src.get(f"/api/transactions?account_id={acc['id']}").json()["transactions"]
    assert any(t["source_id"] == f"div:{p['id']}:2026-03-31" for t in txs)


def test_csv_export_localized_values(admin_c):
    """v2026.09.025 — les exports CSV localisent les VALEURS humaines
    (classes d'actifs, types) selon Accept-Language ; les identifiants
    canoniques restent stables (ré-importables, en-têtes inchangés)."""
    b = _bourse_auto(admin_c, "PEA l10n")
    # un actif bourse + une opération income
    tx = admin_c.post("/api/transactions",
                      json={"account_id": b, "op_date": "2026-05-01", "kind": "income",
                            "amount": 50, "note": "Coupon"}).json()
    assert tx.get("id"), tx
    fr = admin_c.get("/api/export/csv/accounts").text
    assert "Bourse & assurances-vie" in fr and ",bourse," not in fr
    en = admin_c.get("/api/export/csv/accounts", headers={"Accept-Language": "en-US"}).text
    assert "Stocks & life insurance" in en
    de = admin_c.get("/api/export/csv/transactions", headers={"Accept-Language": "de-DE"}).text
    assert "Einkommen" in de and ",income," not in de
    # langue inconnue → FR
    fr2 = admin_c.get("/api/export/csv/accounts", headers={"Accept-Language": "zh-CN"}).text
    assert "Bourse & assurances-vie" in fr2


def test_wrapper_envelope_skeleton(admin_c):
    """v2026.09.027 — enveloppe fiscale par actif (pea|av|cto) : stockée,
    exposée dans le payload, validée par classe, préservée par l'export
    JSON (les règles de PV nette arriveront avec le feuillet FR/LU)."""
    # PEA sur un compte bourse + CTO + AV sur épargne
    pea = admin_c.post("/api/accounts", json={
        "name": "PEA Bourso", "asset_class": "bourse", "wrapper": "pea",
        "open_date": "2021-05-01"}).json()["id"]
    av = admin_c.post("/api/accounts", json={
        "name": "AV Linxea", "asset_class": "epargne", "wrapper": "av"}).json()["id"]
    accs = {a["id"]: a for a in admin_c.get("/api/accounts").json()["accounts"]}
    assert accs[pea]["wrapper"] == "pea" and accs[av]["wrapper"] == "av"
    assert accs[pea]["open_date"] == "2021-05-01"  # date d'ouverture (déjà existante)
    # édition : changement d'enveloppe, remise à vide
    assert admin_c.put(f"/api/accounts/{pea}", json={
        "name": "PEA Bourso", "asset_class": "bourse", "wrapper": "cto"}).status_code == 200
    accs = {a["id"]: a for a in admin_c.get("/api/accounts").json()["accounts"]}
    assert accs[pea]["wrapper"] == "cto"
    assert admin_c.put(f"/api/accounts/{pea}", json={
        "name": "PEA Bourso", "asset_class": "bourse", "wrapper": None}).status_code == 200
    accs = {a["id"]: a for a in admin_c.get("/api/accounts").json()["accounts"]}
    assert accs[pea]["wrapper"] is None
    # validations : enveloppe inconnue, enveloppe sur classe incompatible
    assert admin_c.post("/api/accounts", json={
        "name": "Bad1", "asset_class": "bourse", "wrapper": "livret"}).status_code == 400
    assert admin_c.post("/api/accounts", json={
        "name": "Bad2", "asset_class": "immobilier", "wrapper": "pea"}).status_code == 400
    assert admin_c.post("/api/accounts", json={
        "name": "Bad3", "asset_class": "epargne", "wrapper": "pea"}).status_code == 400
    # export/import JSON : l'enveloppe voyage avec l'actif (roundtrip réel :
    # on restaure SES données — ids AUTOINCREMENT partagés, jamais l'export
    # d'un autre membre dans la même base)
    dst = _new_member(admin_c, "env-dst")
    dpea = dst.post("/api/accounts", json={
        "name": "PEA Bourso", "asset_class": "bourse", "wrapper": "pea"}).json()["id"]
    payload = dst.get("/api/export").json()
    assert next(a for a in payload["accounts"] if a["id"] == dpea)["wrapper"] == "pea"
    assert dst.delete(f"/api/accounts/{dpea}").status_code == 200
    r_imp = dst.post("/api/import", json=payload)
    assert r_imp.status_code == 200, r_imp.text
    dst_acc = next(a for a in dst.get("/api/accounts").json()["accounts"]
                   if a["name"] == "PEA Bourso")
    assert dst_acc["wrapper"] == "pea"
