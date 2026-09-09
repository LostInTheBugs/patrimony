"""Exports/imports de données (extrait de src/app.py, v2026.09.037).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI — la connexion
(base principale OU base mémoire d'un coffre protégé, via le routage de
l'appelant) est TOUJOURS passée en paramètre `conn` ; l'appelant garde la
responsabilité HTTP : gardes d'authentification, audit, statuts/réponses.
Les messages d'erreur sont émis en FR (source de vérité lisible) — le
middleware HTTP de src/app.py les traduit selon Accept-Language.

Contenu : payload JSON complet d'un propriétaire (export_data) + restauration
transactionnelle (do_import, fichiers anciens acceptés), import CSV
d'opérations bancaires (sniffing séparateur, formats FR, doublons), exports
CSV par type (contenu localisé : en-têtes via src/l10n, valeurs cls/type via
les tables internes — les IDENTIFIANTS restent canoniques et ré-importables).
"""

import csv
import io
from datetime import datetime, timezone

from src import l10n


class TransferError(Exception):
    """Échec d'un import/export. Le message (FR, traduit par le middleware
    HTTP) est la réponse JSON `detail` ; `status` porte le code HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- JSON (sauvegarde)

def export_data(conn, username: str, app_version: str) -> dict:
    """Payload JSON complet d'un propriétaire : actifs, valorisations,
    opérations ET règles de revenu (une restauration ne doit rien perdre)."""
    return {
        "app": "patrimony",
        "version": app_version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "owner": username,
        "accounts": [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE owner=?", (username,)).fetchall()],
        "valuations": [dict(r) for r in conn.execute(
            "SELECT v.* FROM valuations v JOIN accounts a ON a.id=v.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "transactions": [dict(r) for r in conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id=t.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "income_rules": [dict(r) for r in conn.execute(
            "SELECT ir.* FROM income_rules ir JOIN accounts a ON a.id=ir.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "positions": [dict(r) for r in conn.execute(
            "SELECT p.* FROM positions p JOIN accounts a ON a.id=p.account_id"
            " WHERE a.owner=?", (username,)).fetchall()],
        "dividend_events": [dict(r) for r in conn.execute(
            "SELECT d.* FROM dividend_events d JOIN positions p ON p.id=d.position_id"
            " JOIN accounts a ON a.id=p.account_id WHERE a.owner=?", (username,)).fetchall()],
        "settings": [dict(r) for r in conn.execute(
            "SELECT key, value FROM settings WHERE member=?", (username,)).fetchall()],
        "crowdfunding": _cf_payload(conn, username),
        "crypto": _cw_payload(conn, username),
        "loans": [dict(r) for r in conn.execute(
            "SELECT * FROM loans WHERE owner=?", (username,)).fetchall()],
    }


def _cf_payload(conn, username: str) -> dict:
    """Données du module Crowdfunding (projets/opérations/plateformes) — import
    différé pour éviter tout cycle d'import au chargement."""
    from src import crowdfund
    return crowdfund.export_payload(conn, username)


def _cw_payload(conn, username: str) -> dict:
    """Données du module Crypto wallets (wallets/transferts/séries/scans)."""
    from src import crypto
    return crypto.export_payload(conn, username)


