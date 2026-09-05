"""Sauvegarde chiffrée + restauration testée (v2026.09.019) :
- cycle complet export chiffré → perte de données → restauration chiffrée
  (actifs, valorisations, OPÉRATIONS et règles de revenu : rien ne se perd) ;
- mauvais mot de passe / fichier altéré → 400 et données intactes ;
- rétrocompatibilité : les anciens exports (actifs+valorisations seuls)
  restent importables ; l'export clair inclut désormais transactions+règles ;
- CLI scripts/backup.py : round-trip fichier (niveau ops) ;
- unités backup_crypto : unicode, erreurs, paramètres d'enveloppe.
"""
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-bak-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

import pytest
from fastapi.testclient import TestClient

import src.app as app
from src.backup_crypto import decrypt_bytes, encrypt_bytes

PWD = "member-pass-2026"
BPWD = "phrase-de-sauvegarde-tres-longue"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    _login(c, name, PWD)
    return c


def _seed_member_data(c):
    """Un actif + valorisations + opérations + règle de revenu."""
    r = c.post("/api/accounts", json={"name": "PEA backup", "asset_class": "bourse"})
    aid = r.json()["id"]
    c.post(f"/api/accounts/{aid}/valuation", json={"value": 1234.56, "val_date": "2026-01-15"})
    c.post(f"/api/accounts/{aid}/valuation", json={"value": 1300.0, "val_date": "2026-06-15"})
    conn = app.db_main()
    try:
        conn.execute(
            "INSERT INTO transactions (account_id, op_date, kind, amount, note) VALUES (?,?,?,?,?)",
            (aid, "2025-02-10", "deposit", 1000, "versement"),
        )
        conn.execute(
            "INSERT INTO income_rules (account_id, label, amount, freq, months_int, next_date) VALUES (?,?,?,?,?,?)",
            (aid, "Dividendes", 12.5, "monthly", 1, "2026-07-01"),
        )
        conn.commit()
    finally:
        conn.close()
    return aid


def _snapshot(c):
    """État complet d'un propriétaire, pour comparaison stricte."""
    conn = app.db_main()
    try:
        accs = [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE owner='bak-user' ORDER BY id").fetchall()]
        vals = [dict(r) for r in conn.execute(
            "SELECT v.* FROM valuations v JOIN accounts a ON a.id=v.account_id"
            " WHERE a.owner='bak-user' ORDER BY v.id").fetchall()]
        txs = [dict(r) for r in conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id"
            " WHERE a.owner='bak-user' ORDER BY t.id").fetchall()]
        rules = [dict(r) for r in conn.execute(
            "SELECT ir.* FROM income_rules ir JOIN accounts a ON a.id=ir.account_id"
            " WHERE a.owner='bak-user' ORDER BY ir.id").fetchall()]
    finally:
        conn.close()
    return accs, vals, txs, rules


def test_encrypted_backup_restore_full_round_trip(admin_c):
    """Export chiffré → wipe → restauration : tout revient à l'identique
    (actifs, valorisations, opérations, règles de revenu)."""
    c = _member(admin_c, "bak-user")
    aid = _seed_member_data(c)
    r = c.post("/api/export/encrypted", json={"password": BPWD})
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload.count(".") <= 1  # une seule chaîne base64 d'enveloppe

    # l'enveloppe se déchiffre et contient bien transactions + règles
    plain = json.loads(decrypt_bytes(payload, BPWD))
    assert plain["app"] == "patrimony" and len(plain["accounts"]) == 1
    assert len(plain["transactions"]) == 1 and len(plain["income_rules"]) == 1

    # perte de données simulée
    conn = app.db_main()
    try:
        conn.execute("DELETE FROM accounts WHERE owner='bak-user'")
        conn.commit()
    finally:
        conn.close()
    assert c.get("/api/summary").json()["total_value"] == 0.0

    # restauration chiffrée
    r = c.post("/api/import/encrypted", json={"payload": payload, "password": BPWD})
    assert r.status_code == 200, r.text
    accs, vals, txs, rules = _snapshot(c)
    assert len(accs) == 1 and accs[0]["id"] == aid and accs[0]["name"] == "PEA backup"
    assert len(vals) == 2 and vals[-1]["value"] == 1300.0
    assert len(txs) == 1 and txs[0]["kind"] == "deposit" and txs[0]["amount"] == 1000.0
    assert len(rules) == 1 and rules[0]["label"] == "Dividendes" and rules[0]["amount"] == 12.5
    s = c.get("/api/summary").json()
    assert s["total_value"] == 1300.0 and s["nb_accounts"] == 1


