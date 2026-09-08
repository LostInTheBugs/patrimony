"""
Module Crowdfunding (v2026.09.046) — suivi projet-par-projet des plateformes
d'investissement participatif immobilier (Bricks.co, La Première Brique).

Porté depuis l'application autonome Crowdfunding Tracker (2026-08) avec les
conventions Patrimony : tables scopées par `owner`, valeurs dérivées jamais
stockées (project_computed), source de vérité = opérations importées, et
matérialisation du patrimoine plateforme dans des comptes-auto de classe
crowdfunding (refresh_integration) pour alimenter le dashboard/évolution/
historique SANS modification de leurs requêtes.

Toutes les fonctions prennent la connexion (base principale OU coffre routé)
et le(s) owner(s) en paramètre — aucune dépendance au contexte HTTP.
"""

import calendar
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- constantes

CF_PLATFORMS = {
    "bricks": "Bricks.co",
    "lapremierebrique": "La Première Brique",
}
CF_PLATFORM_ORDER = ("bricks", "lapremierebrique")

# Statuts possibles (les libellés i18n vivent côté frontend)
CF_STATUS_KEYS = ("en_collecte", "en_cours", "retard", "rembourse", "perdu")

# Types Bricks.co qui représentent un investissement (mise)
BRICKS_INVEST_TYPES = {"Achat de bricks", "Achat marketplace", "Frais d'achat marketplace"}
# Types LPB qui représentent une souscription (mise) / annulation / mensualité
LPB_INVEST_PREFIX = "Souscription au projet"
LPB_CANCEL_PREFIX = "Annulation de la souscription au projet"
LPB_REPAY_PREFIX = "Remboursement mensualité"

# clés candidates pour l'extraction heuristique (captures extension)
_RATE_KEYS = ("rate", "interest_rate", "annual_rate", "annual_interest", "taux", "gross_rate", "yield", "rendement", "rentability")
_DURATION_KEYS = ("duration", "duration_months", "months", "term", "duree", "horizon", "duration_in_months", "maturity_months")
_STATUS_KEYS = ("status", "state", "statut", "phase", "project_status", "current_status")
_INVESTED_KEYS = ("invested", "invested_amount", "total_invested", "amount_invested", "capital_invested", "mise", "total_invested_amount", "investment_amount", "montant_investi", "capital_investi", "montant_invest", "invested_capital")
_NAME_KEYS = ("name", "title", "project_name", "property_name", "label", "nom", "project", "property", "program_name", "slug", "property_name")
_AMOUNT_KEYS = ("amount", "total_amount", "amount_invested", "capital", "invested", "current_amount", "montant", "principal")
_DATE_KEYS = ("end_date", "expected_end_date", "maturity_date", "expected_date", "due_date", "fin_date", "term_date", "date_end")
_START_KEYS = ("start_date", "investment_date", "date_start", "begin_date", "purchase_date", "acquisition_date")

_SC_STATUS_MAP = {
    "en_cours": ("en_cours", "invested", "active", "funded", "financé", "finance", "running", "ongoing", "en cours", "en remboursement", "succeeded", "succes"),
    "en_collecte": ("en_collecte", "collecting", "open", "collecte", "en collecte", "fundraising", "en cours de collecte"),
    "rembourse": ("rembourse", "repaid", "completed", "closed", "finished", "terminé", "termine", "clos", "remboursé", "fini", "refunded", "rembourse"),
    "perdu": ("perdu", "default", "lost", "failure", "perte", "défaut", "defaut", "litige", "en procédure", "late"),
}

