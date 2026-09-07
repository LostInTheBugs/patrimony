"""Monte-Carlo FIRE par bootstrap (v2026.09.042, design validé 2026-09-07).

Domaine PUR : aucune dépendance vers src/app.py ni FastAPI. Le moteur
travaille en pas ANNUEL comme src/fire : chaque trajectoire est une suite de
rendements NOMINAUX annuels tirés d'une série réelle par bootstrap par
blocs de 12 mois (fenêtres glissantes, tirage avec remise) — l'autocorré-
lation annuelle est conservée, les cycles longs ne sont pas figés (choix
12 mois validé par Fred). L'inflation reste le paramètre du membre
(indexation des flux), comme dans la simulation déterministe.

Fonctions :
- make_blocks(levels_by_ym) : rendements annuels nominaux des fenêtres de
  12 mois calendaires [ym, ym+11m] dont les deux extrémités existent.
- simulate(...) : N trajectoires → taux de réussite par horizon (capital
  jamais <= 0), capital médian à l'horizon final, année médiane
  d'épuisement. Déterministe si seed fourni (tests, rejouabilité).
"""

import random
from statistics import median

from src import fire


def make_blocks(levels_by_ym: dict[str, float]) -> list[float]:
    """Rendements annuels nominaux des fenêtres glissantes de 12 mois
    calendaires. levels_by_ym : {YYYY-MM: niveau}. Une fenêtre de 12 mois
    = 12 rendements mensuels consécutifs = [ym, ym + 12 mois) ; elle n'est
    retenue que si ses deux extrémités existent (les trous intermédiaires
    sont sans effet : le rendement se lit aux extrémités)."""
    out = []
    for ym, l0 in levels_by_ym.items():
        if not l0 or l0 <= 0:
            continue
        y, m = int(ym[:4]), int(ym[5:7])
        ym2 = f"{y + (m + 11) // 12:04d}-{(m + 11) % 12 + 1:02d}"  # ym + 12 mois
        l1 = levels_by_ym.get(ym2)
        if l1:
            out.append(l1 / l0 - 1.0)
    return out


def _horizons(max_years: int) -> list[int]:
    """Horizons d'affichage : 10, 20, … <= max_years, plus max_years lui-même
    (déjà inclus si multiple de 10)."""
    hs = {t for t in range(10, max_years + 1, 10)}
    hs.add(max_years)
    return sorted(hs)


def simulate(principal, savings_year, expenses_year, pension_year,
             return_pct, inflation_pct, swr_pct, max_years: int = 70,
             blocks: list[float] | None = None, n_sims: int = 2000,
             seed: int | None = None) -> dict:
    """N trajectoires bootstrapées (tirage avec remise d'un bloc par année).

    Retour :
      n_sims, seed_used,
      horizons: [{t, success_pct}]  — % de trajectoires jamais épuisées ≤ t
      p50_capital_end               — capital médian à max_years (0 si épuisé)
      median_exhaustion_t           — année médiane d'épuisement (traj. en échec)
      exhausted_pct                 — % épuisées avant/à max_years
    """
    if not blocks:
        raise ValueError("série vide — pas de bloc de rendement")
    rng = random.Random(seed)
    hs = _horizons(max_years)
    finals = []
    exhausted_at: list[int] = []
    # compteurs d'échec par horizon (épuisement à une année <= h)
    fails = {h: 0 for h in hs}
    for _ in range(n_sims):
        path = [rng.choice(blocks) for _ in range(max_years)]
        out = fire.simulate(principal, savings_year, expenses_year, pension_year,
                            return_pct, inflation_pct, swr_pct, max_years,
                            returns=path)
        end_t = out["rows"][-1]["t"] if out["rows"] else max_years
        if out["exhausted"]:
            exhausted_at.append(end_t)
            finals.append(0.0)
            for h in hs:
                if end_t <= h:
                    fails[h] += 1
        else:
            finals.append(out["rows"][-1]["capital"])
    return {
        "n_sims": n_sims,
        "seed_used": seed,
        "horizons": [{"t": h, "success_pct": round((n_sims - fails[h]) / n_sims * 100, 1)}
                     for h in hs],
        "p50_capital_end": round(median(finals), 2),
        "median_exhaustion_t": round(median(exhausted_at), 1) if exhausted_at else None,
        "exhausted_pct": round(len(exhausted_at) / n_sims * 100, 1),
    }
