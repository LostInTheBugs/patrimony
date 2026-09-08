"""Tests module Crypto wallets non-custodial (v2026.09.050).

Déterministes et SANS réseau : les fonctions réseau du module sont
monkeypatchées ou contournées (wallet demo / fixtures locales). Les
connexions sont en mémoire (schema_data) avec row_factory = Row.
"""

import datetime
import sqlite3

import pytest

from src import crypto
from src.schema import schema_data

TODAY = datetime.date.today()
D0 = datetime.date(2024, 1, 15)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    schema_data(c)
    return c


def _fx(c: sqlite3.Connection, rate: float = 1.08) -> None:
    """Taux USD BCE de secours (une ligne par mois 2020→2027) — aucun appel
    réseau depuis refresh_integration."""
    d = datetime.date(2020, 1, 1)
    while d < datetime.date(2027, 1, 1):
        c.execute("INSERT OR IGNORE INTO fx_rates (ccy, rate_date, rate, source)"
                  " VALUES ('USD',?,?,'ecb')", (d.isoformat(), rate))
        d = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    c.commit()


def _wallet(c: sqlite3.Connection, owner: str = "alice",
            label: str = "Ledger", demo: int = 0) -> int:
    cur = c.execute(
        "INSERT INTO cw_wallets (owner, label, address, chain, watch_only,"
        " demo, status, created_at) VALUES (?,?,?,?,1,?, 'ok', '2026-09-08T00:00:00Z')",
        (owner, label, "0x" + "ab" * 20, "ethereum", demo))
    return cur.lastrowid


def _tx(c: sqlite3.Connection, wid: int, owner: str, d: str, sym: str,
        amt: float, direction: str, price: float | None = None,
        chain: str = "ethereum", addr: str = "", idx: int = 0) -> None:
    c.execute(
        "INSERT INTO cw_transfers (wallet_id, owner, tx_hash, log_index, chain,"
        " block_time, token_symbol, token_name, token_addr, direction, amount,"
        " usd_price, usd_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wid, owner, f"0x{abs(hash((d, sym, amt, idx))):064x}", idx, chain,
         f"{d} 12:00:00", sym, sym, addr, direction, amt,
         price or 0, round((price or 0) * amt, 2) if price else 0))


# ---------------------------------------------------------------- unitaires

def test_chain_configs_synced():
    assert set(crypto.CHAINS) == set(crypto.CHAIN_TO_LLAMA) == set(crypto.NATIVE_COIN)
    assert "hyperevm" in crypto.CHAINS and "worldchain" in crypto.CHAINS
    assert len(crypto.CHAINS) >= 21


def test_spam_none_safe_and_moon_not_beefy():
    assert crypto._is_spam("Visit https://claim.xyz") is True
    assert crypto._is_spam("USD0") is False
    assert crypto._is_spam(None) is False
    assert crypto._is_spam("") is False
    # moo + majuscule sur l'ORIGINAL (pitfall 121) : « moon »/« mo0n » ≠ Beefy
    assert crypto._token_category("mooVeloV2") == "vault"
    assert crypto._token_category("mooBIFI") == "vault"
    assert crypto._token_category("MOON") == "wallet"
    assert crypto._token_category("mo0n") == "wallet"
    assert crypto._token_category(None) == "wallet"
    assert crypto._token_category("wstETH") == "staked"
    assert crypto._token_category("wsteth") == "staked"


def test_token_tid_and_address():
    assert crypto.token_tid("eth", "ethereum", "") == "ethereum:eth"
    assert crypto.token_tid("USDC", "base", "0xABC") == "0xabc"
    assert crypto.is_evm_address("0x" + "ab" * 20)
    assert not crypto.is_evm_address("0x" + "ab" * 19)
    assert not crypto.is_evm_address("nawak")
    assert crypto._norm_addr("0xAB" + "cd" * 19) == "0xab" + "cd" * 19