_MONTHS_FR = {
    "janvier": "01", "fevrier": "02", "mars": "03", "avril": "04", "mai": "05", "juin": "06",
    "juillet": "07", "aout": "08", "septembre": "09", "octobre": "10", "novembre": "11", "decembre": "12",
    "janv": "01", "fevr": "02", "mars": "03", "avr": "04", "mai": "05", "juin": "06",
    "juil": "07", "aout": "08", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
    # abréviations 3 lettres (LPB : « 28 oct. 2024 », « 31 jan. 2024 », « 26 mar. 2026 »)
    "jan": "01", "fev": "02", "mar": "03", "jun": "06", "jui": "07", "aou": "08", "sep": "09",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- helpers dates & parsing

def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _norm_name(s) -> str:
    """Normalise un nom de projet : minuscules, sans accents, apostrophes unifiées."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().replace("’", "'").replace(" ", "").strip()


def _norm_apos(s: str) -> str:
    """Normalise les apostrophes typographiques (’ U+2019) en apostrophe droite."""
    return str(s).replace("\u2019", "'").replace("\u2018", "'")


def _parse_fr_amount(v) -> float:
    """Parse un montant '35,56 €' / '-100,00 €' / 35.56 / -10.18."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("€", "").replace("\u00a0", "").replace(" ", "").strip()
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_fr_date(v) -> str | None:
    """Parse '10/08/2026' → '2026-08-10'. Retourne None si non parsable."""
    if v is None:
        return None
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_iso_date(s) -> str | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_float(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\u00a0", " ").replace(" ", "").replace("\u20ac", "").replace("€", "").replace("%", "").replace(",", ".")
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_scrape(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().replace("’", "'").replace("‘", "'").strip()


def _parse_fr_text_date(s) -> str | None:
    """Parse une date française texte : '21 octobre 2024', '11 juil. 2025', '28 oct. 2024'."""
    s = str(s or "").strip()
    m = re.match(r"^(\d{1,2})\s+([a-zA-Zéû]+)\.?\s+(\d{4})$", s)
    if m:
        day, mon, year = m.groups()
        mon_key = unicodedata.normalize("NFD", mon.lower())
        mon_key = "".join(c for c in mon_key if unicodedata.category(c) != "Mn")
        if mon_key in _MONTHS_FR:
            try:
                return date(int(year), int(_MONTHS_FR[mon_key]), int(day)).isoformat()
            except ValueError:
                return None
    return _parse_iso_date(s) or _parse_fr_date(s)


# ---------------------------------------------------------------- indicateurs projet

def project_computed(row, today: date | None = None, extra: dict | None = None) -> dict:
    """Calcule tous les indicateurs dérivés d'un projet (jamais stockés).

    Règles métier portées du Crowdfunding Tracker :
    - le retard (`is_late`) repose sur l'échéance RÉELLE uniquement — une
      échéance dérivée (start+durée, « X mois max. restants ») est affichée
      avec ≈ mais ne déclenche JAMAIS de retard ;
    - un contrat Royalties n'a pas d'échéance (capital remboursé à la revente) ;
    - gravité du retard = intérêts reçus vs attendus (in-fine ⇒ négligeable).
    """
    today = today or date.today()
    p = dict(row)
    extra = extra or {}

    start = parse_date(p.get("start_date"))
    expected_real = parse_date(p.get("expected_end_date"))
    expected = expected_real
    derived = None
    if expected is None and p.get("rest_months"):
        derived = add_months(today, int(p["rest_months"]))
        expected = derived
    if expected is None and start is not None and p.get("duration_months"):
        derived = add_months(start, int(p["duration_months"]))
        expected = derived

    invested = p.get("invested") or 0.0
    rate = p.get("rate") or 0.0
    repaid = p.get("repaid_capital") or 0.0
    interest_recv = p.get("interest_received") or 0.0
    status = p.get("status") or "en_cours"

    is_late = False
    days_delayed = 0
    if status in ("en_cours",) and expected_real is not None:
        days_delayed = (today - expected_real).days
        is_late = days_delayed > 0

    accrued = 0.0
    if status == "en_cours" and start is not None and invested > 0 and rate > 0:
        days = (today - start).days
        if days > 0:
            accrued = round(invested * (rate / 100.0) * days / 365.0, 2)

    expected_interest = None
    interest_ratio = None
    late_severity = None
    if status == "retard":
        exp = None
        if start is not None and invested > 0 and rate > 0:
            d = (today - start).days
            if d > 0:
                exp = round(invested * (rate / 100.0) * d / 365.0, 2)
        expected_interest = exp
        if exp:
            interest_ratio = round(interest_recv / exp, 3)
        if p.get("infine"):
            late_severity = "negligeable"
        elif exp is not None:
            ratio = interest_ratio if interest_ratio is not None else 0.0
            if interest_recv <= 0 or ratio < 0.3:
                late_severity = "critique"
            elif ratio < 0.6:
                late_severity = "important"
            elif ratio < 0.9:
                late_severity = "significatif"
            else:
                late_severity = "negligeable"
        else:
            last_int = extra.get("last_interest")
            if last_int and (today - last_int).days < 120:
                late_severity = "significatif"
            else:
                late_severity = "important"
    months_since_interest = None
    if extra.get("last_interest"):
        months_since_interest = round((today - extra["last_interest"]).days / 30.44, 1)

    loss = 0.0
    if status == "perdu":
        loss = round(max(0.0, invested - repaid), 2)

    valuation = p.get("valuation") or 0
    unrealized_loss = 0.0
    if valuation > 0 and invested > valuation and status in ("en_cours", "retard", "en_collecte"):
        unrealized_loss = round(invested - valuation, 2)

    capital_due = round(max(0.0, invested - repaid), 2) if status in ("en_cours", "en_collecte") else 0.0

    total_received = None
    real_annual_pct = None
    if status in ("rembourse", "perdu"):
        recv_map = extra.get("total_received") or {}
        recv = recv_map.get(p.get("id"))
        if recv:
            total_received = round(float(recv), 2)
        else:
            total_received = round(repaid + interest_recv, 2)
        fin = p.get("actual_end_date") or (extra.get("last_op_date") or {}).get(p.get("id")) or p.get("expected_end_date")
        fin_d = parse_date(fin)
        if invested > 0 and start is not None and fin_d is not None:
            j = (fin_d - start).days
            if j > 0:
                ratio = total_received / invested - 1.0
                real_annual_pct = -1.0 if ratio <= -1.0 else (1.0 + ratio) ** (365.0 / j) - 1.0

    total_gain = round(interest_recv + accrued, 2)
    net = round(total_gain - loss, 2)

    return {
        **p,
        "expected_end_date": expected_real.isoformat() if expected_real else None,
        "derived_end_date": derived.isoformat() if derived else None,
        "is_late": is_late,
        "days_delayed": max(0, days_delayed),
        "accrued_interest": accrued,
        "expected_interest": expected_interest,
        "interest_ratio": interest_ratio,
        "late_severity": late_severity,
        "months_since_interest": months_since_interest,
        "unrealized_loss": unrealized_loss,
        "loss": loss,
        "capital_due": capital_due,
        "total_gain": total_gain,
        "net": net,
        "total_received": total_received,
        "real_annual_pct": real_annual_pct,
        "platform_label": CF_PLATFORMS.get(p.get("platform"), p.get("platform")),
    }


# ---------------------------------------------------------------- import (xlsx exports plateformes)

def _parse_bricks_rows(rows) -> list:
    """Rows Bricks.co : id, date, type, statut, propriété, type de contrat, montant (€), prix de la brick (€)."""
    ops = []
    for r in rows:
        if not r or not r[0]:
            continue
        source_id = str(r[0]).strip()
        op_date = _parse_fr_date(r[1])
        if not op_date:
            continue
        ops.append({
            "source_id": source_id,
            "op_date": op_date,
            "type": _norm_apos(r[2] or "").strip(),
            "status": str(r[3] or "Validée").strip(),
            "project_name": _norm_apos(r[4] or "").strip() or None,
            "contract_type": str(r[5] or "").strip(),
            "amount": _parse_fr_amount(r[6]),
            "extra": {"brick_price": r[7]},
        })
    return ops


def _parse_lpb_rows(rows) -> list:
    """Rows LPB : Nature de la transaction, Moyen de paiement, Détails, Montant, Statut, Date d'exécution."""
    ops = []
    for r in rows:
        if not r or not r[0]:
            continue
        nature = str(r[0] or "").strip()
        op_date = _parse_fr_date(r[5])
        if not op_date:
            continue
        m = re.search(r"projet\s+(.+?)\s*$", nature)
        pname = m.group(1).strip() if m else None
        ops.append({
            "source_id": None,  # fingerprint calculé plus bas (pas d'id natif)
            "op_date": op_date,
            "type": nature,
            "status": str(r[4] or "Réussi").strip(),
            "project_name": pname,
            "contract_type": str(r[1] or "").strip(),
            "amount": _parse_fr_amount(r[3]),
            "extra": {},
        })
    # fingerprint : hash(date|nature|montant|statut) pour idempotence
    for o in ops:
        fp = hashlib.sha1(
            f"{o['op_date']}|{o['type']}|{o['amount']}|{o['status']}".encode()
        ).hexdigest()
        o["source_id"] = fp
    return ops


def _detect_platform(headers) -> str | None:
    h = [str(x or "").strip().lower() for x in headers]
    if any("prix de la brick" in x for x in h) or "id" in h and "propriété" in h:
        return "bricks"
    if any("nature de la transaction" in x for x in h):
        return "lapremierebrique"
    return None


def parse_xlsx(raw: bytes) -> tuple[str, list]:
    """Décode un export xlsx plateforme → (platform, ops). Lève ValueError."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Fichier vide")
    platform = _detect_platform(rows[0])
    if platform is None:
        raise ValueError("Format de fichier non reconnu (attendu : export Bricks.co ou La Première Brique)")
    ops = _parse_bricks_rows(rows[1:]) if platform == "bricks" else _parse_lpb_rows(rows[1:])
    return platform, ops


def _find_or_create_project(conn, owner: str, platform: str, pname: str,
                            invested: float, start_date: str) -> int:
    """Retrouve un projet par nom normalisé (même plateforme/owner), sinon le crée (auto_created)."""
    key = _norm_name(pname)
    row = conn.execute(
        "SELECT id, auto_created FROM cf_projects WHERE owner=? AND platform=? AND name=?",
        (owner, platform, pname.strip()),
    ).fetchone()
    if row is None:
        # matching normalisé en dernier recours
        rows = conn.execute(
            "SELECT id, name, auto_created FROM cf_projects WHERE owner=? AND platform=?",
            (owner, platform),
        ).fetchall()
        for r in rows:
            if _norm_name(r["name"]) == key:
                row = r
                break
    if row is not None:
        pid = row["id"]
        # projet auto-créé → on met à jour mise/date si l'import apporte plus d'infos
        if row["auto_created"]:
            cur = conn.execute(
                "SELECT invested, start_date FROM cf_projects WHERE id=?", (pid,)
            ).fetchone()
            new_invested = round(cur["invested"] + invested, 2)
            new_start = start_date
            if cur["start_date"] and cur["start_date"] < start_date:
                new_start = cur["start_date"]
            conn.execute(
                "UPDATE cf_projects SET invested=?, start_date=?, updated_at=? WHERE id=?",
                (new_invested, new_start, now_iso(), pid),
            )
        return pid
    cur = conn.execute(
        """INSERT INTO cf_projects
           (owner, platform, name, city, invested, rate, duration_months, start_date,
            expected_end_date, actual_end_date, status, repaid_capital,
            interest_received, reinvested_from, notes, auto_created, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (owner, platform, pname.strip(), "", invested, 0, 0, start_date, None, None,
         "en_cours", 0, 0, None, "Importé automatiquement depuis l'export de la plateforme",
         1, now_iso(), now_iso()),
    )
    return cur.lastrowid


def import_operations(conn, owner: str, platform: str, ops: list) -> dict:
    """Insère les opérations + crée/maj les projets. Retourne un résumé.

    Les projets ne sont créés/mis à jour qu'à partir des opérations d'investissement
    RÉELLEMENT nouvelles (source_id absent de la DB) — un ré-import du même fichier
    ne doit jamais re-majorer la mise d'un projet.
    """
    summary = {"imported": 0, "duplicates": 0, "projects_created": 0,
               "projects_updated": 0, "warnings": []}

    def _is_invest(o: dict) -> bool:
        if o["status"] not in ("Validée", "Réussi"):
            return False  # opérations annulées/refusées/échouées : l'argent n'a jamais bougé
        if platform == "bricks":
            return o["type"] in BRICKS_INVEST_TYPES
        return o["type"].startswith(LPB_INVEST_PREFIX)

    # source_ids déjà présents (idempotence, par owner)
    existing = {r["source_id"] for r in conn.execute(
        "SELECT source_id FROM cf_operations WHERE owner=? AND platform=?",
        (owner, platform),
    ).fetchall()}

    # 1) investissements NOUVEAUX par projet → création/mise à jour des projets
    inv_new: dict[str, dict] = {}
    for o in ops:
        if not o["project_name"] or not _is_invest(o):
            continue
        if o["source_id"] in existing:
            continue
        d = inv_new.setdefault(o["project_name"], {"total": 0.0, "first": o["op_date"]})
        d["total"] += abs(o["amount"])
        if o["op_date"] < d["first"]:
            d["first"] = o["op_date"]

    for pname, d in inv_new.items():
        before = conn.execute(
            "SELECT COUNT(*) c FROM cf_projects WHERE owner=? AND platform=?",
            (owner, platform),
        ).fetchone()["c"]
        _find_or_create_project(conn, owner, platform, pname, d["total"], d["first"])
        after = conn.execute(
            "SELECT COUNT(*) c FROM cf_projects WHERE owner=? AND platform=?",
            (owner, platform),
        ).fetchone()["c"]
        if after > before:
            summary["projects_created"] += 1
        else:
            summary["projects_updated"] += 1

    # 2) opérations (INSERT OR IGNORE pour l'idempotence)
    for o in ops:
        pid = None
        if o["project_name"]:
            row = conn.execute(
                "SELECT id FROM cf_projects WHERE owner=? AND platform=? AND name=?",
                (owner, platform, o["project_name"].strip()),
            ).fetchone()
            if row is None:
                rows = conn.execute(
                    "SELECT id, name FROM cf_projects WHERE owner=? AND platform=?",
                    (owner, platform),
                ).fetchall()
                for r in rows:
                    if _norm_name(r["name"]) == _norm_name(o["project_name"]):
                        row = r
                        break
            pid = row["id"] if row else None
        cur = conn.execute(
            """INSERT OR IGNORE INTO cf_operations
               (owner, platform, source_id, op_date, type, status, project_id, amount,
                details, contract_type, extra, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, platform, o["source_id"], o["op_date"], o["type"], o["status"], pid,
             o["amount"], o["type"], o["contract_type"],
             json.dumps(o["extra"], ensure_ascii=False), now_iso()),
        )
        if cur.rowcount:
            summary["imported"] += 1
        else:
            summary["duplicates"] += 1

    return summary


def sync_indicators_from_ops(conn, owner: str) -> dict:
    """Recalcule interest_received / statut / invested depuis les opérations importées
    (source de vérité = exports) pour les projets auto-créés de cet owner :
    - interest_received = somme des revenus reçus (types « Revenus », hors revente)
    - statut 'rembourse' si une opération de revente totale / remboursement final
    - invested = souscriptions − annulations de souscription (LPB)"""
    stats = {"interest": 0, "status": 0}
    projects = conn.execute(
        "SELECT id, name FROM cf_projects WHERE owner=? AND auto_created=1", (owner,)
    ).fetchall()
    for p in projects:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount),0) tot FROM cf_operations
               WHERE project_id=? AND status IN ('Validée','Réussi')
                 AND type LIKE '%Revenus%' AND type NOT LIKE '%revente%' AND amount > 0""",
            (p["id"],)).fetchone()
        if row and row["tot"]:
            conn.execute("UPDATE cf_projects SET interest_received=? WHERE id=?",
                         (round(row["tot"], 2), p["id"]))
            stats["interest"] += 1
        done = conn.execute(
            """SELECT 1 FROM cf_operations WHERE project_id=?
               AND status IN ('Validée','Réussi')
               AND (type LIKE '%revente totale%' OR type LIKE '%Remboursement final%'
                    OR type LIKE '%remboursement final%')""",
            (p["id"],)).fetchone()
        if done:
            conn.execute(
                "UPDATE cf_projects SET status='rembourse' WHERE id=? AND status='en_cours'",
                (p["id"],))
            stats["status"] += 1
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN amount<0 AND type LIKE '%Souscription%'
                                  THEN -amount ELSE 0 END),0) subs,
                 COALESCE(SUM(CASE WHEN amount>0 AND type LIKE '%nnulation%'
                                  THEN amount ELSE 0 END),0) annul
               FROM cf_operations WHERE project_id=? AND status IN ('Validée','Réussi')""",
            (p["id"],)).fetchone()
        if row and (row["subs"] or row["annul"]):
            inv = round(row["subs"] - row["annul"], 2)
            conn.execute("UPDATE cf_projects SET invested=? WHERE id=?", (inv, p["id"]))
    return stats


# ---------------------------------------------------------------- extraction captures (extension)

def _extract_project_fields(obj: dict) -> dict:
    """Extrait les champs projet d'un dict quelconque (heuristique par noms de clés)."""
    out = {}
    low = {str(k).lower().strip(): v for k, v in obj.items()}
    for k in _NAME_KEYS:
        if k in low and isinstance(low[k], str) and low[k].strip():
            out["name"] = low[k].strip()
            break
    if "name" not in out and "id" in low and isinstance(low["id"], str) and low["id"].strip():
        out["name"] = low["id"].strip()  # slug utilisé en dernier recours
    for k in _RATE_KEYS:
        if k in low:
            v = _to_float(low[k])
            if v is not None:
                out["rate"] = v
                break
    for k in _DURATION_KEYS:
        if k in low:
            v = _to_float(low[k])
            if v is not None:
                out["duration_months"] = v
                break
    for k in _STATUS_KEYS:
        if k in low and isinstance(low[k], str) and low[k].strip():
            out["status"] = low[k].strip().lower()
            break
    for k in _INVESTED_KEYS:
        if k in low:
            v = _to_float(low[k])
            if v is not None:
                out["invested"] = v
                break
    if "invested" not in out:
        for k in _AMOUNT_KEYS:
            if k in low:
                v = _to_float(low[k])
                if v is not None:
                    out["invested"] = v
                    break
    for k in _DATE_KEYS:
        if k in low and isinstance(low[k], str) and low[k].strip():
            d = _parse_fr_date(low[k]) or _parse_iso_date(low[k])
            if d:
                out["expected_end_date"] = d
                break
    for k in _START_KEYS:
        if k in low and isinstance(low[k], str) and low[k].strip():
            d = _parse_fr_date(low[k]) or _parse_iso_date(low[k])
            if d:
                out["start_date"] = d
                break
    return out


def _walk_json(node, out: list, parent: dict | None = None) -> None:
    """Parcourt récursivement une structure JSON et collecte les objets 'projet-like'.

    Si un objet projet-like (ex. `property`) est imbriqué dans un dict parent qui
    porte des champs financiers (ex. `invested_amount`), on fusionne parent+enfant
    pour ne pas perdre le montant (pattern API Bricks.co).
    """
    if isinstance(node, dict):
        cand = _extract_project_fields(node)
        if parent:
            for k in ("invested", "rate", "duration_months", "expected_end_date", "start_date", "status"):
                if k not in cand and k in parent:
                    cand[k] = parent[k]
        has_name = "name" in cand
        if not has_name and parent and "name" in parent:
            cand["name"] = parent["name"]
            has_name = True
        if has_name and ("rate" in cand or "duration_months" in cand or "invested" in cand or "status" in cand):
            out.append(cand)
        for k, v in node.items():
            if isinstance(v, dict):
                _walk_json(v, out, parent={**node, **cand})
            elif isinstance(v, list):
                _walk_json(v, out, parent)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out, parent)


