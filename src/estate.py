"""Module Locations & TCO (v2026.09.058) — suivi locatif + coût total de possession.

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI — la connexion
(base principale OU base mémoire d'un coffre protégé) est TOUJOURS passée en
paramètre `conn`. Messages d'erreur en FR (source de vérité) — le middleware
HTTP de src/app.py les traduit selon Accept-Language.

Design claude/design-locations-tco-2026.md (validé Fred 2026-09-09) :
- Locations : contrats par bien immobilier loué (locataire, loyer, dépôt,
  début/fin) + encaissements datés par mois couvert ; un encaissement
  MATÉRIALISE une opération income (source_id loc:enc:<id>) — zéro double
  saisie, trésorerie cohérente. AUTONOME des income_rules.
- TCO : fiches (tco_items) = biens immo (lien compte, crédit auto-détecté par
  loans.account_id) et véhicules (HORS patrimoine, crédit auto/conso lié
  explicitement). Dépenses imputées = lien vers UNE opération expense de
  l'owner (une op = un objet, jamais de création). Coût du crédit = dérivé du
  module loans (paid_breakdown, simulation théorique documentée).
- VÉHICULE = coût complet CASH (décision Fred) : apport + mensualités versées
  (capital + intérêts + assurance) + imputations — le capital n'est jamais
  compté en plus du prix d'achat. IMMO loué : l'achat n'entre pas (actif
  valorisé) — intérêts + assurance + imputations, loyers en regard.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

from src.loans import paid_breakdown

ITEMS_KINDS = ("immo", "vehicle")
IMPUT_CATS = {
    "immo": ("maintenance", "tax", "insurance", "charges", "work", "other"),
    "vehicle": ("fuel", "maintenance", "insurance", "tax", "parking", "other"),
}
_YM_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ------------------------------------------------------------------ helpers dates

def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y += (m - 1 + k) // 12
    m = (m - 1 + k) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _months_between(ym_a: str, ym_b: str) -> int:
    """Mois entre deux YYYY-MM (b − a, b exclus si on veut une durée)."""
    return (int(ym_b[:4]) - int(ym_a[:4])) * 12 + int(ym_b[5:7]) - int(ym_a[5:7])


def _iter_months(ym_start: str, ym_end_incl: str):
    """Itère les YYYY-MM de start à end incluse (garde-fou 1200)."""
    n = _months_between(ym_start, ym_end_incl)
    for k in range(min(n, 1200) + 1):
        yield _ym_add(ym_start, k)


def _last_value(conn: sqlite3.Connection, account_id: int) -> float | None:
    row = conn.execute(
        "SELECT value FROM valuations WHERE account_id=? ORDER BY date DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    return row["value"] if row else None


def _today_ym() -> str:
    return _ym(date.today())


# ------------------------------------------------------------------ contrats

def contract_err(conn: sqlite3.Connection, owner: str, b: dict,
                 contract_id: int | None = None) -> str | None:
    """Validation POST/PUT d'un contrat. None = OK."""
    if not (b.get("tenant") or "").strip():
        return "Renseignez le nom du locataire"
    try:
        rent = float(b.get("rent_monthly") or 0)
    except (TypeError, ValueError):
        return "Loyer mensuel invalide"
    if rent <= 0:
        return "Le loyer mensuel doit être positif"
    try:
        start = b["start_date"]
    except KeyError:
        return "Date de début requise"
    try:
        date.fromisoformat(start)
    except (TypeError, ValueError):
        return "Date de début invalide"
    end = b.get("end_date")
    if end:
        try:
            date.fromisoformat(end)
        except (TypeError, ValueError):
            return "Date de fin invalide"
        if end < start:
            return "La date de fin précède la date de début"
    acc = conn.execute(
        "SELECT id, asset_class FROM accounts WHERE id=? AND owner=? AND active=1",
        (b.get("account_id"), owner),
    ).fetchone()
    if acc is None or acc["asset_class"] != "immobilier":
        return "Le contrat doit être lié à un bien immobilier"
    if contract_id is not None:
        cur = conn.execute(
            "SELECT id FROM loc_contracts WHERE account_id=? AND active=1"
            " AND id<>? LIMIT 1", (acc["id"], contract_id),
        ).fetchone()
        if cur and (not end or end >= _today_ym() + "-01"):
            return "Ce bien a déjà un contrat actif (clôturez-le d'abord)"
    else:
        cur = conn.execute(
            "SELECT id FROM loc_contracts WHERE account_id=? AND active=1"
            " LIMIT 1", (acc["id"],),
        ).fetchone()
        if cur and (not end or end >= _today_ym() + "-01"):
            return "Ce bien a déjà un contrat actif (clôturez-le d'abord)"
    return None