def test_encrypted_import_wrong_password_and_tamper_leave_data_intact(admin_c):
    c = _member(admin_c, "bak-user2")
    aid = _seed_member_data(c)
    r = c.post("/api/export/encrypted", json={"password": BPWD})
    payload = r.json()["payload"]

    r = c.post("/api/import/encrypted", json={"payload": payload, "password": "mauvais-mdp"})
    assert r.status_code == 400
    assert "Mot de passe invalide" in r.json()["detail"]
    # données intactes après l'échec
    assert {x["id"] for x in c.get("/api/accounts").json()["accounts"]} == {aid}

    # altération d'un octet du payload (payload != password vérifié de façon authentifiée)
    b = bytearray(payload, "ascii")
    b[len(b) // 2] ^= 1  # bascule 1 bit
    r = c.post("/api/import/encrypted", json={"payload": bytes(b).decode("ascii"), "password": BPWD})
    assert r.status_code == 400
    assert {x["id"] for x in c.get("/api/accounts").json()["accounts"]} == {aid}
    # mdp trop court pour l'export
    assert c.post("/api/export/encrypted", json={"password": "court"}).status_code == 400


def test_plain_export_now_contains_tx_and_legacy_import_still_works(admin_c):
    c = _member(admin_c, "bak-user3")
    aid = _seed_member_data(c)
    exp = c.get("/api/export").json()
    assert len(exp["transactions"]) == 1 and len(exp["income_rules"]) == 1
    # ancien format (sans transactions ni règles) : toujours importable
    legacy = {k: exp[k] for k in ("app", "accounts", "valuations")}
    conn = app.db_main()
    try:
        conn.execute("DELETE FROM accounts WHERE owner='bak-user3'")
        conn.commit()
    finally:
        conn.close()
    assert c.post("/api/import", json=legacy).status_code == 200
    accs = c.get("/api/accounts").json()["accounts"]
    assert len(accs) == 1 and accs[0]["id"] == aid


def test_backup_crypto_units():
    for text in ("héllo wörld € — données 123,45", b"\x00\x01\xfe\xff" * 100, "a" * 100_000):
        plain = text if isinstance(text, bytes) else text.encode("utf-8")
        enc = encrypt_bytes(plain, "p@ss ünïcode")
        assert decrypt_bytes(enc, "p@ss ünïcode") == plain
        with pytest.raises(ValueError):
            decrypt_bytes(enc, "autre")
        b = bytearray(enc, "ascii")
        b[5] = b"Z"[0] if b[5] != b"Z"[0] else b"Y"[0]
        with pytest.raises(ValueError):
            decrypt_bytes(bytes(b).decode("ascii"), "p@ss ünïcode")
    with pytest.raises(ValueError):
        decrypt_bytes("pas-une-enveloppe", "x")
    # enveloppe d'un autre app tag refusée
    env = json.loads(__import__("base64").b64decode(encrypt_bytes(b"x", "m")))
    env["app"] = "autre"
    fake = __import__("base64").b64encode(
        json.dumps(env).encode()).decode("ascii")
    with pytest.raises(ValueError):
        decrypt_bytes(fake, "m")


def test_backup_cli_file_round_trip():
    """scripts/backup.py : chiffre/déchiffre un FICHIER (niveau ops, DB complète)."""
    script = os.path.join(REPO, "scripts", "backup.py")
    if not os.path.exists(script):
        pytest.skip("scripts/backup.py absent")
    src = os.path.join(_tmp, "fichier-test.bin")
    enc = os.path.join(_tmp, "out.pat.b64")
    dec = os.path.join(_tmp, "back.bin")
    blob = os.urandom(4096) + b"patrimony-data-fin"
    with open(src, "wb") as f:
        f.write(blob)
    env = dict(os.environ, PATRIMONY_BACKUP_PASS="mdp-cli-ops")
    r = subprocess.run([sys.executable, script, "encrypt", src, enc], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, script, "decrypt", enc, dec], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    with open(dec, "rb") as f:
        assert f.read() == blob
