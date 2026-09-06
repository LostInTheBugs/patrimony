"""Tests du moteur fiscal PUR src/tax (v2026.09.031).

Valeurs exactes calculées à la main / au tableur sur les règles du
FEUILLET-FISCAL-2026.md (validé Fred 06/09/2026) — jamais « ça ne plante
pas ». Les dates de cession sont FIGÉES (asof) : aucun test ne dépend du
jour courant.
"""

from datetime import date

import pytest

from src.tax import (
    A_AV_PRIMES_150K, A_AV_PRIMES_OVER_150K, A_DECENNIAL_AVAILABLE,
    A_LU_CONTRACT, A_MARRIED, A_NO_CESSION_FEES, A_NO_LOSSES,
    A_PROPERTY_BUILT, A_SUBSTANTIAL_NO, A_SUBSTANTIAL_YES, A_TMI_FR,
    A_TMI_LU, W_AV_BEFORE_2017, W_LU_AV_EARLY_REDEMPTION,
    W_LU_IMMO_REVALUATION, W_MISSING_DATE, W_NEGATIVE_GAIN,
    W_NOT_ESTIMATED, W_PEA_PS_HISTORICAL, W_WRAPPER_COUNTRY_MISMATCH,
    TaxInput, classify, compute,
)
from src.tax.rules import get_ruleset

ASOF = date(2026, 9, 6)


def t(**kw):
    kw.setdefault("year", 2026)
    kw.setdefault("asof", ASOF)
    return TaxInput(**kw)


# ------------------------------------------------------------- classifieur
class TestClassify:
    def test_fr_bourse_sans_wrapper_est_cto(self):
        assert classify("fr", "bourse", "") == "cto"

    def test_wrappers_fr(self):
        assert classify("fr", "bourse", "pea") == "pea"
        assert classify("fr", "epargne", "av") == "av"
        assert classify("fr", "bourse", "cto") == "cto"

    def test_pea_hors_france_refuse(self):
        assert classify("lu", "bourse", "pea") == "not_estimated"

    def test_lu_titres_immo_crypto(self):
        assert classify("lu", "bourse", "") == "titres"
        assert classify("lu", "immobilier", "") == "immo"
        assert classify("lu", "crypto", "") == "crypto"

    def test_hors_perimetre(self):
        assert classify("fr", "metaux", "") == "not_estimated"
        assert classify("fr", "crowdfunding", "") == "not_estimated"
        assert classify("fr", "divers", "") == "not_estimated"
        assert classify("fr", "comptes", "") == "not_estimated"
        assert classify("", "bourse", "") == "not_estimated"


