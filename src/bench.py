"""Benchmarks d'indices + simulation de dépôts (extrait de src/app.py,
v2026.09.038).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI. Les fetchs
réseau (Yahoo, fonction `fetch_chart` INJECTÉE par l'appelant) sont des
fonctions synchrones bloquantes : l'appelant les exécute dans son threadpool
et récupère les charts SANS connexion ; les écritures SQL (store_levels)
s'exécutent dans le thread du handler (les connexions sqlite3 de l'app sont
créées avec check_same_thread=True — ne JAMAIS écrire depuis le threadpool).

Piège de conception hérité : les niveaux manquants sont détectés AVANT le
fetch (needs) puis stockés APRÈS — un refresh forcé repasse par le même flux
(needs → charts → store → build).
"""

from datetime import date

from src import fx


def _owner_clause(owners: list[str]) -> tuple[str, list]:
    return "owner IN (%s)" % ",".join("?" * len(owners)), owners


def account_cashflows(conn, owners: list[str]) -> tuple[list[tuple[str, float]], float]:
    """Flux nets par actif des propriétaires donnés : opérations si présentes,
    sinon coût manuel à l'ouverture. Retourne (flows triés, total déposé)."""
    wc, args = _owner_clause(owners)
    rows = conn.execute(
        f"SELECT id, open_date, created_at, cost_basis FROM accounts WHERE active=1 AND {wc}", args
    ).fetchall()
    txn_rows = conn.execute(
        "SELECT t.account_id, t.op_date, t.kind, t.amount FROM transactions t"
        f" JOIN accounts a ON a.id=t.account_id WHERE t.kind IN ('deposit','withdrawal') AND {wc}", args
    ).fetchall()
    by_acc: dict[int, list[tuple[str, float]]] = {}
    for t in txn_rows:
        amt = t["amount"] if t["kind"] == "deposit" else -t["amount"]
        by_acc.setdefault(t["account_id"], []).append((t["op_date"][:10], round(amt, 2)))
    flows: list[tuple[str, float]] = []
    for r in rows:
        if r["id"] in by_acc:
            flows.extend(by_acc[r["id"]])
        elif r["cost_basis"]:
            d = (r["open_date"] or r["created_at"] or "")[:10]
            flows.append((d, round(r["cost_basis"], 2)))
    flows.sort(key=lambda x: x[0])
    deposited = round(sum(a for _, a in flows if a > 0), 2)
    return flows, deposited


def start_ym(conn, owners: list[str], today: date) -> str:
    """Premier mois du benchmark : 1er flux (ou 4 ans glissants par défaut)."""
    flows, _ = account_cashflows(conn, owners)
    if flows:
        fy = date.fromisoformat(flows[0][0])
    else:
        fy = date(today.year - 4, today.month, 1)
    return f"{fy.year:04d}-{fy.month:02d}"


def needs(conn, start_ym: str, force: bool, today: date) -> list[tuple[str, str, int]]:
    """Indices dont les niveaux manquent depuis start_ym.
    Retourne [(key, symbol, années)] — appelé AVANT le fetch."""
    benches = conn.execute("SELECT key, name, symbol FROM benchmarks WHERE symbol<>''").fetchall()
    need: list[tuple[str, str, int]] = []
    for b in benches:
        y, m = int(start_ym[:4]), int(start_ym[5:7])
        missing = conn.execute(
            "SELECT COUNT(*) c FROM index_levels WHERE key=? AND ym>=?", (b["key"], start_ym)
        ).fetchone()["c"]
        span_months = (today.year - y) * 12 + (today.month - m) + 1
        if not force and missing >= min(span_months, 2):
            continue
        years = max(1, min(10, today.year - y + 1))
        need.append((b["key"], b["symbol"], years))
    return need


def fetch_charts(need: list[tuple[str, str, int]], fetch_chart) -> list[tuple[str, dict | None]]:
    """Fetch Yahoo des niveaux manquants — BLOQUANT, exécuté dans le threadpool
    par l'appelant. need: (key, symbol, années) -> [(key, chart|None), ...]"""
    out = []
    for key, symbol, years in need:
        try:
            out.append((key, fetch_chart(symbol, f"{years}y", "1mo")))
        except Exception:
            out.append((key, None))
    return out


