#!/usr/bin/env python3
"""Génère src/l10n.py : fusionne l10n-keys.json (FR maître) + les traductions
sous-agent l10n-{en,de,lu}.json + les chaînes indirectes (helpers), puis
écrit le module final avec la logique de traduction (lookup exact, gabarits
regex à placeholders, cas « Aucune ligne importée » segmenté)."""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = BASE.parent.parent

keys = json.load(open(BASE / "l10n-keys.json"))
en = json.load(open(BASE / "l10n-en.json"))
de = json.load(open(BASE / "l10n-de.json"))
lu = json.load(open(BASE / "l10n-lu.json"))

# chaînes émises par les helpers (absentes des 71 envoyées aux traducteurs)
EXTRA = {
    "Enveloppe fiscale invalide": {
        "en": "Invalid tax wrapper", "de": "Ungültige Steuerhülle", "lu": "Net valabel Steier-Enveloppe"},
    "Enveloppe incompatible avec cette classe d'actif": {
        "en": "Wrapper incompatible with this asset class",
        "de": "Steuerhülle mit dieser Vermögensklasse unvereinbar",
        "lu": "Enveloppe net kompatibel mat dëser Aktiva-Klass"},
    "Pays fiscal invalide (fr, lu ou vide)": {
        "en": "Invalid tax country (fr, lu or empty)",
        "de": "Ungültiges Steuerland (fr, lu oder leer)",
        "lu": "Net valabel Steierland (fr, lu oder eidel)"},
    "Montants du crédit invalides (négatifs)": {
        "en": "Invalid loan amounts (negative)",
        "de": "Ungültige Darlehensbeträge (negativ)",
        "lu": "Net valabel Kreditbeträg (negativ)"},
    "Taux du crédit invalide (> 100 %)": {
        "en": "Invalid loan rate (> 100 %)",
        "de": "Ungültiger Darlehenszins (> 100 %)",
        "lu": "Net valabel Kredittaux (> 100 %)"},
    "Un crédit ne peut être lié qu'à un actif immobilier": {
        "en": "A loan can only be linked to a real-estate asset",
        "de": "Ein Darlehen kann nur an eine Immobilie gebunden werden",
        "lu": "E Kredit kann nëmme mat engem Immobilie-Actif verbonne ginn"},
}

msgs = keys["messages"]
assert len(msgs) == 70, len(msgs)

def build(src, extra, tag):
    out = {}
    for m in msgs:
        t = src.get(m)
        if t is None:
            raise SystemExit(f"MANQUANTE ({tag}): {m!r}")
        out[m] = t
    for m, t in extra.items():
        out[m] = t[tag]
    return out

EN, DE, LU = build(en, EXTRA, "en"), build(de, EXTRA, "de"), build(lu, EXTRA, "lu")
assert len(EN) == len(DE) == len(LU) == 76

# en-têtes CSV : les traducteurs ont reçu la clé FR -> sens ; structure {fr: {lang}}
ERRORS = {m: {"en": EN[m], "de": DE[m], "lu": LU[m]} for m in EN}
CSV_HEADERS = {fr: {"en": en[fr], "de": de[fr], "lu": lu[fr]} for fr in keys["csv_headers"]}
# disclaimers connus : clés FR (ASCII) -> traductions par langue ; le code
# (src/l10n.py) traduit selon Accept-Language. Un texte opérateur absent
# d'ici (env DISCLAIMER) est renvoyé tel quel, sans traduction.
DISCLAIMERS = {
    # démo publique (combiné : données fictives + projet perso)
    "Démo publique — données fictives, aucun compte réel connecté. Projet perso fait pour le plaisir : chiffres à vérifier, aucune garantie.": {
        "en": "Public demo — fictional data, no real account connected. A personal for-fun project: check the figures, no warranty.",
        "de": "Öffentliche Demo — fiktive Daten, kein echtes Konto verbunden. Ein privates Spaßprojekt: Werte prüfen, keine Gewährleistung.",
        "lu": "Ëffentlech Demo — fiktiv Donnéeën, kee reelle Compte verbonnen. En perséinleche Freed-Projet: Wäerter préiwen, keng Garantie.",
    },
    # texte générique (instance réelle / desktop)
    "Projet perso fait pour le plaisir — pas un produit professionnel. Les chiffres affichés (estimations fiscales notamment) sont donnés de bonne foi mais peuvent contenir des erreurs : vérifiez auprès d'un professionnel avant toute décision. Aucune garantie, aucun conseil financier ni fiscal.": {
        "en": "A personal project, built for fun — not a professional product. Figures shown (tax estimates in particular) are best-effort but may contain errors: check with a professional before acting on them. No warranty, no financial or tax advice.",
        "de": "Ein privates Projekt, aus Freude gebaut — kein professionelles Produkt. Die angezeigten Werte (insbesondere Steuerschätzungen) sind nach bestem Wissen, können aber Fehler enthalten: prüfen Sie vor Entscheidungen mit einem Profi. Keine Gewährleistung, keine Finanz- oder Steuerberatung.",
        "lu": "E perséinleche Projet, aus Freed gebaut — ke professionellt Produkt. D'Wäerter déi ugewise ginn (besonnesch Steierschätzungen) sinn no beschten Wëssen, kënnen awer Feeler enthalen: préift mat engem Profi virun Entscheedungen. Keng Garantie, keng Finanz- oder Steierberodung.",
    },
}

