"""Ruleset fiscal FRANCE — année 2026 (cessions 2026).

Source normative v1 : ~/work/patrimony/FEUILLET-FISCAL-2026.md (validé Fred,
revue croisée 06/09/2026, corrigenda §1-8). Chaque groupe de paramètres
porte sa référence ; les valeurs proviennent du feuillet, lui-même sourcé
(impots.gouv.fr, BOFiP, CGI, service-public, consultés le 06/09/2026).
Les points INCONFIRMÉS du feuillet ne sont PAS des constantes ici : ils
sont émis comme warnings/assumptions par le moteur (jamais de règle
silencieuse).
"""

# --- Contexte structurant 2026 (LFSS 2026, loi 2025-1403 du 30/12/2025) ---
FR_2026 = {
    "version": "FR-2026",
    # Prélèvements sociaux capital mobilier (CTO, PEA, actifs numériques).
    # CSG portée 9,2 → 10,6 % au 01/01/2026 → PS 18,6 %. AV/capitalisation
    # et immobilier exclus → 17,2 %.
    "ps_capital": 0.186,
    "ps_capital_av_immo": 0.172,
    # Composition indicative (INCONFIRMÉ officiel — non utilisé en calcul).
    "ps_capital_composition_note": "CSG 10,6 + CRDS 0,5 + prélèvement de solidarité 7,5 (INCONFIRMÉ officiel)",

    # --- CTO (art. 150-0 A CGI ; PFU de plein droit) ---
    "cto_pfu_ir": 0.128,      # 12,8 %
    "cto_pfu_ps": 0.186,      # 18,6 % (2026)
    "cto_pfu_global": 0.314,  # 31,4 %
    # Option barème 2OP (globale) : abattement durée UNIQUEMENT pour titres
    # acquis avant le 01/01/2018 (abattement de droit commun 150-0 D ter).
    "cto_abattement_cutoff": "2018-01-01",
    "cto_abattement_50pct_min_years": 2,   # 50 % : 2 à 8 ans
    "cto_abattement_50pct_max_years": 8,
    "cto_abattement_65pct_min_years": 8,   # 65 % : > 8 ans
    "cto_abattement_50pct": 0.50,
    "cto_abattement_65pct": 0.65,
    # Moins-values : compensées d'abord, reportables 10 ans (données non
    # saisies dans Patrimony v1 → assumption émise par le moteur).
    "cto_mv_report_years": 10,

    # --- PEA (exonération IR après 5 ans) ---
    "pea_exo_ir_after_years": 5,
    "pea_ps": 0.186,  # PS au retrait (mode current_rate ; stratification
    # historique des gains pré-2026 INCONFIRMÉE → warning FR_PEA_PS_HISTORICAL
    # si plan ouvert avant 2018 — jamais de pea_tax = gain × 0,186 muet).
    "pea_ps_historical_cutoff": "2018-01-01",
    "pea_retrait_avant5_ir": 0.128,  # retrait < 5 ans = clôture : 12,8 + PS
    # (le retrait avant 5 ans ferme le plan — cas « liquidation aujourd'hui »)

    # --- Assurance-vie (versements postérieurs au 27/09/2017) ---
    "av_ps": 0.172,             # exclue de la hausse LFSS 2026
    "av_ir_avant8": 0.128,      # rachat < 8 ans (aucun abattement)
    "av_ir_apres8_7p5": 0.075,  # ≥ 8 ans ET primes ≤ 150 000 €
    "av_ir_apres8_12p8": 0.128,  # fraction excédant 150 000 €
    "av_seuil_primes_150k": 150000.0,  # primes globalisées tous contrats
    "av_annees": 8,
    "av_abattement_celib": 4600.0,   # option barème > 8 ans
    "av_abattement_couple": 9200.0,
    "av_contrats_avant_20170927_note": "régime ancien non modélisé (hors périmètre v1)",

    # --- Immobilier (particuliers, droit commun 2026) ---
    "immo_ir_rate": 0.19,
    "immo_ps_rate": 0.172,  # maintenu en 2026 (hors hausse LFSS)
    # Abattement durée de détention IR (6e→21e : 6 %/an ; 22e : 4 % ;
    # exonération totale à 22 ans).
    "immo_abat_ir_y6_21": 0.06,
    "immo_abat_ir_y22": 0.04,
    "immo_exo_ir_years": 22,
    # Abattement durée PS (6e→21e : 1,65 %/an ; 22e : 1,6 % ; 23e→30e : 9 % ;
    # exonération totale à 30 ans).
    "immo_abat_ps_y6_21": 0.0165,
    "immo_abat_ps_y22": 0.016,
    "immo_abat_ps_y23_30": 0.09,
    "immo_exo_ps_years": 30,
    # Assiette : prix de revient majoré = acquisition + frais 7,5 % (forfait
    # ou réel) + travaux 15 % (forfait, si détention > 5 ans, bâti).
    "immo_frais_acquisition_forfait": 0.075,
    "immo_travaux_forfait": 0.15,
    "immo_travaux_min_years": 5,
    # Exonérations : prix de vente ≤ 15 000 € ; résidence principale.
    "immo_exo_vente_15k": 15000.0,
    # Surtaxe PV > 50 000 € (CGI 1609 nonies G ; barème 2048-IMM — hors
    # terrains à bâtir, cf. assumption ASSUME_PROPERTY_BUILT).
    "immo_surtaxe_threshold": 50000.0,
    "immo_surtaxe_land_excluded": True,

    # --- Actifs numériques (art. 150 VH bis ; cessions 2026) ---
    "crypto_pfu_ir": 0.128,
    "crypto_pfu_ps": 0.186,
    "crypto_pfu_global": 0.314,
    "crypto_option_3cn": True,   # option barème INDÉPENDANTE du 2OP
    "crypto_seuil_cession_305": 305.0,  # exonération cessions annuelles
    "crypto_mv_meme_annee_only": True,  # MV imputables même année seulement
    "crypto_sursis_echange": True,      # échanges crypto→crypto non taxables
    # Frais d'acquisition déductibles : INCONFIRMÉ → non déduits (assumption).
    "crypto_frais_acquisition_note": "déductibilité INCONFIRMÉE — non déduits",
}
