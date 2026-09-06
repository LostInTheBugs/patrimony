"""Moteur fiscal Patrimony — estimation « si liquidation aujourd'hui ».

Pur calcul : aucune I/O, aucune dépendance au dashboard. Règles
versionnées par (pays, année) dans rules_fr.py / rules_lu.py (source
normative : FEUILLET-FISCAL-2026.md, validé Fred le 06/09/2026).
"""

from .engine import (
    A_AV_PRIMES_150K,
    A_AV_PRIMES_OVER_150K,
    A_DECENNIAL_AVAILABLE,
    A_LU_CONTRACT,
    A_LU_FUND_7,
    A_MARRIED,
    A_NO_CESSION_FEES,
    A_NO_LOSSES,
    A_PROGRESSIVE,
    A_PROPERTY_BUILT,
    A_SINGLE,
    A_SUBSTANTIAL_NO,
    A_SUBSTANTIAL_YES,
    A_TMI_FR,
    A_TMI_LU,
    W_AV_BEFORE_2017,
    W_LU_AV_EARLY_REDEMPTION,
    W_LU_IMMO_REVALUATION,
    W_MISSING_DATE,
    W_NEGATIVE_GAIN,
    W_NOT_ESTIMATED,
    W_PEA_PS_HISTORICAL,
    W_PROGRESSIVE_NO_TMI,
    W_WRAPPER_COUNTRY_MISMATCH,
    TaxInput,
    TaxLine,
    TaxResult,
    classify,
    compute,
)

__all__ = [
    "TaxInput", "TaxLine", "TaxResult", "compute", "classify",
    "W_NEGATIVE_GAIN", "W_PEA_PS_HISTORICAL", "W_AV_BEFORE_2017",
    "W_LU_IMMO_REVALUATION", "W_LU_AV_EARLY_REDEMPTION", "W_MISSING_DATE",
    "W_NOT_ESTIMATED", "W_WRAPPER_COUNTRY_MISMATCH", "W_PROGRESSIVE_NO_TMI",
    "A_TMI_FR", "A_TMI_LU", "A_NO_CESSION_FEES", "A_NO_LOSSES",
    "A_AV_PRIMES_150K", "A_AV_PRIMES_OVER_150K", "A_SUBSTANTIAL_NO",
    "A_SUBSTANTIAL_YES", "A_DECENNIAL_AVAILABLE", "A_PROPERTY_BUILT",
    "A_LU_CONTRACT", "A_LU_FUND_7", "A_MARRIED", "A_SINGLE", "A_PROGRESSIVE",
]
