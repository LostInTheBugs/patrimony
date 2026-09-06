"""Moteur fiscal patrimony — PUR (aucune I/O, aucune dépendance au
dashboard ni à l'UI). Il reçoit un profil d'actif + des hypothèses de
foyer et renvoie le breakdown d'estimation « si liquidation aujourd'hui ».

Contrat (spec Fred, 06/09/2026) :
    entrée  : country, asset_class, wrapper, acquisition_date,
              acquisition_cost, current_value, losses,
              tax_options (progressive 2op/3cn),
              household_assumptions (tmi, married, ...)
    sortie  : gross_gain, losses_applied, taxable_gain, income_tax,
              social_contributions, extra_tax, estimated_net_gain,
              lines[] (chaque ligne porte l'id de règle, ex.
              FR_CTO_PFU_IR_2026), warnings[], assumptions[],
              ruleset_version.

Règle d'or (corrigenda Fred §7) : jamais de règle silencieuse — tout
point INCONFIRMÉ ou hypothèse qui affecte le montant est émis dans
warnings[]/assumptions[]. L'année fiscale est versionnée : (country,
year) → ruleset ; pas de ruleset = refus explicite de calculer.
"""

from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime

from .rules import get_ruleset

# --- codes stables warnings / assumptions (le front les traduit) ---
W_NEGATIVE_GAIN = "NEGATIVE_GAIN"
W_PEA_PS_HISTORICAL = "FR_PEA_PS_HISTORICAL"
W_AV_BEFORE_2017 = "FR_AV_BEFORE_2017"
W_LU_IMMO_REVALUATION = "LU_IMMO_REVALUATION_NI"
W_LU_AV_EARLY_REDEMPTION = "LU_AV_EARLY_REDEMPTION"
W_MISSING_DATE = "MISSING_ACQUISITION_DATE"
W_NOT_ESTIMATED = "NOT_ESTIMATED"
W_WRAPPER_COUNTRY_MISMATCH = "WRAPPER_COUNTRY_MISMATCH"
W_PROGRESSIVE_NO_TMI = "PROGRESSIVE_NO_TMI"

A_TMI_FR = "ASSUME_TMI_FR"
A_TMI_LU = "ASSUME_TMI_LU"
A_NO_CESSION_FEES = "ASSUME_NO_CESSION_FEES"
A_NO_LOSSES = "ASSUME_NO_LOSSES"
A_AV_PRIMES_150K = "ASSUME_AV_PRIMES_150K"
A_AV_PRIMES_OVER_150K = "ASSUME_AV_PRIMES_OVER_150K"
A_SUBSTANTIAL_NO = "ASSUME_SUBSTANTIAL_NO"
A_SUBSTANTIAL_YES = "ASSUME_SUBSTANTIAL_YES"
A_DECENNIAL_AVAILABLE = "ASSUME_DECENNIAL_AVAILABLE"
A_PROPERTY_BUILT = "ASSUME_PROPERTY_BUILT"
A_LU_CONTRACT = "ASSUME_LU_CONTRACT"
A_LU_FUND_7 = "ASSUME_LU_FUND_7"
A_MARRIED = "ASSUME_MARRIED"
A_SINGLE = "ASSUME_SINGLE"
A_PROGRESSIVE = "ASSUME_PROGRESSIVE_OPTION"


@dataclass
class TaxInput:
    country: str = ""            # 'fr' | 'lu'
    asset_class: str = ""        # clés serveur : comptes, epargne, bourse,
    #                              immobilier, crowdfunding, crypto, metaux, divers
    wrapper: str = ""            # '' | 'pea' | 'av' | 'cto'
    open_date: str = ""          # 'YYYY-MM-DD' (acquisition / ouverture)
    acquisition_cost: float = 0.0   # € — coût effectif (transactions) ou cost_basis
    current_value: float = 0.0      # € — dernière valorisation
    losses: float = 0.0             # € — moins-values de l'année (non saisies v1)
    year: int = 2026                # année fiscale de la cession simulée
    # --- tax_options ---
    progressive: str = ""        # '' | '2op' (option barème CTO/AV FR) |
    #                              '3cn' (crypto FR — INDÉPENDANT du 2OP)
    # --- household_assumptions ---
    tmi_fr: float = 0.0          # taux marginal FR (si option barème)
    tmi_lu: float = 0.428        # taux marginal LU (assumption A_TMI_LU)
    married: bool = False        # imposition collective (abattements ×2)
    av_primes_under_150k: bool = True   # AV FR : primes ≤ 150 k€
    substantial_holding: bool = False   # LU : participation > 10 % détenue > 6 mois
    property_built: bool = True         # FR immo : bien bâti (surtaxe due)
    asof: _date | None = None           # date de cession simulée (défaut : jour)


