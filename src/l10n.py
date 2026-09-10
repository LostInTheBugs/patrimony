"""Traductions serveur (v2026.09.035) : messages d'erreur API, en-têtes
d'export CSV et disclaimer — clés = texte FR maître (ce que le code émet),
valeurs = {en, de, lu}. Généré par tools/l10n-build.py — ne pas éditer à la
main ; le test d'intégrité (tests/test_l10n.py) vérifie l'exhaustivité et
la cohérence des placeholders.

Le code émet toujours le FR (source lisible + fallback par défaut) ;
le middleware de src/app.py traduit la réponse JSON après coup
(header Accept-Language, repli fr).
"""
import re

ERRORS = { "Actif introuvable": { "en": "Asset not found", "de": "Vermögenswert nicht gefunden", "lu": "Aktif net fonnt" }, "Administrateur requis": { "en": "Administrator required", "de": "Administrator erforderlich", "lu": "Administrateur néideg" }, "Aucune valorisation — impossible d'estimer": { "en": "No valuation — unable to estimate", "de": "Keine Bewertung vorhanden – Schätzung nicht möglich", "lu": "Keng Valuatioun — net méiglech ze schätzen" }, "BCE injoignable — réessayez plus tard": { "en": "BCE unreachable — please try again later", "de": "BCE nicht erreichbar – bitte versuchen Sie es später erneut", "lu": "BCE net erreechbar — probéiert et méi spéit nach eng Kéier" }, "Ce nom d'utilisateur existe déjà": { "en": "This username already exists", "de": "Dieser Benutzername ist bereits vergeben", "lu": "Dëse Benotzernumm existéiert schonn" }, "Chiffrement impossible": { "en": "Encryption failed", "de": "Verschlüsselung nicht möglich", "lu": "Verschlësselung onméiglech" }, "Classe d'actif invalide": { "en": "Invalid asset class", "de": "Ungültige Anlageklasse", "lu": "Aktifklass net valabel" }, "Clé de coffre invalide": { "en": "Invalid vault key", "de": "Ungültiger Tresorschlüssel", "lu": "Tresorschlëssel net valabel" }, "Clé de récupération invalide": { "en": "Invalid recovery key", "de": "Ungültiger Wiederherstellungsschlüssel", "lu": "Récuperatiounsschlëssel net valabel" }, "Coffre déjà initialisé": { "en": "Vault already initialized", "de": "Tresor bereits initialisiert", "lu": "Tresor schonn initialiséiert" }, "Coffre non initialisé": { "en": "Vault not initialized", "de": "Tresor nicht initialisiert", "lu": "Tresor net initialiséiert" }, "Coffre verrouillé": { "en": "Vault locked", "de": "Tresor gesperrt", "lu": "Tresor gespaart" }, "Compte non protégé": { "en": "Account not protected", "de": "Konto nicht geschützt", "lu": "Compte net geschützt" }, "Compte protégé : réinitialisation impossible par conception.": { "en": "Protected account: reset is impossible by design.", "de": "Geschütztes Konto: Zurücksetzung ist konstruktionsbedingt nicht möglich.", "lu": "Compte geschützt: Reset ass aus Prinzip onméiglech." }, "Date invalide": { "en": "Invalid date", "de": "Ungültiges Datum", "lu": "Datum net valabel" }, "Devise non supportée": { "en": "Unsupported currency", "de": "Nicht unterstützte Währung", "lu": "Währung net ënnerstëtzt" }, "Dividende géré depuis la ligne du portefeuille": { "en": "Dividend managed from the portfolio entry", "de": "Dividende wird über die Portfolioposition verwaltet", "lu": "Dividend gëtt iwwer d'Portefeuillelinn geréiert" }, "Dividende introuvable": { "en": "Dividend not found", "de": "Dividende nicht gefunden", "lu": "Dividend net fonnt" }, "Déverrouillez d'abord le coffre": { "en": "Unlock the vault first", "de": "Entsperren Sie zuerst den Tresor", "lu": "Späert fir d'éischt den Tresor op" }, "En-tête incompréhensible — colonnes attendues : date, libellé, montant (ou débit/crédit). Séparateur virgule, point-virgule ou tabulation.": { "en": "Unreadable header — expected columns: date, label, amount (or debit/credit). Separator: comma, semicolon or tab.", "de": "Unverständlicher Kopf – erwartete Spalten: Datum, Bezeichnung, Betrag (oder Soll/Haben). Trennzeichen: Komma, Semikolon oder Tabulator.", "lu": "Kappzeil net verstännlech — erwaart Spalten: Datum, Libell, Montant (oder Débit/Kredit). Separator: Komma, Semikolon oder Tabulatioun." }, "Expiration invalide (1-3650 jours)": { "en": "Invalid expiration (1-3650 days)", "de": "Ungültige Gültigkeitsdauer (1-3650 Tage)", "lu": "Expiratioun net valabel (1-3650 Deeg)" }, "Fichier illisible": { "en": "File unreadable", "de": "Datei nicht lesbar", "lu": "Fichier net liesbar" }, "Fichier manquant": { "en": "File missing", "de": "Datei fehlt", "lu": "Fichier feelt" }, "Fichier vide (en-tête + au moins une ligne)": { "en": "Empty file (header + at least one row)", "de": "Leere Datei (Kopf + mindestens eine Zeile erforderlich)", "lu": "Eidele Fichier (Kappzeil + mindestens eng Linn)" }, "Fichier vide ou trop volumineux (2 Mo max)": { "en": "Empty or too large file (2 MB max)", "de": "Leere oder zu große Datei (max. 2 MB)", "lu": "Fichier eidel oder ze grouss (max. 2 MB)" }, "Frais annuels invalides": { "en": "Invalid annual fees", "de": "Ungültige jährliche Gebühren", "lu": "Joreskäschten net valabel" }, "Horizon invalide (1-100 ans)": { "en": "Invalid horizon (1-100 years)", "de": "Ungültiger Zeithorizont (1-100 Jahre)", "lu": "Horizont net valabel (1-100 Joer)" }, "Identifiants invalides": { "en": "Invalid credentials", "de": "Ungültige Anmeldedaten", "lu": "Login-Donnéeën net valabel" }, "Impossible sur votre propre compte": { "en": "Not possible on your own account", "de": "Auf Ihrem eigenen Konto nicht möglich", "lu": "Onméiglech op Ärem eegene Compte" }, "JSON invalide": { "en": "Invalid JSON", "de": "Ungültiges JSON", "lu": "JSON net valabel" }, "Jeton introuvable": { "en": "Token not found", "de": "Token nicht gefunden", "lu": "Token net fonnt" }, "Jeton à portée limitée — action non autorisée": { "en": "Restricted-scope token — action not authorized", "de": "Token mit eingeschränktem Umfang – Aktion nicht autorisiert", "lu": "Token mat limitéierter Portée — Aktioun net autoriséiert" }, "Libellé ou montant invalide": { "en": "Invalid label or amount", "de": "Ungültige Bezeichnung oder ungültiger Betrag", "lu": "Libell oder Montant net valabel" }, "Ligne introuvable": { "en": "Entry not found", "de": "Position nicht gefunden", "lu": "Linn net fonnt" }, "Lignes réservées aux comptes bourse valorisés au cours": { "en": "Entries reserved for brokerage accounts valued at market price", "de": "Positionen nur für Börsenkonten, die zum Kurs bewertet werden", "lu": "Linnen nëmme fir Bourse-Compte mat Kursvaluatioun" }, "Membre introuvable": { "en": "Member not found", "de": "Mitglied nicht gefunden", "lu": "Member net fonnt" }, "Mode invalide": { "en": "Invalid mode", "de": "Ungültiger Modus", "lu": "Modus net valabel" }, "Montant invalide": { "en": "Invalid amount", "de": "Ungültiger Betrag", "lu": "Montant net valabel" }, "Montant par action invalide": { "en": "Invalid amount per share", "de": "Ungültiger Betrag pro Aktie", "lu": "Montant pro Aktie net valabel" }, "Montants invalides (>= 0 attendus)": { "en": "Invalid amounts (>= 0 expected)", "de": "Ungültige Beträge (>= 0 erwartet)", "lu": "Montanten net valabel (>= 0 erwaart)" }, "Mot de passe actuel incorrect": { "en": "Incorrect current password", "de": "Aktuelles Passwort falsch", "lu": "Aktuellt Passwuert net korrekt" }, "Mot de passe trop court (8 caractères minimum)": { "en": "Password too short (minimum 8 characters)", "de": "Passwort zu kurz (mindestens 8 Zeichen)", "lu": "Passwuert ze kuerz (minimum 8 Zeechen)" }, "Nom d'utilisateur invalide (3-32 : a-z 0-9 . _ -)": { "en": "Invalid username (3-32: a-z 0-9 . _ -)", "de": "Ungültiger Benutzername (3-32: a-z 0-9 . _ -)", "lu": "Benotzernumm net valabel (3-32: a-z 0-9 . _ -)" }, "Nom requis": { "en": "Name required", "de": "Name erforderlich", "lu": "Numm erfuerderlech" }, "Non authentifié": { "en": "Not authenticated", "de": "Nicht authentifiziert", "lu": "Net authentifizéiert" }, "Non disponible pour les comptes protégés": { "en": "Not available for protected accounts", "de": "Für geschützte Konten nicht verfügbar", "lu": "Net verfügbar fir geschützt Compte" }, "Option invalide (2op|3cn)": { "en": "Invalid option (2op|3cn)", "de": "Ungültige Option (2op|3cn)", "lu": "Optioun net valabel (2op|3cn)" }, "Opération introuvable": { "en": "Operation not found", "de": "Vorgang nicht gefunden", "lu": "Operatioun net fonnt" }, "PRU invalide": { "en": "Invalid PRU", "de": "Ungültiger PRU", "lu": "PRU net valabel" }, "Paramètres hors plage (rendement -5..25, inflation 0..15, retrait 0..25)": { "en": "Parameters out of range (return -5..25, inflation 0..15, withdrawal 0..25)", "de": "Parameter außerhalb des Bereichs (Rendite -5..25, Inflation 0..15, Entnahme 0..25)", "lu": "Parameter ausserhalb vum Beräich (Rendement -5..25, Inflatioun 0..15, Réckzuch 0..25)" }, "Pays fiscal non renseigné — définissez-le dans l'actif": { "en": "Tax country not set — define it in the asset", "de": "Kein Steuerland angegeben – bitte im Vermögenswert festlegen", "lu": "Steierland net agestallt — definéiert et am Aktif" }, "Portée invalide (full|capture)": { "en": "Invalid scope (full|capture)", "de": "Ungültiger Geltungsbereich (full|capture)", "lu": "Portée net valabel (full|capture)" }, "Quantité invalide": { "en": "Invalid quantity", "de": "Ungültige Menge", "lu": "Quantitéit net valabel" }, "Re-chiffrement du coffre requis (wrapped + salt)": { "en": "Vault re-encryption required (wrapped + salt)", "de": "Neuverschlüsselung des Tresors erforderlich (wrapped + salt)", "lu": "Nei Verschlësselung vum Tresor erfuerderlech (wrapped + salt)" }, "Règle introuvable": { "en": "Rule not found", "de": "Regel nicht gefunden", "lu": "Reegel net fonnt" }, "Récupération impossible": { "en": "Recovery impossible", "de": "Wiederherstellung nicht möglich", "lu": "Recuperatioun onméiglech" }, "Symbole invalide": { "en": "Invalid symbol", "de": "Ungültiges Symbol", "lu": "Symbol net valabel" }, "Taux marginal invalide (0-100 %)": { "en": "Invalid marginal rate (0-100 %)", "de": "Ungültiger Grenzsteuersatz (0-100 %)", "lu": "Marginalen Taux net valabel (0-100 %)" }, "Type d'opération invalide": { "en": "Invalid operation type", "de": "Ungültiger Vorgangstyp", "lu": "Operatiounstyp net valabel" }, "Type de règle invalide": { "en": "Invalid rule type", "de": "Ungültiger Regeltyp", "lu": "Reegeltyp net valabel" }, "Type inconnu": { "en": "Unknown type", "de": "Unbekannter Typ", "lu": "Onbekannten Typ" }, "Type inconnu (accounts|transactions|valuations|rules)": { "en": "Unknown type (accounts|transactions|valuations|rules)", "de": "Unbekannter Typ (accounts|transactions|valuations|rules)", "lu": "Onbekannten Typ (accounts|transactions|valuations|rules)" }, "Valeur invalide (0 ou 1)": { "en": "Invalid value (0 or 1)", "de": "Ungültiger Wert (0 oder 1)", "lu": "Wäert net valabel (0 oder 1)" }, "Aucune ligne importée — ": { "en": "No rows imported — ", "de": "Keine Zeile importiert – ", "lu": "Keng Linn importéiert — " }, "CSV illisible : {e}": { "en": "CSV unreadable: {e}", "de": "CSV nicht lesbar: {e}", "lu": "CSV net liesbar: {e}" }, "Champ {label} invalide": { "en": "Invalid {label} field", "de": "Ungültiges Feld {label}", "lu": "Feld {label} net valabel" }, "Mot de passe trop court (min. {n} caractères)": { "en": "Password too short (min. {n} characters)", "de": "Passwort zu kurz (min. {n} Zeichen)", "lu": "Passwuert ze kuerz (min. {n} Zeechen)" }, "Trop de tentatives. Réessayez dans {m} min.": { "en": "Too many attempts. Try again in {m} min.", "de": "Zu viele Versuche. Bitte versuchen Sie es in {m} Min. erneut.", "lu": "Ze vill Versich. Probéiert et an {m} Min. nach eng Kéier." }, "Valeur {key} invalide ({lo}-{hi})": { "en": "Invalid {key} value ({lo}-{hi})", "de": "Ungültiger Wert für {key} ({lo}-{hi})", "lu": "Wäert {key} net valabel ({lo}-{hi})" }, "ligne {ln} : date ou montant invalide": { "en": "row {ln}: invalid date or amount", "de": "Zeile {ln}: ungültiges Datum oder ungültiger Betrag", "lu": "Linn {ln}: Datum oder Montant net valabel" }, "ligne {ln} : montant nul": { "en": "row {ln}: null amount", "de": "Zeile {ln}: Betrag ist null", "lu": "Linn {ln}: Montant null" }, "Enveloppe fiscale invalide": { "en": "Invalid tax wrapper", "de": "Ungültige Steuerhülle", "lu": "Net valabel Steier-Enveloppe" }, "Enveloppe incompatible avec cette classe d'actif": { "en": "Wrapper incompatible with this asset class", "de": "Steuerhülle mit dieser Vermögensklasse unvereinbar", "lu": "Enveloppe net kompatibel mat dëser Aktiva-Klass" }, "Pays fiscal invalide (fr, lu ou vide)": { "en": "Invalid tax country (fr, lu or empty)", "de": "Ungültiges Steuerland (fr, lu oder leer)", "lu": "Net valabel Steierland (fr, lu oder eidel)" }, "Montants du crédit invalides (négatifs)": { "en": "Invalid loan amounts (negative)", "de": "Ungültige Darlehensbeträge (negativ)", "lu": "Net valabel Kreditbeträg (negativ)" }, "Taux du crédit invalide (> 100 %)": { "en": "Invalid loan rate (> 100 %)", "de": "Ungültiger Darlehenszins (> 100 %)", "lu": "Net valabel Kredittaux (> 100 %)" }, "Un crédit ne peut être lié qu'à un actif immobilier": { "en": "A loan can only be linked to a real-estate asset", "de": "Ein Darlehen kann nur an eine Immobilie gebunden werden", "lu": "E Kredit kann nëmme mat engem Immobilie-Actif verbonne ginn" } }
CSV_HEADERS = { "compte": { "en": "account", "de": "Konto", "lu": "Compte" }, "date": { "en": "date", "de": "Datum", "lu": "Datum" }, "type": { "en": "type", "de": "Typ", "lu": "Typ" }, "montant": { "en": "amount", "de": "Betrag", "lu": "Montant" }, "note": { "en": "note", "de": "Notiz", "lu": "Notiz" }, "valeur": { "en": "value", "de": "Wert", "lu": "Wäert" }, "source": { "en": "source", "de": "Quelle", "lu": "Quell" }, "libelle": { "en": "label", "de": "Bezeichnung", "lu": "Libell" }, "frequence": { "en": "frequency", "de": "Häufigkeit", "lu": "Frequenz" }, "prochaine_date": { "en": "next date", "de": "nächstes Datum", "lu": "Nächst Datum" }, "active": { "en": "active", "de": "aktiv", "lu": "Aktiv" } }
DISCLAIMERS = { "Démo publique — données fictives, aucun compte réel connecté. Projet perso fait pour le plaisir : chiffres à vérifier, aucune garantie.": { "en": "Public demo — fictional data, no real account connected. A personal for-fun project: check the figures, no warranty.", "de": "Öffentliche Demo — fiktive Daten, kein echtes Konto verbunden. Ein privates Spaßprojekt: Werte prüfen, keine Gewährleistung.", "lu": "Ëffentlech Demo — fiktiv Donnéeën, kee reelle Compte verbonnen. En perséinleche Freed-Projet: Wäerter préiwen, keng Garantie." }, "Projet perso fait pour le plaisir — pas un produit professionnel. Les chiffres affichés (estimations fiscales notamment) sont donnés de bonne foi mais peuvent contenir des erreurs : vérifiez auprès d'un professionnel avant toute décision. Aucune garantie, aucun conseil financier ni fiscal.": { "en": "A personal project, built for fun — not a professional product. Figures shown (tax estimates in particular) are best-effort but may contain errors: check with a professional before acting on them. No warranty, no financial or tax advice.", "de": "Ein privates Projekt, aus Freude gebaut — kein professionelles Produkt. Die angezeigten Werte (insbesondere Steuerschätzungen) sind nach bestem Wissen, können aber Fehler enthalten: prüfen Sie vor Entscheidungen mit einem Profi. Keine Gewährleistung, keine Finanz- oder Steuerberatung.", "lu": "E perséinleche Projet, aus Freed gebaut — ke professionellt Produkt. D'Wäerter déi ugewise ginn (besonnesch Steierschätzungen) sinn no beschten Wëssen, kënnen awer Feeler enthalen: préift mat engem Profi virun Entscheedungen. Keng Garantie, keng Finanz- oder Steierberodung." } }

_DIRECT = {fr: d for fr, d in ERRORS.items() if "{" not in fr}
# gabarits à placeholders : regex compilée (FR) + gabarits localisés
_GAB = []
for fr, d in ERRORS.items():
    if "{" not in fr:
        continue
    pat = "^" + re.escape(fr) + "$"
    for p in set(ph := re.findall(r"\{([a-z_]+)\}", fr)):
        pat = pat.replace("\\{" + p + "\\}", "(?P<g_" + p + ">[^\"]*?)")
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
    return re.sub(r"\{([a-z_]+)\}", rep, tmpl)

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