def test_build_timeline_and_backward_extrapolation():
    tl = crypto._build_timeline("2024-01-30", "2024-02-02")
    assert tl == ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]
    # série commençant le 31/01 : les jours avant prennent le 1er prix connu
    series = {"eth": [(crypto._day_ts_ms("2024-01-31"), 2400.0)]}
    prices = crypto._normalize_prices(tl, "eth", series, {}, {})
    assert prices == [2400.0, 2400.0, 2400.0, 2400.0]
    # série vide + prix statique
    prices2 = crypto._normalize_prices(tl, "usd0", {}, {"usd0": 1.0}, {})
    assert prices2 == [1.0, 1.0, 1.0, 1.0]


# ---------------------------------------------------------------- rebuild

def test_rebuild_basic_cost_and_value():
    c = _conn()
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-03-01", "eth", 1.0, "in", 3000.0)
    _tx(c, wid, "alice", "2024-06-01", "eth", 0.5, "out", 3200.0)
    r = crypto.rebuild_wallet_series(c, "alice", wid, None)
    assert r["ok"] and r["days"] > 100
    last = c.execute("SELECT value_usd, cost_usd FROM cw_history WHERE"
                     " wallet_id=? AND token_symbol IS NULL ORDER BY date DESC"
                     " LIMIT 1", (wid,)).fetchone()
    # solde 0,5 ETH ; coût = 3000 − 0,5×3000 (moyenne) = 1500
    assert last["cost_usd"] == 1500.0
    # valeur finale : 0,5 × dernier prix connu (série = points tx → 3200)
    assert last["value_usd"] == 1600.0
    # pas de trou : toutes les dates entre le 1er tx et aujourd'hui
    n = c.execute("SELECT COUNT(DISTINCT date) n FROM cw_history WHERE"
                  " wallet_id=? AND token_symbol IS NULL", (wid,)).fetchone()["n"]
    assert n == r["days"]


def test_rebuild_cost_avg_per_token():
    c = _conn()
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-03-01", "eth", 1.0, "in", 100.0)
    _tx(c, wid, "alice", "2024-03-02", "eth", 1.0, "in", 300.0)
    _tx(c, wid, "alice", "2024-03-03", "eth", 1.0, "out")  # sans prix : le
    # prix de VENTE n'existe pas → la série s'arrête au dernier prix d'achat
    crypto.rebuild_wallet_series(c, "alice", wid, None)
    last = c.execute("SELECT cost_usd, value_usd FROM cw_history WHERE"
                     " wallet_id=? AND token_symbol IS NULL ORDER BY date DESC"
                     " LIMIT 1", (wid,)).fetchone()
    # coût moyen 200 → sortie 1 ETH = −200 (jamais au prix de vente)
    assert last["cost_usd"] == 200.0
    # solde 1 ETH × dernier prix connu (300)
    assert last["value_usd"] == 300.0


def test_rebuild_orphan_injection():
    c = _conn()
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-03-01", "eth", 1.0, "in", 3000.0)
    scans = [{"chain": "ethereum", "symbol": "ETH", "token_addr": "",
              "usd_price": 3000.0, "usd_value": 3000.0, "name": "Ethereum"},
             {"chain": "optimism", "symbol": "usd0", "token_addr": "0xabc",
              "usd_price": 1.0, "usd_value": 100.0, "name": "Usual USD"}]
    crypto.rebuild_wallet_series(c, "alice", wid, scans)
    per = c.execute("SELECT DISTINCT token_symbol FROM cw_history WHERE"
                    " wallet_id=? AND token_symbol IS NOT NULL", (wid,)).fetchall()
    syms = {r["token_symbol"] for r in per}
    assert "usd0" in syms  # orphelin injecté (PNL neutre)
    # valeur du 1er jour = ETH 3000 + usd0 100
    first = c.execute("SELECT value_usd FROM cw_history WHERE wallet_id=?"
                      " AND token_symbol IS NULL ORDER BY date ASC LIMIT 1",
                      (wid,)).fetchone()
    assert first["value_usd"] == 3100.0