def _extract_from_text(text: str, projects: list, global_ctx: bool = False) -> list:
    """Cherche chaque projet connu dans un texte brut (innerText d'une page détail)
    et extrait les infos disponibles dans son voisinage : taux %, durée mois, échéance.
    Si global_ctx=True (page détail d'UN projet), une passe globale sur tout le texte
    complète : revenus cumulés bruts/nets, revenus restants, taux réel, taux cible et
    échéance via « Remboursement final <Mois> <Année> » (souvent loin du nom)."""
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text)
    hay = _norm_scrape(flat)
    out = []
    for p in projects:
        name = p["name"]
        if not name or len(name) < 3:
            continue
        needle = _norm_scrape(name)
        if not needle:
            continue
        idx = hay.find(needle)
        if idx < 0:
            continue
        # fenêtre de contexte : du nom jusqu'au prochain projet connu (les listes
        # LPB/Bricks empilent les blocs → 900 chars débordent sur le voisin).
        # Limite gauche : après la fin du bloc précédent (« dont intérêts : X € »)
        ctx_start = max(0, idx - 400)
        mi = hay.rfind("dont interets :", 0, idx)
        if mi >= 0:
            e = hay.find("€", mi)
            if e >= 0:
                ctx_start = max(ctx_start, e + 1)
        ctx_end = idx + len(needle) + 1500
        for p2 in projects:
            n2 = _norm_scrape(p2["name"])
            if not n2 or p2["name"] == name:
                continue
            j2 = hay.find(n2, idx + len(needle))
            if j2 >= 0 and j2 < ctx_end:
                ctx_end = j2
        ctx = flat[ctx_start: ctx_end]
        item = {"name": name}
        # taux : UNIQUEMENT avec label explicite (évite « 31,4 % » = imposition)
        m = re.search(
            r"(?:taux|rendement annuel|rendement brut|annuel|par an|par mois)\s*:?\s*([\d][\d.,]*)\s*%",
            ctx, re.I)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                item["rate"] = v
        # durée : UNIQUEMENT avec label « durée » ET pas « restante » (mois OU années).
        # « durée de vie du contrat » est un label explicite → fiable même si
        # « Durée restante X » traîne dans le contexte (pages projet Bricks)
        m = re.search(r"durée de vie du contrat[^0-9]{0,80}?(\d{1,3})\s*mois", ctx, re.I)
        if m:
            item["duration_months"] = int(m.group(1))
        else:
            m = re.search(
                r"(?:durée du contrat|durée totale|durée prévue|durée du projet|durée)\s*:?\s*(\d{1,3})\s*mois",
                ctx, re.I)
            if m and not re.search(r"restant|restante|il y a", ctx, re.I):
                item["duration_months"] = int(m.group(1))
            else:
                m2 = re.search(r"durée de vie du contrat[^0-9]{0,250}?(\d{1,2})\s*ans?", ctx, re.I)
                if m2:
                    item["duration_months"] = int(m2.group(1)) * 12
        # badges juste après le nom : « Royalties » et « Retard de paiement ».
        # Fenêtre = jusqu'au premier « Cumulé » (max 120) : les badges du projet
        # VOISIN arrivent après son propre « Cumulé » → pas de faux positifs.
        tail = hay[idx + len(needle): idx + len(needle) + 120]
        cut = tail.find(" cumule")
        if cut >= 0:
            tail = tail[:cut]
        if re.search(r"royalt", tail, re.I):
            item["contract_type"] = "royalty"
        if re.search(r"retard de paiement", tail, re.I):
            item["status"] = "retard"
        # échéance : mot-clé + date FR / ISO, OU « Remboursement final X € <Mois> <Année> »
        m = re.search(
            r"(?:échéance|écheance|jusqu'au|jusqu au|remboursement prévu|remboursement prevu|maturité|maturite|date de fin|date de fin|termine le|terminé le)"
            r"\s*:?\s*(\d{1,2}\s+[a-zA-Zéû]+\.?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
            ctx, re.I)
        if m:
            iso = _parse_fr_text_date(m.group(1))
            if iso:
                item["expected_end_date"] = iso
        if not m:
            # « Remboursement final X € <Mois> <Année> » = fin du contrat UNIQUEMENT
            # pour les prêts classiques. Pour un ROYALTY, c'est une revente
            # PARTIELLE d'un lot : le contrat dure 10 ans (Horizon) — ne jamais
            # prendre cette date comme échéance (faux retard type Belfort).
            m2 = re.search(r"remboursement final\s*[\d\s.,]*€?\s*([a-zA-Zéû]+\.?)\s+(\d{4})", ctx, re.I)
            if m2 and not re.search(r"royalt", ctx, re.I):
                iso = _parse_fr_text_date("1 " + m2.group(1) + " " + m2.group(2))
                if iso:
                    y, mo = int(iso[:4]), int(iso[5:7])
                    item["expected_end_date"] = f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        # taux LPB : « 13,25% / an sur 18 mois » (pourcentage AVANT le label)
        if "rate" not in item:
            m = re.search(r"([\d][\d.,]*)\s*%\s*/\s*an", ctx, re.I)
            if m:
                v = _to_float(m.group(1))
                if v is not None:
                    item["rate"] = v
        # durée LPB : « sur 18 mois » (jamais « X mois max. restants »)
        if "duration_months" not in item:
            m = re.search(r"sur\s*(\d{1,3})\s*mois", ctx, re.I)
            if m:
                item["duration_months"] = int(m.group(1))
        # Royalties : durée = « Horizon X ans » (10 ans) — la « durée de vie du
        # contrat » (6 ans…) est un horizon initial révisable, pas l'échéance
        if item.get("contract_type") == "royalty" and "duration_months" not in item:
            m = re.search(r"horizon\s*:?\s*(\d{1,2})\s*ans?", ctx, re.I)
            if m:
                item["duration_months"] = int(m.group(1)) * 12
        # durée restante LPB : « 3 mois max. restants » — l'échéance OFFICIELLE
        m = re.search(r"(\d{1,3})\s*mois\s*max\.?\s*restants?", ctx, re.I)
        if m:
            item["rest_months"] = int(m.group(1))
        # paiement in-fine LPB : les intérêts sont versés au TERME du prêt
        if re.search(r"paiement in[- ]?fine", ctx, re.I):
            item["infine"] = 1
        # start_date LPB : « Financé le 28 oct. 2024 » (début du contrat)
        if "start_date" not in item:
            m = re.search(r"financé le\s*(\d{1,2})\s*([a-zéû]+\.?)\s*(\d{4})", ctx, re.I)
            if m:
                iso = _parse_fr_text_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")
                if iso:
                    item["start_date"] = iso
        # intérêts LPB : « dont intérêts : 12,24 € » (capital hors intérêts)
        if "interest_received" not in item:
            m = re.search(r"dont intérêts\s*:?\s*([\d][\d\s.,]*)\s*€", ctx, re.I)
            if m:
                v = _to_float(m.group(1))
                if v is not None and v > 0:
                    item["interest_received"] = v
        # capital remboursé LPB : « Montant remboursé 101,24 € dont intérêts : 12,24 € »
        if "repaid_capital" not in item:
            m = re.search(
                r"montant remboursé\s*([\d][\d\s.,]*)\s*€\s*dont intérêts\s*:?\s*([\d][\d\s.,]*)\s*€",
                ctx, re.I)
            if m:
                tot = _to_float(m.group(1))
                ints = _to_float(m.group(2))
                if tot is not None and ints is not None:
                    v = round(tot - ints, 2)
                    if v > 0:
                        item["repaid_capital"] = v
        # statut LPB : « Terminé le … » → remboursé
        if re.search(r"terminé le|termine le", ctx, re.I):
            item["status"] = "Terminé"
        # montant investi : label + montant
        m = re.search(r"(?:investi|montant investi|capital investi)\s*:?\s*([\d][\d\s.,]*)\s*€", ctx, re.I)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                item["invested"] = v
        # revenus cumulés (page Bricks) → intérêt reçu
        m = re.search(r"revenus cumulés\s*:?\s*[+]?([\d][\d\s.,]*)\s*€", ctx, re.I)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                item["interest_received"] = v
        out.append(item)

    # ---- passe globale (page détail d'UN projet) : les blocs sont loin du nom ----
    if global_ctx and out:
        g = out[0]
        if "duration_months" not in g:
            m = re.search(r"durée de vie du contrat[^0-9]{0,250}?(\d{1,3})\s*mois", flat, re.I)
            if m:
                g["duration_months"] = int(m.group(1))
            else:
                m = re.search(r"durée de vie du contrat[^0-9]{0,250}?(\d{1,2})\s*ans?", flat, re.I)
                if m:
                    g["duration_months"] = int(m.group(1)) * 12
        # tags « Royalties » / « Retard de paiement » : après la DERNIÈRE occurrence
        # du nom (le détail de la page, pas le panneau latéral)
        if ("contract_type" not in g) or ("status" not in g):
            needle = _norm_scrape(g["name"])
            last = -1
            pos = 0
            while needle:
                j = hay.find(needle, pos)
                if j < 0:
                    break
                last = j
                pos = j + len(needle)
            if last >= 0:
                tail = hay[last + len(needle): last + len(needle) + 120]
                cut = tail.find(" cumule")
                if cut >= 0:
                    tail = tail[:cut]
                if "contract_type" not in g and re.search(r"royalt", tail, re.I):
                    g["contract_type"] = "royalty"
                if "status" not in g and re.search(r"retard de paiement", tail, re.I):
                    g["status"] = "retard"
        # Royalties : durée = « Horizon X ans » (10 ans), écrase la « durée de vie
        # du contrat » (horizon initial révisable — pas l'échéance)
        if g.get("contract_type") == "royalty":
            m = re.search(r"horizon\s*:?\s*(\d{1,2})\s*ans?", flat, re.I)
            if m:
                g["duration_months"] = int(m.group(1)) * 12
        # revenus cumulés brut + net (« +5,66 € / +3,97 € après fiscalité »)
        m = re.search(
            r"revenus cumulés\s*:?\s*[+]?([\d][\d\s.,]*)\s*€\s*[+]?([\d][\d\s.,]*)\s*€\s*après fiscalité",
            flat, re.I)
        if m:
            v1, v2 = _to_float(m.group(1)), _to_float(m.group(2))
            if v1 is not None:
                g["interest_received"] = v1
            if v2 is not None:
                g["interest_net"] = v2
        # revenus restants estimés (brut, puis net après fiscalité)
        m = re.search(
            r"revenus restants estimés\s*[^€]{0,120}?[+]?([\d][\d\s.,]*)\s*€(?:\s*[+]?([\d][\d\s.,]*)\s*€\s*après fiscalité)?",
            flat, re.I)
        if m:
            v1 = _to_float(m.group(1))
            if v1 is not None:
                g["interest_remaining"] = v1
            if m.group(2):
                v2 = _to_float(m.group(2))
                if v2 is not None:
                    g["interest_remaining_net"] = v2
        # NOTE : le « 31,4 % » sous « Revenus cumulés » est le TAUX D'IMPOSITION
        # (fiscalité), pas un rendement → volontairement PAS capturé comme real_rate.
        # taux cible / rentabilité annoncée
        m = re.search(
            r"(?:rentabilité cible|taux cible|rendement cible|taux annuel|taux brut)\s*:?\s*([\d][\d.,]*)\s*%",
            flat, re.I)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                g["rate"] = v
        # échéance : « Remboursement final 25,81 € Octobre 2026 » → fin de mois
        if "expected_end_date" not in g:
            m2 = re.search(r"remboursement final\s*[\d\s.,]*€?\s*([a-zA-Zéû]+\.?)\s+(\d{4})", flat, re.I)
            if m2:
                iso = _parse_fr_text_date("1 " + m2.group(1) + " " + m2.group(2))
                if iso:
                    y, mo = int(iso[:4]), int(iso[5:7])
                    g["expected_end_date"] = f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        # montant investi de la page (label « Investi »/« Montant investi »)
        if "invested" not in g:
            m = re.search(r"(?:investi|montant investi|capital investi)\s*:?\s*([\d][\d\s.,]*)\s*€", flat, re.I)
            if m:
                v = _to_float(m.group(1))
                if v is not None:
                    g["invested"] = v
        # valeur actuelle des bricks (« Valeur de mes Bricks … 18,62 € »)
        m = re.search(r"valeur de mes bricks[^0-9]{0,160}?([\d][\d\s.,]*)\s*€", flat, re.I)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                g["valuation"] = v
    return out


