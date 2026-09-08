"""One-shot Crowdfunding Tracker → Patrimony module migration (v2026.09.046).

Reads a Crowdfunding Tracker SQLite backup file and imports its projects,
operations and platform metadata into a Patrimony database (data dir), under
a single owner, then materialises the derived auto accounts (refresh).

Usage (run from the repo root, with the venv activated):
    python scripts/migrate_crowdfunding.py --ct /path/to/ct-backup.db \
        --data /path/to/patrimony-data-dir --owner frederic [--force]

Parity checks are printed and enforced: project/operation counts, invested
totals per platform, LPB capital due vs the legacy DB, auto-account values.
Exit code 0 only when every check passes.

NEVER run against a live app database: stop the app first, or point --data at
a fresh directory (init_db runs automatically).
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ct", required=True, help="Crowdfunding Tracker SQLite backup file")
    ap.add_argument("--data", required=True, help="Patrimony DATA_DIR (created if missing)")
    ap.add_argument("--owner", default="admin", help="Patrimony owner username")
    ap.add_argument("--force", action="store_true", help="Import even if the module is not empty")
    args = ap.parse_args()

    ct_path = Path(args.ct)
    if not ct_path.is_file():
        print(f"ERROR: CT backup not found: {ct_path}")
        return 2
    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)

    # fresh import of src.app initialises the schema in DATA_DIR
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["ADMIN_USER"] = args.owner
    os.environ["ADMIN_PASSWORD"] = "x-not-used-2026"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["SEED_DEMO"] = "0"
    import src.app as app  # noqa: E402  (init_db at import)
    import src.crowdfund as cf  # noqa: E402

    conn = app.db_main()
    try:
        n_cf = conn.execute("SELECT COUNT(*) c FROM cf_projects").fetchone()["c"]
        if n_cf and not args.force:
            print(f"ERROR: module not empty ({n_cf} projects) — use --force to wipe and retry")
            return 2
        conn.execute("DELETE FROM cf_operations")
        conn.execute("DELETE FROM cf_projects")
        conn.execute("DELETE FROM cf_platforms")
        conn.execute("DELETE FROM cf_reports")
        conn.execute("DELETE FROM accounts WHERE asset_class='crowdfunding'")
        conn.commit()
        res = cf.import_ct_backup(conn, args.owner, str(ct_path))
        cf.refresh_integration(conn, args.owner)
        conn.commit()
    except Exception as e:  # pragma: no cover
        conn.rollback()
        print(f"ERROR: migration failed: {e}")
        return 1
    finally:
        conn.close()

    # ------------------------------------------------------------ parity checks
    print(f"imported: {res['platforms']} platforms / {res['projects']} projects / "
          f"{res['operations']} operations")
    src = sqlite3.connect(f"file:{ct_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    conn = app.db_main()
    try:
        checks = []
        n_src_proj = src.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        n_dst_proj = conn.execute("SELECT COUNT(*) c FROM cf_projects").fetchone()["c"]
        n_src_ops = src.execute("SELECT COUNT(*) c FROM operations").fetchone()["c"]
        n_dst_ops = conn.execute("SELECT COUNT(*) c FROM cf_operations").fetchone()["c"]
        checks.append(("projects", n_src_proj, n_dst_proj))
        checks.append(("operations", n_src_ops, n_dst_ops))
        for plat in ("bricks", "lapremierebrique"):
            s = src.execute("SELECT COALESCE(SUM(invested),0) s FROM projects WHERE platform=?",
                            (plat,)).fetchone()["s"]
            d = conn.execute("SELECT COALESCE(SUM(invested),0) s FROM cf_projects WHERE platform=?",
                             (plat,)).fetchone()["s"]
            checks.append((f"invested {plat}", round(s, 2), round(d, 2)))
        # LPB « dans les projets » = capital dû (même définition que le tracker)
        cap_src = src.execute(
            "SELECT COALESCE(SUM(MAX(0, invested - COALESCE(repaid_capital,0))),0) s"
            " FROM projects WHERE platform='lapremierebrique'").fetchone()["s"]
        cap_dst = conn.execute(
            "SELECT COALESCE(SUM(MAX(0, invested - COALESCE(repaid_capital,0))),0) s"
            " FROM cf_projects WHERE platform='lapremierebrique'").fetchone()["s"]
        checks.append(("LPB capital due", round(cap_src, 2), round(cap_dst, 2)))
        # auto-accounts : valeur = balance + valeur plateforme (Bricks = meta
        # éditée ; LPB = capital dû auto)
        cap_due = conn.execute(
            "SELECT COALESCE(SUM(MAX(0, invested - COALESCE(repaid_capital,0))),0) s"
            " FROM cf_projects WHERE owner=? AND platform='lapremierebrique'",
            (args.owner,)).fetchone()["s"]
        for plat, bal in (("bricks", 129.65), ("lapremierebrique", 519.85)):
            row = conn.execute(
                "SELECT a.id, a.cost_basis FROM accounts a"
                " JOIN cf_platforms p ON p.account_id=a.id"
                " WHERE p.owner=? AND p.platform=?", (args.owner, plat)).fetchone()
            checks.append((f"auto account {plat}", 1, 1 if row else 0))
            if row:
                v = conn.execute(
                    "SELECT value FROM valuations WHERE account_id=?"
                    " ORDER BY val_date DESC LIMIT 1", (row["id"],)).fetchone()
                meta = conn.execute(
                    "SELECT invested_value FROM cf_platforms WHERE owner=? AND platform=?",
                    (args.owner, plat)).fetchone()
                expected = bal + (cap_due if plat == "lapremierebrique"
                                  else (meta["invested_value"] or 0))
                checks.append((f"value {plat}", round(expected, 2),
                               round(v["value"], 2) if v else None))
        # aucune opération orpheline (project_id présent mais projet absent)
        orphans = conn.execute(
            "SELECT COUNT(*) c FROM cf_operations o WHERE o.project_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM cf_projects p WHERE p.id=o.project_id)"
        ).fetchone()["c"]
        checks.append(("orphan operations", 0, orphans))
    finally:
        src.close()
        conn.close()

    ok = True
    for name, exp, got in checks:
        match = (exp == got) if exp is not None else True
        ok = ok and match
        print(f"{'OK ' if match else 'FAIL'} {name}: expected={exp} got={got}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