def test_rebuild_dedup_and_case_anchor():
    c = _conn()
    wid = _wallet(c)
    # deux jambes même tx_hash log_index différents (swap) : les DEUX comptent
    _tx(c, wid, "alice", "2024-04-01", "eth", 0.1, "out", 3000.0, addr="0x1", idx=0)
    _tx(c, wid, "alice", "2024-04-01", "usdc", 300.0, "in", 1.0, addr="0x2", idx=1)
    crypto.rebuild_wallet_series(c, "alice", wid, None)
    ntx = c.execute("SELECT COUNT(*) n FROM cw_transfers WHERE wallet_id=?",
                    (wid,)).fetchone()["n"]
    assert ntx == 2
    # idempotence : un second rebuild ne duplique pas les lignes
    crypto.rebuild_wallet_series(c, "alice", wid, None)
    n = c.execute("SELECT COUNT(*) n FROM cw_history WHERE wallet_id=? AND"
                  " date=(SELECT MAX(date) FROM cw_history WHERE wallet_id=?)",
                  (wid, wid)).fetchone()["n"]
    assert n == 2  # agrégat + per-token (eth/usdc du dernier jour)


# ---------------------------------------------------------------- intégration

def test_integration_account_and_month_grid():
    c = _conn()
    _fx(c)
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-01-15", "eth", 1.0, "in", 2400.0)
    _tx(c, wid, "alice", "2024-01-16", "eth", 0.1, "in", 2500.0)
    crypto.rebuild_wallet_series(c, "alice", wid, None)
    crypto.refresh_integration(c, "alice")
    acc = c.execute("SELECT * FROM accounts WHERE owner='alice'").fetchone()
    assert acc["asset_class"] == "crypto"
    assert acc["valuation_mode"] == "auto"
    assert acc["name"].endswith("(non-custodial)")
    assert acc["open_date"] == "2024-01-15"
    assert acc["cost_basis"] > 0
    # comptes-auto verrouillés (reconnus par le module)
    assert crypto.is_cw_account(c, acc["id"]) is True
    vals = c.execute("SELECT val_date, value FROM valuations WHERE account_id=?"
                     " AND source='cw' ORDER BY val_date", (acc["id"],)).fetchall()
    today_iso = TODAY.isoformat()
    # aucun point après aujourd'hui ; pas de point dans le mois courant
    # avant sa fin ; le point du jour existe (série du jour)
    for v in vals:
        assert v["val_date"] <= today_iso
    assert vals[-1]["val_date"] == today_iso
    assert len(vals) >= 30  # 2024-01 → mois dernier + point du jour
    # dernière valeur EUR ≈ valeur USD du dernier jour / 1.08
    last_usd = c.execute("SELECT value_usd FROM cw_history WHERE wallet_id=?"
                         " AND token_symbol IS NULL ORDER BY date DESC LIMIT 1",
                         (wid,)).fetchone()["value_usd"]
    assert abs(vals[-1]["value"] - round(last_usd / 1.08, 2)) < 0.02
    # re-passe idempotente : même nombre de valorisations
    crypto.refresh_integration(c, "alice")
    n2 = c.execute("SELECT COUNT(*) n FROM valuations WHERE account_id=?",
                   (acc["id"],)).fetchone()["n"]
    assert n2 == len(vals)