# placeholders d'un gabarit
def ph(t):
    return re.findall(r"\{([a-z_]+)\}", t)

# les gabarits avec placeholders doivent garder les MÊMES placeholders ×4
for fr, d in [(m, {"en": EN[m], "de": DE[m], "lu": LU[m]}) for m in msgs if ph(m)]:
    base = set(ph(fr))
    for lang, t in d.items():
        if set(ph(t)) != base:
            raise SystemExit(f"PLACEHOLDERS ({lang}): {fr!r} -> {t!r}")

# échappement : le FR ne doit pas contenir de placeholders inconnus
for m in msgs:
    for p in ph(m):
        if p not in ("e", "label", "n", "m", "key", "lo", "hi", "ln"):
            raise SystemExit(f"PLACEHOLDER INCONNU: {m!r} {p}")

def py(v):
    return json.dumps(v, ensure_ascii=False, indent=0).replace("\n", " ")

head = '''"""Traductions serveur (v2026.09.035) : messages d'erreur API, en-têtes
d'export CSV et disclaimer — clés = texte FR maître (ce que le code émet),
valeurs = {en, de, lu}. Généré par tools/l10n-build.py — ne pas éditer à la
main ; le test d'intégrité (tests/test_l10n.py) vérifie l'exhaustivité et
la cohérence des placeholders.

Le code émet toujours le FR (source lisible + fallback par défaut) ;
le middleware de src/app.py traduit la réponse JSON après coup
(header Accept-Language, repli fr).
"""
import re

ERRORS = %s
CSV_HEADERS = %s
DISCLAIMERS = %s

_DIRECT = {fr: d for fr, d in ERRORS.items() if "{" not in fr}
# gabarits à placeholders : regex compilée (FR) + gabarits localisés
_GAB = []
for fr, d in ERRORS.items():
    if "{" not in fr:
        continue
    pat = "^" + re.escape(fr) + "$"
    for p in set(ph := re.findall(r"\\{([a-z_]+)\\}", fr)):
        pat = pat.replace("\\\\{" + p + "\\\\}", "(?P<g_" + p + ">[^\\"]*?)")
    _GAB.append((re.compile(pat), d))
_GAB.sort(key=lambda x: -len(x[0].pattern))

_LANGS = ("en", "de", "lu")

def lang_of(accept_language: str) -> str:
    for part in (accept_language or "").split(","):
        base = part.strip().split(";")[0].lower().split("-")[0]
        if base in _LANGS:
            return base
    return "fr"

def _fill(tmpl: str, groups: dict) -> str:
    def rep(m):
        return groups.get("g_" + m.group(1), m.group(0))
    return re.sub(r"\\{([a-z_]+)\\}", rep, tmpl)

def _translate(value: str, lang: str) -> str:
    """Traduit un message FR rendu (constant ou gabarit interpolé)."""
    direct = _DIRECT.get(value)
    if direct is not None:
        return direct.get(lang, value)
    for pat, d in _GAB:
        m = pat.match(value)
        if m:
            return _fill(d.get(lang, value), {k: v for k, v in m.groupdict().items() if v is not None})
    # import CSV : « Aucune ligne importée — ligne 2 : montant nul ; … »
    pref = "Aucune ligne importée — "
    if value.startswith(pref) and lang != "fr":
        dpref = _DIRECT.get(pref)
        head_l = dpref.get(lang) if dpref else None
        if head_l is None:
            return value
        segs = []
        for seg0 in value[len(pref):].split("; "):
            seg = seg0.strip()
            done = False
            for pat, d in _GAB:
                m = pat.match(seg)
                if m:
                    segs.append(_fill(d.get(lang, seg), {k: v for k, v in m.groupdict().items() if v is not None}))
                    done = True
                    break
            if not done:
                segs.append(seg)
        return head_l + "; ".join(segs)
    return value

def translate_detail(value, accept_language: str) -> str:
    return _translate(value, lang_of(accept_language))

def csv_header(col: str, accept_language: str) -> str:
    d = CSV_HEADERS.get(col)
    if d is None:
        return col
    return d.get(lang_of(accept_language), col)

def translate_disclaimer(value, accept_language: str) -> str:
    d = DISCLAIMERS.get(value)
    if d is None:
        # texte opérateur (env DISCLAIMER) : tolérer les apostrophes
        # typographiques (U+2019/U+2018) quand la clé FR est en ASCII
        norm = value.replace(chr(0x2019), "'").replace(chr(0x2018), "'")
        d = DISCLAIMERS.get(norm)
    if d is None:
        return value
    return d.get(lang_of(accept_language), value)
'''

open(REPO / "src/l10n.py", "w").write(
    head % (py(ERRORS), py(CSV_HEADERS), py(DISCLAIMERS)))
print("src/l10n.py écrit :", len(ERRORS), "messages,", len(CSV_HEADERS), "en-têtes,", len(DISCLAIMERS), "disclaimer")
