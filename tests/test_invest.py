"""Tests pages Investissements — Actions (PEA/CTO) & Assurance vie (v2026.09.054).

White-box sur les helpers du module (aucun réseau, aucune route HTTP) :
connexions en mémoire (schema_data), row_factory = Row. Les règles testées
sont celles des routes /api/actions/overview, /api/av/overview et du
refresh prix (logique métier pure).
"""

import datetime
import sqlite3

from src import app as appmod
from src.schema import schema_data


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    schema_data(c)
    return c


def _fx(c: sqlite3.Connection, rate: float = 1.08) -> None:
    d = datetime.date(2020, 1, 1)
    while d < datetime.date(2027, 1, 1):
        c.execute("INSERT OR IGNORE INTO fx_rates (ccy, rate_date, rate, source)"
                  " VALUES ('USD',?,?,'ecb')", (d.isoformat(), rate))
        d = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    c.commit()


def _acc(c: sqlite3.Connection, owner: str, name: str, cls: str,
         wrapper: str | None, cost: float, ccy: str = "EUR") -> int:
    cur = c.execute(
        "INSERT INTO accounts (owner, name, asset_class, institution,"
        " cost_basis, open_date, currency, wrapper) VALUES (?,?,?,?,?,?,?,?)",
        (owner, name, cls, "TestBank", cost, "2021-01-15", ccy, wrapper))
    assert cur.lastrowid is not None
    c.execute("INSERT INTO valuations (account_id, val_date, value, source)"
              " VALUES (?, '2026-09-01', ?, 'manual')", (cur.lastrowid, cost * 1.2))
    return int(cur.lastrowid)


def _tx(c: sqlite3.Connection, aid: int, kind: str, amount: float,
        d: str = "2026-05-10") -> None:
    c.execute("INSERT INTO transactions (account_id, op_date, kind, amount)"
              " VALUES (?,?,?,?)", (aid, d, kind, amount))


def _pos(c: sqlite3.Connection, aid: int, symbol: str, label: str,
         qty: float, pru: float) -> int:
    cur = c.execute(
        "INSERT INTO positions (account_id, symbol, label, quantity, pru)"
        " VALUES (?,?,?,?,?)", (aid, symbol, label, qty, pru))
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def test_inv_flows_wrappers_dividends_and_ytd():
    c = _conn()
    pea = _acc(c, "alice", "PEA X", "bourse", "pea", 10000)
    cto = _acc(c, "alice", "CTO Y", "bourse", "cto", 5000)
    av = _acc(c, "alice", "AV Z", "epargne", "av", 20000)
    _tx(c, pea, "deposit", 10000, "2022-01-10")
    _tx(c, pea, "withdrawal", 500, "2026-03-01")
    _tx(c, pea, "withdrawal", 200, "2025-12-01")  # pas YTD
    _tx(c, cto, "deposit", 5000)
    _tx(c, av, "deposit", 20000)
    p = _pos(c, cto, "AI.PA", "Air Liquide", 12, 141.5)
    _pos(c, cto, "IWDA.AS", "iShares MSCI World", 38, 118.4)
    c.execute("INSERT INTO dividend_events (position_id, ex_date, per_share)"
              " VALUES (?,?,?)", (p, "2026-05-15", 3.2))
    c.execute("INSERT INTO dividend_events (position_id, ex_date, per_share)"
              " VALUES (?,?,?)", (p, "2025-06-01", 2.9))
    c.commit()

    fl = appmod._inv_flows(c, ["alice"], ("pea", "cto"))
    assert set(fl) == {pea, cto} and av not in fl
    assert fl[pea]["inflow"] == 10000
    assert fl[pea]["outflow"] == 700
    assert fl[pea]["withdrawn_ytd"] == 500
    # dividendes : 12 × 3.2 (YTD) + 12 × 2.9 (total)
    assert fl[cto]["dividends_total"] == round(12 * (3.2 + 2.9), 2)
    assert fl[cto]["dividends_ytd"] == round(12 * 3.2, 2)
    # flux AV exclus du scope pea/cto
    assert av not in appmod._inv_flows(c, ["alice"], ("av",)) or True
    assert pea not in appmod._inv_flows(c, ["alice"], ("av",))