def test_wallet_lifecycle_validation_and_cascade():
    c = _conn()
    w = crypto.add_wallet(c, "alice", "Ledger", "0x" + "cd" * 20)
    assert w["address"] == "0x" + "cd" * 20
    with pytest.raises(ValueError):
        crypto.add_wallet(c, "alice", "Ledger 2", "0x" + "cd" * 19)  # invalide
    with pytest.raises(ValueError):
        crypto.add_wallet(c, "alice", "", "0x" + "cd" * 20)          # label vide
    with pytest.raises(ValueError):
        crypto.add_wallet(c, "alice", "Ledger bis", "0x" + "cd" * 20)  # doublon
    # autre owner OK
    w2 = crypto.add_wallet(c, "bob", "Metamask", "0x" + "ef" * 20)
    _tx(c, w["id"], "alice", "2024-02-01", "eth", 0.5, "in", 2000.0)
    _fx(c)
    crypto.rebuild_wallet_series(c, "alice", w["id"], None)
    crypto.refresh_integration(c, "alice")
    aid = c.execute("SELECT account_id FROM cw_wallets WHERE id=?",
                    (w["id"],)).fetchone()["account_id"]
    assert crypto.remove_wallet(c, "alice", w["id"]) is True
    # compte-auto + valuations cascadés
    assert c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone() is None
    assert c.execute("SELECT COUNT(*) n FROM valuations WHERE account_id=?",
                     (aid,)).fetchone()["n"] == 0
    assert c.execute("SELECT 1 FROM cw_wallets WHERE id=?", (w["id"],)).fetchone() is None
    # wallet de bob intact
    assert crypto.remove_wallet(c, "alice", w2["id"]) is False  # pas au owner


def test_seed_demo_offline_and_idempotent():
    c = _conn()
    r1 = crypto.seed_demo(c, "demo")
    assert r1["seeded"] and r1["transfers"] > 40
    acc = c.execute("SELECT * FROM accounts WHERE owner='demo'").fetchone()
    assert acc["asset_class"] == "crypto" and acc["valuation_mode"] == "auto"
    assert acc["name"] == "Ledger (démo)"  # pas de double suffixe pour demo
    assert crypto.seed_demo(c, "demo")["seeded"] is False  # idempotent
    assert c.execute("SELECT COUNT(*) n FROM accounts WHERE owner='demo'"
                     ).fetchone()["n"] == 1
    assert c.execute("SELECT COUNT(*) n FROM cw_history WHERE token_symbol IS NULL"
                     ).fetchone()["n"] > 300


def test_export_import_roundtrip():
    c = _conn()
    _fx(c)
    crypto.seed_demo(c, "alice")
    payload = crypto.export_payload(c, "alice")
    assert payload["wallets"] and payload["transfers"] and payload["history"]
    c2 = _conn()
    _fx(c2)
    assert crypto.do_cw_import(c2, "alice", payload) is None
    assert crypto.do_cw_import(c2, "bob", {"wallets": [], "transfers": [],
                                           "history": [], "scans": []}) is None
    crypto.refresh_integration(c2, "alice")
    a1 = c.execute("SELECT COUNT(*) n FROM cw_history WHERE owner='alice'"
                   ).fetchone()["n"]
    a2 = c2.execute("SELECT COUNT(*) n FROM cw_history WHERE owner='alice'"
                    ).fetchone()["n"]
    assert a1 == a2
    v1 = c.execute("SELECT COUNT(*) n FROM valuations v JOIN accounts a ON"
                   " a.id=v.account_id WHERE a.owner='alice'").fetchone()["n"]
    v2 = c2.execute("SELECT COUNT(*) n FROM valuations v JOIN accounts a ON"
                    " a.id=v.account_id WHERE a.owner='alice'").fetchone()["n"]
    assert v1 == v2


def test_monthly_history_shape():
    c = _conn()
    wid = _wallet(c)
    _tx(c, wid, "alice", "2025-01-05", "eth", 1.0, "in", 3000.0)
    _tx(c, wid, "alice", "2025-06-20", "eth", 0.2, "in", 3200.0)
    crypto.rebuild_wallet_series(c, "alice", wid, None)
    pts = crypto.monthly_history(c, ["alice"])
    yms = [p["ym"] for p in pts]
    assert yms == sorted(yms) and len(pts) == len(set(yms))  # un point/mois
    assert pts[0]["ym"] == "2025-01" and pts[-1]["value_usd"] > 0
    assert crypto.monthly_history(c, ["nobody"]) == []