@dataclass
class TaxLine:
    id: str          # id de règle auditable, ex. 'FR_CTO_PFU_IR_2026'
    kind: str        # gross|losses|allowance|taxable|taxable_ps|ir|ps|extra|exempt|net
    amount: float    # € (signé : pertes/abattements négatifs)
    pct: float | None = None   # taux affiché en % (ex. 12.8)


@dataclass
class TaxResult:
    regime: str = "not_estimated"
    ruleset_version: str = ""
    gross_gain: float = 0.0
    losses_applied: float = 0.0
    taxable_gain: float | None = None
    income_tax: float = 0.0
    social_contributions: float = 0.0
    extra_tax: float = 0.0
    estimated_net_gain: float | None = None
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)


def _r2(x: float) -> float:
    return round(x + 1e-9, 2)


def _line(id_: str, kind: str, amount: float, pct: float | None = None) -> TaxLine:
    return TaxLine(id=id_, kind=kind, amount=_r2(amount), pct=pct)


def _holding_months(open_date: str, asof: _date | None) -> int | None:
    """Mois révolus entre open_date et asof (None si date invalide)."""
    if not open_date:
        return None
    try:
        od = datetime.strptime(open_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    if asof is None:
        asof = _date.today()
    months = (asof.year - od.year) * 12 + (asof.month - od.month)
    if asof.day < od.day:
        months -= 1
    return max(0, months)


def classify(country: str, asset_class: str, wrapper: str) -> str:
    """(pays, classe, enveloppe) → régime fiscal. 'not_estimated' pour les
    combinaisons hors périmètre v1 (le moteur émet alors un warning)."""
    c = country.strip().lower()
    a = asset_class.strip().lower()
    w = wrapper.strip().lower()
    if c not in ("fr", "lu"):
        return "not_estimated"
    if w == "pea":
        return "pea" if c == "fr" else "not_estimated"
    if w == "av":
        return "av"
    if w == "cto":
        return "cto" if c == "fr" else "titres"
    if a == "bourse":
        return "cto" if c == "fr" else "titres"
    if a == "epargne":
        return "livret" if c == "fr" else "titres"
    if a == "immobilier":
        return "immo"
    if a == "crypto":
        return "crypto"
    return "not_estimated"


# ------------------------------------------------------------- utilitaires
def _result(regime, rules, lines, gross, losses_applied, taxable,
            income_tax, ps, net, extra=0.0, warnings=(), assumptions=()):
    return TaxResult(
        regime=regime, ruleset_version=rules["version"],
        gross_gain=gross, losses_applied=losses_applied, taxable_gain=taxable,
        income_tax=income_tax, social_contributions=ps, extra_tax=extra,
        estimated_net_gain=net, lines=lines,
        warnings=list(warnings), assumptions=list(assumptions),
    )


def _not_estimated(inp, reason_code=W_NOT_ESTIMATED, extra_warnings=()):
    r = {"version": ""}
    try:
        r = get_ruleset(inp.country, inp.year)
    except KeyError:
        pass
    res = TaxResult(regime="not_estimated", ruleset_version=r.get("version", ""))
    res.warnings = [reason_code, *extra_warnings]
    return res


def _add_assumptions_unique(res, codes):
    for c in codes:
        if c not in res.assumptions:
            res.assumptions.append(c)


def _add_warnings_unique(res, codes):
    for c in codes:
        if c not in res.warnings:
            res.warnings.append(c)


def compute(inp: TaxInput) -> TaxResult:
    """Point d'entrée : (profil, hypothèses) → estimation complète."""
    if inp.country.strip().lower() not in ("fr", "lu"):
        return _not_estimated(inp)
    rules = get_ruleset(inp.country, inp.year)   # KeyError si non versionné
    regime = classify(inp.country, inp.asset_class, inp.wrapper)

    # Combinaisons structurellement impossibles → refus explicite.
    if inp.country == "lu" and inp.wrapper == "pea":
        return _not_estimated(inp, W_WRAPPER_COUNTRY_MISMATCH)

    if regime == "not_estimated":
        return _not_estimated(inp)

    if regime == "livret":     # épargne réglementée sans enveloppe : hors PV
        return _not_estimated(inp)

    fn = {
        "cto": _fr_cto, "pea": _fr_pea,
        "av": _fr_av if inp.country == "fr" else _lu_av,
        "immo": _fr_immo if inp.country == "fr" else _lu_immo,
        "crypto": _fr_crypto if inp.country == "fr" else _lu_crypto,
        "titres": _lu_titres,
    }[regime]
    return fn(inp, rules)


# ------------------------------------------------------------- FRANCE
def _fr_gabarit(inp, rules, ir_rate, ps_rate, ir_id, ps_id, year,
                abat_ir=0.0, abat_ir_id=None, abat_ir_pct=None,
                allow_losses=True, exo_ir_id=None, ps_rate_id=None,
                notes_ps=(), extra_tax_fn=None, ps_on=None):
    """Gabarit FR : PV brute → − MV → base → [abattement IR] → IR → PS → net.
    Les PS ne sont JAMAIS réduits par l'abattement IR. Retourne un TaxResult
    complet. ps_on : assiette PS (fraction de la base) si ≠ base."""
    lines, warns, assumps = [], [], []
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines.append(_line("GROSS_GAIN", "gross", gross))
    if gross <= 0:
        res = _result("", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_NEGATIVE_GAIN])
        return res
    losses_applied = 0.0
    if allow_losses and inp.losses > 0:
        losses_applied = _r2(min(inp.losses, gross))
        lines.append(_line("LOSSES_APPLIED", "losses", -losses_applied))
    elif allow_losses:
        assumps.append(A_NO_LOSSES)
    base = _r2(gross - losses_applied)
    base_ir = _r2(base * (1.0 - abat_ir)) if abat_ir else base
    if abat_ir:
        lines.append(_line(abat_ir_id or "ALLOWANCE_ABATTEMENT", "allowance",
                           -_r2(base * abat_ir),
                           pct=abat_ir_pct if abat_ir_pct is not None
                           else _r2(abat_ir * 100)))
    lines.append(_line("TAXABLE_GAIN", "taxable", base_ir))
    income_tax = 0.0
    if exo_ir_id:
        lines.append(_line(exo_ir_id, "exempt", 0.0))
    else:
        income_tax = _r2(base_ir * ir_rate)
        lines.append(_line(ir_id, "ir", income_tax, pct=_r2(ir_rate * 100)))
    ps_base = _r2(base * ps_on) if ps_on else base
    ps = _r2(ps_base * ps_rate)
    lines.append(_line(ps_id, "ps", ps,
                       pct=_r2((ps_rate * (ps_on or 1.0)) * 100)))
    extra = 0.0
    if extra_tax_fn:
        extra = _r2(extra_tax_fn(base_ir))
        if extra > 0:
            lines.append(_line(f"EXTRA_TAX_{year}", "extra", extra))
    net = _r2(base - income_tax - ps - extra)
    lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
    res = _result("", rules, lines, gross, losses_applied, base_ir,
                  income_tax, ps, net, extra=extra,
                  warnings=warns, assumptions=assumps)
    for w in notes_ps:
        _add_warnings_unique(res, [w])
    return res


