"""Registre des rulesets fiscaux versionnés (FR-2026, LU-2026, ...).

Une nouvelle année fiscale = un nouveau ruleset, jamais une édition de
l'ancien. Le moteur résout (country, year) → ruleset ; sans ruleset, il
refuse de calculer (erreur explicite, jamais de règle par défaut).
"""

from .rules_fr import FR_2026
from .rules_lu import LU_2026

RULESETS = {
    "FR-2026": FR_2026,
    "LU-2026": LU_2026,
}


def get_ruleset(country: str, year: int) -> dict:
    """Retourne le ruleset (country, year) ou lève KeyError si inconnu."""
    key = f"{country.upper()}-{year}"
    if key not in RULESETS:
        raise KeyError(
            f"Aucun ruleset fiscal {key} — règles versionnées uniquement "
            f"(disponibles : {', '.join(sorted(RULESETS))})"
        )
    return RULESETS[key]