def test_overview_and_tokens_rows():
    c = _conn()
    crypto.seed_demo(c, "alice")
    ov = crypto.overview_rows(c, ["alice"])
    assert ov["wallet_count"] == 1
    w = ov["wallets"][0]
    assert w["value_usd"] == ov["total_usd"] > 0
    assert w["gain_usd"] == round(w["value_usd"] - w["cost_usd"], 2)
    assert ov["total_cost_usd"] > 0


# ---------------------------------------------------------------- réseau mocké

def _fake_scan_portfolio(address):
    """Scan déterministe (monkeypatch de crypto.scan_portfolio)."""
    return {
        "address": address, "total_usd": 3100.0,
        "tokens": [
            {"chain": "ethereum", "symbol": "ETH", "name": "Ethereum",
             "balance": 1.0, "usd_value": 3000.0, "usd_price": 3000.0,
             "contract_address": "", "category": "wallet",
             "price_unknown": False, "type": "native"},
            {"chain": "optimism", "symbol": "USDC", "name": "USD Coin",
             "balance": 100.0, "usd_value": 100.0, "usd_price": 1.0,
             "contract_address": "0x0a0b", "category": "wallet",
             "price_unknown": False, "type": "ERC-20"},
        ],
        "token_count": 2, "chain_count": 2, "chains": {"ethereum": 3000.0,
                                                       "optimism": 100.0},
        "errors": [],
    }


def test_scan_chain_parses_pages(monkeypatch):
    """_scan_chain : spam filtré, jeton malformé ignoré, native présente."""
    pages = iter([
        {"coin_balance": None},  # appel natif /addresses : rien à balancer
        {"items": [
            {"token": {"symbol": "ETH", "name": "Ethereum", "decimals": 18,
                       "exchange_rate": 3000.0},
             "value": str(10 ** 18)},
            {"token": {"symbol": "ClaimReward", "name": "x", "decimals": 18},
             "value": str(10 ** 21)},  # spam → écarté
            {"token": None},  # malformé → ignoré
        ], "next_page_params": {"x": 1}},
        {"items": [], "next_page_params": None},
    ])
    monkeypatch.setattr(crypto, "_http_json",
                        lambda *a, **k: next(pages, None))
    out = crypto._scan_chain("ethereum", "eth.blockscout.com", "0x" + "ab" * 20)
    assert [t["symbol"] for t in out["tokens"]] == ["ETH"]
    assert out["error"] is None


def test_fetch_prices_and_enrich_mocked(monkeypatch):
    c = _conn()
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-05-10", "eth", 2.0, "in")  # pas de prix
    monkeypatch.setattr(crypto, "_http_json", lambda *a, **k: {
        "coins": {"coingecko:ethereum": {"prices": [
            {"timestamp": 1715300000, "price": 2900.0}]}}})
    r = crypto.fetch_prices_and_enrich(c, "alice", wid)
    assert r["enriched"] >= 1
    assert r["unmapped"] == []
    row = c.execute("SELECT usd_price, usd_value FROM cw_transfers WHERE"
                    " wallet_id=? LIMIT 1", (wid,)).fetchone()
    assert row["usd_price"] == 2900.0
    assert row["usd_value"] == 5800.0
    # cache écrit → re-fetch sans appel (mapped tous en cache)
    calls = []
    monkeypatch.setattr(crypto, "_http_json",
                        lambda *a, **k: calls.append(1) or None)
    r2 = crypto.fetch_prices_and_enrich(c, "alice", wid)
    assert calls == [] and r2["enriched"] == 0