def _parse_lpb_cards(cards) -> list:
    """Normalise les cards d'investissement LPB capturées par l'extension (DOM)."""
    out = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        item = {"name": name}
        inv = _to_float(c.get("invested"))
        if inv is not None:
            item["invested"] = inv
        rate = _to_float(c.get("rate"))
        if rate is not None:
            item["rate"] = rate
        dur = _to_float(c.get("duration_months") or c.get("duration"))
        if dur is not None:
            item["duration_months"] = dur
        status = (c.get("status") or "").strip()
        if status:
            item["status_raw"] = status
            item["status"] = status  # gardé pour rapport
        d = c.get("date") or c.get("start_date")
        if d:
            iso = _parse_fr_text_date(d)
            if iso:
                item["start_date"] = iso
        e = c.get("end_date")
        if e:
            iso = _parse_fr_text_date(e)
            if iso:
                item["expected_end_date"] = iso
        out.append(item)
    return out


def _map_status(s: str) -> str | None:
    s = s.strip().lower()
    if "retard de paiement" in s:
        return "retard"
    for canon, aliases in _SC_STATUS_MAP.items():
        for a in aliases:
            if a in s:
                return canon
    return None


def run_ingest(conn, owner: str, captures: list) -> dict:
    """Traite les captures de l'extension pour un owner : stockage brut, extraction
    heuristique, enrichissement des projets (champs ≤0/vides uniquement, sauf
    durée restante / capital remboursé qui évoluent), solde plateforme, puis
    rapport de conformité site vs exports. Retourne {summary, conformity}."""
    if not isinstance(captures, list) or not captures:
        raise ValueError("Aucune capture reçue (captures: [])")

    # stockage des captures brutes (limite 500 par passe)
    for c in captures[-500:]:
        try:
            body = json.dumps(c.get("body"), ensure_ascii=False)
        except Exception:
            body = str(c.get("body"))[:200000]
        conn.execute(
            "INSERT INTO cf_captures (owner, ts, platform, url, status_code, body) VALUES (?,?,?,?,?,?)",
            (owner, c.get("ts") or now_iso(), (c.get("platform") or "")[:30],
             (c.get("url") or "")[:400], int(c.get("status") or 0), body),
        )

    found: dict[str, dict] = {}
    dom_captures = 0
    projects = conn.execute(
        "SELECT * FROM cf_projects WHERE owner=?", (owner,)
    ).fetchall()

    def _absorb(items):
        for it in items:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            key = _norm_scrape(name)
            if not key:
                continue
            if key not in found:
                found[key] = dict(it)
                continue
            # FUSION : l'item texte (taux, durée…) complète l'item cards (statut,
            # date) au lieu de le remplacer
            cur = found[key]
            for k, v in it.items():
                if v is None or v == "" or v == 0:
                    continue  # ne jamais écraser avec une valeur vide/nulle
                if k in ("status", "status_raw", "name") and cur.get(k):
                    continue  # le statut des cards est plus riche que le texte
                cur[k] = v

    for c in captures:
        body = c.get("body")
        if not body:
            continue
        if isinstance(body, dict) and "_dom" in body:
            dom_captures += 1
            dom = body["_dom"]
            if isinstance(dom, dict):
                if dom.get("lpb_cards"):
                    _absorb(_parse_lpb_cards(dom["lpb_cards"]))
                if dom.get("cards"):
                    _absorb(_parse_lpb_cards(dom["cards"]))
                # texte brut : ne chercher QUE les projets affichés dans CETTE page
                # (les pages Bricks listent d'autres projets dans un panneau latéral
                #  → contexte croisé interdit : un projet ne peut être mis à jour
                #    que par sa propre page)
                if dom.get("inner_text"):
                    targets = []
                    for k in ("cards", "lpb_cards"):
                        for cc in dom.get(k) or []:
                            if isinstance(cc, dict) and cc.get("name"):
                                targets.append(cc["name"])
                    if not targets and dom.get("_bricks_project_page"):
                        # page projet Bricks sans cards (DOM expo, pas de h1/h2) :
                        # le projet affiché est celui dont le nom apparaît le plus
                        flat = dom["inner_text"].replace("\u00a0", " ")
                        norm_flat = _norm_scrape(flat)
                        counts = []
                        for p in projects:
                            needle = _norm_scrape(p["name"])
                            if needle and len(needle) >= 4:
                                n = norm_flat.count(needle)
                                if n > 0:
                                    counts.append((n, p["name"]))
                        if counts:
                            counts.sort(key=lambda x: -x[0])
                            top_n = counts[0][0]
                            if top_n >= 2:
                                targets = [c2[1] for c2 in counts if c2[0] >= 2]
                    if targets:
                        tn = {_norm_scrape(t) for t in targets}
                        sub = [p for p in projects if _norm_scrape(p["name"]) in tn]
                        _absorb(_extract_from_text(dom["inner_text"], sub,
                                                   global_ctx=len(sub) == 1))
                    else:
                        _absorb(_extract_from_text(dom["inner_text"], projects))
                # solde disponible de la plateforme (« Solde 519,85 € »)
                flat_txt = re.sub(r"\s+", " ", dom.get("inner_text") or "")
                m = re.search(r"solde\s*:?\s*([\d][\d\s.,]*)\s*€", flat_txt, re.I)
                if m:
                    v = _to_float(m.group(1))
                    plat = (c.get("platform") or "").strip()
                    if v and v > 0 and plat in CF_PLATFORMS:
                        row = conn.execute(
                            "SELECT balance FROM cf_platforms WHERE owner=? AND platform=?",
                            (owner, plat)).fetchone()
                        cur_bal = row["balance"] if row else 0
                        if abs((cur_bal or 0) - v) > 0.5:
                            conn.execute(
                                "INSERT INTO cf_platforms (owner, platform, balance, updated_at)"
                                " VALUES (?,?,?,?) ON CONFLICT(owner, platform)"
                                " DO UPDATE SET balance=?, updated_at=?",
                                (owner, plat, v, now_iso(), v, now_iso()))
            continue
        items = []
        try:
            _walk_json(body, items)
        except Exception:
            continue
        _absorb(items)

    # matching + mise à jour des projets (enrichit les champs vides ; durée
    # restante et capital remboursé sont mis à jour systématiquement)
    by_norm = {_norm_scrape(p["name"]): p for p in projects}
    updated, matched, new_fields = [], 0, 0
    for key, it in found.items():
        p = by_norm.get(key)
        if p is None:
            continue
        matched += 1
        updates, uparams = [], []
        if "rate" in it and (p["rate"] is None or p["rate"] <= 0):
            updates.append("rate=?")
            uparams.append(round(it["rate"], 2))
            new_fields += 1
        if "duration_months" in it and (p["duration_months"] is None or p["duration_months"] <= 0):
            updates.append("duration_months=?")
            uparams.append(int(it["duration_months"]))
            new_fields += 1
        if "rest_months" in it and it["rest_months"] and it["rest_months"] != p["rest_months"]:
            updates.append("rest_months=?")
            uparams.append(int(it["rest_months"]))
            new_fields += 1
        if "repaid_capital" in it and it["repaid_capital"] != (p["repaid_capital"] or 0):
            updates.append("repaid_capital=?")
            uparams.append(round(it["repaid_capital"], 2))
            new_fields += 1
        if "interest_received" in it and (p["interest_received"] is None or p["interest_received"] <= 0):
            updates.append("interest_received=?")
            uparams.append(round(it["interest_received"], 2))
            new_fields += 1
        for extra_col in ("interest_net", "interest_remaining", "interest_remaining_net", "real_rate"):
            if extra_col in it and (p[extra_col] is None or p[extra_col] <= 0):
                updates.append(f"{extra_col}=?")
                uparams.append(round(it[extra_col], 2))
                new_fields += 1
        if "contract_type" in it and it["contract_type"] and not p["contract_type"]:
            updates.append("contract_type=?")
            uparams.append(it["contract_type"])
            new_fields += 1
        if "valuation" in it and it["valuation"] and (p["valuation"] is None or p["valuation"] <= 0):
            updates.append("valuation=?")
            uparams.append(round(it["valuation"], 2))
            new_fields += 1
        if "infine" in it and it["infine"] and not p["infine"]:
            updates.append("infine=1")
            new_fields += 1
        if "expected_end_date" in it and not p["expected_end_date"]:
            updates.append("expected_end_date=?")
            uparams.append(it["expected_end_date"])
            new_fields += 1
        if "start_date" in it and not p["start_date"]:
            updates.append("start_date=?")
            uparams.append(it["start_date"])
            new_fields += 1
        if "status" in it:
            mapped = _map_status(it["status"])
            if mapped and p["status"] in ("en_cours", "retard") and mapped != "en_cours" and mapped != p["status"]:
                updates.append("status=?")
                uparams.append(mapped)
        if updates:
            uparams.append(p["id"])
            conn.execute(
                f"UPDATE cf_projects SET {', '.join(updates)}, updated_at=? WHERE id=?",
                uparams[:-1] + [now_iso(), p["id"]])
            updated.append(p["name"])

    # rapport de conformité : montant site vs montant exporté (opérations)
    report = []
    for key, it in found.items():
        p = by_norm.get(key)
        if p is None:
            continue
        site_inv = it.get("invested")
        if site_inv is None:
            continue
        row = conn.execute(
            """SELECT SUM(-amount) t FROM cf_operations
               WHERE project_id=? AND owner=? AND status IN ('Validée','Réussi') AND amount < 0""",
            (p["id"], owner),
        ).fetchone()
        export_inv = row["t"] or 0.0
        diff = round(site_inv - export_inv, 2)
        report.append({
            "project": p["name"],
            "platform": p["platform"],
            "site_invested": round(site_inv, 2),
            "export_invested": round(export_inv, 2),
            "diff": diff,
            "ok": abs(diff) < 0.01,
            "site_rate": it.get("rate"),
            "site_status": it.get("status"),
        })

    summ = {
        "captures": len(captures),
        "dom_captures": dom_captures,
        "projects_detected": len(found),
        "projects_matched": matched,
        "projects_updated": len(updated),
        "fields_filled": new_fields,
        "updated_names": updated[:50],
    }
    conn.execute(
        "INSERT OR REPLACE INTO cf_reports (owner, data, created_at) VALUES (?,?,?)",
        (owner, json.dumps({"summary": summ, "conformity": report}, ensure_ascii=False), now_iso()),
    )
    return {
        "summary": summ,
        "conformity": sorted(report, key=lambda r: abs(r["diff"]), reverse=True)[:100],
    }


