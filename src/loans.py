"""Module Crédits (v2026.09.056) — passifs suivis par type (immo/auto/conso).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI — la connexion
(base principale OU base mémoire d'un coffre protégé, via le routage de
l'appelant) est TOUJOURS passée en paramètre `conn`. Les messages d'erreur
sont émis en FR (source de vérité lisible) — le middleware HTTP de src/app.py
les traduit selon Accept-Language.

Principes (design claude/design-credits-2026.md, validé Fred 2026-09-09) :
- UN crédit = UNE ligne `loans`. Le **capital restant dû est DÉCLARÉ**
  (source de vérité du passif : taux variables, remboursements anticipés,
  reports → la réalité prime sur l'échéancier théorique).
- PAS de table d'échéancier stockée : l'amortissement (français) est
  calculé à la demande (`schedule` / `amortize`) — même formule que
  l'ancienne courbe JS v033, portée ici = source unique (question D du
  design : le frontend appellera l'API, plus de JS dupliqué).
- `recompute` : valeur théorique du restant au jour J simulée depuis
  `start_date` — proposée à l'utilisateur, JAMAIS appliquée d'office.
- Migration boot : les colonnes legacy `accounts.loan_principal/rate/
  monthly` (crédit lié immo v033) sont migrées vers `loans` puis
  **neutralisées** (remises à 0) — le compte immobilier ne porte plus le
  passif.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date

LOAN_TYPES = ("immo", "auto", "conso")

# Garde-fou d'une simulation d'amortissement (mensualité pathologique à taux
# nul : n = P0/M peut dépasser 100 000 mois) — les lignes détaillées sont de
# toute façon plafonnées par le paramètre `months` des routes.
_SIM_MAX = 6000


# ---------------------------------------------------------------- migration legacy (v033 → v056)

def migrate_legacy(conn: sqlite3.Connection) -> int:
    """Migre les crédits liés legacy (`accounts.loan_*`, immo uniquement) vers
    la table `loans` puis neutralise les colonnes (remises à 0).

    Idempotent : un compte déjà lié à une ligne loans (même soft-deleted) est
    ignoré, et les colonnes neutralisées ne re-matchent plus. À exécuter sur
    la base principale AU BOOT et sur la base mémoire d'un coffre à son
    ouverture/init (les comptes protégés vivent dans le blob chiffré).

    Retourne le nombre de crédits créés (0 = rien à migrer).
    """
    # bases très anciennes (tests de migration) : sans `currency`, il n'y a
    # par définition aucun crédit lié — ne pas SELECT sur une colonne absente
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "currency" not in cols or "loan_principal" not in cols:
        return 0
    rows = conn.execute(
        "SELECT id, owner, name, currency, loan_principal, loan_rate,"
        " loan_monthly, open_date FROM accounts"
        " WHERE asset_class='immobilier' AND active=1 AND loan_principal>0"
        " AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.account_id=accounts.id)"
    ).fetchall()
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO loans (owner, name, loan_type, currency,"
            " principal_initial, principal_remaining, rate_annual,"
            " monthly_payment, insurance_monthly, start_date, account_id,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,0,?,?, datetime('now'), datetime('now'))",
            (r["owner"], r["name"], "immo", r["currency"] or "EUR",
             r["loan_principal"], r["loan_principal"], r["loan_rate"] or 0,
             r["loan_monthly"] or 0, r["open_date"], r["id"]),
        )
        # neutralisation : le passif vit désormais dans loans — le compte ne
        # doit plus être relu comme porteur de crédit (summary/équité)
        conn.execute(
            "UPDATE accounts SET loan_principal=0, loan_rate=0,"
            " loan_monthly=0, updated_at=datetime('now') WHERE id=?",
            (r["id"],),
        )
        n += 1
    return n


# ---------------------------------------------------------------- amortissement (formule unique)

def amortize(principal_remaining: float, rate_annual: float,
             monthly_payment: float, months: int = 480) -> dict | None:
    """Amortissement français depuis le restant dû « aujourd'hui ».

    Portage EXACT de l'ancienne courbe JS v033 (loanCurve) : n = ceil(log(M /
    (M − P0·r)) / log(1+r)), dernière échéance ajustée au restant. Retourne
    None si le crédit ne s'amortit jamais (mensualité nulle ou ≤ intérêts du
    1er mois).

    Sortie : {months_left, rows:[{k, month:'YYYY-MM', interest, principal,
    remaining}], interests_left, next_year_capital, next_year_interests} —
    `rows` plafonné à `months` lignes (détail), les totaux restent exacts.
    """
    if principal_remaining <= 0 or monthly_payment <= 0:
        return None
    r = rate_annual / 100.0 / 12.0
    if r > 0 and monthly_payment <= principal_remaining * r:
        return None
    if r > 0:
        n = int(math.ceil(
            math.log(monthly_payment / (monthly_payment - principal_remaining * r))
            / math.log(1 + r)))
    else:
        n = int(math.ceil(principal_remaining / monthly_payment))
    # simulation mensuelle (dernière échéance ajustée au restant exact)
    rest = principal_remaining
    rows: list[dict] = []
    total_interest = 0.0
    today = date.today()
    ny_cap = 12  # agrégats des 12 prochains mois
    ny_capital = ny_interest = 0.0
    for k in range(1, min(n, _SIM_MAX) + 1):
        it = rest * r
        am = monthly_payment - it
        if am >= rest:
            am = rest
        rest -= am
        total_interest += it
        y, m = today.year, today.month
        y += (m - 1 + k) // 12
        mo = (m - 1 + k) % 12 + 1
        row = {"k": k, "month": f"{y:04d}-{mo:02d}",
               "interest": round(it, 2), "principal": round(am, 2),
               "remaining": round(rest, 2)}
        if k <= ny_cap:
            ny_capital += am
            ny_interest += it
        if len(rows) < months:
            rows.append(row)
        if rest <= 1e-9:
            break
    return {
        "months_left": n,
        "rows": rows,
        "interests_left": round(total_interest, 2),
        "next_year_capital": round(ny_capital, 2),
        "next_year_interests": round(ny_interest, 2),
    }


def _months_anniversaries(start: date, asof: date) -> int:
    """Nombre d'échéances mensuelles échues depuis start (1re échéance à
    start + 1 mois) jusqu'à asof inclus."""
    k = 1
    while True:
        y = start.year + (start.month - 1 + k) // 12
        m = (start.month - 1 + k) % 12 + 1
        if date(y, m, 1) > asof:
            return k - 1
        k += 1