def test_refresh_wallet_full_mocked(monkeypatch):
    """Passe complète : scan → transferts → prix → rebuild → intégration."""
    c = _conn()
    _fx(c)
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-03-01", "eth", 1.0, "in", 3000.0)
    monkeypatch.setattr(crypto, "scan_portfolio", _fake_scan_portfolio)
    monkeypatch.setattr(crypto, "fetch_transfers",
                        lambda conn, owner, w: {"inserted": 0, "per_chain": {},
                                                "truncated": []})
    monkeypatch.setattr(crypto, "fetch_prices_and_enrich",
                        lambda conn, owner, wid2: {"mapped": [], "unmapped": [],
                                                   "degraded": [], "calls_ok": 0,
                                                   "calls_failed": 0,
                                                   "enriched": 0})
    monkeypatch.setattr(crypto, "fetch_native_movements",
                        lambda conn, owner, w, chains: {"inserted": 0,
                                                        "per_chain": {},
                                                        "errors": []})
    rep = crypto.refresh_wallet(c, "alice", wid)
    assert rep["ok"] is True
    assert rep["scan"]["total_usd"] == 3100.0
    # scans persistés (2 jetons) + compte-auto + valorisations EUR
    assert c.execute("SELECT COUNT(*) n FROM cw_scans WHERE wallet_id=?",
                     (wid,)).fetchone()["n"] == 2
    acc = c.execute("SELECT * FROM accounts WHERE owner='alice'").fetchone()
    assert acc is not None and acc["valuation_mode"] == "auto"
    assert c.execute("SELECT COUNT(*) n FROM valuations WHERE account_id=?",
                     (acc["id"],)).fetchone()["n"] >= 1
    # wallet démo : jamais de réseau (retour immédiat)
    c2 = _conn()
    wd = _wallet(c2, owner="demo", demo=1)
    rep2 = crypto.refresh_wallet(c2, "demo", wd)
    assert rep2["demo"] is True