def contract_payload(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    out = {k: row[k] for k in row.keys()}
    acc = conn.execute("SELECT name FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
    out["account_name"] = acc["name"] if acc else None
    ag = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM loc_payments"
        " WHERE contract_id=?", (row["id"],),
    ).fetchone()
    out["payments_count"] = ag["n"]
    out["payments_sum"] = round(ag["s"], 2)
    return out


# ------------------------------------------------------------------ encaissements

def pay_err(conn: sqlite3.Connection, owner: str, b: dict) -> str | None:
    try:
        amount = float(b.get("amount") or 0)
    except (TypeError, ValueError):
        return "Montant invalide"
    if amount <= 0:
        return "Le montant doit être positif"
    if not _YM_RE.match(b.get("month") or ""):
        return "Mois couvert invalide (YYYY-MM)"
    try:
        date.fromisoformat(b.get("op_date") or "")
    except (TypeError, ValueError):
        return "Date d'encaissement invalide"
    c = conn.execute(
        "SELECT c.id FROM loc_contracts c WHERE c.id=? AND c.owner=?", (b.get("contract_id"), owner),
    ).fetchone()
    if c is None:
        return "Contrat introuvable"
    acc = conn.execute(
        "SELECT id FROM accounts WHERE id=? AND owner=? AND active=1",
        (b.get("cash_account_id"), owner),
    ).fetchone()
    if acc is None:
        return "Compte d'encaissement introuvable"
    return None


def payments_for(conn: sqlite3.Connection, contract_id: int, year: str | None = None):
    sql = "SELECT p.* FROM loc_payments p WHERE p.contract_id=?"
    args: list = [contract_id]
    if year:
        if re.match(r"^\d{4}-\d{2}$", year):
            sql += " AND p.month = ?"
            args.append(year)
        elif re.match(r"^\d{4}$", year):
            sql += " AND p.month LIKE ?"
            args.append(year + "%")
    return conn.execute(sql + " ORDER BY p.month DESC, p.id DESC", args).fetchall()


# ------------------------------------------------------------------ Locations : attendu / perçu / occupation / rendements

def _contracts_active(conn: sqlite3.Connection, owner: str, account_id: int):
    """Contrats (actifs ou à cheval sur la période) du bien — active=1 ou
    clôturés : seuls les actifs (ou clôturés mais qui ont couvert des mois)
    comptent dans l'attendu passé ; un contrat clôturé garde ses mois."""
    return conn.execute(
        "SELECT * FROM loc_contracts WHERE owner=? AND account_id=?", (owner, account_id),
    ).fetchall()


def expected_by_month(conn: sqlite3.Connection, owner: str, account_id: int,
                      ym_start: str, ym_end: str) -> dict[str, float]:
    """Loyer ATTENDU par mois sur [ym_start, ym_end] — somme des contrats
    couvrant chaque mois (start ≤ mois ≤ min(end, aujourd'hui))."""
    out: dict[str, float] = {}
    today = _today_ym()
    for c in _contracts_active(conn, owner, account_id):
        s = max(c["start_date"][:7], ym_start)
        e = c["end_date"][:7] if c["end_date"] else today
        e = min(e, ym_end)
        if e < s or s > ym_end:
            continue
        for m in _iter_months(s, e):
            out[m] = round(out.get(m, 0.0) + c["rent_monthly"], 2)
    return out


def perceived_by_month(conn: sqlite3.Connection, owner: str, account_id: int,
                       ym_start: str, ym_end: str) -> dict[str, float]:
    out: dict[str, float] = {}
    rows = conn.execute(
        "SELECT p.month, p.amount FROM loc_payments p"
        " JOIN loc_contracts c ON c.id=p.contract_id"
        " WHERE c.owner=? AND c.account_id=? AND p.month BETWEEN ? AND ?",
        (owner, account_id, ym_start, ym_end),
    ).fetchall()
    for r in rows:
        out[r["month"]] = round(out.get(r["month"], 0.0) + r["amount"], 2)
    return out


def occupancy(conn: sqlite3.Connection, owner: str, account_id: int) -> dict:
    """Taux d'occupation : mois couverts par ≥ 1 contrat actif ÷ mois depuis
    max(open_date du compte, 1er contrat). Borné au mois courant."""
    acc = conn.execute(
        "SELECT open_date FROM accounts WHERE id=? AND owner=?", (account_id, owner),
    ).fetchone()
    contracts = _contracts_active(conn, owner, account_id)
    first = min((c["start_date"][:7] for c in contracts), default=None)
    if not contracts or not first:
        return {"from": None, "months": 0, "covered": 0, "pct": None}
    start = max(acc["open_date"][:7] if acc and acc["open_date"] else first, first)
    end = _today_ym()
    if _months_between(start, end) < 0:
        return {"from": start, "months": 0, "covered": 0, "pct": None}
    covered = set()
    for c in contracts:
        if c["active"] == 0 and not c["end_date"]:
            continue
        s = max(c["start_date"][:7], start)
        e = c["end_date"][:7] if c["end_date"] else end
        for m in _iter_months(s, min(e, end)):
            covered.add(m)
    total = _months_between(start, end) + 1
    pct = round(len(covered) / total * 100, 1) if total > 0 else None
    return {"from": start, "months": total, "covered": len(covered), "pct": pct}


def _imputed_window(conn: sqlite3.Connection, owner: str, account_id: int,
                    ym_from: str | None = None) -> float:
    """Σ des imputations sur la fiche TCO du bien (si elle existe) — fenêtre
    optionnelle par mois ≥ ym_from."""
    sql = ("SELECT COALESCE(SUM(t.amount),0) AS s FROM tco_imputations i"
           " JOIN transactions t ON t.id=i.transaction_id"
           " JOIN tco_items it ON it.id=i.item_id"
           " WHERE i.owner=? AND it.account_id=?")
    args: list = [owner, account_id]
    if ym_from:
        sql += " AND t.op_date >= ?"
        args.append(ym_from + "-01")
    return round(conn.execute(sql, args).fetchone()["s"], 2)


def credit_paid_12m(conn: sqlite3.Connection, account_id: int) -> float:
    """Intérêts + assurance emprunteur payés sur les 12 derniers mois (crédit
    du bien via loans.account_id). 0 si aucun crédit exploitable."""
    loan = conn.execute(
        "SELECT id FROM loans WHERE account_id=? AND active=1"
        " AND COALESCE(principal_remaining,0)>0 ORDER BY id LIMIT 1", (account_id,),
    ).fetchone()
    if loan is None:
        return 0.0
    t = date.today()
    b = paid_breakdown(conn, loan["id"], t)
    a = paid_breakdown(conn, loan["id"], date(t.year - 1, t.month, 1))
    if b is None:
        return 0.0
    if a is None:
        return round(b["paid_interest"] + b["paid_insurance"], 2)
    return round((b["paid_interest"] - a["paid_interest"])
                 + (b["paid_insurance"] - a["paid_insurance"]), 2)


# ------------------------------------------------------------------ TCO : fiches

def item_err(conn: sqlite3.Connection, owner: str, b: dict,
             item_id: int | None = None) -> str | None:
    kind = b.get("kind")
    if kind not in ITEMS_KINDS:
        return "Type d'objet invalide (immo ou vehicle)"
    if not (b.get("label") or "").strip():
        return "Renseignez un nom"
    acc_id = b.get("account_id")
    if kind == "immo":
        acc = conn.execute(
            "SELECT id FROM accounts WHERE id=? AND owner=? AND active=1"
            " AND asset_class='immobilier'", (acc_id, owner),
        ).fetchone()
        if acc is None:
            return "Le bien immobilier lié est introuvable"
        dup = conn.execute(
            "SELECT id FROM tco_items WHERE owner=? AND kind='immo' AND account_id=?"
            " AND active=1 AND id<>? LIMIT 1", (owner, acc_id, item_id or -1),
        ).fetchone()
        if dup:
            return "Ce bien a déjà une fiche de coûts"
    loan_id = b.get("loan_id")
    if loan_id:
        loan = conn.execute(
            "SELECT id, loan_type, owner FROM loans WHERE id=? AND owner=?",
            (loan_id, owner),
        ).fetchone()
        if loan is None:
            return "Crédit lié introuvable"
        if kind == "vehicle" and loan["loan_type"] not in ("auto", "conso"):
            return "Le crédit d'un véhicule doit être de type auto ou conso"
        if kind == "immo" and loan["loan_type"] != "immo":
            return "Le crédit d'un bien immobilier doit être de type immo"
        dup = conn.execute(
            "SELECT id FROM tco_items WHERE owner=? AND loan_id=? AND active=1"
            " AND id<>? LIMIT 1", (owner, loan_id, item_id or -1),
        ).fetchone()
        if dup:
            return "Ce crédit est déjà lié à une autre fiche"
    for f in ("purchase_date",):
        if b.get(f):
            try:
                date.fromisoformat(b[f])
            except (TypeError, ValueError):
                return "Date invalide"
    try:
        if b.get("purchase_price") is not None and float(b["purchase_price"]) < 0:
            return "Prix d'achat invalide"
    except (TypeError, ValueError):
        return "Prix d'achat invalide"
    return None


def _item_loan(conn: sqlite3.Connection, item_row) -> sqlite3.Row | None:
    """Crédit de la fiche : loan_id explicite, sinon (immo) loan du compte."""
    if item_row["loan_id"]:
        return conn.execute("SELECT * FROM loans WHERE id=?", (item_row["loan_id"],)).fetchone()
    if item_row["kind"] == "immo" and item_row["account_id"]:
        return conn.execute(
            "SELECT * FROM loans WHERE account_id=? AND active=1"
            " AND COALESCE(principal_remaining,0)>0 ORDER BY id LIMIT 1",
            (item_row["account_id"],),
        ).fetchone()
    return None


def impute_err(conn: sqlite3.Connection, owner: str, transaction_id: int,
               item_id: int, category: str) -> str | None:
    tx = conn.execute(
        "SELECT t.id, t.kind FROM transactions t"
        " JOIN accounts a ON a.id=t.account_id"
        " WHERE t.id=? AND a.owner=?", (transaction_id, owner),
    ).fetchone()
    if tx is None:
        return "Opération introuvable"
    if tx["kind"] != "expense":
        return "Seules les dépenses (expense) peuvent être imputées"
    item = conn.execute(
        "SELECT id, kind, owner FROM tco_items WHERE id=? AND owner=? AND active=1",
        (item_id, owner),
    ).fetchone()
    if item is None:
        return "Fiche de coûts introuvable"
    if category not in IMPUT_CATS.get(item["kind"], ()):
        return "Catégorie invalide pour ce type d'objet"
    return None


def item_costs(conn: sqlite3.Connection, item_row, asof: date | None = None) -> dict:
    """Coûts d'une fiche TCO : imputations (total, par catégorie, par année,
    12 m glissants) + composante crédit dérivée (paid_breakdown). Pour un
    véhicule : acquisition cash (apport + mensualités versées) et projection
    au terme — le capital n'est jamais compté en plus du prix d'achat."""
    asof = asof or date.today()
    own = item_row["owner"]
    # --- imputations
    rows = conn.execute(
        "SELECT t.op_date, t.amount, i.category FROM tco_imputations i"
        " JOIN transactions t ON t.id=i.transaction_id"
        " WHERE i.item_id=? AND i.owner=? AND t.kind='expense'",
        (item_row["id"], own),
    ).fetchall()
    by_cat: dict[str, float] = {}
    by_year: dict[str, float] = {}
    total_usage = 0.0
    last12 = 0.0
    first_usage: str | None = None
    cutoff = _ym_add(_ym(asof), -12)
    for r in rows:
        by_cat[r["category"]] = round(by_cat.get(r["category"], 0.0) + r["amount"], 2)
        by_year[r["op_date"][:4]] = round(by_year.get(r["op_date"][:4], 0.0) + r["amount"], 2)
        total_usage = round(total_usage + r["amount"], 2)
        if r["op_date"] >= cutoff + "-01":
            last12 = round(last12 + r["amount"], 2)
        if first_usage is None or r["op_date"] < first_usage:
            first_usage = r["op_date"]
    # --- crédit dérivé
    loan = _item_loan(conn, item_row)
    bd = paid_breakdown(conn, loan["id"], asof) if loan is not None else None
    credit = None
    if loan is not None:
        credit = {
            "id": loan["id"], "name": loan["name"],
            "paid_capital": bd["paid_capital"] if bd else None,
            "paid_interest": bd["paid_interest"] if bd else None,
            "paid_insurance": bd["paid_insurance"] if bd else None,
            "months_paid": bd["months_paid"] if bd else None,
            "months_left": (bd["scheduled_payments"] - bd["months_paid"])
            if bd and bd["scheduled_payments"] else None,
            "credit_total_at_term": bd["credit_total_at_term"] if bd else None,
            "insurance_total_at_term": bd["insurance_total_at_term"] if bd else None,
            "term_months": bd["term_months"] if bd else None,
            "note": None if bd else "Crédit sans date de départ — coût non ventilable",
        }
    # --- agrégation
    if item_row["kind"] == "vehicle":
        price = item_row["purchase_price"] or 0.0
        financed = loan["principal_initial"] if (loan is not None and bd) else 0.0
        down = max(0.0, price - financed) if loan is not None else price
        if bd is not None:
            acquisition = round(down + bd["paid_capital"] + bd["paid_interest"]
                                + bd["paid_insurance"], 2)
            at_term = round(down + bd["credit_total_at_term"]
                            + bd["insurance_total_at_term"], 2)
        else:
            acquisition = down
            at_term = down
        total = round(acquisition + total_usage, 2)
        start_ym = None
        for s in (item_row["purchase_date"], loan["start_date"] if loan else None, first_usage):
            if s and (start_ym is None or s[:7] < start_ym):
                start_ym = s[:7]
        span = _months_between(start_ym, _ym(asof)) + 1 if start_ym else 0
        per_month = round(total / span, 2) if span > 0 else None
        # années : crédit (capital+intérêts+assurance) + imputations
        ytable: dict[str, dict] = {}
        if bd is not None:
            for y, v in bd["years"].items():
                ytable[y] = {"credit": round(v["capital"] + v["interest"] + v["insurance"], 2),
                             "imputations": by_year.get(y, 0.0),
                             "total": round(v["capital"] + v["interest"] + v["insurance"]
                                            + by_year.get(y, 0.0), 2)}
        for y, v in by_year.items():
            if y not in ytable:
                ytable[y] = {"credit": 0.0, "imputations": v, "total": round(v, 2)}
        return {
            "kind": "vehicle", "label": item_row["label"],
            "acquisition_to_date": round(acquisition, 2),
            "usage_to_date": round(total_usage, 2),
            "total_to_date": total,
            "total_at_term": round(at_term + total_usage, 2),
            "months_owned": span, "per_month": per_month,
            "per_year": round(per_month * 12, 2) if per_month is not None else None,
            "by_cat": by_cat, "by_year": dict(sorted(ytable.items())),
            "last12_usage": last12, "credit": credit,
        }
    # immo : intérêts + assurance payés + imputations (l'achat n'entre pas)
    ytable = {}
    credit_to_date = 0.0
    if bd is not None:
        for y, v in bd["years"].items():
            c = round(v["interest"] + v["insurance"], 2)
            ytable[y] = {"credit": c, "imputations": by_year.get(y, 0.0),
                         "total": round(c + by_year.get(y, 0.0), 2)}
            if y <= str(asof.year):
                credit_to_date = round(credit_to_date + c, 2)
    for y, v in by_year.items():
        if y not in ytable:
            ytable[y] = {"credit": 0.0, "imputations": v, "total": round(v, 2)}
    total = round(credit_to_date + total_usage, 2)
    return {
        "kind": "immo", "label": item_row["label"],
        "credit_to_date": round(credit_to_date, 2),
        "usage_to_date": round(total_usage, 2),
        "total_to_date": total,
        "by_cat": by_cat, "by_year": dict(sorted(ytable.items())),
        "last12_usage": last12,
        "last12_credit": round(credit_paid_12m(conn, item_row["account_id"]), 2)
        if item_row["account_id"] else 0.0,
        "credit": credit,
    }