def _fr_cto(inp, rules):
    y = inp.year
    if inp.progressive == "2op":
        if not inp.tmi_fr:
            return _not_estimated(inp, W_PROGRESSIVE_NO_TMI)
        abat = 0.0
        abat_pct = None
        if inp.open_date and inp.open_date < rules["cto_abattement_cutoff"]:
            hy = (_holding_months(inp.open_date, inp.asof) or 0) // 12
            if hy >= rules["cto_abattement_65pct_min_years"]:
                abat, abat_pct = rules["cto_abattement_65pct"], 65.0
            elif hy >= rules["cto_abattement_50pct_min_years"]:
                abat, abat_pct = rules["cto_abattement_50pct"], 50.0
        res = _fr_gabarit(inp, rules, inp.tmi_fr, rules["cto_pfu_ps"],
                          f"FR_CTO_BAREME_IR_{y}", f"FR_CTO_PFU_PS_{y}", y,
                          abat_ir=abat,
                          abat_ir_id=f"FR_CTO_ABAT_DET_{y}",
                          abat_ir_pct=abat_pct if abat else None)
        res.regime = "cto"
        _add_assumptions_unique(res, [A_TMI_FR, A_PROGRESSIVE])
        return res
    res = _fr_gabarit(inp, rules, rules["cto_pfu_ir"], rules["cto_pfu_ps"],
                      f"FR_CTO_PFU_IR_{y}", f"FR_CTO_PFU_PS_{y}", y)
    res.regime = "cto"
    _add_assumptions_unique(res, [A_NO_CESSION_FEES])
    return res