# ---------------------------------------------------------------- lectures & agrégats

def _wc(owners: list[str]) -> tuple[str, list]:
    """Clause owner IN (…) — mêmes conventions que app._owner_clause."""
    return "owner IN (%s)" % ",".join("?" * len(owners)), owners


def project_extras(conn, owners: list[str], platform: str | None = None,
                   status: str | None = None) -> list:
    """Projets (calculés) des owners, avec les agrégats d'opérations nécessaires
    aux indicateurs : dernier revenu, total reçu, dernière opération."""
    wc, args = _wc(owners)
    conds = [wc]
    if platform:
        conds.append("platform=?")
        args.append(platform)
    if status:
        conds.append("status=?")
        args.append(status)
    rows = conn.execute(
        f"SELECT * FROM cf_projects WHERE {' AND '.join(conds)} ORDER BY id DESC", args
    ).fetchall()
    last_int = {}
    for r in conn.execute(
        f"""SELECT o.project_id, MAX(o.op_date) d FROM cf_operations o
            WHERE o.type LIKE '%Revenus%' AND o.amount > 0
              AND o.status IN ('Validée','Réussi') AND {wc}
            GROUP BY o.project_id""", args):
        d = parse_date(r["d"])
        if d:
            last_int[r["project_id"]] = d
    recv_tot, last_op = {}, {}
    for r in conn.execute(
        f"""SELECT o.project_id,
                  SUM(CASE WHEN o.amount > 0 THEN o.amount ELSE 0 END) recv,
                  MAX(o.op_date) d FROM cf_operations o
            WHERE o.status IN ('Validée','Réussi') AND o.project_id IS NOT NULL AND {wc}
            GROUP BY o.project_id""", args):
        recv_tot[r["project_id"]] = r["recv"] or 0
        last_op[r["project_id"]] = r["d"]
    return [project_computed(r, extra={
        "last_interest": last_int.get(r["id"]),
        "total_received": recv_tot,
        "last_op_date": last_op,
    }) for r in rows]


