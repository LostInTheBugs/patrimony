"""Moteur FIRE déterministe (v2026.09.034) — PUR : zéro I/O, zéro dépendance.

Modèle (cadrage Fred ②, 2026-09-05 : « simulateur déterministe — âge/date
cible, rendement nominal et réel, inflation, retraits programmés,
rentes/pensions + courbes de sensibilité », pas de Monte-Carlo) :

- Période d'accumulation : le capital croît au rendement nominal, l'épargne
  annuelle (constante réelle) est indexée sur l'inflation et versée en fin
  d'année.
- Indépendance financière (FIRE) : atteinte fin d'année t quand le capital
  nominal couvre les dépenses nettes des rentes au taux de retrait
  soutenable (règle des 4 % par défaut) : capital(t) >= (D(t)-R(t))/swr.
  Les dépenses ET les rentes sont indexées sur l'inflation (montants réels
  constants).
- Période de retrait (après le FIRE) : l'épargne cesse, le retrait annuel
  net est prélevé fin d'année. Si le retrait dépasse durablement le
  rendement réel (swr > rendement réel), le capital finit par s'épuiser :
  la série s'arrête avec exhausted=True (rien n'est masqué).
- Si les rentes couvrent déjà les dépenses (D0 <= R0), le foyer est déjà
  indépendant (fire à l'année 0).

Tout est en euros NOMINAUX (affichage « argent du jour ») ; l'inflation
sert à l'indexation et au calcul du rendement réel affiché.

Les temps sont RELATIFS (années depuis aujourd'hui) : le moteur n'a pas
d'horloge — la route convertit en années civiles.
"""


def _indexed(v0: float, i: float, t: int) -> float:
    """Valeur nominale l'année t (1re année : t=0) d'un montant réel v0."""
    return v0 * (1.0 + i) ** t


def simulate(principal, savings_year, expenses_year, pension_year,
             return_pct, inflation_pct, swr_pct, max_years=70):
    """Projection déterministe année par année.

    Retour : dict {
      real_return_pct,            # rendement réel annuel (info)
      net_expenses_year0,         # dépenses annuelles nettes des rentes (an 0)
      rows: [{t, capital, target, retired}],   # capital & cible nominaux fin d'année
      fire: {t, capital} | None,  # t = 0 si déjà indépendant
      exhausted: bool,            # capital épuisé pendant la phase retrait
      retired: bool,              # FIRE atteint dans l'horizon
    }
    """
    r = return_pct / 100.0
    i = inflation_pct / 100.0
    swr = swr_pct / 100.0
    real = (1.0 + r) / (1.0 + i) - 1.0
    net_exp = max(expenses_year - pension_year, 0.0)

    rows = []
    cap = float(principal)
    retired = False
    fire = {"t": 0, "capital": round(cap, 2)} if net_exp <= 0.0 else None
    exhausted = False
    if net_exp <= 0.0:
        retired = True  # déjà indépendant : la projection montre la retraite

    for t in range(1, max_years + 1):
        idx_prev = (1.0 + i) ** (t - 1)  # indexation de l'année qui s'achève
        if not retired:
            cap = cap * (1.0 + r) + savings_year * idx_prev
            target = net_exp * idx_prev / swr if swr > 0 else None
            if fire is None and target is not None and cap >= target:
                retired = True
                fire = {"t": t, "capital": round(cap, 2)}
        else:
            target = None
            cap = cap * (1.0 + r) - net_exp * idx_prev
            if cap < 0:
                exhausted = True
                rows.append({"t": t, "capital": 0.0,
                             "target": None, "retired": True})
                break
        rows.append({"t": t, "capital": round(cap, 2),
                     "target": None if target is None else round(target, 2),
                     "retired": retired})

    return {
        "real_return_pct": round(real * 100.0, 2),
        "net_expenses_year0": round(net_exp, 2),
        "rows": rows,
        "fire": fire,
        "exhausted": exhausted,
        "retired": retired or (fire is not None),
    }


def fire_year(principal, savings_year, expenses_year, pension_year,
              return_pct, inflation_pct, swr_pct, max_years=70):
    """Année relative du FIRE (None si jamais atteint dans l'horizon)."""
    out = simulate(principal, savings_year, expenses_year, pension_year,
                   return_pct, inflation_pct, swr_pct, max_years)
    return None if out["fire"] is None else out["fire"]["t"]


def sensitivity(principal, savings_year, expenses_year, pension_year,
                return_pct, inflation_pct, swr_pct, deltas=(-2.0, 0.0, 2.0),
                max_years=70):
    """Dates FIRE selon le rendement nominal ± deltas (points de %)."""
    out = []
    for d in deltas:
        fy = fire_year(principal, savings_year, expenses_year, pension_year,
                       return_pct + d, inflation_pct, swr_pct, max_years)
        out.append({"return_pct": round(return_pct + d, 1), "fire_t": fy})
    return out