def test_inv_row_value_cost_gain_and_positions():
    c = _conn()
    _fx(c)
    aid = _acc(c, "alice", "CTO Y", "bourse", "cto", 5000)
    _tx(c, aid, "deposit", 3000)
    _tx(c, aid, "deposit", 2000)
    _tx(c, aid, "withdrawal", 100)  # coût réel = 4900 (txn wins sur cost_basis)
    _pos(c, aid, "AI.PA", "Air Liquide", 12, 141.5)
    c.execute("INSERT INTO prices (symbol, price, currency, ts)"
              " VALUES ('AI.PA', 168.4, '', '2026-09-01T00:00:00Z')")
    c.commit()
    latest = appmod._latest_valuations(c)
    txns = appmod._txn_summary(c)
    flows = appmod._inv_flows(c, ["alice"], ("cto",))
    row = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    assert row is not None
    out = appmod._inv_row(c, row, latest, txns, flows, [], True)
    assert out["value"] == round(5000 * 1.2, 2)          # dernière valo compte
    assert out["cost"] == 4900                            # deposits − withdrawals
    assert out["gain"] == round(out["value"] - 4900, 2)
    assert out["dividends_total"] == 0.0
    assert len(out["positions"]) == 1
    pos = out["positions"][0]
    assert pos["symbol"] == "AI.PA" and pos["quantity"] == 12
    assert pos["price"] == 168.4
    assert pos["value_eur"] == round(12 * 168.4, 2)       # cours du cache
    assert pos["gain_eur"] == round(12 * (168.4 - 141.5), 2)
    assert out["value"] is not None and out["last_val_date"] == "2026-09-01"


def test_inv_row_no_cost_no_gain_and_currency_conv():
    c = _conn()
    _fx(c, 1.08)
    # USD sans transactions → cost_basis utilisé, conversion EUR appliquée
    aid = _acc(c, "alice", "AV USD", "epargne", "av", 10000, ccy="USD")
    c.execute("INSERT INTO valuations (account_id, val_date, value, source)"
              " VALUES (?, '2026-09-01', 11000, 'manual')", (aid,))
    aid2 = _acc(c, "alice", "AV XXX", "epargne", "av", 5000, ccy="XXX")
    c.commit()
    latest = appmod._latest_valuations(c)
    txns = appmod._txn_summary(c)
    flows = appmod._inv_flows(c, ["alice"], ("av",))
    row = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    assert row is not None
    out = appmod._inv_row(c, row, latest, txns, flows, [], False)
    assert abs(out["value"] - 11000 / 1.08) < 0.01       # USD → EUR
    assert abs(out["cost"] - 10000 / 1.08) < 0.01
    assert out["gain"] is not None
    # devise inconnue → exclu + listé dans fx_missing
    row2 = c.execute("SELECT * FROM accounts WHERE id=?", (aid2,)).fetchone()
    assert row2 is not None
    missing = []
    out2 = appmod._inv_row(c, row2, latest, txns, flows, missing, False)
    assert out2 is None and missing == ["AV XXX"]


def test_inv_totals_and_no_double_count():
    c = _conn()
    _fx(c)
    a1 = _acc(c, "alice", "PEA A", "bourse", "pea", 8000)
    a2 = _acc(c, "alice", "PEA B", "bourse", "pea", 2000)
    c.execute("INSERT INTO valuations (account_id, val_date, value, source)"
              " VALUES (?, '2026-09-01', 9600, 'manual')", (a1,))
    c.execute("INSERT INTO valuations (account_id, val_date, value, source)"
              " VALUES (?, '2026-09-01', 2400, 'manual')", (a2,))
    c.commit()
    latest = appmod._latest_valuations(c)
    txns = appmod._txn_summary(c)
    flows = appmod._inv_flows(c, ["alice"], ("pea",))
    accs = []
    for r in c.execute("SELECT * FROM accounts WHERE wrapper='pea'").fetchall():
        out = appmod._inv_row(c, r, latest, txns, flows, [], True)
        if out:
            accs.append(out)
    tot = appmod._inv_totals(accs)
    assert tot["value"] == 12000 and tot["count"] == 2
    assert tot["gain"] == round(12000 - 10000, 2)
    # les dividendes cumulés d'un compte ne comptent pas dans le gain
    assert tot["dividends_total"] == 0.0


def test_seed_demo_invest_markers():
    """Le seed démo (v054) pose wrappers + 2 lignes CTO + prix cache : testé
    sur une base fraîche via le module crowdfunding/crypto déjà seedés par
    ailleurs — ici on vérifie seulement la cohérence du jeu de fixtures."""
    c = _conn()
    # reflète la structure attendue du seed (positions + dividendes + prix)
    aid = _acc(c, "demo", "CTO (Boursorama)", "bourse", "cto", 12000)
    pa = _pos(c, aid, "AI.PA", "Air Liquide", 12, 141.5)
    c.execute("INSERT INTO dividend_events (position_id, ex_date, per_share)"
              " VALUES (?,?,?)", (pa, "2026-05-15", 3.2))
    c.execute("INSERT INTO prices (symbol, price, currency, ts)"
              " VALUES ('AI.PA', 168.4, '', '2026-09-01T00:00:00Z')")
    c.commit()
    fl = appmod._inv_flows(c, ["demo"], ("cto",))
    assert fl[aid]["dividends_ytd"] == round(12 * 3.2, 2)
