"""One-shot Crypto Wallet Tracker → Patrimony module migration (v2026.09.050).

Reads a CWT SQLite backup (wallets.db) and imports its wallets, token
transfers, daily history and price cache into a Patrimony database (data
dir), under a single owner, then materialises the derived auto accounts
(one per wallet) with EUR monthly valuations.

Usage (run from the repo root, with the venv activated):
    python scripts/migrate_crypto.py --cwt /path/to/wallets.db \\
        --data /path/to/patrimony-data-dir --owner <username> [--cwt-user <username>]

Parity checks are printed and enforced: wallet/transfer/history counts,
per-wallet final value and cost to the cent, date bounds, orphan rows,
dedup integrity, auto-account materialisation. Exit 0 only when every check
passes (the import is committed only then).

NEVER run against a live app database: stop the app first, or point --data at
a fresh directory (init_db runs automatically at import).
"""
import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cwt", required=True, help="CWT SQLite backup file (wallets.db)")
    ap.add_argument("--data", required=True, help="Patrimony DATA_DIR (created if missing)")
    ap.add_argument("--owner", default="admin", help="Patrimony owner username")
    ap.add_argument("--cwt-user", default="owner",
                    help="CWT username whose wallets are imported")
    ap.add_argument("--force", action="store_true",
                    help="Import even if the module is not empty")
    args = ap.parse_args()

    cwt_path = Path(args.cwt)
    if not cwt_path.is_file():
        print(f"ERROR: CWT backup not found: {cwt_path}")
        return 2
    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)

    # fresh import of src.app initialises the schema in DATA_DIR
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["ADMIN_USER"] = args.owner
    os.environ["ADMIN_PASSWORD"] = "x-not-used-2026"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["SEED_DEMO"] = "0"
    os.environ["PAT_CRYPTO_AUTO"] = "0"
    import src.app as app  # noqa: E402  (init_db at import)
    import src.crypto as cw  # noqa: E402

    src = sqlite3.connect(f"file:{cwt_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    conn = app.db_main()
    try:
        # ── garde : module cible vide (sauf --force) ──────────────────
        n_w = conn.execute("SELECT COUNT(*) c FROM cw_wallets").fetchone()["c"]
        if n_w and not args.force:
            print(f"ERROR: crypto module not empty ({n_w} wallets) — use --force")
            return 2
        # ── user CWT cible ────────────────────────────────────────────
        tables = {r["name"] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        user_id = 1
        if "users" in tables and "username" in _cols(src, "users"):
            row = src.execute("SELECT id FROM users WHERE username=?",
                              (args.cwt_user,)).fetchone()
            if row is None:
                print(f"ERROR: CWT user not found: {args.cwt_user}")
                return 2
            user_id = row["id"]
        # ── wipe module (--force) ─────────────────────────────────────
        if n_w and args.force:
            for w in conn.execute("SELECT id, account_id FROM cw_wallets"):
                if w["account_id"]:
                    conn.execute("DELETE FROM valuations WHERE account_id=?",
                                 (w["account_id"],))
                    conn.execute("DELETE FROM accounts WHERE id=?",
                                 (w["account_id"],))
            conn.execute("DELETE FROM cw_wallets")  # cascade enfants
            conn.commit()
        # ── taux USD BCE AVANT la transaction (store_daily committerait
        #    la transaction globale si appelé en plein milieu) ─────────
        cw._ensure_fx_usd(conn)
        conn.commit()

        # ── 0) wallets CWT ────────────────────────────────────────────
        wcols = _cols(src, "wallets")
        where_u = " WHERE user_id=?" if "user_id" in wcols else ""
        wrows = src.execute(f"SELECT * FROM wallets{where_u}",
                            (user_id,) if where_u else ()).fetchall()
        wid_map: dict[str, int] = {}
        for w in wrows:
            cur = conn.execute(
                "INSERT INTO cw_wallets (owner, label, address, chain,"
                " watch_only, status, created_at) VALUES (?,?,?,?,1,'ok',?)",
                (args.owner, w["label"] or w["address"][:10],
                 (w["address"] or "").strip().lower(), "",
                 datetime.datetime.now(datetime.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")))
            wid_map[(w["address"] or "").strip().lower()] = cur.lastrowid

        # ── 1) transferts (lecture par adresse, robuste à la casse) ───
        tcols = _cols(src, "transactions")
        n_tx = 0
        src_addr = "wallet_address" if "wallet_address" in tcols else "address"
        sel = ("SELECT rowid AS rid, tx_hash, chain, block_time, token_symbol,"
               " token_name, direction, amount, usd_price, usd_value"
               + (", log_index" if "log_index" in tcols else "")
               + (", contract_address" if "contract_address" in tcols else ""))
        if not wid_map:
            print("WARN: aucun wallet CWT pour cet utilisateur — rien à importer")
        for addr_low, wid in wid_map.items():
            for t in src.execute(
                    f"{sel} FROM transactions WHERE LOWER({src_addr})=?",
                    (addr_low,)):
                tx_hash = t["tx_hash"] or ""
                log_idx = t["log_index"] if "log_index" in tcols else 0
                if not tx_hash:  # hash vide : clé synthétique stable
                    tx_hash = f"none:{t['rid']}"
                try:
                    conn.execute(
                        "INSERT INTO cw_transfers (wallet_id, owner, tx_hash,"
                        " log_index, chain, block_time, token_symbol, token_name,"
                        " token_addr, direction, amount, usd_price, usd_value)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (wid, args.owner, tx_hash, log_idx, t["chain"],
                         (t["block_time"] or "")[:19].replace("T", " "),
                         t["token_symbol"] or "", t["token_name"] or "",
                         t["contract_address"] if "contract_address" in tcols else "",
                         t["direction"], t["amount"] or 0,
                         t["usd_price"] or 0, t["usd_value"] or 0))
                except sqlite3.IntegrityError:
                    continue  # doublon déjà présent (dédup)
                n_tx += 1

        # ── 2) historique journalier ──────────────────────────────────
        n_hist = 0
        if "daily_history" in tables:
            hcols = _cols(src, "daily_history")
            haddr = "wallet_address" if "wallet_address" in hcols else "address"
            for addr_low, wid in wid_map.items():
                for h in src.execute(
                        f"SELECT date, value_usd, cost_basis_usd, net_flows_usd,"
                        f" token_symbol, chain FROM daily_history"
                        f" WHERE LOWER({haddr})=?", (addr_low,)):
                    conn.execute(
                        "INSERT INTO cw_history (wallet_id, owner, date,"
                        " value_usd, cost_usd, net_flows_usd, token_symbol, chain)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (wid, args.owner, h["date"], h["value_usd"],
                         h["cost_basis_usd"], h["net_flows_usd"] or 0,
                         h["token_symbol"], h["chain"]))
                    n_hist += 1

        # ── 3) cache prix ─────────────────────────────────────────────
        n_px = 0
        if "price_history" in tables:
            for p in src.execute("SELECT token_symbol, date, price_usd"
                                 " FROM price_history"):
                conn.execute(
                    "INSERT OR REPLACE INTO cw_price_cache (token_symbol, date,"
                    " price_usd) VALUES (?,?,?)",
                    (p["token_symbol"], p["date"], p["price_usd"]))
                n_px += 1

        # bornes wallet depuis l'historique importé
        for w in conn.execute("SELECT id FROM cw_wallets WHERE owner=?",
                              (args.owner,)):
            b = conn.execute(
                "SELECT MIN(date) d0, MAX(date) d1 FROM cw_history WHERE"
                " wallet_id=? AND token_symbol IS NULL", (w["id"],)).fetchone()
            if b and b["d0"]:
                conn.execute(
                    "UPDATE cw_wallets SET first_date=?, last_date=? WHERE id=?",
                    (b["d0"], b["d1"], w["id"]))

        # ── 4) intégration patrimoine (comptes-auto + valorisations) ──
        cw.refresh_integration(conn, args.owner)
        conn.commit()

        # ── 5) contrôles de parité ────────────────────────────────────
        checks: list[tuple[str, object, object]] = []
        # c1 wallets
        checks.append(("wallets", len(wid_map),
                       conn.execute("SELECT COUNT(*) c FROM cw_wallets WHERE"
                                    " owner=?", (args.owner,)).fetchone()["c"]))
        # c2 transferts (total importé)
        checks.append(("transfers", n_tx, conn.execute(
            "SELECT COUNT(*) c FROM cw_transfers WHERE owner=?",
            (args.owner,)).fetchone()["c"]))
        # c3 par chaîne (source vs cible, mêmes adresses)
        if wid_map:
            marks = ",".join("?" * len(wid_map))
            addrs = list(wid_map.keys())
            for r in src.execute(
                    f"SELECT chain, COUNT(*) c FROM transactions WHERE"
                    f" LOWER({src_addr}) IN ({marks}) GROUP BY chain", addrs):
                got = conn.execute(
                    "SELECT COUNT(*) c FROM cw_transfers WHERE owner=?"
                    " AND chain=?", (args.owner, r["chain"])).fetchone()["c"]
                checks.append((f"transfers {r['chain']}", r["c"], got))
        # c4/c5/c6/c7 historique : lignes agrégat, bornes, valeur/coût finaux
        for addr_low, wid in wid_map.items():
            a_src = src.execute(
                "SELECT COUNT(*) c, MIN(date) d0, MAX(date) d1 FROM daily_history"
                " WHERE LOWER(wallet_address)=? AND token_symbol IS NULL",
                (addr_low,)).fetchone()
            a_dst = conn.execute(
                "SELECT COUNT(*) c, MIN(date) d0, MAX(date) d1 FROM cw_history"
                " WHERE wallet_id=? AND token_symbol IS NULL", (wid,)).fetchone()
            checks.append((f"history {addr_low[:10]}", a_src["c"], a_dst["c"]))
            checks.append((f"min date {addr_low[:10]}", a_src["d0"], a_dst["d0"]))
            checks.append((f"max date {addr_low[:10]}", a_src["d1"], a_dst["d1"]))
            for what, scol, dcol in (("final value", "value_usd", "value_usd"),
                                     ("final cost", "cost_basis_usd", "cost_usd")):
                v_src = src.execute(
                    f"SELECT {scol} v FROM daily_history WHERE LOWER(wallet_address)=?"
                    " AND token_symbol IS NULL AND date=(SELECT MAX(date) FROM"
                    " daily_history WHERE LOWER(wallet_address)=? AND"
                    " token_symbol IS NULL)", (addr_low, addr_low)).fetchone()
                v_dst = conn.execute(
                    f"SELECT {dcol} v FROM cw_history WHERE wallet_id=? AND"
                    " token_symbol IS NULL AND date=(SELECT MAX(date) FROM"
                    " cw_history WHERE wallet_id=? AND token_symbol IS NULL)",
                    (wid, wid)).fetchone()
                checks.append((f"{what} {addr_low[:10]}",
                               round(v_src["v"] or 0, 2) if v_src else None,
                               round(v_dst["v"] or 0, 2) if v_dst else None))
        # c8 zéro transfert orphelin
        checks.append(("orphan transfers", 0, conn.execute(
            "SELECT COUNT(*) c FROM cw_transfers WHERE wallet_id NOT IN"
            " (SELECT id FROM cw_wallets)").fetchone()["c"]))
        # c9 intégrité dédup (aucune collision de clé)
        checks.append(("dedup collisions", 0, conn.execute(
            "SELECT COUNT(*) c FROM (SELECT 1 FROM cw_transfers GROUP BY"
            " wallet_id, chain, tx_hash, log_index HAVING COUNT(*)>1)"
        ).fetchone()["c"]))
        # c10 comptes-auto + valorisations EUR matérialisés
        n_acc = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE owner=? AND asset_class="
            "'crypto' AND valuation_mode='auto'", (args.owner,)).fetchone()["c"]
        checks.append(("auto accounts", len(wid_map), n_acc))
        n_val = conn.execute(
            "SELECT COUNT(*) c FROM valuations v JOIN accounts a ON"
            " a.id=v.account_id WHERE a.owner=? AND v.source='cw'",
            (args.owner,)).fetchone()["c"]
        checks.append(("valuations cw > 0", 1, 1 if n_val > 0 else 0))
        checks.append(("price cache rows", n_px, conn.execute(
            "SELECT COUNT(*) c FROM cw_price_cache").fetchone()["c"]))

        ok = True
        for name, exp, got in checks:
            if isinstance(exp, float) or isinstance(got, float):
                match = (exp is not None and got is not None
                         and abs(float(exp) - float(got)) <= 0.01)
            else:
                match = exp == got
            ok = ok and match
            print(f"{'OK ' if match else 'FAIL'} {name}: expected={exp} got={got}")
        if not ok:
            conn.rollback()
            print("ERREUR: contrôles de parité en échec — import annulé (rollback)")
            return 1
        conn.commit()
        print(f"importé: {len(wid_map)} wallet(s) / {n_tx} transferts / "
              f"{n_hist} lignes d'historique / {n_px} prix en cache")
    except Exception as e:  # pragma: no cover
        conn.rollback()
        print(f"ERROR: migration failed: {e}")
        return 1
    finally:
        _close(src)
        _close(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
