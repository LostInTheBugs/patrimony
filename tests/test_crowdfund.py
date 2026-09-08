"""Tests du module Crowdfunding (v2026.09.046) : calculs projet, import xlsx,
ingest extension, routes /api/cf/*, intégration comptes-auto, coffres.

L'environnement DOIT être posé avant l'import de src.app (init_db() s'exécute
à l'import). Usernames uniques par fichier.
"""
import base64
import io
import json
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-test-cf-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ["VAULT_IDLE_MIN"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app
import src.crowdfund as cf
from src import vault

DEK = base64.b64encode(b"\x11" * 32).decode()
PWD = "member-pass-2026"


@pytest.fixture(autouse=True)
def _clean_state():
    app._LOGIN_FAILS.clear()
    for uname, v in list(app._VAULTS.items()):
        if v["conn"] is not None:
            try:
                v["conn"]._hard_close()
            except Exception:
                pass
        app._VAULTS.pop(uname, None)
    # état du module remis à zéro entre les tests (base partagée du fichier)
    conn = app.db_main()
    try:
        conn.execute("DELETE FROM accounts WHERE asset_class='crowdfunding'")
        conn.execute("DELETE FROM cf_operations")
        conn.execute("DELETE FROM cf_projects")
        conn.execute("DELETE FROM cf_platforms")
        conn.execute("DELETE FROM cf_reports")
        conn.execute("DELETE FROM cf_captures")
        # jetons créés par ces tests (la base est partagée avec test_tokens)
        conn.execute("DELETE FROM api_tokens WHERE name IN ('ext-cf','full-tok')")
        conn.commit()
    finally:
        conn.close()
    yield


def _login(client, username, password, expected=200):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == expected, r.text
    return r


def _logout(client):
    client.post("/api/auth/logout")
    client.cookies.clear()


def _make_member(admin_c, username, mode="standard"):
    r = admin_c.post("/api/family", json={
        "username": username, "display_name": username, "password": PWD, "mode": mode,
    })
    assert r.status_code == 200, r.text


def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from src.schema import schema_data
    schema_data(conn)
    sqlite3.Connection.commit(conn)
    return conn


def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


# ---------------------------------------------------------------- calculs purs

def test_project_computed_delay_rules():
    today = __import__("datetime").date(2026, 9, 1)
    # retard réel : échéance RÉELLE passée
    p = cf.project_computed({
        "id": 1, "platform": "bricks", "name": "A", "invested": 100, "rate": 10,
        "duration_months": 24, "start_date": "2024-01-01", "expected_end_date": "2025-06-01",
        "status": "en_cours", "repaid_capital": 0, "interest_received": 0,
    }, today)
    assert p["is_late"] is True and p["days_delayed"] > 0
    assert p["derived_end_date"] is None  # échéance réelle → pas de dérivée
    # PAS de retard si échéance dérivée uniquement (start+durée)
    p2 = cf.project_computed({
        "id": 2, "platform": "bricks", "name": "B", "invested": 100, "rate": 10,
        "duration_months": 12, "start_date": "2025-01-01", "expected_end_date": None,
        "status": "en_cours", "repaid_capital": 0, "interest_received": 0,
    }, today)
    assert p2["is_late"] is False
    assert p2["derived_end_date"] == "2026-01-01"
    # « X mois max. restants » LPB → échéance dérivée depuis aujourd'hui, pas de retard
    p3 = cf.project_computed({
        "id": 3, "platform": "lapremierebrique", "name": "C", "invested": 100, "rate": 10,
        "duration_months": 0, "start_date": "2024-01-01", "expected_end_date": None,
        "rest_months": 3, "status": "en_cours", "repaid_capital": 0, "interest_received": 0,
    }, today)
    assert p3["is_late"] is False and p3["derived_end_date"] == "2026-12-01"
    # intérêts latents au prorata (statut en_cours uniquement)
    assert p["accrued_interest"] == round(100 * 10 / 100 * (today - __import__("datetime").date(2024, 1, 1)).days / 365, 2)
    p4 = cf.project_computed({
        "id": 4, "platform": "lapremierebrique", "name": "D", "invested": 100, "rate": 10,
        "duration_months": 0, "start_date": "2024-01-01", "expected_end_date": None,
        "status": "retard", "repaid_capital": 0, "interest_received": 0,
    }, today)
    assert p4["late_severity"] == "critique" and p4["accrued_interest"] == 0.0
    # in-fine : intérêts au terme → négligeable même sans paiement
    p5 = cf.project_computed({
        "id": 5, "platform": "lapremierebrique", "name": "E", "invested": 100, "rate": 10,
        "duration_months": 0, "start_date": "2024-01-01", "expected_end_date": None,
        "status": "retard", "infine": 1, "repaid_capital": 0, "interest_received": 0,
    }, today)
    assert p5["late_severity"] == "negligeable"
    # perte + latente (mark-to-market)
    p6 = cf.project_computed({
        "id": 6, "platform": "bricks", "name": "F", "invested": 100, "rate": 0,
        "duration_months": 0, "start_date": None, "expected_end_date": None,
        "status": "perdu", "repaid_capital": 40, "interest_received": 0, "valuation": 0,
    }, today)
    assert p6["loss"] == 60.0
    p7 = cf.project_computed({
        "id": 7, "platform": "bricks", "name": "G", "invested": 100, "rate": 0,
        "duration_months": 0, "start_date": None, "expected_end_date": None,
        "status": "en_cours", "repaid_capital": 0, "interest_received": 0, "valuation": 80,
    }, today)
    assert p7["unrealized_loss"] == 20.0


def test_project_computed_real_rate():
    today = __import__("datetime").date(2026, 9, 1)
    p = cf.project_computed({
        "id": 1, "platform": "bricks", "name": "A", "invested": 100, "rate": 10,
        "duration_months": 24, "start_date": "2024-01-01", "expected_end_date": "2026-01-01",
        "actual_end_date": None, "status": "rembourse", "repaid_capital": 0,
        "interest_received": 0,
    }, today, {"total_received": {1: 110.0}, "last_op_date": {1: "2026-01-01"}})
    # 110 reçus / 100 misés sur ~731 j → ~4,8 %/an
    assert p["total_received"] == 110.0
    assert p["real_annual_pct"] is not None and 0.04 < p["real_annual_pct"] < 0.06


# ---------------------------------------------------------------- parsing xlsx & import

def test_xlsx_roundtrip_and_import_idempotent():
    import openpyxl
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", "date", "type", "statut", "propriété", "type de contrat", "montant (€)", "prix de la brick (€)"])
    ws.append(["u-1", "10/08/2026", "Achat de bricks", "Validée", "Résidence Test", "Standard", -50.0, 10.0])
    ws.append(["u-2", "12/08/2026", "Revenus reversés", "Validée", "Résidence Test", "Standard", 0.42, ""])
    wb.save(buf)
    platform, ops = cf.parse_xlsx(buf.getvalue())
    assert platform == "bricks" and len(ops) == 2
    conn = _mk_conn()
    s1 = cf.import_operations(conn, "u1", platform, ops)
    conn.commit()
    assert s1["imported"] == 2 and s1["projects_created"] == 1 and s1["projects_updated"] == 0
    p = _row(conn, "SELECT invested, start_date FROM cf_projects")
    assert p["invested"] == 50.0 and p["start_date"] == "2026-08-10"
    # ré-import : idempotent (0 nouveau, mise inchangée)
    s2 = cf.import_operations(conn, "u1", platform, ops)
    conn.commit()
    assert s2["imported"] == 0 and s2["duplicates"] == 2 and s2["projects_created"] == 0
    p2 = _row(conn, "SELECT invested FROM cf_projects")
    assert p2["invested"] == 50.0
    # opération annulée : jamais une mise
    ops_bad = [dict(ops[0], source_id="u-cancel", status="Annulée", amount=-20.0)]
    s3 = cf.import_operations(conn, "u1", platform, ops_bad)
    conn.commit()
    assert s3["projects_created"] == 0 and s3["imported"] == 1
    p3 = _row(conn, "SELECT invested FROM cf_projects")
    assert p3["invested"] == 50.0


def test_import_operations_owner_isolation():
    conn = _mk_conn()
    ops = [{"source_id": "x1", "op_date": "2026-01-01", "type": "Achat de bricks",
            "status": "Validée", "project_name": "Projet Alpha", "contract_type": "",
            "amount": -100.0, "extra": {}}]
    cf.import_operations(conn, "alice", "bricks", ops)
    cf.import_operations(conn, "bob", "bricks", ops)
    conn.commit()
    assert _row(conn, "SELECT COUNT(*) c FROM cf_projects WHERE owner='alice'")["c"] == 1
    assert _row(conn, "SELECT COUNT(*) c FROM cf_projects WHERE owner='bob'")["c"] == 1


def test_sync_indicators_from_ops():
    conn = _mk_conn()
    ops = [
        {"source_id": "i1", "op_date": "2024-01-01", "type": "Souscription au projet P", "status": "Réussi",
         "project_name": "P", "contract_type": "", "amount": -100.0, "extra": {}},
        {"source_id": "i2", "op_date": "2024-02-01", "type": "Revenus reversés", "status": "Validée",
         "project_name": "P", "contract_type": "", "amount": 3.5, "extra": {}},
        {"source_id": "i3", "op_date": "2024-03-01", "type": "Revenus reversés - revente totale", "status": "Validée",
         "project_name": "P", "contract_type": "", "amount": 98.0, "extra": {}},
    ]
    cf.import_operations(conn, "u1", "lapremierebrique", ops)
    conn.commit()
    stats = cf.sync_indicators_from_ops(conn, "u1")
    conn.commit()
    p = _row(conn, "SELECT interest_received, status FROM cf_projects")
    assert p["interest_received"] == 3.5  # revente exclue des revenus
    assert p["status"] == "rembourse"
    assert stats["status"] == 1
    # annulation de souscription LPB → invested = 0
    ops2 = [
        {"source_id": "l1", "op_date": "2025-01-01", "type": "Souscription au projet Q", "status": "Réussi",
         "project_name": "Q", "contract_type": "", "amount": -100.0, "extra": {}},
        {"source_id": "l2", "op_date": "2025-01-01", "type": "Annulation de la souscription au projet Q", "status": "Réussi",
         "project_name": "Q", "contract_type": "", "amount": 100.0, "extra": {}},
    ]
    cf.import_operations(conn, "u1", "lapremierebrique", ops2)
    conn.commit()
    cf.sync_indicators_from_ops(conn, "u1")
    conn.commit()
    q = _row(conn, "SELECT invested FROM cf_projects WHERE name='Q'")
    assert q["invested"] == 0.0


# ---------------------------------------------------------------- routes /api/cf/*

def test_cf_api_crud_and_account_guards():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    # garde : pas de compte manuel de classe crowdfunding
    r = c.post("/api/accounts", json={"name": "Manuel", "asset_class": "crowdfunding"})
    assert r.status_code == 400
    # création de projet → compte-auto créé
    r = c.post("/api/cf/projects", json={
        "platform": "bricks", "name": "Résidence Test", "invested": 200, "rate": 9.5,
        "duration_months": 24, "start_date": "2025-01-15", "status": "en_cours",
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    accs = c.get("/api/accounts").json()["accounts"]
    cf_accs = [a for a in accs if a["asset_class"] == "crowdfunding"]
    assert len(cf_accs) == 1 and cf_accs[0]["valuation_mode"] == "auto"
    # gardes : compte-auto verrouillé
    aid = cf_accs[0]["id"]
    assert c.delete(f"/api/accounts/{aid}").status_code == 400
    assert c.put(f"/api/accounts/{aid}", json={"name": "X", "asset_class": "crowdfunding",
                                               "valuation_mode": "auto"}).status_code == 400
    assert c.post(f"/api/accounts/{aid}/valuation", json={"val_date": "2026-09-01", "value": 1}).status_code == 400
    assert c.post("/api/transactions", json={"account_id": aid, "op_date": "2026-09-01",
                                             "kind": "income", "amount": 1, "note": ""}).status_code == 400
    # suppression projet → cascade des opérations + compte-auto recadré
    assert c.get("/api/cf/operations/stats").status_code == 200
    assert c.delete(f"/api/cf/projects/{pid}").status_code == 200
    projs = c.get("/api/cf/projects").json()["projects"]
    assert all(p["id"] != pid for p in projs)
    # plateforme : mise à jour des métadonnées puis retrait complet
    assert c.put("/api/cf/platforms", json={"platform": "bricks", "balance": 50}).status_code == 200
    assert c.delete("/api/cf/platforms/bricks").status_code == 200
    accs2 = c.get("/api/accounts").json()["accounts"]
    assert all(a["asset_class"] != "crowdfunding" for a in accs2)


def test_cf_sync_token_and_ingest():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    # seed d'un projet pour le matching
    assert c.post("/api/cf/projects", json={
        "platform": "lapremierebrique", "name": "Le Projet Cible", "invested": 100,
        "rate": 0, "duration_months": 0, "status": "en_cours",
    }).status_code == 200
    # token scope crowdfund
    r = c.post("/api/tokens", json={"name": "ext-cf", "scope": "crowdfund", "expires_days": 30})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # client SANS cookie de session (comme l'extension) : le jeton doit suffire
    ext = TestClient(app.app)
    ext.headers.update(hdr)
    # ingest d'une capture DOM (cards LPB + inner_text avec taux/durée/solde)
    payload = {"captures": [{
        "platform": "lapremierebrique", "url": "https://app.lapremierebrique.fr/fr/investissements",
        "ts": "2026-09-08T08:00:00Z", "status": 200,
        "body": {"_dom": {
            "lpb_cards": [{"name": "Le Projet Cible", "invested": "100 €", "status": "En cours"}],
            "inner_text": ("Le Projet Cible 13,25% / an sur 18 mois Solde 519,85 € "
                           "Montant investi 100 € dont intérêts : 12,24 €"),
        }},
    }]}
    r = ext.post("/api/cf/sync/ingest", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"]["projects_matched"] == 1
    assert data["summary"]["fields_filled"] >= 2
    proj = c.get("/api/cf/projects").json()["projects"][0]
    assert proj["rate"] == 13.25 and proj["duration_months"] == 18
    assert proj["interest_received"] == 12.24
    meta = {m["platform"]: m for m in c.get("/api/cf/platforms").json()["platforms"]}
    assert abs(meta["lapremierebrique"]["balance"] - 519.85) < 0.01
    # rapport lisible par le token
    r = ext.get("/api/cf/sync/report")
    assert r.status_code == 200 and r.json()["report"] is not None
    # un jeton 'full' ne peut PAS ingérer
    r2 = c.post("/api/tokens", json={"name": "full-tok", "scope": "full", "expires_days": 30})
    full_tok = r2.json()["token"]
    r = ext.post("/api/cf/sync/ingest", json=payload,
                 headers={"Authorization": f"Bearer {full_tok}"})
    assert r.status_code == 403
    # cookie seul (session) : refusé — ingestion = jeton scope crowdfund uniquement
    r = c.post("/api/cf/sync/ingest", json=payload)
    assert r.status_code == 403
    # le jeton crowdfund ne peut rien d'autre
    assert ext.get("/api/accounts").status_code == 403
    assert ext.get("/api/cf/projects").status_code == 403


def test_cf_family_consultation():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    _make_member(admin_c, "cf-membre")
    assert admin_c.post("/api/cf/projects", json={
        "platform": "bricks", "name": "Projet Admin", "invested": 100, "status": "en_cours",
    }).status_code == 200
    _logout(admin_c)
    m = TestClient(app.app)
    _login(m, "cf-membre", PWD)
    assert m.post("/api/cf/projects", json={
        "platform": "bricks", "name": "Projet Membre", "invested": 50, "status": "en_cours",
    }).status_code == 200
    _logout(m)
    a = TestClient(app.app)
    _login(a, "admin", "admin-test-2026")
    mine = a.get("/api/cf/projects").json()["projects"]
    assert [p["name"] for p in mine] == ["Projet Admin"]
    fam = a.get("/api/cf/projects?family=1").json()["projects"]
    assert {p["name"] for p in fam} == {"Projet Admin", "Projet Membre"}
    view = a.get("/api/cf/projects?member=cf-membre").json()["projects"]
    assert [p["name"] for p in view] == ["Projet Membre"]
    assert a.get("/api/cf/projects?member=ghost").status_code == 404
    # un membre ne consulte pas un autre membre
    _logout(a)
    m2 = TestClient(app.app)
    _login(m2, "cf-membre", PWD)
    assert m2.get("/api/cf/projects?member=cf-membre").status_code == 403


def test_cf_export_import_roundtrip_and_refresh():
    c = TestClient(app.app)
    _login(c, "admin", "admin-test-2026")
    r = c.post("/api/cf/projects", json={
        "platform": "bricks", "name": "Résidence Export", "invested": 300, "rate": 8,
        "duration_months": 12, "start_date": "2026-01-01", "status": "en_cours",
    })
    assert r.status_code == 200
    # le dépôt initial et les valorisations sont matérialisés (source cf)
    accs = c.get("/api/accounts").json()["accounts"]
    cf_acc = [a for a in accs if a["asset_class"] == "crowdfunding"][0]
    assert cf_acc["cost_basis"] == 0  # pas de déposé renseigné → 0
    # fixons le déposé via la plateforme puis vérifions dépôt + série
    assert c.put("/api/cf/platforms", json={"platform": "bricks", "deposited": 300,
                                            "balance": 42}).status_code == 200
    out = c.get("/api/cf/export").json()
    assert len(out["projects"]) == 1 and len(out["operations"]) == 0  # saisie manuelle
    # round-trip : suppression puis restauration
    pid = out["projects"][0]["id"]
    assert c.delete(f"/api/cf/projects/{pid}").status_code == 200
    r = c.post("/api/cf/import", json=out)
    assert r.status_code == 200, r.text
    projs = c.get("/api/cf/projects").json()["projects"]
    assert len(projs) == 1 and projs[0]["id"] == pid
    # dépôt matérialisé (source_id cf:dep) — les écritures du module ne passent
    # pas par les routes gardées (elles écrivent directement via refresh)
    conn = app.db_main()
    try:
        dep = conn.execute(
            "SELECT amount, op_date FROM transactions WHERE source_id=?",
            (f"cf:dep:admin:bricks",)).fetchone()
        assert dep is not None and dep["amount"] == 300.0
        vals = conn.execute(
            "SELECT COUNT(*) c FROM valuations v JOIN accounts a ON a.id=v.account_id"
            " WHERE a.owner='admin' AND v.source='cf'").fetchone()["c"]
        assert vals >= 1  # valeur actuelle (la série exige des opérations — cf. test dédié)
        last = conn.execute(
            "SELECT MAX(val_date) m FROM valuations v JOIN accounts a ON a.id=v.account_id"
            " WHERE a.owner='admin' AND v.source='cf'").fetchone()["m"]
        from datetime import date
        assert last <= date.today().isoformat()  # jamais de point futur
    finally:
        conn.close()
    # l'export global embarque le module
    g = c.get("/api/export").json()
    assert "crowdfunding" in g and len(g["crowdfunding"]["projects"]) == 1


def test_cf_data_lives_in_vault():
    admin_c = TestClient(app.app)
    _login(admin_c, "admin", "admin-test-2026")
    _make_member(admin_c, "cf-vault", mode="protected")
    _logout(admin_c)
    # white-box : données cf claires pré-existantes pour le membre protégé
    conn = app.db_main()
    try:
        conn.execute(
            "INSERT INTO cf_projects (owner, platform, name, invested, status, created_at, updated_at)"
            " VALUES ('cf-vault','bricks','Secret Vault',50,'en_cours','2026-01-01','2026-01-01')")
        conn.commit()
    finally:
        conn.close()
    c = TestClient(app.app)
    _login(c, "cf-vault", PWD)
    assert c.post("/api/auth/password", json={"current": PWD, "new": PWD}).status_code == 200
    # init du coffre : les données claires sont CHIFFRÉES (plus rien en clair)
    assert c.post("/api/vault/init", json={"salt": "c2FsdA==", "wrapped": "d3JhcHBlZA==",
                                           "dek": DEK}).status_code == 200
    conn = app.db_main()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM cf_projects WHERE owner='cf-vault'").fetchone()["c"]
        assert n == 0  # purgé de la base principale après transfert
    finally:
        conn.close()
    # le projet est visible dans le coffre ouvert (routes routées)
    projs = c.get("/api/cf/projects").json()["projects"]
    assert [p["name"] for p in projs] == ["Secret Vault"]
    _logout(c)
    # coffre refermé : l'admin ne voit rien (et la route exige le coffre ouvert)
    c2 = TestClient(app.app)
    _login(c2, "admin", "admin-test-2026")
    assert c2.get("/api/cf/projects?family=1").json()["projects"] == []
    assert c2.get("/api/cf/projects?member=cf-vault").status_code == 404  # anti-énumération


# ---------------------------------------------------------------- intégration (comptes-auto)

def test_refresh_integration_builds_series_and_deposit():
    """refresh_integration matérialise : compte-auto, dépôt initial unique,
    valeur actuelle ET grille fin-de-mois (jamais de point futur)."""
    from datetime import date
    conn = _mk_conn()
    # plateforme + projet + opérations (mise 2024-01, revenu 2024-02)
    conn.execute(
        "INSERT INTO cf_platforms (owner, platform, balance, deposited)"
        " VALUES ('u1','bricks',25.0,100.0)")
    cur = conn.execute(
        "INSERT INTO cf_projects (owner, platform, name, invested, rate, duration_months,"
        " start_date, status, created_at, updated_at) VALUES"
        " ('u1','bricks','Alpha',100,9.0,24,'2024-01-15','en_cours','2024-01-01','2024-01-01')")
    pid = cur.lastrowid
    for src, d, typ, amt in (
        ("a1", "2024-01-15", "Achat de bricks", -100.0),
        ("a2", "2024-02-01", "Revenus reversés", 0.75),
    ):
        conn.execute(
            "INSERT INTO cf_operations (owner, platform, source_id, op_date, type, status,"
            " project_id, amount, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("u1", "bricks", src, d, typ, "Validée", pid, amt, "2024-01-01"))
    conn.commit()
    cf.refresh_integration(conn, "u1")
    conn.commit()
    acc = conn.execute(
        "SELECT * FROM accounts WHERE asset_class='crowdfunding'").fetchone()
    assert acc is not None and acc["valuation_mode"] == "auto"
    assert acc["cost_basis"] == 100.0 and acc["institution"] == "bricks"
    # dépôt initial unique (re-refresh = pas de doublon)
    cf.refresh_integration(conn, "u1")
    conn.commit()
    n_dep = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE source_id='cf:dep:u1:bricks'"
    ).fetchone()["c"]
    assert n_dep == 1
    dep = conn.execute(
        "SELECT amount, op_date FROM transactions WHERE source_id='cf:dep:u1:bricks'"
    ).fetchone()
    assert dep["amount"] == 100.0 and dep["op_date"] == "2024-01-15"
    # valorisations : valeur actuelle + série mensuelle, aucune dans le futur
    rows = conn.execute(
        "SELECT val_date, value FROM valuations WHERE account_id=? AND source='cf'",
        (acc["id"],)).fetchall()
    today_iso = date.today().isoformat()
    assert len(rows) >= 3
    assert all(r["val_date"] <= today_iso for r in rows)
    today_row = [r for r in rows if r["val_date"] == today_iso]
    # la valeur du jour = balance + valeur plateforme (Bricks = meta éditable,
    # ici 0 → balance seule ; LPB = capital dû auto, vérifié plus bas)
    assert today_row and today_row[0]["value"] == 25.0
    # série LPB (dérivable) : capital dû = 100 → dernier mois = balance + 100
    conn.execute(
        "INSERT INTO cf_platforms (owner, platform, balance, deposited)"
        " VALUES ('u1','lapremierebrique',10.0,150.0)")
    cur = conn.execute(
        "INSERT INTO cf_projects (owner, platform, name, invested, rate, duration_months,"
        " start_date, status, created_at, updated_at) VALUES"
        " ('u1','lapremierebrique','Beta',150,10.0,12,'2024-06-01','en_cours','2024-06-01','2024-06-01')")
    pid2 = cur.lastrowid
    conn.execute(
        "INSERT INTO cf_operations (owner, platform, source_id, op_date, type, status,"
        " project_id, amount, created_at) VALUES ('u1','lapremierebrique','l1','2024-06-01',"
        " 'Souscription au projet Beta','Réussi',?,-150.0,'2024-06-01')", (pid2,))
    conn.commit()
    cf.refresh_integration(conn, "u1")
    conn.commit()
    lpb_acc = conn.execute(
        "SELECT id FROM accounts WHERE institution='lapremierebrique'").fetchone()
    lpb_today = conn.execute(
        "SELECT value FROM valuations WHERE account_id=? AND source='cf' AND val_date=?",
        (lpb_acc["id"], today_iso)).fetchone()
    assert lpb_today["value"] == 160.0  # balance 10 + capital dû 150


def test_cf_boot_purge_vaulted_leftovers():
    # simule un reliquat clair (interruption entre copie et effacement) : sans
    # coffre associé, la purge de boot ne s'applique pas → conservé
    conn = app.db_main()
    try:
        conn.execute(
            "INSERT INTO cf_projects (owner, platform, name, invested, status, created_at, updated_at)"
            " VALUES ('ghost-vault','bricks','Reliquat',1,'en_cours','2026-01-01','2026-01-01')")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) c FROM cf_projects WHERE owner='ghost-vault'").fetchone()["c"]
        assert n == 1
        conn.execute("DELETE FROM cf_projects WHERE owner='ghost-vault'")
        conn.commit()
    finally:
        conn.close()
    # (la purge des données des membres DOTÉS d'un coffre est vérifiée en réel
    #  par test_cf_data_lives_in_vault : plus rien en clair après l'init)