def _fr_pea(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    if hm < rules["pea_exo_ir_after_years"] * 12:
        # retrait < 5 ans = clôture : gains au PFU (12,8 + 18,6), option 2OP possible
        if inp.progressive == "2op":
            if not inp.tmi_fr:
                return _not_estimated(inp, W_PROGRESSIVE_NO_TMI)
            res = _fr_gabarit(inp, rules, inp.tmi_fr, rules["pea_ps"],
                              f"FR_PEA_BAREME_IR_{y}", f"FR_PEA_PS_{y}", y)
            res.regime = "pea"
            _add_assumptions_unique(res, [A_TMI_FR, A_PROGRESSIVE])
            return res
        res = _fr_gabarit(inp, rules, rules["pea_retrait_avant5_ir"],
                          rules["pea_ps"], f"FR_PEA_IR_AVANT5_{y}",
                          f"FR_PEA_PS_{y}", y)
        res.regime = "pea"
        _add_assumptions_unique(res, [A_NO_CESSION_FEES])
        return res
    # ≥ 5 ans : exonération IR, PS au taux en vigueur (mode current_rate)
    res = _fr_gabarit(inp, rules, 0.0, rules["pea_ps"], f"FR_PEA_EXO_IR_{y}",
                      f"FR_PEA_PS_{y}", y, exo_ir_id=f"FR_PEA_EXO_IR_{y}",
                      allow_losses=False)
    res.regime = "pea"
    if inp.open_date and inp.open_date < rules["pea_ps_historical_cutoff"]:
        _add_warnings_unique(res, [W_PEA_PS_HISTORICAL])
    _add_assumptions_unique(res, [A_NO_CESSION_FEES])
    return res


def _fr_av(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    res = None
    if inp.progressive == "2op":
        if not inp.tmi_fr:
            return _not_estimated(inp, W_PROGRESSIVE_NO_TMI)
        if hm >= rules["av_annees"] * 12:
            # ≥ 8 ans : abattement annuel 4 600/9 200 € sur la base IR
            # (jamais sur les PS), puis barème — breakdown dédié.
            abat = rules["av_abattement_couple"] if inp.married \
                else rules["av_abattement_celib"]
            return _av_bareme_avec_abattement(inp, rules, y, abat, hm)
        res = _fr_gabarit(inp, rules, inp.tmi_fr, rules["av_ps"],
                          f"FR_AV_BAREME_IR_{y}", f"FR_AV_PS_{y}", y,
                          allow_losses=False)
        res.regime = "av"
        _add_assumptions_unique(res, [A_TMI_FR, A_PROGRESSIVE])
    elif hm < rules["av_annees"] * 12:
        res = _fr_gabarit(inp, rules, rules["av_ir_avant8"], rules["av_ps"],
                          f"FR_AV_IR_AVANT8_{y}", f"FR_AV_PS_{y}", y,
                          allow_losses=False)
        res.regime = "av"
    else:
        if inp.av_primes_under_150k:
            res = _fr_gabarit(inp, rules, rules["av_ir_apres8_7p5"],
                              rules["av_ps"], f"FR_AV_IR_7P5_{y}",
                              f"FR_AV_PS_{y}", y, allow_losses=False)
            res.regime = "av"
            _add_assumptions_unique(res, [A_AV_PRIMES_150K])
        else:
            res = _fr_gabarit(inp, rules, rules["av_ir_apres8_12p8"],
                              rules["av_ps"], f"FR_AV_IR_12P8_{y}",
                              f"FR_AV_PS_{y}", y, allow_losses=False)
            res.regime = "av"
            _add_assumptions_unique(res, [A_AV_PRIMES_OVER_150K])
    if inp.open_date and inp.open_date < "2017-09-27":
        _add_warnings_unique(res, [W_AV_BEFORE_2017])
    _add_assumptions_unique(res, [A_NO_CESSION_FEES])
    return res


def _av_bareme_avec_abattement(inp, rules, y, abat, hm):
    """Option barème AV ≥ 8 ans : abattement annuel 4 600/9 200 € sur la
    base IR, PS 17,2 % sur la base entière. Construit un breakdown propre."""
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines = [_line("GROSS_GAIN", "gross", gross)]
    if gross <= 0:
        res = _result("av", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_NEGATIVE_GAIN])
        return res
    allowance = _r2(min(abat, gross))
    if allowance:
        lines.append(_line(f"FR_AV_ABATT_ANNUEL_{y}", "allowance", -allowance,
                           pct=None))
    base_ir = _r2(max(0.0, gross - allowance))
    lines.append(_line("TAXABLE_GAIN", "taxable", base_ir))
    ir = _r2(base_ir * inp.tmi_fr)
    lines.append(_line(f"FR_AV_BAREME_IR_{y}", "ir", ir,
                       pct=_r2(inp.tmi_fr * 100)))
    ps = _r2(gross * rules["av_ps"])
    lines.append(_line(f"FR_AV_PS_{y}", "ps", ps,
                       pct=_r2(rules["av_ps"] * 100)))
    net = _r2(gross - ir - ps)
    lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
    res = _result("av", rules, lines, gross, 0.0, base_ir, ir, ps, net)
    _add_assumptions_unique(res, [A_TMI_FR, A_PROGRESSIVE, A_NO_CESSION_FEES])
    if inp.open_date and inp.open_date < "2017-09-27":
        _add_warnings_unique(res, [W_AV_BEFORE_2017])
    return res


def _fr_immo(inp, rules):
    """Immo FR : prix de revient majoré (frais 7,5 % + travaux 15 % si
    > 5 ans), abattements durée IR et PS DISTINCTS, IR 19 %, PS 17,2 %,
    surtaxe > 50 k€ (bâti). Les années = périodes de 12 mois révolues."""
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    hy = hm // 12
    gross_book = _r2(inp.current_value - inp.acquisition_cost)
    if gross_book <= 0:
        res = _not_estimated(inp)
        return res
    # prix de revient majoré
    maj = rules["immo_frais_acquisition_forfait"]
    if hy > rules["immo_travaux_min_years"]:
        maj += rules["immo_travaux_forfait"]
    adjustment = _r2(inp.acquisition_cost * maj)
    pv = _r2(gross_book - adjustment)
    lines = [_line("GROSS_GAIN", "gross", gross_book)]
    if adjustment:
        lines.append(_line(f"FR_IMMO_FRAIS_TRAVAUX_{y}", "allowance",
                           -adjustment))
    if pv <= 0:
        # PV fiscale nulle ou négative : aucun impôt — le gain comptable
        # (déjà minoré de l'ajustement) est conservé intégralement.
        lines.append(_line("TAXABLE_GAIN", "taxable", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross_book))
        res = _result("immo", rules, lines, gross_book, 0.0, 0.0, 0.0, 0.0,
                      gross_book)
        _add_assumptions_unique(res, [A_PROPERTY_BUILT])
        return res
    # abattements durée de détention (années révolues)
    def _abat_ir(h):
        a = min(hy, rules["immo_exo_ir_years"]) if hy >= 22 else hy
        acc = rules["immo_abat_ir_y6_21"] * min(max(h - 5, 0), 16)
        if h >= 22:
            acc += rules["immo_abat_ir_y22"]
        return min(1.0, acc)

    def _abat_ps(h):
        acc = rules["immo_abat_ps_y6_21"] * min(max(h - 5, 0), 16)
        if h >= 22:
            acc += rules["immo_abat_ps_y22"]
        if h > 22:
            acc += rules["immo_abat_ps_y23_30"] * min(h - 22, 8)
        return min(1.0, acc)

    a_ir, a_ps = _abat_ir(hy), _abat_ps(hy)
    base_ir = _r2(pv * (1.0 - a_ir))
    base_ps = _r2(pv * (1.0 - a_ps))
    if a_ir:
        lines.append(_line(f"FR_IMMO_ABAT_IR_{y}", "allowance",
                           -_r2(pv * a_ir), pct=_r2(a_ir * 100)))
    lines.append(_line("TAXABLE_GAIN", "taxable", base_ir))
    ir = _r2(base_ir * rules["immo_ir_rate"])
    lines.append(_line(f"FR_IMMO_IR_{y}", "ir", ir,
                       pct=_r2(rules["immo_ir_rate"] * 100)))
    if a_ps and a_ps != a_ir:
        lines.append(_line(f"FR_IMMO_ABAT_PS_{y}", "allowance",
                           -_r2(pv * a_ps), pct=_r2(a_ps * 100)))
    lines.append(_line("TAXABLE_PS", "taxable_ps", base_ps))
    ps = _r2(base_ps * rules["immo_ps_rate"])
    lines.append(_line(f"FR_IMMO_PS_{y}", "ps", ps,
                       pct=_r2(rules["immo_ps_rate"] * 100)))
    # surtaxe (CGI 1609 nonies G) — biens bâtis seulement
    extra = 0.0
    if inp.property_built and base_ir > rules["immo_surtaxe_threshold"]:
        extra = _surtaxe_immo(base_ir)
        if extra > 0:
            lines.append(_line(f"FR_IMMO_SURTAXE_{y}", "extra", extra))
    net = _r2(pv - ir - ps - extra)
    lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
    res = _result("immo", rules, lines, gross_book, 0.0, base_ir, ir, ps,
                  net, extra=extra)
    _add_assumptions_unique(res, [A_PROPERTY_BUILT])
    return res


def _surtaxe_immo(pv: float) -> float:
    """Barème 2048-IMM de la taxe sur les PV immobilières élevées (formules
    légales exactes du feuillet, §FD)."""
    if pv <= 50000:
        return 0.0
    if pv <= 60000:
        return 0.02 * pv - (60000 - pv) / 20
    if pv <= 100000:
        return 0.02 * pv
    if pv <= 110000:
        return 0.03 * pv - (110000 - pv) / 10
    if pv <= 150000:
        return 0.03 * pv
    if pv <= 160000:
        return 0.04 * pv - (160000 - pv) * 0.15
    if pv <= 200000:
        return 0.04 * pv
    if pv <= 210000:
        return 0.05 * pv - (210000 - pv) * 0.20
    if pv <= 250000:
        return 0.05 * pv
    if pv <= 260000:
        return 0.06 * pv - (260000 - pv) * 0.25
    return 0.06 * pv


def _fr_crypto(inp, rules):
    y = inp.year
    if inp.progressive == "3cn":      # option INDÉPENDANTE du 2OP (case 3CN)
        if not inp.tmi_fr:
            return _not_estimated(inp, W_PROGRESSIVE_NO_TMI)
        res = _fr_gabarit(inp, rules, inp.tmi_fr, rules["crypto_pfu_ps"],
                          f"FR_CRYPTO_BAREME_IR_{y}", f"FR_CRYPTO_PS_{y}", y,
                          allow_losses=False)
        res.regime = "crypto"
        _add_assumptions_unique(res, [A_TMI_FR, A_PROGRESSIVE])
        return res
    res = _fr_gabarit(inp, rules, rules["crypto_pfu_ir"], rules["crypto_pfu_ps"],
                      f"FR_CRYPTO_PFU_IR_{y}", f"FR_CRYPTO_PFU_PS_{y}", y,
                      allow_losses=False)
    res.regime = "crypto"
    _add_assumptions_unique(res, [A_NO_CESSION_FEES])
    return res


# ------------------------------------------------------------- LUXEMBOURG
def _married_code(inp) -> str:
    return A_MARRIED if inp.married else A_SINGLE


def _lu_titres(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines = [_line("GROSS_GAIN", "gross", gross)]
    if gross <= 0:
        res = _result("titres", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_NEGATIVE_GAIN])
        _add_assumptions_unique(res, [_married_code(inp)])
        return res
    # franchise 500 €/an
    if gross < rules["titres_franchise_500"]:
        lines.append(_line("LU_FRANCHISE_500", "exempt", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
        res = _result("titres", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_assumptions_unique(res, [_married_code(inp)])
        return res
    # date requise pour trancher spéculation / exonération
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    if hm < rules["titres_speculation_months"]:
        # spéculation (< 6 mois) : « revenus nets divers », barème au taux
        # marginal d'hypothèse + fonds pour l'emploi + CADEP
        lines.append(_line("TAXABLE_GAIN", "taxable", gross))
        ir = _r2(gross * inp.tmi_lu)
        lines.append(_line(f"LU_TITRES_SPECULATION_IR_{y}", "ir", ir,
                           pct=_r2(inp.tmi_lu * 100)))
        fund = _r2(ir * rules["fonds_emploi_7pct"])
        if fund > 0:
            lines.append(_line(f"LU_FONDS_EMPLOI_{y}", "extra", fund,
                               pct=_r2(rules["fonds_emploi_7pct"] * 100)))
        cadep = _r2(gross * rules["cadep_1p4"])
        lines.append(_line(f"LU_CADEP_{y}", "ps", cadep,
                           pct=_r2(rules["cadep_1p4"] * 100)))
        net = _r2(gross - ir - fund - cadep)
        lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
        res = _result("titres", rules, lines, gross, 0.0, gross, ir,
                      cadep, net, extra=fund)
        _add_assumptions_unique(res, [A_TMI_LU, A_LU_FUND_7,
                                      A_SINGLE if not inp.married else A_MARRIED])
        return res
    # > 6 mois : exonération (participation < 10 %) ou demi-taux
    if not inp.substantial_holding:
        lines.append(_line("LU_TITRES_EXO_10PCT_6MOIS", "exempt", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
        res = _result("titres", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_assumptions_unique(res, [A_SUBSTANTIAL_NO,
                                      A_SINGLE if not inp.married else A_MARRIED])
        return res
    # participation importante (> 10 %, > 6 mois) : demi-taux + abattement
    abat = rules["titres_abattement_50000"] * (2 if inp.married else 1)
    lines.append(_line("LU_TITRES_ABATTEMENT_PARTICIPATION", "allowance",
                       -_r2(min(abat, gross))))
    base = _r2(max(0.0, gross - abat))
    lines.append(_line("TAXABLE_GAIN", "taxable", base))
    half = min(inp.tmi_lu / 2, rules["titres_half_rate_max"])
    ir = _r2(base * half)
    lines.append(_line(f"LU_TITRES_PARTICIPATION_IR_{y}", "ir", ir,
                       pct=_r2(half * 100)))
    net = _r2(gross - ir)
    lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
    res = _result("titres", rules, lines, gross, 0.0, base, ir, 0.0, net)
    _add_assumptions_unique(res, [A_SUBSTANTIAL_YES, A_TMI_LU,
                                  A_DECENNIAL_AVAILABLE,
                                  A_SINGLE if not inp.married else A_MARRIED])
    return res


def _lu_av(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines = [_line("GROSS_GAIN", "gross", gross)]
    if hm is not None and hm < 6:
        lines.append(_line("LU_AV_EXO_ART115", "exempt", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
        res = _result("av", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_LU_AV_EARLY_REDEMPTION])
        _add_assumptions_unique(res, [A_LU_CONTRACT])
        return res
    lines.append(_line("LU_AV_EXO_ART115", "exempt", 0.0))
    lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
    res = _result("av", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
    _add_assumptions_unique(res, [A_LU_CONTRACT])
    return res


def _lu_immo(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines = [_line("GROSS_GAIN", "gross", gross)]
    if gross <= 0:
        res = _result("immo", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_NEGATIVE_GAIN])
        return res
    if gross < rules["immo_franchise_500"]:
        lines.append(_line("LU_FRANCHISE_500", "exempt", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
        return _result("immo", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
    if hm < rules["immo_speculation_years"] * 12:
        # bénéfice de spéculation (≤ 5 ans) : barème + fonds + CADEP
        lines.append(_line("TAXABLE_GAIN", "taxable", gross))
        ir = _r2(gross * inp.tmi_lu)
        lines.append(_line(f"LU_IMMO_SPECULATION_IR_{y}", "ir", ir,
                           pct=_r2(inp.tmi_lu * 100)))
        fund = _r2(ir * rules["fonds_emploi_7pct"])
        if fund > 0:
            lines.append(_line(f"LU_FONDS_EMPLOI_{y}", "extra", fund,
                               pct=_r2(rules["fonds_emploi_7pct"] * 100)))
        cadep = _r2(gross * rules["cadep_1p4"])
        lines.append(_line(f"LU_CADEP_{y}", "ps", cadep,
                           pct=_r2(rules["cadep_1p4"] * 100)))
        net = _r2(gross - ir - fund - cadep)
        lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
        res = _result("immo", rules, lines, gross, 0.0, gross, ir, cadep,
                      net, extra=fund)
        _add_assumptions_unique(res, [A_TMI_LU, A_LU_FUND_7])
        return res
    # bénéfice de cession (> 5 ans) : demi-taux max 21 % + abattement
    # décennal 50/100 k€ ; réévaluation monétaire NON modélisée (warning)
    abat = rules["immo_abattement_decennal"] * (2 if inp.married else 1)
    if abat:
        lines.append(_line("LU_IMMO_ABATTEMENT_DECENNAL", "allowance",
                           -_r2(min(abat, gross))))
    base = _r2(max(0.0, gross - abat))
    lines.append(_line("TAXABLE_GAIN", "taxable", base))
    half = min(inp.tmi_lu / 2, rules["immo_cession_half_rate_max"])
    ir = _r2(base * half)
    lines.append(_line(f"LU_IMMO_CESSION_IR_{y}", "ir", ir,
                       pct=_r2(half * 100)))
    net = _r2(gross - ir)
    lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
    res = _result("immo", rules, lines, gross, 0.0, base, ir, 0.0, net)
    _add_warnings_unique(res, [W_LU_IMMO_REVALUATION])
    _add_assumptions_unique(res, [A_TMI_LU, A_DECENNIAL_AVAILABLE])
    return res


def _lu_crypto(inp, rules):
    y = inp.year
    hm = _holding_months(inp.open_date, inp.asof)
    gross = _r2(inp.current_value - inp.acquisition_cost)
    lines = [_line("GROSS_GAIN", "gross", gross)]
    if gross <= 0:
        res = _result("crypto", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
        _add_warnings_unique(res, [W_NEGATIVE_GAIN])
        return res
    if gross < rules["crypto_franchise_500"]:
        lines.append(_line("LU_FRANCHISE_500", "exempt", 0.0))
        lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
        return _result("crypto", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
    if hm is None:
        return _not_estimated(inp, W_MISSING_DATE)
    if hm < rules["crypto_speculation_months"]:
        lines.append(_line("TAXABLE_GAIN", "taxable", gross))
        ir = _r2(gross * inp.tmi_lu)
        lines.append(_line(f"LU_CRYPTO_SPECULATION_IR_{y}", "ir", ir,
                           pct=_r2(inp.tmi_lu * 100)))
        fund = _r2(ir * rules["fonds_emploi_7pct"])
        if fund > 0:
            lines.append(_line(f"LU_FONDS_EMPLOI_{y}", "extra", fund,
                               pct=_r2(rules["fonds_emploi_7pct"] * 100)))
        cadep = _r2(gross * rules["cadep_1p4"])
        lines.append(_line(f"LU_CADEP_{y}", "ps", cadep,
                           pct=_r2(rules["cadep_1p4"] * 100)))
        net = _r2(gross - ir - fund - cadep)
        lines.append(_line("ESTIMATED_NET_GAIN", "net", net))
        res = _result("crypto", rules, lines, gross, 0.0, gross, ir, cadep,
                      net, extra=fund)
        _add_assumptions_unique(res, [A_TMI_LU, A_LU_FUND_7])
        return res
    lines.append(_line("LU_CRYPTO_EXO_6MOIS", "exempt", 0.0))
    lines.append(_line("ESTIMATED_NET_GAIN", "net", gross))
    res = _result("crypto", rules, lines, gross, 0.0, 0.0, 0.0, 0.0, gross)
    _add_assumptions_unique(res, [A_SINGLE if not inp.married else A_MARRIED])
    return res