def summary_agg(conn, owners: list[str]) -> dict:
    """Cartes de stats du module (équivalent /api/summary du tracker), multi-owners."""
    wc, args = _wc(owners)
    today = date.today()
    rows = conn.execute(f"SELECT * FROM cf_projects WHERE {wc}", args).fetchall()
    metas = {}
    for r in conn.execute(f"SELECT platform, deposited FROM cf_platforms WHERE {wc}", args):
        metas[r["platform"]] = r["deposited"] or 0

    total_invested = capital_due = interest_received = accrued = 0.0
    loss = unrealized = 0.0
    n_total = len(rows)
    n_active = n_late = n_repaid = n_lost = 0
    by_platform: dict[str, dict] = {}
    for r in rows:
        c = project_computed(r, today)
        total_invested += c["invested"]
        capital_due += c["capital_due"]
        interest_received += c["interest_received"]
        accrued += c["accrued_interest"]
        loss += c["loss"]
        unrealized += c["unrealized_loss"]
        if c["status"] == "en_cours":
            n_active += 1
        if c["is_late"]:
            n_late += 1
        if c["status"] == "rembourse":
            n_repaid += 1
        if c["status"] == "perdu":
            n_lost += 1
        pkey = c["platform"] or "autre"
        bp = by_platform.setdefault(
            pkey, {"label": c["platform_label"], "invested": 0.0, "capital_due": 0.0,
                   "gain": 0.0, "loss": 0.0, "count": 0})
        bp["invested"] += c["invested"]
        bp["capital_due"] += c["capital_due"]
        bp["gain"] += c["interest_received"] + c["accrued_interest"]
        bp["loss"] += c["loss"]
        bp["count"] += 1
    total_gain = round(interest_received + accrued, 2)
    net = round(total_gain - loss, 2)
    # « Capital recyclé » = investi au-delà des dépôts (remboursements + intérêts
    # réinvestis) — par plateforme : max(0, investi − déposé)
    reinvested = round(sum(max(0.0, bp["invested"] - metas.get(k, 0))
                           for k, bp in by_platform.items()), 2)
    return {
        "total_invested": round(total_invested, 2),
        "capital_due": round(capital_due, 2),
        "interest_received": round(interest_received, 2),
        "accrued_interest": round(accrued, 2),
        "total_gain": total_gain,
        "loss": round(loss, 2),
        "unrealized_loss": round(unrealized, 2),
        "net": net,
        "reinvested": reinvested,
        "counts": {"total": n_total, "active": n_active, "late": n_late,
                   "repaid": n_repaid, "lost": n_lost},
        "by_platform": [{"key": k, **v}
                        for k, v in sorted(by_platform.items(), key=lambda kv: -kv[1]["invested"])],
    }


def platform_invested_value(conn, owners: list[str], platform: str) -> float:
    """Valeur dans les projets : LPB = capital restant dû (auto, dérivable) ;
    Bricks = valeur des bricks (éditable, la valeur du site fait foi)."""
    wc, args = _wc(owners)
    if platform == "lapremierebrique":
        r = conn.execute(
            f"SELECT SUM(MAX(0, invested - COALESCE(repaid_capital,0))) s"
            f" FROM cf_projects WHERE platform=? AND {wc}", [platform] + args).fetchone()
        return round(r["s"] or 0, 2)
    r = conn.execute(
        f"SELECT COALESCE(SUM(invested_value),0) s FROM cf_platforms"
        f" WHERE platform=? AND {wc}", [platform] + args).fetchone()
    return round(r["s"] or 0, 2)


def overview_rows(conn, owners: list[str]) -> list[dict]:
    """Performance par plateforme : déposé / solde / dans les projets / patrimoine /
    gain / ratio / annualisé depuis le 1er achat (équivalent /api/overview)."""
    wc, args = _wc(owners)
    today = date.today()
    metas = {r["platform"]: dict(r) for r in conn.execute(
        f"SELECT * FROM cf_platforms WHERE {wc}", args)}
    first_buy = {}
    wco = wc.replace("owner IN", "o.owner IN", 1)  # requête avec JOIN → qualifier
    for r in conn.execute(
        f"""SELECT o.platform, MIN(o.op_date) d FROM cf_operations o
            JOIN cf_projects p ON p.id=o.project_id
            WHERE o.amount<0 AND (o.type LIKE '%Achat%' OR o.type LIKE '%ouscription%')
              AND o.status IN ('Validée','Réussi') AND {wco}
            GROUP BY o.platform""", args):
        first_buy[r["platform"]] = r["d"]
    out = []
    for plat in CF_PLATFORM_ORDER:
        m = metas.get(plat, {})
        if not m and plat not in first_buy:
            continue  # plateforme absente du module
        balance = round(m.get("balance") or 0, 2)
        deposited = round(m.get("deposited") or 0, 2)
        invested_value = platform_invested_value(conn, owners, plat)
        patrimoine = round(balance + invested_value, 2)
        gain = round(patrimoine - deposited, 2) if deposited else None
        ratio = round(gain / deposited, 4) if (deposited and gain is not None) else None
        annual = None
        d0 = first_buy.get(plat)
        if ratio is not None and d0:
            try:
                days = (today - date.fromisoformat(d0)).days
                if days > 0 and (1 + ratio) > 0:
                    annual = round(((1 + ratio) ** (365.0 / days) - 1) * 100, 2)
            except ValueError:
                pass
        out.append({
            "platform": plat,
            "label": CF_PLATFORMS[plat],
            "balance": balance,
            "deposited": deposited,
            "invested_value": invested_value,
            "invested_value_auto": plat == "lapremierebrique",
            "patrimoine": patrimoine,
            "gain": gain,
            "ratio": ratio,
            "annual_pct": annual,
            "start_date": d0,
            "updated_at": m.get("updated_at") or "",
        })
    # total toutes plateformes (annualisé depuis le 1er achat global)
    t_dep = round(sum(p["deposited"] for p in out), 2)
    t_pat = round(sum(p["patrimoine"] for p in out), 2)
    t_gain = round(t_pat - t_dep, 2) if t_dep else None
    t_ratio = round(t_gain / t_dep, 4) if (t_dep and t_gain is not None) else None
    t_annual = None
    d0s = [p["start_date"] for p in out if p["start_date"]]
    if t_ratio is not None and d0s:
        try:
            days = (today - date.fromisoformat(min(d0s))).days
            if days > 0 and (1 + t_ratio) > 0:
                t_annual = round(((1 + t_ratio) ** (365.0 / days) - 1) * 100, 2)
        except ValueError:
            pass
    return {"platforms": out, "total": {
        "deposited": t_dep, "patrimoine": t_pat, "gain": t_gain,
        "ratio": t_ratio, "annual_pct": t_annual}}


def reconstitute_series(conn, owner: str, platform: str) -> list[tuple[str, float]]:
    """Grille fin-de-mois du patrimoine d'UNE plateforme (cash + encours au coût),
    calibrée sur le patrimoine actuel (mêmes conventions que le tracker) :
    cash(t) = solde actuel − Σ ops après t ; encours(t) = Σ mises − Σ capital
    remboursé (ratio capital/intérêts des remboursements par projet) ; l'écart
    final (PV latentes, ratio approximé) est réparti au prorata de l'encours →
    le dernier point vaut exactement le patrimoine actuel. Retourne
    [(YYYY-MM-DD fin de mois, valeur)] du 1er événement à aujourd'hui."""
    from bisect import bisect_right
    today = date.today()
    ops = conn.execute(
        """SELECT op_date, amount, project_id, type FROM cf_operations
           WHERE owner=? AND platform=? AND status IN ('Validée','Réussi')
           ORDER BY op_date""", (owner, platform)).fetchall()
    meta = conn.execute(
        "SELECT balance, invested_value FROM cf_platforms WHERE owner=? AND platform=?",
        (owner, platform)).fetchone()
    bal = round((meta["balance"] if meta else 0) or 0, 2)
    invval = platform_invested_value(conn, [owner], platform)
    pat_now = round(bal + invval, 2)
    ratio_cap = {}
    for r in conn.execute(
        "SELECT id, repaid_capital, interest_received FROM cf_projects WHERE owner=? AND platform=?",
        (owner, platform)):
        tot = (r["repaid_capital"] or 0) + (r["interest_received"] or 0)
        ratio_cap[r["id"]] = (r["repaid_capital"] or 0) / tot if tot > 0 else 1.0

    events = []  # (date, delta_cash, delta_enc)
    for o in ops:
        try:
            d = date.fromisoformat(o["op_date"])
        except (TypeError, ValueError):
            continue
        amt = o["amount"] or 0
        delta_cash = amt  # toute op signée joue sur le solde
        delta_enc = 0.0
        if o["project_id"]:
            if amt < 0:  # mise / achat
                delta_enc = -amt
            elif o["type"] and "Revenus" in o["type"]:
                delta_enc = 0.0  # intérêts purs (Bricks)
            else:  # remboursement / revente : part capital selon le ratio du projet
                delta_enc = -amt * ratio_cap.get(o["project_id"], 1.0)
        events.append((d, delta_cash, delta_enc))
    if not events:
        return []
    events.sort(key=lambda e: e[0])
    d0 = events[0][0]
    months = []
    y, m = d0.year, d0.month
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        months.append(date(ny, nm, 1) - timedelta(days=1))
        y, m = ny, nm
    edates = [e[0] for e in events]
    tot_cash = sum(e[1] for e in events)
    cash_ts, enc_ts, pats = [], [], []
    idx = 0
    run_cash = run_enc = 0.0
    for t in months:
        i = bisect_right(edates, t)
        while idx < i:
            dc, de = events[idx][1], events[idx][2]
            run_cash += dc
            run_enc += de
            idx += 1
        cash_t = round(bal - (tot_cash - run_cash), 2)
        cash_ts.append(cash_t)
        enc_ts.append(run_enc)
        pats.append(round(cash_t + run_enc, 2))
    enc_now = max(0.0, enc_ts[-1])
    ecart = pat_now - (cash_ts[-1] + enc_ts[-1])
    out = []
    today_iso = today.isoformat()
    for i in range(len(pats)):
        # jamais de point futur : la fin du mois en cours dépasse aujourd'hui et
        # deviendrait la « dernière valorisation » (asof) du dashboard global
        if months[i].isoformat() > today_iso:
            continue
        ajust = ecart * (enc_ts[i] / enc_now) if enc_now > 0 else 0.0
        out.append((months[i].isoformat(), round(cash_ts[i] + enc_ts[i] + ajust, 2)))
    return out


