"""Tests localisation serveur (v2026.09.035) : intégrité du dictionnaire
src/l10n.py (généré), traduction des messages d'erreur API par
Accept-Language (middleware), en-têtes d'export CSV, disclaimer.

Le code émet toujours le FR ; sans header Accept-Language la langue par
défaut est le français (aucun test existant ne doit changer)."""

import os
import re
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"
os.environ.pop("DISCLAIMER", None)

from fastapi.testclient import TestClient  # noqa: E402

import src.app as app  # noqa: E402
from src import l10n  # noqa: E402

c = TestClient(app.app)

LANGS = ("en", "de", "lu")


def _login(c, user="admin", pwd="admin-test-2026"):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text


def test_integrite_dictionnaire():
    # messages : 77 (71 envoyés aux traducteurs + 6 chaînes des helpers),
    # chacun présent dans les 4 langues (fr = la clé)
    assert len(l10n.ERRORS) == 77, len(l10n.ERRORS)
    for fr, d in l10n.ERRORS.items():
        for lang in LANGS:
            assert lang in d and d[lang].strip(), (fr, lang)
        assert fr == fr.strip() or fr.endswith(" ")  # seule l'espace finale tolérée
    assert len(l10n.CSV_HEADERS) == 11
    for fr, d in l10n.CSV_HEADERS.items():
        for lang in LANGS:
            assert lang in d and d[lang].strip(), (fr, lang)
    assert len(l10n.DISCLAIMERS) == 1
    # placeholders : les gabarits localisés portent exactement les mêmes
    ph = lambda t: re.findall(r"\{([a-z_]+)\}", t)
    for fr, d in l10n.ERRORS.items():
        base = set(ph(fr))
        for lang in LANGS:
            assert set(ph(d[lang])) == base, (fr, lang)


def test_translate_direct_gabarit_et_segmente():
    # constantes
    assert l10n.translate_detail("Actif introuvable", "en") == "Asset not found"
    assert l10n.translate_detail("Actif introuvable", "de") != "Actif introuvable"
    assert l10n.translate_detail("Actif introuvable", "fr") == "Actif introuvable"
    assert l10n.translate_detail("Actif introuvable", "") == "Actif introuvable"
    assert l10n.translate_detail("Actif introuvable", "zh") == "Actif introuvable"  # inconnu → FR
    # gabarit interpolé (rendu réel, ex. min. 12)
    r = l10n.translate_detail("Mot de passe trop court (min. 12 caractères)", "en")
    assert "12" in r and "Actif" not in r
    assert l10n.translate_detail("Valeur fire_swr invalide (0.1-25)", "de") != \
        "Valeur fire_swr invalide (0.1-25)"
    # import segmenté : préfixe + motifs de ligne
    v = "Aucune ligne importée — ligne 2 : montant nul ; ligne 5 : date ou montant invalide"
    r = l10n.translate_detail(v, "en")
    assert r.startswith("No rows imported") and "row 2" in r and "row 5" in r
    # inconnu : inchangé
    assert l10n.translate_detail("Message inconnu XYZ", "en") == "Message inconnu XYZ"


def test_middleware_erreurs_par_langue():
    _login(c)
    # PUT settings swr 30 → 400 « Valeur fire_swr invalide (0.1-25) »
    for lang, probe in (("en", "invalid"), ("de", "Ungültig"), ("lu", "wäert")):
        r = c.put("/api/settings", json={"fire_swr": 30},
                  headers={"Accept-Language": lang})
        assert r.status_code == 400
        assert r.json()["detail"].lower().startswith(probe.lower()), (lang, r.text)
    # sans header → français
    r = c.put("/api/settings", json={"fire_swr": 30})
    assert r.status_code == 400 and "Valeur fire_swr invalide" in r.json()["detail"]
    # erreur à gabarit interpolé via l'API (login mauvais mdp → Identifiants invalides est direct ;
    # on prend une route f-string : mot de passe court)
    r2 = c.post("/api/family", json={"username": "valid1", "password": "short",
                                     "display_name": "x"}, headers={"Accept-Language": "de"})
    assert r2.status_code == 400
    assert "Passwort" in r2.json()["detail"] or "kurz" in r2.json()["detail"].lower(), r2.text


def test_csv_headers_par_langue():
    _login(c)
    r = c.post("/api/accounts", json={"name": "Cpte Test", "asset_class": "comptes",
                                      "currency": "EUR", "cost_basis": 0, "open_date": "2026-01-01",
                                      "initial_value": 100})
    assert r.status_code == 200, r.text
    r = c.post("/api/income-rules", json={"account_id": r.json()["id"], "label": "Loyer",
                                          "amount": 100, "freq": "monthly", "next_date": "2026-09-10",
                                          "active": 1, "kind": "income"})
    assert r.status_code == 200, r.text
    r = c.get("/api/export/csv/rules", headers={"Accept-Language": "en"})
    assert r.status_code == 200
    first = r.content.decode("utf-8-sig").split("\r\n")[0]
    assert "label" in first and "account" in first and "frequency" in first, first
    r2 = c.get("/api/export/csv/rules", headers={"Accept-Language": "lu"})
    first2 = r2.content.decode("utf-8-sig").split("\r\n")[0]
    assert "libelle" not in first2 and first2 != first, first2
    # sans header : français (comportement historique)
    r3 = c.get("/api/export/csv/rules")
    first3 = r3.content.decode("utf-8-sig").split("\r\n")[0]
    assert "libelle" in first3


def test_disclaimer_localise_et_reponses_intactes():
    _login(c)
    os.environ["DISCLAIMER"] = "Démo publique — données fictives à but d'illustration. Aucun compte réel n'est connecté."
    try:
        r = c.get("/api/version", headers={"Accept-Language": "en"})
        assert r.status_code == 200
        assert "demo" in r.json()["disclaimer"].lower() or "Demo" in r.json()["disclaimer"]
        r2 = c.get("/api/version", headers={"Accept-Language": "fr"})
        assert r2.json()["disclaimer"] == os.environ["DISCLAIMER"]
    finally:
        os.environ.pop("DISCLAIMER", None)
    # les grosses réponses JSON passent le middleware sans corruption
    r3 = c.get("/api/summary")
    assert r3.status_code == 200
    assert "total_value" in r3.json()
    r4 = c.get("/api/accounts")
    assert r4.status_code == 200 and "accounts" in r4.json()


def test_disclaimer_apostrophe_typographique():
    """Le DISCLAIMER réel de la démo (v022) porte l'apostrophe typographique
    U+2019 alors que la clé FR du dict est en ASCII — le lookup doit tolérer
    la variante, sinon le texte opérateur ne se traduit jamais."""
    os.environ["DISCLAIMER"] = "Démo publique — données fictives à but d’illustration. Aucun compte réel n’est connecté."
    try:
        r = c.get("/api/version", headers={"Accept-Language": "de"})
        assert r.status_code == 200
        d = r.json()["disclaimer"]
        assert "fiktive Daten" in d and "kein echtes Konto" in d, d
        r2 = c.get("/api/version", headers={"Accept-Language": "fr"})
        assert r2.json()["disclaimer"] == os.environ["DISCLAIMER"]
    finally:
        os.environ.pop("DISCLAIMER", None)
    # unitaire : variante simple guillemet aussi tolérée
    v = l10n.translate_disclaimer(
        "Démo publique — données fictives à but d’illustration. Aucun compte réel n’est connecté.", "en")
    assert v.startswith("Public demo"), v
