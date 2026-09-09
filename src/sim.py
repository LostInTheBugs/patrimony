"""Moteurs des simulateurs (v2026.09.064) — PUR : zéro I/O, zéro dépendance.

Cadrage Fred 2026-09-09 : page « Simulateurs » élargie — projection
classique (intérêts composés), rente potentielle (certaine / à vie /
perpétuelle), en plus du moteur FIRE existant (src/fire.py, inchangé).

Conventions communes aux trois modes :

- Taux MENSUELS équivalents : r_m = (1 + r)^(1/12) - 1. Les versements et
  retraits sont mensuels, en fin de mois, en euros NOMINAUX (argent du
  jour) ; l'inflation ne sert qu'au calcul de la série RÉELLE (valeurs
  déflatées) et aux lectures « pouvoir d'achat ».
- Les moteurs n'ont pas d'horloge : les temps sont en années RELATIVES
  (la route convertit en années civiles, comme fire.py).

Fonctions exportées :

- project(principal, pmt_month, r_pct, i_pct, years) : intérêts composés +
  versements mensuels constants — séries capital nominal / réel / cumul des
  versements + stats de fin.
- rente(principal, mode, r_pct, i_pct, years, swr_pct) : rente mensuelle
  potentielle — modes : "years" (rente certaine : le capital s'épuise au
  terme), "life" (retrait du taux soutenable : capital non garanti),
  "perp" (les intérêts seuls, capital intact).
"""


def _rate_monthly(r_pct: float) -> float:
    return (1.0 + r_pct / 100.0) ** (1.0 / 12.0) - 1.0


def _deflate(amount: float, i_m: float, months: int) -> float:
    """Valeur en euros du jour d'un montant nominal versé dans `months` mois."""
    if i_m <= 0.0:
        return amount
    return amount / ((1.0 + i_m) ** months)


def project(principal, pmt_month, r_pct, i_pct, years):
    """Projection classique — intérêts composés mensuels + versement constant.

    Renvoie {labels (années relatives), nominal[], reel[], invested[],
    final_nominal, final_reel, total_pmts, interest} — un point par année
    (fin d'année), plus le point initial (année 0 = principal).
    """
    months = max(1, int(years)) * 12
    r_m = _rate_monthly(r_pct)
    i_m = _rate_monthly(i_pct)
    cap = float(principal)
    pts_n, pts_r, pts_v = [cap], [cap], [float(principal)]
    for m in range(1, months + 1):
        cap = cap * (1.0 + r_m) + pmt_month
        if m % 12 == 0:
            t = m // 12
            pts_n.append(round(cap, 2))
            pts_r.append(round(_deflate(cap, i_m, m), 2))
            pts_v.append(round(float(principal) + pmt_month * m, 2))
    total_pmts = float(principal) + pmt_month * months
    return {
        "labels": list(range(len(pts_n))),
        "nominal": pts_n,
        "reel": pts_r,
        "invested": pts_v,
        "final_nominal": round(pts_n[-1], 2),
        "final_reel": round(pts_r[-1], 2),
        "total_pmts": round(total_pmts, 2),
        "interest": round(pts_n[-1] - total_pmts, 2),
        "years": int(years),
    }


def _rent_pmt_years(principal, r_pct, years):
    """Mensualité de rente certaine (fin de mois) : capital épuisé au terme."""
    months = max(1, int(years)) * 12
    r_m = _rate_monthly(r_pct)
    if r_m <= 0.0:
        return principal / months
    return principal * r_m / (1.0 - (1.0 + r_m) ** (-months))


def rente(principal, mode, r_pct, i_pct, years, swr_pct):
    """Rente mensuelle potentielle d'un capital.

    Renvoie {pmt_month (nominal), pmt_month_reel_end (pouvoir d'achat au
    terme pour le mode "years"), pmt_year, mode, capital[] (fin d'année),
    total_payout (cumul des rentes versées)}.
    """
    principal = float(principal)
    if mode == "perp":
        r_m = _rate_monthly(r_pct)
        pmt = principal * r_m if r_m > 0.0 else 0.0
        cap_end = principal
    elif mode == "life":
        pmt = principal * max(swr_pct, 0.0) / 100.0 / 12.0
        cap_end = None  # série simulée ci-dessous
    else:  # "years" : rente certaine
        pmt = _rent_pmt_years(principal, r_pct, years)
        cap_end = 0.0

    # série du capital : simulation mensuelle (rente certaine jusqu'au terme,
    # vie sur `years`, perpétuelle sur `years` avec le capital intact)
    months = max(1, int(years)) * 12
    r_m = _rate_monthly(r_pct)
    cap = principal
    out = [principal]
    total = 0.0
    for m in range(1, months + 1):
        if mode == "years":
            cap = max(0.0, cap * (1.0 + r_m) - pmt)
        elif mode == "life":
            cap = cap * (1.0 + r_m) - pmt
        else:
            cap = cap * (1.0 + r_m) - pmt  # pmt == intérêts -> capital stable
        total += pmt
        if m % 12 == 0:
            out.append(round(cap, 2))
    if mode == "years":
        # l'échéancier de rente certaine s'arrête au terme (capital 0)
        pass

    i_m = _rate_monthly(i_pct)
    return {
        "pmt_month": round(pmt, 2),
        "pmt_month_real_end": round(_deflate(pmt, i_m, months), 2),
        "pmt_year": round(pmt * 12.0, 2),
        "mode": mode,
        "capital": out,
        "total_payout": round(total, 2),
        "cap_end": round(cap_end, 2) if cap_end is not None else round(out[-1], 2),
        "years": int(years),
    }