# ---------------------------------------------------------------- intégration patrimoine (comptes-auto)

CF_ACC_NOTE = "Valeur calculée par le module Crowdfunding — ne pas éditer manuellement."


def is_cf_account(conn, aid: int) -> bool:
    """Un compte de classe crowdfunding géré par le module ? (dérivé, read-only)."""
    return conn.execute(
        "SELECT 1 FROM cf_platforms WHERE account_id=?", (aid,)).fetchone() is not None


def is_manual_crowdfunding(conn, row) -> bool:
    """Interdit les comptes manuels de classe crowdfunding : la classe est
    alimentée à 100 % par le module (décision Fred 2026-09-08)."""
    return (row["asset_class"] == "crowdfunding"
            and not is_cf_account(conn, row["id"]))


def refresh_integration(conn, owner: str, today: date | None = None) -> None:
    """Matérialise l'état du module dans des comptes-auto de classe crowdfunding
    (un par plateforme) : valeur actuelle + série fin-de-mois + dépôt initial
    (pour la simulation ETF et la décomposition Flux/Revenus de l'évolution).
    Idempotent — appelé après chaque écriture du module."""
    today = today or date.today()
    for plat in CF_PLATFORM_ORDER:
        meta = conn.execute(
            "SELECT * FROM cf_platforms WHERE owner=? AND platform=?", (owner, plat)).fetchone()
        nproj = conn.execute(
            "SELECT COUNT(*) c FROM cf_projects WHERE owner=? AND platform=?",
            (owner, plat)).fetchone()["c"]
        if meta is None and nproj == 0:
            continue  # plateforme absente du module
        balance = round((meta["balance"] if meta else 0) or 0, 2)
        deposited = round((meta["deposited"] if meta else 0) or 0, 2)
        invval = platform_invested_value(conn, [owner], plat)
        first_buy = conn.execute(
            """SELECT MIN(o.op_date) d FROM cf_operations o
               JOIN cf_projects p ON p.id=o.project_id
               WHERE o.owner=? AND o.platform=? AND o.amount<0
                 AND (o.type LIKE '%Achat%' OR o.type LIKE '%ouscription%')
                 AND o.status IN ('Validée','Réussi')""", (owner, plat)).fetchone()["d"]
        if first_buy is None:
            first_buy = conn.execute(
                "SELECT MIN(start_date) d FROM cf_projects WHERE owner=? AND platform=?",
                (owner, plat)).fetchone()["d"]
        # 1) compte-auto
        aid = meta["account_id"] if meta else None
        if aid is None or conn.execute(
                "SELECT id FROM accounts WHERE id=?", (aid,)).fetchone() is None:
            cur = conn.execute(
                """INSERT INTO accounts (owner, name, asset_class, institution, cost_basis,
                   open_date, notes, valuation_mode)
                   VALUES (?,?,?,?,?,?,?,'auto')""",
                (owner, CF_PLATFORMS[plat], "crowdfunding", plat, deposited,
                 first_buy, CF_ACC_NOTE))
            aid = cur.lastrowid
        else:
            conn.execute(
                "UPDATE accounts SET cost_basis=?, open_date=COALESCE(open_date,?) WHERE id=?",
                (deposited, first_buy, aid))
        conn.execute(
            "INSERT INTO cf_platforms (owner, platform, account_id, balance, deposited, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(owner, platform)"
            " DO UPDATE SET account_id=?, updated_at=?",
            (owner, plat, aid, balance, deposited, now_iso(), aid, now_iso()))
        # 2) dépôt initial unique (sim ETF + décomposition évolution) — pas de
        # contrainte UNIQUE sur transactions.source_id → garde par SELECT
        if deposited > 0 and first_buy:
            sid = f"cf:dep:{owner}:{plat}"
            if conn.execute(
                "SELECT 1 FROM transactions WHERE account_id=? AND source_id=?",
                (aid, sid)).fetchone() is None:
                conn.execute(
                    """INSERT INTO transactions
                       (account_id, op_date, kind, amount, note, source_id)
                       VALUES (?,?, 'deposit', ?, 'Dépôts cumulés sur la plateforme', ?)""",
                    (aid, first_buy, deposited, sid))
        # 3) valorisations : valeur actuelle + grille fin-de-mois (source='cf')
        conn.execute("DELETE FROM valuations WHERE account_id=? AND source='cf'", (aid,))
        now_v = round(balance + invval, 2)
        conn.execute(
            "INSERT INTO valuations (account_id, val_date, value, source, note)"
            " VALUES (?,?,?, 'cf', 'module crowdfunding')",
            (aid, today.isoformat(), now_v))
        for d_iso, v in reconstitute_series(conn, owner, plat):
            conn.execute(
                "INSERT INTO valuations (account_id, val_date, value, source, note)"
                " VALUES (?,?,?, 'cf', 'module crowdfunding')",
                (aid, d_iso, v))


def remove_platform(conn, owner: str, platform: str) -> None:
    """Supprime une plateforme du module + son compte-auto (valeurs cascadées)."""
    if platform not in CF_PLATFORMS:
        raise ValueError("Plateforme inconnue")
    row = conn.execute(
        "SELECT account_id FROM cf_platforms WHERE owner=? AND platform=?",
        (owner, platform)).fetchone()
    if row and row["account_id"]:
        conn.execute("DELETE FROM accounts WHERE id=? AND owner=?", (row["account_id"], owner))
    conn.execute("DELETE FROM cf_projects WHERE owner=? AND platform=?", (owner, platform))
    conn.execute("DELETE FROM cf_operations WHERE owner=? AND platform=?", (owner, platform))
    conn.execute("DELETE FROM cf_platforms WHERE owner=? AND platform=?", (owner, platform))


# ---------------------------------------------------------------- export / migration

def export_payload(conn, owner: str) -> dict:
    """Payload JSON complet du module pour cet owner (projets + opérations +
    plateformes + captures?). Les ids sont conservés pour une restauration fidèle."""
    return {
        "platforms": [dict(r) for r in conn.execute(
            "SELECT * FROM cf_platforms WHERE owner=?", (owner,)).fetchall()],
        "projects": [dict(r) for r in conn.execute(
            "SELECT * FROM cf_projects WHERE owner=?", (owner,)).fetchall()],
        "operations": [dict(r) for r in conn.execute(
            "SELECT * FROM cf_operations WHERE owner=?", (owner,)).fetchall()],
        "report": None,
    }