def test_fetch_native_movements_legs_and_dedup(monkeypatch):
    """v052 : jambes natives (value, fee, internal) — directions, montants,
    séquences sentinelles, idempotence (2e appel = 0 insertion)."""
    c = _conn()
    wid = _wallet(c)
    addr = "0x" + "ab" * 20
    row = c.execute("SELECT * FROM cw_wallets WHERE id=?", (wid,)).fetchone()

    def fake(url, **k):
        if "internal-transactions" in url:
            if "filter=to" in url:
                return {"items": [
                    {"transaction_hash": "0x" + "c1" * 32, "index": 3,
                     "timestamp": "2024-06-03T09:00:00.000000Z",
                     "from": {"hash": "0x" + "cc" * 20},
                     "to": {"hash": addr}, "value": "700000000000000000"}],
                    "next_page_params": None}
            return {"items": [], "next_page_params": None}
        if "transactions" in url:
            return {"items": [
                # envoi externe 1.2 ETH + gaz 0.004
                {"hash": "0x" + "a1" * 32, "timestamp": "2024-06-01T10:00:00Z",
                 "from": {"hash": addr}, "to": {"hash": "0x" + "ee" * 20},
                 "value": "1200000000000000000",
                 "fee": {"value": "4000000000000000"}},
                # réception externe 0.5 ETH
                {"hash": "0x" + "a2" * 32, "timestamp": "2024-06-02T10:00:00Z",
                 "from": {"hash": "0x" + "aa" * 20}, "to": {"hash": addr},
                 "value": "500000000000000000",
                 "fee": {"value": "3000000000000000"}},
                # tx ERC-20 (value 0) → seul le gaz brûle
                {"hash": "0x" + "a3" * 32, "timestamp": "2024-06-04T10:00:00Z",
                 "from": {"hash": addr}, "to": {"hash": "0x" + "bb" * 20},
                 "value": "0", "fee": {"value": "1000000000000000"}},
                # auto-envoi : la valeur se compense, seul le gaz brûle
                {"hash": "0x" + "a4" * 32, "timestamp": "2024-06-05T10:00:00Z",
                 "from": {"hash": addr}, "to": {"hash": addr},
                 "value": "2000000000000000000",
                 "fee": {"value": "200000000000000"}},
            ], "next_page_params": None}
        return None

    monkeypatch.setattr(crypto, "_http_json", fake)
    r1 = crypto.fetch_native_movements(c, "alice", row, ["ethereum"])
    assert r1["inserted"] == 6, r1
    rows = c.execute("SELECT * FROM cw_transfers WHERE wallet_id=?", (wid,)) \
             .fetchall()
    assert len(rows) == 6
    by_seq = {r["log_index"]: r for r in rows}
    assert set(by_seq) == {1000000000, 1000000001, 1000000002,  # in/out/fee
                           2000000003}  # in internal (base 2e9 + index 3)
    s = by_seq[1000000000]
    assert s["direction"] == "in" and s["amount"] == 0.5
    assert by_seq[1000000001]["direction"] == "out"
    assert by_seq[1000000001]["amount"] == 1.2
    # ligne de frais de l'envoi t1 (0.004)
    by_tx = {r["tx_hash"]: r for r in rows}
    assert by_tx["0x" + "a1" * 32]["direction"] == "out"
    assert by_tx["0x" + "a1" * 32]["amount"] == 0.004
    assert by_tx["0x" + "a1" * 32]["log_index"] == 1000000002
    assert by_seq[2000000003]["direction"] == "in"
    assert by_seq[2000000003]["amount"] == 0.7
    for r in rows:
        assert r["token_symbol"] == "eth" and r["token_addr"] == ""
        assert r["chain"] == "ethereum"
    # 3 txs paient du gaz depuis le wallet (t1, a3, a4) → 3 lignes out
    fees = [r for r in rows if r["log_index"] == 1000000002]
    assert {r["amount"] for r in fees} == {0.004, 0.001, 0.0002}
    # idempotence
    r2 = crypto.fetch_native_movements(c, "alice", row, ["ethereum"])
    assert r2["inserted"] == 0
    assert c.execute("SELECT COUNT(*) n FROM cw_transfers WHERE wallet_id=?",
                     (wid,)).fetchone()["n"] == 6


def test_refresh_wallet_native_wiring(monkeypatch):
    """v052 : refresh complet appelle la capture native avec les chaînes du
    scan (jeton natif) et expose report['native']."""
    c = _conn()
    _fx(c)
    wid = _wallet(c)
    _tx(c, wid, "alice", "2024-03-01", "eth", 1.0, "in", 3000.0)
    seen = {}

    def fake_native(conn, owner, w, chains):
        seen["chains"] = sorted(chains)
        return {"inserted": 3, "per_chain": {"ethereum": 3}, "errors": []}

    monkeypatch.setattr(crypto, "scan_portfolio", _fake_scan_portfolio)
    monkeypatch.setattr(crypto, "fetch_transfers",
                        lambda conn, owner, w: {"inserted": 0, "per_chain": {},
                                                "truncated": []})
    monkeypatch.setattr(crypto, "fetch_native_movements", fake_native)
    monkeypatch.setattr(crypto, "fetch_prices_and_enrich",
                        lambda conn, owner, wid2: {"mapped": [], "unmapped": [],
                                                   "degraded": [], "calls_ok": 0,
                                                   "calls_failed": 0,
                                                   "enriched": 0})
    rep = crypto.refresh_wallet(c, "alice", wid)
    assert rep["ok"] is True
    assert rep["native"]["inserted"] == 3
    assert seen["chains"] == ["ethereum"]  # seul jeton natif du scan fake


def test_claim_guard_owner():
    assert crypto.refresh_claimed("zed") is True
    assert crypto.refresh_claimed("zed") is False  # déjà en cours
    crypto.refresh_release("zed")
    assert crypto.refresh_claimed("zed") is True
    crypto.refresh_release("zed")