def do_import(conn, username: str, body: dict) -> str | None:
    """Remplace les données du propriétaire par le payload. Retourne une
    erreur lisible ou None. Transactions + règles incluses (v2026.09.019) ;
    les fichiers anciens (actifs+valorisations seuls) restent acceptés."""
    if body.get("app") != "patrimony" or "accounts" not in body or "valuations" not in body:
        return "Fichier non reconnu"
    try:
        conn.execute("BEGIN")
        # module Crédits (v2026.09.056) : les crédits sont restaurés avec des
        # ids explicites (account_id lié conservé) — suppression AVANT les
        # comptes (le lien FK serait sinon nullifié par le cascade)
        conn.execute("DELETE FROM loans WHERE owner=?", (username,))
        conn.execute("DELETE FROM accounts WHERE owner=?", (username,))  # cascade enfants
        for a in body["accounts"]:
            conn.execute(
                "INSERT INTO accounts (id, owner, name, asset_class, institution, currency, valuation_mode,"
                " cost_basis, fees_pct, wrapper, tax_country, loan_principal, loan_rate, loan_monthly, open_date, close_date, notes, active, created_at, updated_at)"
                " VALUES (:id,:owner,:name,:asset_class,:institution,:currency,:valuation_mode,:cost_basis,"
                " :fees_pct,:wrapper,:tax_country,:loan_principal,:loan_rate,:loan_monthly,:open_date,:close_date,:notes,:active,:created_at,:updated_at)",
                {**a, "owner": username, "fees_pct": a.get("fees_pct"), "wrapper": a.get("wrapper"),
                 "tax_country": a.get("tax_country") or "",
                 "loan_principal": a.get("loan_principal", 0), "loan_rate": a.get("loan_rate", 0),
                 "loan_monthly": a.get("loan_monthly", 0)},
            )
        for v in body["valuations"]:
            conn.execute(
                "INSERT INTO valuations (id, account_id, val_date, value, source, note)"
                " VALUES (:id,:account_id,:val_date,:value,:source,:note)",
                v,
            )
        for t in body.get("transactions") or []:
            conn.execute(
                "INSERT INTO transactions (id, account_id, op_date, kind, amount, note, source_id, created_at)"
                " VALUES (:id,:account_id,:op_date,:kind,:amount,:note,:source_id,:created_at)",
                t,
            )
        for ir in body.get("income_rules") or []:
            conn.execute(
                "INSERT INTO income_rules (id, account_id, label, amount, freq, months_int, next_date, active, kind)"
                " VALUES (:id,:account_id,:label,:amount,:freq,:months_int,:next_date,:active,:kind)",
                {**ir, "kind": ir.get("kind") or "income"},
            )
        for p in body.get("positions") or []:
            conn.execute(
                "INSERT INTO positions (id, account_id, symbol, label, quantity, pru, active, created_at, updated_at)"
                " VALUES (:id,:account_id,:symbol,:label,:quantity,:pru,:active,:created_at,:updated_at)",
                p,
            )
        for d in body.get("dividend_events") or []:
            conn.execute(
                "INSERT INTO dividend_events (id, position_id, ex_date, per_share, note, created_at)"
                " VALUES (:id,:position_id,:ex_date,:per_share,:note,:created_at)",
                d,
            )
        conn.execute("DELETE FROM settings WHERE member=?", (username,))
        for s in body.get("settings") or []:
            conn.execute(
                "INSERT INTO settings (member, key, value) VALUES (?,?,?)",
                (username, s["key"], s["value"]),
            )
        # module Crowdfunding (v2026.09.046) : les sauvegardes récentes portent
        # les données du module ; les anciennes n'en ont pas (rien à restaurer)
        if "crowdfunding" in body and body["crowdfunding"]:
            from src import crowdfund
            err = crowdfund.do_cf_import(conn, username, body["crowdfunding"])
            if err:
                conn.rollback()
                return err
            crowdfund.refresh_integration(conn, username)
        # module Crypto wallets (v2026.09.050)
        if "crypto" in body and body["crypto"]:
            from src import crypto
            err = crypto.do_cw_import(conn, username, body["crypto"])
            if err:
                conn.rollback()
                return err
            crypto.refresh_integration(conn, username)
        # module Crédits (v2026.09.056) : lignes restaurées APRÈS les comptes
        # (FK account_id) — les sauvegardes anciennes n'ont pas la section
        for ln in body.get("loans") or []:
            conn.execute(
                "INSERT INTO loans (id, owner, name, loan_type, lender, currency,"
                " principal_initial, principal_remaining, rate_annual,"
                " monthly_payment, insurance_monthly, start_date, account_id,"
                " notes, active, created_at, updated_at)"
                " VALUES (:id,:owner,:name,:loan_type,:lender,:currency,"
                " :principal_initial,:principal_remaining,:rate_annual,"
                " :monthly_payment,:insurance_monthly,:start_date,:account_id,"
                " :notes,:active,:created_at,:updated_at)",
                {**ln, "owner": username},
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return f"Import impossible : {e}"
    return None


# ---------------------------------------------------------------- import CSV d'opérations

# Libellés humains des exports CSV : la langue suit Accept-Language
# (défaut FR) — les IDENTIFIANTS restent canoniques.
_CSV_L10N: dict[str, dict[str, dict[str, str]]] = {
    "cls": {
        "fr": {"comptes": "Comptes courants", "epargne": "Livrets & épargne", "bourse": "Bourse & assurances-vie",
               "immobilier": "Immobilier", "crowdfunding": "Crowdfunding", "crypto": "Cryptomonnaies",
               "metaux": "Métaux précieux", "divers": "Divers"},
        "en": {"comptes": "Current accounts", "epargne": "Savings accounts", "bourse": "Stocks & life insurance",
               "immobilier": "Real estate", "crowdfunding": "Crowdfunding", "crypto": "Cryptocurrencies",
               "metaux": "Precious metals", "divers": "Other"},
        "de": {"comptes": "Girokonten", "epargne": "Sparkonten", "bourse": "Aktien & Lebensversicherung",
               "immobilier": "Immobilien", "crowdfunding": "Crowdfunding", "crypto": "Kryptowährungen",
               "metaux": "Edelmetalle", "divers": "Sonstiges"},
        "lu": {"comptes": "Lafend Konten", "epargne": "Spuerkonten", "bourse": "Aktien & Liewensversécherung",
               "immobilier": "Immobilien", "crowdfunding": "Crowdfunding", "crypto": "Kryptowährungen",
               "metaux": "Edelmetaller", "divers": "Divis"},
    },
    "kind": {
        "fr": {"deposit": "Dépôt", "withdrawal": "Retrait", "income": "Revenu", "expense": "Frais / dépense"},
        "en": {"deposit": "Deposit", "withdrawal": "Withdrawal", "income": "Income", "expense": "Fee / expense"},
        "de": {"deposit": "Einzahlung", "withdrawal": "Auszahlung", "income": "Einkommen", "expense": "Gebühr / Ausgabe"},
        "lu": {"deposit": "Abezuelung", "withdrawal": "Auszuelung", "income": "Akommes", "expense": "Fraisen / Ausgab"},
    },
}
_CSV_LANG_ORDER = ("fr", "en", "de", "lu")

TX_KINDS = {"deposit", "withdrawal", "income", "expense"}
_TX_SIGN_FLIP = {
    "deposit": "withdrawal", "withdrawal": "deposit",
    "income": "expense", "expense": "income",
}


def csv_lang(accept_language: str) -> str:
    hdr = accept_language or ""
    for part in hdr.split(","):
        tag = part.strip().split(";")[0].lower()
        base = tag.split("-")[0]
        if base in _CSV_LANG_ORDER:
            return base
    return "fr"


def _l10n_map(kind: str, lang: str) -> dict[str, str]:
    return _CSV_L10N[kind].get(lang) or _CSV_L10N[kind]["fr"]


def csv_num(s):
    """Montant CSV -> float ou None. Gère '1 234,56', '1.234,56', débit/crédit '(', '€'."""
    if s is None:
        return None
    s = s.replace("\u00a0", " ").replace(" ", "").replace("€", "").replace("EUR", "").strip()
    if not s:
        return None
    neg = s.startswith("-") or s.startswith("(")
    s = s.lstrip("-(+").rstrip(")")
    if "," in s and "." in s:
        s = s.replace(".", "") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def csv_date(s):
    """Date CSV -> 'YYYY-MM-DD' ou None. Accepte JJ/MM/AAAA, JJ.MM.AAAA, AAAA-MM-JJ…"""
    if not s:
        return None
    s = s.strip().strip('"').split(" ")[0].split("T")[0]
    for pat in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            if s == datetime.strptime(s, pat).strftime(pat):
                return datetime.strptime(s, pat).date().isoformat()
        except ValueError:
            continue
    return None


def import_tx_csv(conn, account_id: int, default_kind: str, csv_text: str) -> dict:
    """Importe un CSV bancaire (opérations) dans un actif appartenant à
    l'utilisateur. Colonnes d'en-tête : date + libellé + montant (ou
    débit/crédit). Montant négatif = type inversé (dépôt↔retrait,
    revenu↔dépense). Doublons ignorés. Retourne {inserted, skipped,
    invalid, errors} ; lève TransferError pour les fichiers illisibles."""
    if default_kind not in TX_KINDS:
        raise TransferError("Type inconnu")
    if not csv_text or len(csv_text) > 2_000_000:
        raise TransferError("Fichier vide ou trop volumineux (2 Mo max)")
    text = csv_text.lstrip("\ufeff")
    first = text.splitlines()[0] if text else ""
    delims = [d for d in (",", ";", "\t") if d in first]
    delim = max(delims, key=first.count) if delims else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error as e:
        raise TransferError(f"CSV illisible : {e}") from None
    if len(rows) < 2:
        raise TransferError("Fichier vide (en-tête + au moins une ligne)")
    hdr = [c.strip().lower() for c in rows[0]]

    def find_col(*names):
        for i, h in enumerate(hdr):
            if h in names:
                return i
        return None

    i_date = find_col("date", "op_date", "value_date", "datetime", "date_operation")
    i_note = find_col("note", "libelle", "label", "description", "memo", "libellé", "nom")
    i_amt = find_col("montant", "amount", "total", "montant_euro", "valeur")
    i_db = find_col("debit", "débit")
    i_cr = find_col("credit", "crédit")
    if i_date is None or (i_amt is None and i_db is None and i_cr is None):
        raise TransferError(
            "En-tête incompréhensible — colonnes attendues : date, libellé,"
            " montant (ou débit/crédit). Séparateur virgule, point-virgule ou tabulation."
        )
    existing = {
        (d, round(a, 2), (n or "").strip().lower())
        for d, a, n in conn.execute(
            "SELECT op_date, amount, note FROM transactions WHERE account_id=?", (account_id,)
        )
    }
    cur = conn.cursor()
    inserted = skipped = invalid = 0
    errors, seen = [], set()
    for ln, r in enumerate(rows[1:], start=2):
        if not r or not any(c.strip() for c in r):
            continue
        d = csv_date(r[i_date]) if i_date < len(r) else None
        amt = csv_num(r[i_amt]) if i_amt is not None and i_amt < len(r) else None
        if amt is None and (i_db is not None or i_cr is not None):
            dbv = csv_num(r[i_db]) if i_db is not None and i_db < len(r) else None
            crv = csv_num(r[i_cr]) if i_cr is not None and i_cr < len(r) else None
            amt = (crv or 0) - (dbv or 0) if (dbv or crv) else None
        note = (r[i_note] or "").strip()[:200] if i_note is not None and i_note < len(r) else ""
        if d is None or amt is None:
            invalid += 1
            if len(errors) < 5:
                errors.append(f"ligne {ln} : date ou montant invalide")
            continue
        kind = default_kind if amt >= 0 else _TX_SIGN_FLIP[default_kind]
        a = round(abs(amt), 2)  # montants stockés positifs (le type porte le sens, cf. add_transaction)
        if a == 0:
            invalid += 1
            if len(errors) < 5:
                errors.append(f"ligne {ln} : montant nul")
            continue
        key = (d, a, note.lower())
        if key in seen or key in existing:
            skipped += 1
            continue
        cur.execute(
            "INSERT INTO transactions (account_id, op_date, kind, amount, note) VALUES (?,?,?,?,?)",
            (account_id, d, kind, a, note),
        )
        inserted += 1
        seen.add(key)
    if inserted:
        conn.commit()
    if invalid and inserted == 0 and skipped == 0:
        raise TransferError("Aucune ligne importée — " + "; ".join(errors))
    return {"inserted": inserted, "skipped": skipped, "invalid": invalid, "errors": errors[:5]}


# ---------------------------------------------------------------- exports CSV

CSV_KINDS = {
    "accounts": ("actifs", "SELECT * FROM accounts WHERE owner=?"),
    "transactions": (
        "operations",
        "SELECT t.id, a.name AS compte, t.op_date AS date, t.kind AS type, t.amount AS montant,"
        " t.note AS note FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.owner=?",
    ),
    "valuations": (
        "valorisations",
        "SELECT v.id, a.name AS compte, v.val_date AS date, v.value AS valeur, v.source AS source"
        " FROM valuations v JOIN accounts a ON a.id=v.account_id WHERE a.owner=?",
    ),
    "rules": (
        "regles-revenus",
        "SELECT r.id, a.name AS compte, r.label AS libelle, r.amount AS montant, r.freq AS frequence,"
        " r.next_date AS prochaine_date, r.active AS active FROM income_rules r"
        " JOIN accounts a ON a.id=r.account_id WHERE a.owner=?",
    ),
}


def csv_content(rows, accept_language: str) -> str:
    """Contenu CSV UTF-8 (BOM pour Excel) : en-têtes localisés (src/l10n) et
    VALEURS cls/type localisées — identifiants canoniques, ré-importables."""
    buf = io.StringIO()
    buf.write("\ufeff")
    if rows:
        cols = list(rows[0].keys())
        lang = csv_lang(accept_language)
        cls_map = _l10n_map("cls", lang)
        kind_map = _l10n_map("kind", lang)
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow([l10n.csv_header(col, accept_language) for col in cols])
        for r in rows:
            row = dict(r)
            if "asset_class" in row:
                row["asset_class"] = cls_map.get(row["asset_class"], row["asset_class"])
            if "type" in row:
                row["type"] = kind_map.get(row["type"], row["type"])
            w.writerow([row[k] for k in cols])
    return buf.getvalue()
