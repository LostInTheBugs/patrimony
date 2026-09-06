"""Ruleset fiscal LUXEMBOURG — année 2026 (cessions 2026).

Source normative v1 : ~/work/patrimony/FEUILLET-FISCAL-2026.md (validé Fred,
revue croisée 06/09/2026). Références : guichet.lu, ACD (impotsdirects
.public.lu), art. 99bis/99ter/100/115 LIR, circulaires ACD (100/1 ;
14/5-99/3-99bis/3 du 26/07/2018), loi du 22/05/2024 (seuil immobilier 2 → 5
ans, en vigueur 30/09/2025). Les points INCONFIRMÉS ne sont pas des
constantes : le moteur les émet comme warnings/assumptions.
"""

LU_2026 = {
    "version": "LU-2026",

    # --- Titres (cessions) — base : art. 99bis / 99ter / 100 LIR ---
    # Exonération : participation < 10 % du capital ET détenue > 6 mois.
    # Le taux marginal réel dépend du revenu du foyer → le moteur simule au
    # taux marginal d'HYPOTHÈSE (assumption ASSUME_TMI_LU), jamais de
    # constante « lu_max_rate ».
    "titres_exo_participation_max": 0.10,   # seuil strict : « plus de 10 % »
    "titres_exo_holding_months": 6,         # > 6 mois pour l'exonération
    "titres_speculation_months": 6,         # revente ≤ 6 mois = spéculation
    "titres_franchise_500": 500.0,          # bénéfice annuel < 500 € non imposable
    "titres_participation_window_years": 5,  # > 10 % à un moment des 5 dernières années
    # Participation importante (> 10 %, > 6 mois) : « revenu extraordinaire »,
    # moitié du taux global, max ≈ 21,4 % ; abattement 50 000 € (doublé en
    # imposition collective), réduit des abattements déjà utilisés sur 10 ans.
    "titres_half_rate_max": 0.214,
    "titres_abattement_50000": 50000.0,
    # Spéculation (< 6 mois) : « revenus nets divers », barème progressif.
    # Taux nominal plafond du barème 2026 : 42 % (au-delà de 234 870 €,
    # classe 1). Majoration fonds pour l'emploi : 7 % (revenu ≤ 150 k€
    # classe 1 / ≤ 300 k€ classe 2), 9 % au-delà.
    "bareme_top_nominal": 0.42,
    "fonds_emploi_7pct": 0.07,
    "fonds_emploi_9pct": 0.09,
    "cadep_1p4": 0.014,  # contribution assurance dépendance (revenus nets divers)
    # Moins-values : compensables avec les gains de même nature ; ni
    # reportables, ni imputables sur d'autres catégories.
    "titres_mv_non_reportables": True,

    # --- Assurance-vie (résident) ---
    # Art. 115 LIR n° 17 : capital touché du chef d'une assurance sur la vie
    # exonéré de l'IR. Contrats de prévoyance-vieillesse à primes déduites :
    # hors périmètre (imposition rectificative — non modélisée).
    "av_exoneree_art115": True,
    "av_rachat_6mois_note": "rachat très précoce < 6 mois : imposition au barème possible (INCONFIRMÉ officiel)",

    # --- Immobilier (résident, cessions 2026) ---
    # Loi du 22/05/2024 (paquet logement) : seuil porté de 2 à 5 ans, en
    # vigueur depuis le 30/09/2025. Le seuil de 2 ans est HISTORIQUE et ne
    # concerne pas les cessions 2026.
    "immo_speculation_years": 5,   # ≤ 5 ans → bénéfice de spéculation
    "immo_cession_half_rate_max": 0.21,  # > 5 ans : demi-taux global, max 21 %
    # Bénéfice de cession : abattement décennal 50 000 € (100 000 € en
    # imposition collective), réduit des abattements utilisés sur 10 ans
    # (assumption ASSUME_DECENNIAL_AVAILABLE).
    "immo_abattement_decennal": 50000.0,
    "immo_abattement_decennal_couple": 100000.0,
    "immo_franchise_500": 500.0,  # spéculation : bénéfice annuel < 500 €
    # Assiette > 5 ans : prix d'acquisition RÉÉVALUÉ par coefficients
    # monétaires (table ACD) — NON modélisés v1 → warning LU_IMMO_REVALUATION_NI
    # (sans réévaluation, la PV estimée est surestimée pour les détentions
    # longues ; jamais appliqués en silence).
    "immo_revaluation_note": "coefficients de réévaluation monétaire non appliqués (table ACD hors moteur v1)",
    # Résidence principale : exonérée quelle que soit la durée.
    "immo_exo_residence_principale": True,

    # --- Crypto (circulaire ACD 26/07/2018) ---
    # Biens incorporels ; gestion de patrimoine privé : gain > 6 mois non
    # imposable ; < 6 mois = spéculation (art. 99bis) ; franchise 500 €.
    # Coût : prix moyen pondéré (PMP) — pour une liquidation totale du
    # portefeuille, PV = valeur − coût (cohérent avec le coût Patrimony).
    "crypto_speculation_months": 6,
    "crypto_franchise_500": 500.0,
    "crypto_pmp_only": True,
    # Staking/minage = revenus (BNC/art. 14) — HORS PV ; cadre staking
    # INCONFIRMÉ (pas de texte ACD) → jamais calculé.
    "crypto_staking_inconfirme": True,
    # Détention substantielle d'un OPC > 10 % : non tranché selon la forme
    # juridique (INCONFIRMÉ) — le moteur traite les titres < 10 % par défaut.
    "opc_substantial_note": "participation > 10 % dans un OPC : INCONFIRMÉ (selon forme juridique)",
}