def theoretical_remaining(conn: sqlite3.Connection, loan_id: int) -> float | None:
    """Restant dû théorique au jour J : simulation depuis `start_date` avec
    mensualités régulières (principal_initial, taux, mensualité actuels).
    None si la simulation ne converge pas (mensualité trop faible) ou si la
    date de départ manque — l'appelant refuse alors proprement."""
    row = conn.execute(
        "SELECT principal_initial, principal_remaining, rate_annual,"
        " monthly_payment, start_date FROM loans WHERE id=?",
        (loan_id,),
    ).fetchone()
    if row is None or not row["start_date"]:
        return None
    r = (row["rate_annual"] or 0) / 100.0 / 12.0
    M = row["monthly_payment"] or 0
    P0 = row["principal_initial"] or 0
    if M <= 0 or P0 <= 0 or (r > 0 and M <= P0 * r):
        return None
    try:
        start = date.fromisoformat(row["start_date"])
    except ValueError:
        return None
    paid = _months_anniversaries(start, date.today())
    if paid <= 0:
        return round(P0, 2)
    rest = P0
    for _ in range(min(paid, _SIM_MAX)):
        it = rest * r
        am = M - it
        if am >= rest:
            am = rest
        rest -= am
        if rest <= 0:
            break
    return round(max(0.0, rest), 2)