def do_cf_import(conn, owner: str, body: dict) -> str | None:
    """Remplace les données du module de cet owner par le payload (ids conservés).
    Retourne une erreur lisible ou None. Ne gère PAS la transaction : l'appelant
    (route /api/cf/import ou transfer.do_import) BEGIN/commit/rollback."""
    if "projects" not in body or "operations" not in body:
        return "Sauvegarde du module invalide"
    try:
        conn.execute("DELETE FROM cf_operations WHERE owner=?", (owner,))
        conn.execute("DELETE FROM cf_projects WHERE owner=?", (owner,))
        conn.execute("DELETE FROM cf_platforms WHERE owner=?", (owner,))
        for r in body.get("platforms") or []:
            conn.execute(
                """INSERT OR REPLACE INTO cf_platforms
                   (owner, platform, account_id, balance, deposited, invested_value, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (owner, r.get("platform", ""), r.get("account_id"), r.get("balance") or 0,
                 r.get("deposited") or 0, r.get("invested_value") or 0,
                 r.get("updated_at") or now_iso()))
        for r in body["projects"]:
            conn.execute(
                """INSERT OR REPLACE INTO cf_projects
                   (id, owner, platform, name, city, invested, rate, duration_months,
                    start_date, expected_end_date, actual_end_date, status, repaid_capital,
                    interest_received, interest_net, interest_remaining,
                    interest_remaining_net, real_rate, valuation, contract_type, infine,
                    rest_months, reinvested_from, auto_created, notes, legacy_id,
                    created_at, updated_at)
                   VALUES (:id,:owner,:platform,:name,:city,:invested,:rate,:duration_months,
                    :start_date,:expected_end_date,:actual_end_date,:status,:repaid_capital,
                    :interest_received,:interest_net,:interest_remaining,
                    :interest_remaining_net,:real_rate,:valuation,:contract_type,:infine,
                    :rest_months,:reinvested_from,:auto_created,:notes,:legacy_id,
                    :created_at,:updated_at)""",
                {**r, "owner": owner})
        for r in body["operations"]:
            conn.execute(
                """INSERT OR REPLACE INTO cf_operations
                   (id, owner, platform, source_id, op_date, type, status, project_id,
                    amount, details, contract_type, extra, created_at)
                   VALUES (:id,:owner,:platform,:source_id,:op_date,:type,:status,:project_id,
                    :amount,:details,:contract_type,:extra,:created_at)""",
                {**r, "owner": owner})
        conn.execute("DELETE FROM cf_reports WHERE owner=?", (owner,))
    except Exception as e:
        return f"Import du module impossible : {e}"
    return None


def import_ct_backup(conn, owner: str, ct_db_path: str) -> dict:
    """Migration one-shot depuis une base Crowdfunding Tracker (fichier sqlite
    local) : projets (legacy_id conservé), opérations (ids projet re-mappés),
    métadonnées plateformes. Retourne un décompte. Appeler ensuite
    refresh_integration(conn, owner)."""
    import os
    if not os.path.isfile(ct_db_path):
        raise FileNotFoundError(f"Base Crowdfunding Tracker introuvable : {ct_db_path}")
    src = sqlite3.connect(f"file:{ct_db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        # plateformes (owner scope ; invest_value Bricks conservé, LPB = auto → 0)
        n_meta = 0
        for r in src.execute("SELECT * FROM platform_meta"):
            plat = r["platform"]
            if plat not in CF_PLATFORMS:
                continue
            iv = r["invested_value"] or 0 if plat == "bricks" else 0
            conn.execute(
                """INSERT INTO cf_platforms (owner, platform, balance, deposited,
                   invested_value, updated_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(owner, platform) DO UPDATE SET balance=?, deposited=?,
                   invested_value=?, updated_at=?""",
                (owner, plat, r["balance"] or 0, r["deposited"] or 0, iv,
                 r["updated_at"] or now_iso(), r["balance"] or 0, r["deposited"] or 0,
                 iv, r["updated_at"] or now_iso()))
            n_meta += 1
        # projets : créer d'abord tous les projets avec leur legacy_id
        id_map: dict[int, int] = {}
        n_proj = 0
        for r in src.execute("SELECT * FROM projects ORDER BY id"):
            plat = r["platform"]
            if plat not in CF_PLATFORMS:
                continue
            cur = conn.execute(
                """INSERT INTO cf_projects
                   (owner, platform, name, city, invested, rate, duration_months,
                    start_date, expected_end_date, actual_end_date, status, repaid_capital,
                    interest_received, interest_net, interest_remaining,
                    interest_remaining_net, real_rate, valuation, contract_type, infine,
                    rest_months, reinvested_from, auto_created, notes, legacy_id,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, plat, r["name"], r["city"] or "", r["invested"] or 0,
                 r["rate"] or 0, r["duration_months"] or 0, r["start_date"],
                 r["expected_end_date"], r["actual_end_date"], r["status"] or "en_cours",
                 r["repaid_capital"] or 0, r["interest_received"] or 0,
                 r["interest_net"] or 0, r["interest_remaining"] or 0,
                 r["interest_remaining_net"] or 0, r["real_rate"] or 0,
                 r["valuation"] or 0, r["contract_type"] or "", r["infine"] or 0,
                 r["rest_months"] or 0, r["reinvested_from"],
                 r["auto_created"] or 0, r["notes"] or "", r["id"],
                 r["created_at"] or now_iso(), r["updated_at"] or now_iso()))
            id_map[r["id"]] = cur.lastrowid
            n_proj += 1
        # liens de réinvestissement (après création de tous les projets)
        for r in src.execute("SELECT id, reinvested_from FROM projects WHERE reinvested_from IS NOT NULL"):
            if r["id"] in id_map and r["reinvested_from"] in id_map:
                conn.execute("UPDATE cf_projects SET reinvested_from=? WHERE id=?",
                             (id_map[r["reinvested_from"]], id_map[r["id"]]))
        # opérations
        n_ops = 0
        for r in src.execute("SELECT * FROM operations ORDER BY id"):
            plat = r["platform"]
            if plat not in CF_PLATFORMS:
                continue
            pid = id_map.get(r["project_id"]) if r["project_id"] else None
            conn.execute(
                """INSERT OR IGNORE INTO cf_operations
                   (owner, platform, source_id, op_date, type, status, project_id, amount,
                    details, contract_type, extra, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, plat, r["source_id"], r["op_date"], r["type"],
                 r["status"] or "Validée", pid, r["amount"] or 0, r["details"] or "",
                 r["contract_type"] or "", r["extra"] or "", r["created_at"] or now_iso()))
            n_ops += 1
        # statut rembourse du sync des ops (source de vérité) — porté tel quel :
        # les statuts LPB déjà alignés (7 remboursés/14 retards) sont conservés
        # par la migration (données), le sync n'écrase pas un statut non en_cours.
    finally:
        src.close()
    return {"platforms": n_meta, "projects": n_proj, "operations": n_ops}


# ---------------------------------------------------------------- seed démo

def seed_demo(conn, owner: str) -> None:
    """Données de DÉMO du module (SEED_DEMO=1 uniquement) : 2 plateformes avec
    projets fictifs + opérations → comptes-auto via refresh_integration."""
    if conn.execute("SELECT COUNT(*) c FROM cf_projects").fetchone()["c"] > 0:
        return
    metas = [
        # (plateforme, solde dispo, déposé cumulé, valeur dans les projets Bricks)
        ("bricks", 239.75, 1400.0, 1180.0),
        ("lapremierebrique", 220.0, 600.0, 0.0),
    ]
    for plat, bal, dep, iv in metas:
        conn.execute(
            "INSERT OR IGNORE INTO cf_platforms (owner, platform, balance, deposited, invested_value)"
            " VALUES (?,?,?,?,?)", (owner, plat, bal, dep, iv))
    demo_projects = [
        # (platform, nom, ville, mise, taux, durée, start, statut, royalties?, notes)
        ("bricks", "Résidence Les Tilleuls", "Nantes", 500, 9.5, 24, "2024-06-15", "en_cours", 0, 0, ""),
        ("bricks", "Immeuble Pasteur", "Lille", 300, 10.0, 30, "2024-02-01", "en_cours", 1, 0, "contrat Royalties — capital à la revente"),
        ("bricks", "Le Clos Saint-Jean", "Avignon", 200, 8.0, 36, "2023-11-20", "rembourse", 0, 200, "remboursé avec 24 mois d'avance"),
        ("bricks", "Résidence Beau Rivage", "Annecy", 400, 9.0, 24, "2023-05-10", "retard", 0, 0, "impayés locataires (badge plateforme)"),
        ("lapremierebrique", "L'Atelier des Arts", "Lyon", 250, 11.5, 18, "2024-09-01", "en_cours", 0, 0, ""),
        ("lapremierebrique", "Le Grenier Céleste", "Strasbourg", 150, 10.0, 24, "2022-08-01", "retard", 0, 0, "en retard de paiement"),
        ("lapremierebrique", "La Halle aux Grains", "Toulouse", 200, 9.0, 12, "2022-03-01", "rembourse", 0, 200, "projet terminé"),
    ]
    created: dict[str, int] = {}
    for plat, name, city, mise, rate, dur, start, status, royalty, repaid, notes in demo_projects:
        cur = conn.execute(
            """INSERT INTO cf_projects (owner, platform, name, city, invested, rate,
               duration_months, start_date, status, repaid_capital, contract_type,
               notes, auto_created, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (owner, plat, name, city, mise, rate, dur, start, status, repaid,
             "royalty" if royalty else "", notes, now_iso(), now_iso()))
        created[f"{plat}|{name}"] = cur.lastrowid
    # quelques opérations pour la reconstitution historique et la conformité
    demo_ops = [
        ("bricks", "Résidence Les Tilleuls", "2024-06-15", "Achat de bricks", -500, "Validée"),
        ("bricks", "Résidence Les Tilleuls", "2024-07-01", "Revenus reversés", 4.75, "Validée"),
        ("bricks", "Immeuble Pasteur", "2024-02-01", "Achat de bricks", -300, "Validée"),
        ("bricks", "Le Clos Saint-Jean", "2023-11-20", "Achat de bricks", -200, "Validée"),
        ("bricks", "Le Clos Saint-Jean", "2025-11-30", "Revente des bricks", 232.0, "Validée"),
        ("bricks", "Résidence Beau Rivage", "2023-05-10", "Achat de bricks", -400, "Validée"),
        ("bricks", "Résidence Beau Rivage", "2023-06-01", "Revenus reversés", 3.0, "Validée"),
        ("lapremierebrique", "L'Atelier des Arts", "2024-09-01", "Souscription au projet L'Atelier des Arts", -250, "Réussi"),
        ("lapremierebrique", "Le Grenier Céleste", "2022-08-01", "Souscription au projet Le Grenier Céleste", -150, "Réussi"),
        ("lapremierebrique", "La Halle aux Grains", "2022-03-01", "Souscription au projet La Halle aux Grains", -200, "Réussi"),
        ("lapremierebrique", "La Halle aux Grains", "2023-03-01", "Remboursement final du projet La Halle aux Grains", 218.2, "Réussi"),
    ]
    for plat, pname, d, typ, amt, status in demo_ops:
        pid = created.get(f"{plat}|{pname}")
        src = hashlib.sha1(f"demo|{plat}|{d}|{typ}|{amt}".encode()).hexdigest()
        conn.execute(
            """INSERT OR IGNORE INTO cf_operations (owner, platform, source_id, op_date,
               type, status, project_id, amount, details, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (owner, plat, src, d, typ, status, pid, amt, typ, now_iso()))
    refresh_integration(conn, owner)