def store_levels(conn, start_ym: str, charts: list[tuple[str, dict | None]]) -> None:
    """Stocke les niveaux reçus (INSERT OR REPLACE) + commit. À exécuter dans
    le thread du handler (connexion sqlite3 check_same_thread=True)."""
    for key, chart in charts:
        if not chart:
            continue
        for dstr, close in chart["points"]:
            if dstr[:7] < start_ym:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO index_levels (key, ym, level) VALUES (?,?,?)",
                (key, dstr[:7], close),
            )
    conn.commit()


def build(conn, owners: list[str], latest: dict[int, dict], start_ym: str,
          today: date) -> dict:
    """Résultat complet /api/benchmarks : niveaux (dont livret A synthétique),
    annualisés par indice, simulation « même dépôts » par indice, ligne
    utilisateur (dépôts nets, valeur convertie EUR, gain, annualisé)."""
    lvl_rows = conn.execute("SELECT key, ym, level FROM index_levels").fetchall()
    levels: dict[str, dict[str, float]] = {}
    for r in lvl_rows:
        levels.setdefault(r["key"], {})[r["ym"]] = r["level"]
    rate = 2.2
    bench_row = conn.execute("SELECT annual_pct FROM benchmarks WHERE key='livret'").fetchone()
    if bench_row and bench_row["annual_pct"]:
        rate = bench_row["annual_pct"]
    lv = levels.setdefault("livret", {})
    y0, m0 = int(start_ym[:4]), int(start_ym[5:7])
    n = 0
    d = date(y0, m0, 1)
    while d <= today:
        lv[d.strftime("%Y-%m")] = (1 + rate / 100 / 12) ** n
        n += 1
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    flows, deposited = account_cashflows(conn, owners)
    wc, args = _owner_clause(owners)
    tot_value = 0.0
    for r in conn.execute(
        f"SELECT id, currency, fx_override FROM accounts WHERE active=1 AND {wc}", args
    ).fetchall():
        l = latest.get(r["id"])
        if l:
            ccy = r["currency"] or "EUR"
            if ccy != "EUR":
                fxr = fx.lookup(conn, ccy, l["date"], r["fx_override"])
                if fxr is None:
                    continue  # actif non convertible : exclu du benchmark utilisateur
                tot_value += l["value"] / fxr["rate"]
            else:
                tot_value += l["value"]
    benches = [dict(r) for r in conn.execute("SELECT * FROM benchmarks").fetchall()]

    rows_out = []
    end_ym = None
    for b in benches:
        lk = levels.get(b["key"], {})
        yms = sorted(lk.keys())
        if not yms:
            continue
        first, last = yms[0], yms[-1]
        end_ym = last if end_ym is None else max(end_ym, last)
        l_first, l_last = lk[first], lk[last]
        span_m = (int(last[:4]) - int(first[:4])) * 12 + (int(last[5:7]) - int(first[5:7]))
        annualized = None
        if span_m >= 3 and l_first > 0:
            annualized = round(((l_last / l_first) ** (12 / span_m) - 1) * 100, 2)
        sim_value = sim_gain = None
        if flows and l_last > 0:
            sv = 0.0
            for fdate, famt in flows:
                fym = fdate[:7]
                if fym < first:
                    fym = first
                elif fym > last:
                    fym = last
                lf = lk.get(fym)
                if lf:
                    sv += famt * (l_last / lf)
            sim_value = round(sv, 2)
            sim_gain = round(sv - deposited, 2)
        rows_out.append({
            "key": b["key"], "name": b["name"], "symbol": b["symbol"],
            "note": b["note"], "annualized": annualized, "sim_value": sim_value,
            "sim_gain": sim_gain, "first_ym": first, "last_ym": last,
        })
    rows_out.sort(key=lambda x: (x["annualized"] is None, -(x["annualized"] or 0)))
    user_ann = None
    user_net = round(sum(a for _, a in flows), 2) if flows else 0.0
    if flows and user_net > 0 and tot_value:
        d0 = date.fromisoformat(flows[0][0])
        days = max(1, (today - d0).days)
        ratio = tot_value / user_net - 1
        if ratio > -1:
            user_ann = round(((1 + ratio) ** (365 / days) - 1) * 100, 2)
    user = {
        "deposited": deposited, "net": user_net, "value": round(tot_value, 2),
        "gain": round(tot_value - user_net, 2) if user_net else None,
        "annualized": user_ann, "first_ym": start_ym, "last_ym": today.strftime("%Y-%m"),
    }
    return {"user": user, "benchmarks": rows_out, "end_ym": end_ym}