# ------------------------------------------------------------- CTO FR
class TestCtoFr:
    def test_pfu_314_pct_exact(self):
        """PV 42 800 → IR 12,8 %, PS 18,6 % — l'exemple du contrat d'affichage."""
        r = compute(t(country="fr", asset_class="bourse",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "cto" and r.ruleset_version == "FR-2026"
        assert r.gross_gain == 42800.0
        assert r.taxable_gain == 42800.0
        assert r.income_tax == pytest.approx(42800 * 0.128, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)
        assert r.estimated_net_gain == pytest.approx(42800 * (1 - 0.314), abs=0.01)
        ids = [l.id for l in r.lines]
        assert "FR_CTO_PFU_IR_2026" in ids and "FR_CTO_PFU_PS_2026" in ids

    def test_moins_values_avant_imposition(self):
        """Le breakdown de Fred : MV 2 000 déduites AVANT le calcul d'impôt."""
        r = compute(t(country="fr", asset_class="bourse",
                      open_date="2020-01-10", losses=2000.0,
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.losses_applied == 2000.0
        assert r.taxable_gain == 40800.0
        assert r.income_tax == pytest.approx(40800 * 0.128, abs=0.01)
        assert r.estimated_net_gain == pytest.approx(42800 - 2000
                                                     - 40800 * 0.314, abs=0.01)

    def test_option_bareme_sans_tmi_refuse(self):
        r = compute(t(country="fr", asset_class="bourse", progressive="2op",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "not_estimated"

    def test_option_bareme_abattement_titres_anciens(self):
        """Titres acquis avant 2018, > 8 ans : abattement 65 % sous barème
        (jamais sous PFU)."""
        base = dict(country="fr", asset_class="bourse", progressive="2op",
                    tmi_fr=0.30, acquisition_cost=100000.0,
                    current_value=142800.0)
        r = compute(t(open_date="2015-01-10", **base))
        assert r.income_tax == pytest.approx(42800 * 0.35 * 0.30, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)
        # PFU : pas d'abattement
        r2 = compute(t(open_date="2015-01-10",
                       country="fr", asset_class="bourse",
                       acquisition_cost=100000.0, current_value=142800.0))
        assert r2.income_tax == pytest.approx(42800 * 0.128, abs=0.01)

    def test_perte_pas_d_impot(self):
        r = compute(t(country="fr", asset_class="bourse",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=80000.0))
        assert r.income_tax == 0.0 and r.social_contributions == 0.0
        assert r.estimated_net_gain == -20000.0
        assert W_NEGATIVE_GAIN in r.warnings

    def test_regle_versionnee_refuse_annee_inconnue(self):
        with pytest.raises(KeyError):
            compute(t(country="fr", asset_class="bourse", year=2025,
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))


# ------------------------------------------------------------- PEA FR
class TestPeaFr:
    def test_apres_5_ans_ir_exonere_ps_seuls(self):
        r = compute(t(country="fr", asset_class="bourse", wrapper="pea",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == 0.0
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)
        assert W_PEA_PS_HISTORICAL not in r.warnings

    def test_warning_stratification_si_plan_pre_2018(self):
        r = compute(t(country="fr", asset_class="bourse", wrapper="pea",
                      open_date="2016-06-01",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert W_PEA_PS_HISTORICAL in r.warnings

    def test_avant_5_ans_cloture_pfu(self):
        r = compute(t(country="fr", asset_class="bourse", wrapper="pea",
                      open_date="2023-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.128, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)


# ------------------------------------------------------------- AV FR
class TestAvFr:
    def test_avant_8_ans_30_pct(self):
        r = compute(t(country="fr", asset_class="epargne", wrapper="av",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.128, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.172, abs=0.01)
        assert r.estimated_net_gain == pytest.approx(42800 * (1 - 0.30), abs=0.01)

    def test_apres_8_ans_7p5_pas_30(self):
        """Jamais de av_global_rate = 30 % : ≥ 8 ans & primes ≤ 150 k€ →
        7,5 % + 17,2 % = 24,7 %."""
        r = compute(t(country="fr", asset_class="epargne", wrapper="av",
                      open_date="2016-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.075, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.172, abs=0.01)
        assert A_AV_PRIMES_150K in r.assumptions

    def test_primes_depassent_150k_12p8(self):
        r = compute(t(country="fr", asset_class="epargne", wrapper="av",
                      open_date="2016-01-10", av_primes_under_150k=False,
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.128, abs=0.01)
        assert A_AV_PRIMES_OVER_150K in r.assumptions

    def test_option_bareme_8_ans_abattement_annuel(self):
        """≥ 8 ans, option 2OP : abattement 4 600 € puis TMI 30 % ;
        PS sur la base entière (l'abattement ne les réduit pas)."""
        r = compute(t(country="fr", asset_class="epargne", wrapper="av",
                      open_date="2016-01-10", progressive="2op", tmi_fr=0.30,
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.taxable_gain == pytest.approx(42800 - 4600, abs=0.01)
        assert r.income_tax == pytest.approx((42800 - 4600) * 0.30, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.172, abs=0.01)
        assert A_MARRIED not in r.assumptions

    def test_warning_contrat_avant_2017(self):
        r = compute(t(country="fr", asset_class="epargne", wrapper="av",
                      open_date="2015-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert W_AV_BEFORE_2017 in r.warnings


# ------------------------------------------------------------- Immo FR
class TestImmoFr:
    def test_10_ans_abattements_et_surtaxe(self):
        """Acheté 200 000 € il y a 10 ans, vendu 350 000 € :
        revient majoré 245 000 (7,5 % frais + 15 % travaux) → PV 105 000 ;
        abattement IR 30 % (6 % × 5) → 73 500 × 19 % = 13 965 ;
        abattement PS 8,25 % → 96 337,50 × 17,2 % = 16 570,05 ;
        surtaxe 2 % × 73 500 = 1 470 (PV IR > 50 k€, bâti) ;
        net = 105 000 − 13 965 − 16 570,05 − 1 470 = 72 994,95."""
        r = compute(t(country="fr", asset_class="immobilier",
                      open_date="2016-09-06",
                      acquisition_cost=200000.0, current_value=350000.0))
        assert r.gross_gain == 150000.0
        assert r.taxable_gain == pytest.approx(73500.0, abs=0.01)
        assert r.income_tax == pytest.approx(13965.0, abs=0.01)
        assert r.social_contributions == pytest.approx(16570.05, abs=0.01)
        assert r.extra_tax == pytest.approx(1470.0, abs=0.01)
        assert r.estimated_net_gain == pytest.approx(72994.95, abs=0.01)
        assert A_PROPERTY_BUILT in r.assumptions

    def test_exoneration_22_30_ans(self):
        """> 22 ans : IR exonéré ; PS jusqu'à 30 ans (9 %/an après la 22e)."""
        r22 = compute(t(country="fr", asset_class="immobilier",
                        open_date="2003-09-06",
                        acquisition_cost=200000.0, current_value=350000.0))
        assert r22.income_tax == 0.0
        # 23 ans révolus : abat PS = 26,4+1,6+9 = 37 % → base 66 150 → 11 377,80
        assert r22.social_contributions == pytest.approx(
            (150000 - 45000) * (1 - 0.37) * 0.172, abs=0.01)
        r30 = compute(t(country="fr", asset_class="immobilier",
                        open_date="1996-01-01",
                        acquisition_cost=200000.0, current_value=350000.0))
        assert r30.social_contributions == 0.0
        assert r30.estimated_net_gain == pytest.approx(105000.0, abs=0.01)

    def test_travaux_forfait_absent_avant_5_ans(self):
        r = compute(t(country="fr", asset_class="immobilier",
                      open_date="2022-06-01",
                      acquisition_cost=200000.0, current_value=350000.0))
        # 4 ans : pas de travaux 15 % ; frais 7,5 % → PV 150 000 − 15 000
        assert r.gross_gain == 150000.0
        assert r.taxable_gain == 135000.0  # pas d'abattement durée < 5 ans

    def test_surtaxe_barreme_transitions(self):
        from src.tax.engine import _surtaxe_immo
        assert _surtaxe_immo(40000) == 0.0
        assert _surtaxe_immo(80000) == pytest.approx(1600.0)          # 2 %
        assert _surtaxe_immo(120000) == pytest.approx(3600.0)         # 3 %
        assert _surtaxe_immo(180000) == pytest.approx(7200.0)         # 4 %
        assert _surtaxe_immo(230000) == pytest.approx(11500.0)        # 5 %
        assert _surtaxe_immo(300000) == pytest.approx(18000.0)        # 6 %
        # bornes basses des paliers lissés
        assert _surtaxe_immo(50001) > 0


# ------------------------------------------------------------- Crypto FR
class TestCryptoFr:
    def test_pfu_314(self):
        r = compute(t(country="fr", asset_class="crypto",
                      open_date="2021-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.128, abs=0.01)
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)

    def test_option_3cn_independante(self):
        r = compute(t(country="fr", asset_class="crypto", progressive="3cn",
                      tmi_fr=0.11,
                      open_date="2021-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == pytest.approx(42800 * 0.11, abs=0.01)
        # PS inchangés même sous barème
        assert r.social_contributions == pytest.approx(42800 * 0.186, abs=0.01)


# ------------------------------------------------------------- LU titres
class TestLuTitres:
    def test_exoneration_moins_10_pct_plus_6_mois(self):
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == 0.0 and r.social_contributions == 0.0
        assert r.estimated_net_gain == 42800.0
        assert A_SUBSTANTIAL_NO in r.assumptions
        assert "LU_TITRES_EXO_10PCT_6MOIS" in [l.id for l in r.lines]

    def test_speculation_moins_6_mois_bareme(self):
        """Revente < 6 mois : barème au taux marginal d'hypothèse (42,8 %)
        + fonds pour l'emploi 7 % + CADEP 1,4 %."""
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2026-07-01", tmi_lu=0.428,
                      acquisition_cost=100000.0, current_value=110000.0))
        assert r.income_tax == pytest.approx(10000 * 0.428, abs=0.01)
        assert r.extra_tax == pytest.approx(10000 * 0.428 * 0.07, abs=0.01)
        assert r.social_contributions == pytest.approx(10000 * 0.014, abs=0.01)
        assert A_TMI_LU in r.assumptions

    def test_franchise_500(self):
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2026-08-01",
                      acquisition_cost=100000.0, current_value=100400.0))
        assert r.income_tax == 0.0
        assert "LU_FRANCHISE_500" in [l.id for l in r.lines]

    def test_participation_importante_demi_taux(self):
        """> 10 % détenue > 6 mois : abattement 50 000 € puis demi-taux
        (min(42,8/2 ; 21,4) = 21,4 %)."""
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2015-01-10", substantial_holding=True,
                      acquisition_cost=100000.0, current_value=242800.0))
        assert r.taxable_gain == pytest.approx(142800 - 50000, abs=0.01)
        assert r.income_tax == pytest.approx(92800 * 0.214, abs=0.01)
        assert A_SUBSTANTIAL_YES in r.assumptions
        assert A_DECENNIAL_AVAILABLE in r.assumptions

    def test_participation_abattement_double_si_marie(self):
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2015-01-10", substantial_holding=True,
                      married=True,
                      acquisition_cost=100000.0, current_value=242800.0))
        assert r.taxable_gain == pytest.approx(142800 - 100000, abs=0.01)
        assert A_MARRIED in r.assumptions

    def test_pea_lu_mismatch(self):
        r = compute(t(country="lu", asset_class="bourse", wrapper="pea",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "not_estimated"
        assert W_WRAPPER_COUNTRY_MISMATCH in r.warnings


# ------------------------------------------------------------- LU AV / immo / crypto
class TestLuAutres:
    def test_av_exoneree_art115(self):
        r = compute(t(country="lu", asset_class="epargne", wrapper="av",
                      open_date="2015-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == 0.0
        assert r.estimated_net_gain == 42800.0
        assert "LU_AV_EXO_ART115" in [l.id for l in r.lines]
        assert A_LU_CONTRACT in r.assumptions

    def test_av_rachat_precoce_warning(self):
        r = compute(t(country="lu", asset_class="epargne", wrapper="av",
                      open_date="2026-06-01",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert W_LU_AV_EARLY_REDEMPTION in r.warnings

    def test_immo_moins_5_ans_speculation(self):
        r = compute(t(country="lu", asset_class="immobilier",
                      open_date="2023-01-10", tmi_lu=0.40,
                      acquisition_cost=200000.0, current_value=260000.0))
        assert r.income_tax == pytest.approx(60000 * 0.40, abs=0.01)
        assert r.extra_tax == pytest.approx(60000 * 0.40 * 0.07, abs=0.01)
        assert r.social_contributions == pytest.approx(60000 * 0.014, abs=0.01)

    def test_immo_plus_5_ans_demi_taux_abattement_decennal(self):
        r = compute(t(country="lu", asset_class="immobilier",
                      open_date="2010-06-01",
                      acquisition_cost=200000.0, current_value=350000.0))
        assert r.taxable_gain == pytest.approx(100000.0, abs=0.01)  # −50 k€
        assert r.income_tax == pytest.approx(100000 * 0.21, abs=0.01)
        assert r.estimated_net_gain == pytest.approx(129000.0, abs=0.01)
        assert W_LU_IMMO_REVALUATION in r.warnings

    def test_crypto_plus_6_mois_exoneree(self):
        r = compute(t(country="lu", asset_class="crypto",
                      open_date="2023-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.income_tax == 0.0
        assert "LU_CRYPTO_EXO_6MOIS" in [l.id for l in r.lines]

    def test_crypto_moins_6_mois_speculation(self):
        r = compute(t(country="lu", asset_class="crypto",
                      open_date="2026-08-01",
                      acquisition_cost=100000.0, current_value=110000.0))
        assert r.income_tax == pytest.approx(10000 * 0.428, abs=0.01)


# ------------------------------------------------------------- non estimés
class TestNonEstimes:
    def test_metaux_fr(self):
        r = compute(t(country="fr", asset_class="metaux",
                      open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "not_estimated"
        assert W_NOT_ESTIMATED in r.warnings

    def test_date_manquante_pea(self):
        r = compute(t(country="fr", asset_class="bourse", wrapper="pea",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "not_estimated"
        assert W_MISSING_DATE in r.warnings

    def test_sans_pays(self):
        r = compute(t(asset_class="bourse", open_date="2020-01-10",
                      acquisition_cost=100000.0, current_value=142800.0))
        assert r.regime == "not_estimated"

    def test_rulesets_disponibles(self):
        assert get_ruleset("fr", 2026)["version"] == "FR-2026"
        assert get_ruleset("lu", 2026)["version"] == "LU-2026"
        assert get_ruleset("fr", 2026)["cto_pfu_ir"] == 0.128
        assert get_ruleset("lu", 2026)["immo_speculation_years"] == 5


# ------------------------------------------------------------- hypothèses non silencieuses
class TestNonSilence:
    def test_toutes_les_assumptions_sont_explicites(self):
        """Aucune hypothèse d'utilisateur ne doit passer inaperçue : chaque
        calcul qui en dépend émet son code."""
        r = compute(t(country="fr", asset_class="bourse",
                      open_date="2020-01-10", losses=0.0,
                      acquisition_cost=100000.0, current_value=142800.0))
        assert A_NO_LOSSES in r.assumptions
        assert A_NO_CESSION_FEES in r.assumptions

    def test_tmi_lu_jamais_silencieux(self):
        r = compute(t(country="lu", asset_class="bourse",
                      open_date="2026-08-01",
                      acquisition_cost=100000.0, current_value=110000.0))
        assert A_TMI_LU in r.assumptions
