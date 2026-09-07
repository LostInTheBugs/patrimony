"""Taux de change EUR (extrait de src/app.py, v2026.09.038).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI — les connexions
sont passées en paramètre, les fetchs réseau sont des fonctions SYNCHRONES
bloquantes que l'appelant exécute dans son threadpool (run_in_threadpool) ;
l'appelant garde HTTP/audit/statuts. Le User-Agent des fetchs est injecté
(paramètre `ua`) pour rester cohérent avec le reste de l'app.

Convention (v2026.09.020) : taux BCE « 1 EUR = X devises », EUR = valeur /
rate. Priorité : override manuel de l'actif > taux BCE <= date > taux BCE le
plus ancien. Fin de mois uniquement pour l'historique (backfill).
"""

import urllib.request
from datetime import date
from xml.etree import ElementTree as ET

SUPPORTED = ["EUR", "USD", "CHF", "GBP", "JPY", "CAD", "AUD"]

FX_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
# Historique complet BCE (depuis 1999) : backfill des fins de mois pour les
# conversions des historiques anciens (le fichier fait ~8 Mo, une seule passe)
FX_ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"

_ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"


def lookup(conn, ccy: str, d: str | None, override: float | None = None) -> dict | None:
    """Taux EUR pour `ccy` le jour `d` (ou taux le plus proche dispo) :
    rate = unités de `ccy` pour 1 EUR → EUR = valeur / rate.
    Priorité : override manuel de l'actif > taux BCE <= d > taux BCE le plus
    ancien. Retourne {rate, date, source} ou None (ccy EUR ⇒ rate 1)."""
    if ccy in (None, "", "EUR"):
        return {"rate": 1.0, "date": None, "source": "fixed"}
    if override:
        return {"rate": float(override), "date": None, "source": "manual"}
    row = None
    if d:
        row = conn.execute(
            "SELECT rate, rate_date, source FROM fx_rates WHERE ccy=? AND rate_date<=?"
            " ORDER BY rate_date DESC LIMIT 1", (ccy, d)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT rate, rate_date, source FROM fx_rates WHERE ccy=?"
            " ORDER BY rate_date ASC LIMIT 1", (ccy,)
        ).fetchone()
    if row is None:
        return None
    return {"rate": row["rate"], "date": row["rate_date"], "source": row["source"]}


def warn(rate: dict | None, d: str | None) -> bool:
    """⚠️ taux BCE âgé de plus de 7 jours (saisie manuelle honnête)."""
    if not rate or rate["source"] == "manual" or rate["date"] is None:
        return False
    try:
        return (date.fromisoformat(d or rate["date"]) - date.fromisoformat(rate["date"])).days > 7
    except ValueError:
        return False


def parse_daily(xml_text: str) -> list[tuple[str, str, float]]:
    """(ccy, YYYY-MM-DD, rate) depuis le XML BCE (eurofxref-daily)."""
    out = []
    root = ET.fromstring(xml_text)
    day = None
    for cube in root.iter(_ECB_NS + "Cube"):
        if "time" in cube.attrib:
            day = cube.attrib["time"]
        elif "currency" in cube.attrib and day:
            try:
                out.append((cube.attrib["currency"], day, float(cube.attrib["rate"])))
            except (ValueError, KeyError):
                continue
    return out


def parse_hist(xml_text: str) -> list[tuple[str, str, float]]:
    """Fins de mois (dernier jour BCE dispo du mois) sur l'historique complet :
    (ccy, YYYY-MM-DD, rate) — un seul taux par devise et par mois."""
    root = ET.fromstring(xml_text)
    last: dict[tuple[str, str], tuple[str, float]] = {}  # (ccy, ym) -> (day, rate)
    day = None
    for cube in root.iter(_ECB_NS + "Cube"):
        if "time" in cube.attrib:
            day = cube.attrib["time"]
        elif "currency" in cube.attrib and day:
            ccy = cube.attrib["currency"]
            try:
                rate = float(cube.attrib["rate"])
            except (ValueError, KeyError):
                continue
            ym = day[:7]
            prev = last.get((ccy, ym))
            if prev is None or day > prev[0]:
                last[(ccy, ym)] = (day, rate)
    return [(ccy, d, r) for (ccy, _ym), (d, r) in last.items()]


def fetch_daily(ua: dict) -> list[tuple[str, str, float]]:
    """Rates du jour BCE — BLOQUANT, à exécuter dans le threadpool."""
    req = urllib.request.Request(FX_ECB_URL, headers={**ua, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return parse_daily(r.read().decode("utf-8"))


def fetch_hist(ua: dict) -> list[tuple[str, str, float]]:
    """Fins de mois BCE (historique depuis 1999) — BLOQUANT, threadpool."""
    req = urllib.request.Request(FX_ECB_HIST_URL, headers={**ua, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return parse_hist(r.read().decode("utf-8"))


def store_daily(conn, rates: list[tuple[str, str, float]]) -> tuple[int, str]:
    """Rate du jour BCE → fx_rates (INSERT OR REPLACE). Retourne (nb ccy, date du jour)."""
    today = date.today().isoformat()
    for ccy, day, rate in rates:
        if ccy in SUPPORTED and day <= today:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source) VALUES (?,?,?, 'ecb')",
                (ccy, day, rate),
            )
    conn.commit()
    return len([r for r in rates if r[0] in SUPPORTED]), today


def store_hist(conn, rates: list[tuple[str, str, float]]) -> int:
    """Backfill idempotent des fins de mois BCE (INSERT OR REPLACE — aucune
    donnée existante touchée). Retourne le nombre de mois couverts."""
    for ccy, day, rate in rates:
        if ccy in SUPPORTED:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (ccy, rate_date, rate, source) VALUES (?,?,?, 'ecb')",
                (ccy, day, rate),
            )
    conn.commit()
    return len({day[:7] for ccy, day, _ in rates if ccy in SUPPORTED})
