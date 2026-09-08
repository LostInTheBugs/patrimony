"""Module crypto — wallets EVM non-custodial (v2026.09.050).

Portage du moteur du « Crypto Wallet Tracker » (LostInTheBugs) dans
Patrimony : portefeuilles suivis par ADRESSE PUBLIQUE uniquement (aucune
credential), 21+ chaînes Blockscout v2 + prix DefiLlama (gratuit, sans clé).

La valeur agrégée est matérialisée dans des comptes-auto de classe crypto
(cw_wallets.account_id, valuation_mode='auto') — un compte par wallet —
pour alimenter dashboard / évolution / historique, exactement comme le
module Crowdfunding alimente sa classe.

Conventions module :
- PUR : aucune dépendance vers src.app ni FastAPI. Les connexions sqlite
  sont passées en paramètre ; les fonctions réseau sont SYNCHRONES
  (stdlib urllib) — l'appelant les exécute dans un thread (to_thread /
  ThreadPoolExecutor). Les fonctions « refresh » ouvrent leur propre
  connexion (chemin de base passé en paramètre).
- sqlite3.Row : pas de .get() → indexation par try.
- Écritures owner-scopées ; journaux par wallet ; gardes anti-course.
"""

import calendar
import datetime
import json
import logging
import math
import re
import sqlite3
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Lock

from src import fx

log = logging.getLogger("patrimony.crypto")

UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Patrimony/2026.09"),
    "Accept": "application/json",
}

# ═══════════════════════════════════════════════════════════════════
# Config chaînes (CWT) — les 3 dicts DOIVENT rester synchronisés.
# ═══════════════════════════════════════════════════════════════════

CHAINS = {
    "ethereum":   "eth.blockscout.com",
    "base":       "base.blockscout.com",
    "optimism":   "explorer.optimism.io",
    "arbitrum":   "arbitrum.blockscout.com",
    "polygon":    "polygon.blockscout.com",
    "gnosis":     "gnosis.blockscout.com",
    "zksync":     "zksync.blockscout.com",
    "celo":       "celo.blockscout.com",
    "scroll":     "scroll.blockscout.com",
    "soneium":    "soneium.blockscout.com",
    "ink":        "explorer.inkonchain.com",
    "mode":       "explorer.mode.network",
    "unichain":   "unichain.blockscout.com",
    "lisk":       "blockscout.lisk.com",
    "linea":      "api-explorer.linea.build",
    "etherlink":  "explorer.etherlink.com",
    "metis":      "andromeda-explorer.metis.io",
    "manta":      "pacific-explorer.manta.network",
    "bob":        "explorer.gobob.xyz",
    "zora":       "explorer.zora.energy",
    "worldchain": "worldchain-mainnet.explorer.alchemy.com",
    "hyperevm":   "www.hyperscan.com",
}

CHAIN_TO_LLAMA = {
    "ethereum":   "ethereum",
    "base":       "base",
    "optimism":   "optimism",
    "arbitrum":   "arbitrum",
    "polygon":    "polygon",
    "gnosis":     "xdai",       # Gnosis = xdai sur DefiLlama
    "zksync":     "era",        # zkSync = era sur DefiLlama
    "celo":       "celo",
    "scroll":     "scroll",
    "soneium":    "soneium",
    "ink":        "ink",
    "mode":       "mode",
    "unichain":   "unichain",
    "lisk":       "lisk",
    "linea":      "linea",
    "etherlink":  "etherlink",
    "metis":      "metis",
    "manta":      "manta",
    "bob":        "bob",
    "zora":       "zora",
    "worldchain": "wc",         # worldchain = wc sur DefiLlama
    "hyperevm":   "hyperliquid",
}

NATIVE_COIN = {
    "ethereum":   {"name": "Ethereum", "symbol": "ETH"},
    "base":       {"name": "Ethereum", "symbol": "ETH"},
    "optimism":   {"name": "Ethereum", "symbol": "ETH"},
    "arbitrum":   {"name": "Ethereum", "symbol": "ETH"},
    "zksync":     {"name": "Ethereum", "symbol": "ETH"},
    "scroll":     {"name": "Ethereum", "symbol": "ETH"},
    "soneium":    {"name": "Ethereum", "symbol": "ETH"},
    "ink":        {"name": "Ethereum", "symbol": "ETH"},
    "mode":       {"name": "Ethereum", "symbol": "ETH"},
    "unichain":   {"name": "Ethereum", "symbol": "ETH"},
    "lisk":       {"name": "Ethereum", "symbol": "ETH"},
    "linea":      {"name": "Ethereum", "symbol": "ETH"},
    "polygon":    {"name": "Polygon",  "symbol": "POL"},
    "gnosis":     {"name": "xDai",     "symbol": "xDAI"},
    "celo":       {"name": "Celo",     "symbol": "CELO"},
    "etherlink":  {"name": "Tezos",    "symbol": "XTZ"},
    "metis":      {"name": "Metis",    "symbol": "METIS"},
    "manta":      {"name": "Ethereum", "symbol": "ETH"},
    "bob":        {"name": "Ethereum", "symbol": "ETH"},
    "zora":       {"name": "Ethereum", "symbol": "ETH"},
    "worldchain": {"name": "Ethereum", "symbol": "ETH"},
    "hyperevm":   {"name": "Hyperliquid", "symbol": "HYPE"},
}

# Native « enveloppé » pour le pricing DefiLlama de secours (ex. HYPE ← WHYPE)
NATIVE_WRAPPED = {
    "hyperevm": "0x5555555555555555555555555555555555555555",
}

# Mapping symbole → id DefiLlama/CoinGecko (séries historiques)
SYMBOL_TO_CG = {
    "eth": "ethereum", "weth": "ethereum", "matic": "matic-network",
    "pol": "polygon-ecosystem-token",
    "usdt": "tether", "usdc": "usd-coin", "dai": "dai",
    "wbtc": "wrapped-bitcoin", "btc": "bitcoin",
    "link": "chainlink", "uni": "uniswap", "aave": "aave",
    "crv": "curve-dao-token", "snx": "synthetix-network-token",
    "mkr": "maker", "comp": "compound-governance-token",
    "grt": "the-graph", "sand": "the-sandbox", "mana": "decentraland",
    "enj": "enjincoin", "bat": "basic-attention-token", "zrx": "0x",
    "1inch": "1inch", "ldo": "lido-dao", "op": "optimism",
    "arb": "arbitrum", "ape": "apecoin", "shib": "shiba-inu",
    "pepe": "pepe", "floki": "floki", "fet": "fetch-ai",
    "rndr": "render-token", "imx": "immutable-x", "axs": "axie-infinity",
    "gmx": "gmx", "dydx": "dydx", "stg": "stargate-finance",
    "woo": "woo-network", "ens": "ethereum-name-service",
    "lrc": "loopring", "blur": "blur", "strk": "starknet",
    "ena": "ethena", "eigen": "eigenlayer",
    "jup": "jupiter-exchange-solana", "bonk": "bonk",
    "wif": "dogwifcoin", "pyth": "pyth-network",
    "celo": "celo", "cusd": "celo-dollar", "creal": "celo-real",
    "zro": "layerzero", "joe": "joe", "magic": "treasure",
    "edu": "open-campus", "ube": "ubeswap",
    "usdc.e": "usd-coin", "usdt0": "tether", "orbeth": "ethereum",
    "doge": "dogecoin", "wld": "worldcoin-wld",
    "wsteth": "wrapped-steth", "reth": "rocket-pool-eth",
    "morpho": "morpho", "sena": "sena", "thales": "thales",
    "nexo": "nexo", "adai": "aave-dai", "usde": "ethena-usde",
    "wormhole": "wormhole", "frax": "frax",
    "rseth": "kelp-dao-restaked-eth", "ezeth": "renzo-restaked-eth",
    "weeth": "wrapped-eeth", "susdc": "usd-coin", "ausdc": "usd-coin",
    "ceur": "celo-euro", "ceth": "celo", "pendle": "pendle",
    "mav": "maverick-protocol", "fluid": "fluid",
    "spectra": "spectra", "seam": "seamless-protocol",
    "logx": "logx", "hyper": "hypercycle",
    "eura": "monerium-eur-money", "usdt.e": "tether", "usd0": "usual-usd",
}

# ═══════════════════════════════════════════════════════════════════
# Spam + catégories (portages CWT — les deux copies _is_spam restent
# synchronisées ici : une seule définition par module).
# ═══════════════════════════════════════════════════════════════════

SPAM_PATTERNS = [
    "visit ", "claim ", "reward", "airdrop", "http", "t.me", ".cfd", ".cc",
    ".lat", ".lol", ".top", ".xyz", ".win", ".vip", ".club", "random",
    "you are eligible", "you received", "you won", "coupon", "giveaway",
    "visit website", "mint airdrop", "gift", "voucher", "bonus", "! ", "? ",
    "$ claim", "www.", "@", "token", "web3", "web4", "nft", "u5dc",
    "usdtclaim", "official website", "verify", "us_pool", "us_circle",
    "tronvanity",
]


def _is_spam(sym) -> bool:
    """Un symbole de jeton spam ? None-safe."""
    if not sym or not isinstance(sym, str):
        return False
    sym_lower = sym.lower()
    for p in SPAM_PATTERNS:
        if p in sym_lower:
            return True
    return False


_LST_EXACT = frozenset({
    "wsteth", "reth", "wrseth", "ezeth", "weeth", "rseth",
    "cbeth", "sfrxeth", "steth", "ankreth", "lseth",
    "sweth", "oseth", "rsteth", "msweth", "wbeth",
    "wsupereth", "reth2", "rsweth",
})
_KNOWN_BASES = frozenset({
    "usdt", "usdc", "dai", "eth", "weth", "wbtc", "op", "arb",
    "matic", "pol", "link", "uni", "aave", "crv", "snx", "wsteth",
    "reth", "cbeth", "wmatic", "maticx", "cake", "bal", "ldo",
    "sushi", "1inch", "ens", "mkr", "gno",
})
_LP_EXACT = frozenset({"uni-v2", "slp", "cake-lp", "spooky-lp", "joe-lp"})
_LP_SUFFIXES = ("-lp", "-gauge")
_VAULT_EXACT = frozenset({"yvusdc", "yvdai", "yvusdt", "yveth", "yvweth"})
_SYNTHETIC_EXACT = frozenset({
    "feusd", "hyusd", "susde", "sdai", "gho", "crvusd",
    "usde", "fdusd",
})
_DEFI_CATEGORIES = frozenset({"lending", "lp", "staked", "vault", "synthetic"})


def _token_category(symbol) -> str:
    """Classification conservatrice : wallet par défaut ; None-safe."""
    if not symbol or not isinstance(symbol, str):
        return "wallet"
    orig = symbol.strip()
    sym = orig.lower()
    if not sym:
        return "wallet"
    if sym in _LST_EXACT:
        return "staked"
    if sym.startswith("a") and len(sym) > 1 and sym[1:] in _KNOWN_BASES:
        return "lending"
    if sym.startswith("c") and len(sym) > 1 and sym[1:] in _KNOWN_BASES:
        return "lending"
    for debt_prefix in ("variabledebt", "stabledebt", "vardebt"):
        if sym.startswith(debt_prefix) and len(sym) > len(debt_prefix):
            return "lending"
    # Beefy : « moo» + majuscule sur le symbole ORIGINAL (pitfall CWT : ne
    # jamais matcher « moon »/« mo0n » — comparer sur la casse d'origine)
    if orig.startswith("moo") and len(orig) >= 4:
        if orig[3].isupper():
            return "vault"
        if orig.startswith("moobifi") or orig.startswith("moovelo"):
            return "vault"
    if sym.startswith("yv") and len(sym) > 3:
        return "vault"
    if sym.startswith("s*"):
        return "vault"
    if sym.startswith("erc") or sym.startswith("v-"):
        return "vault"
    if sym in _LP_EXACT:
        return "lp"
    for sfx in _LP_SUFFIXES:
        if sym.endswith(sfx):
            return "lp"
    if sym.endswith("crv") and len(sym) > 3:
        return "lp"
    if sym.endswith("-f") and len(sym) > 2:
        return "lp"
    if sym.startswith("vamm-") or sym.startswith("samm-"):
        return "lp"
    if sym in _SYNTHETIC_EXACT:
        return "synthetic"
    return "wallet"


def token_tid(symbol, chain, contract) -> str:
    """Identité canonique d'un jeton : adresse de contrat (minuscule) ou
    « chain:symbol » pour les natifs. Doit rester identique entre le scan
    live et la reconstruction historique."""
    c = (contract or "").strip().lower()
    return c if c else f"{(chain or '').lower()}:{(symbol or '').lower()}"


# ═══════════════════════════════════════════════════════════════════
# Petits utilitaires
# ═══════════════════════════════════════════════════════════════════

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_evm_address(s: str) -> bool:
    return bool(s and _EVM_RE.match(s.strip()))


def _norm_addr(s: str) -> str:
    return s.strip().lower()


def _http_json(url: str, timeout: float = 20.0, retries: int = 3) -> dict | None:
    """GET JSON synchrone avec retries (transitoires 5xx/timeout/429).

    Retourne None si échec définitif ; les erreurs sont loggées par
    l'appelant via le compte-rendu. BLOQUANT — threadpool uniquement."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    last_err = f"HTTP {r.status}"
                    if r.status < 500:
                        break
                else:
                    return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code < 500:
                break
        except Exception as e:  # timeout, connexion…
            last_err = str(e)[:120]
        if attempt + 1 < retries:
            time.sleep(2 ** attempt * 1.5)
    log.warning("http_json échec (%s): %s", last_err, url[:140])
    return None


def _usd_to_eur(conn, usd: float, d: str | None) -> float | None:
    """Convertit USD → EUR au taux BCE ≤ d (ou taux le plus ancien).

    Retourne None si aucun taux dispo (base fraîche, hors-ligne)."""
    if usd is None or not math.isfinite(usd):
        return None
    rate = fx.lookup(conn, "USD", d, None)
    if rate is None:
        return None
    return round(usd / rate["rate"], 2)


# ═══════════════════════════════════════════════════════════════════
# Scan portfolio (Blockscout, 21+ chaînes en parallèle)
# ═══════════════════════════════════════════════════════════════════

_MAX_TOKEN_PAGES = 10


def _scan_chain(chain: str, host: str, address: str) -> dict:
    """JETONS ERC (pages) + MONNAIE NATIVE (appel /addresses) pour une chaîne.

    Un jeton malformé ne tue pas la chaîne ; une chaîne en erreur ne tue
    pas le wallet. Retourne {chain, tokens:[…], error}."""
    tokens = []
    error = None
    try:
        # 1) monnaie native (appel dédié OBLIGATOIRE : /tokens ne la liste pas)
        native = None
        try:
            data = _http_json(f"https://{host}/api/v2/addresses/{address}", timeout=12)
            if data:
                coin_balance = data.get("coin_balance")
                if coin_balance and str(coin_balance) not in ("0", "None"):
                    meta = NATIVE_COIN.get(chain, {"name": "Native", "symbol": "?"})
                    tokens.append({
                        "name": meta["name"], "symbol": meta["symbol"],
                        "decimals": 18, "balance_raw": str(coin_balance),
                        "usd_price": float(data.get("exchange_rate") or 0),
                        "type": "native", "contract_address": "",
                        "category": "wallet", "chain": chain,
                    })
        except Exception:
            pass
        # 2) jetons ERC-20/721/1155 paginés
        url = f"https://{host}/api/v2/addresses/{address}/tokens"
        params = {"type": "ERC-20,ERC-721,ERC-1155"}
        for _page in range(_MAX_TOKEN_PAGES):
            sep = "&" if "?" in url else "?"
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            data = _http_json(f"{url}{sep}{qs}", timeout=15)
            if not data:
                if _page == 0:
                    error = "réponse vide"
                break
            for item in data.get("items") or []:
                if item is None:
                    continue
                try:
                    t = item.get("token") or {}
                    symbol = t.get("symbol") or "?"
                    if _is_spam(symbol):
                        continue
                    raw = item.get("value")
                    try:
                        amount = int(str(raw or "0")) / (10 ** int(t.get("decimals") or 18))
                    except Exception:
                        continue
                    if amount <= 0:
                        continue
                    contract = (t.get("address") or t.get("address_hash") or "")
                    tokens.append({
                        "name": t.get("name") or "Unknown", "symbol": symbol,
                        "decimals": int(t.get("decimals") or 18),
                        "balance_raw": str(raw) if raw else "0",
                        "usd_price": float(t.get("exchange_rate") or 0),
                        "type": t.get("type") or "ERC-20",
                        "contract_address": contract or "",
                        "category": "wallet", "chain": chain,
                    })
                except Exception:
                    continue
            nxt = data.get("next_page_params")
            if not nxt:
                break
            params = {**params, **nxt}
    except Exception as e:
        error = str(e)[:100]
    return {"chain": chain, "tokens": tokens, "error": error}


def _fetch_defillama_current_prices(queries: list[str]) -> dict:
    """Prix courants DefiLlama (batch ≤ 50). Chaque entrée porte SON préfixe
    : `ethereum:0x…` (jeton par chaîne) ou `coingecko:ethereum` (monnaie
    native). Retourne {clé: {price, confidence}} — clé = segment après le
    premier « : » (adresse minuscule ou id natif)."""
    out = {}
    if not queries:
        return out
    by_prefix: dict[str, list[str]] = {}
    for q in queries:
        prefix, _, rest = q.partition(":")
        if not rest:
            continue
        by_prefix.setdefault(prefix, []).append(f"{prefix}:{rest}")
    for _prefix, entries in by_prefix.items():
        for i in range(0, len(entries), 50):
            batch = entries[i:i + 50]
            addr_csv = ",".join(batch)
            url = f"https://coins.llama.fi/prices/current/{addr_csv}"
            if len(url) > 4000:
                batch = batch[: len(batch) // 2]
                addr_csv = ",".join(batch)
                url = f"https://coins.llama.fi/prices/current/{addr_csv}"
            data = _http_json(url, timeout=15, retries=2)
            if data:
                for key, cd in (data.get("coins") or {}).items():
                    price = cd.get("price") or 0
                    if price > 0:
                        addr = key.split(":", 1)[-1].lower() if ":" in key \
                            else key.lower()
                        conf = cd.get("confidence")
                        try:
                            conf = float(conf) if conf is not None else None
                        except (TypeError, ValueError):
                            conf = None
                        out[addr] = {"price": float(price), "confidence": conf}
            if i + 50 < len(entries):
                time.sleep(0.5)
    return out


def _llama_native_key(chain: str, sym: str) -> str | None:
    """Clé prix courant llama pour une monnaie native (`coingecko:<id>`) —
    DefiLlama ne connaît pas les clés « chaîne seule » sur /prices/current."""
    cg = SYMBOL_TO_CG.get((sym or "").lower())
    if not cg:
        return None
    return f"coingecko:{cg}"


def scan_portfolio(address: str) -> dict:
    """Portfolio courant d'une adresse : 21+ chaînes parallèles + prix
    courants DefiLlama pour les jetons non évalués. Retourne {tokens,
    total_usd, chain_totals, chain_count, token_count, errors}."""
    address = _norm_addr(address)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_scan_chain, c, h, address): c for c, h in CHAINS.items()}
        results = [f.result(timeout=120) for f in futures]

    items = []
    total = 0.0
    for r in results:
        for t in r["tokens"]:
            try:
                bal = int(t["balance_raw"]) / (10 ** t["decimals"])
            except Exception:
                continue
            bal = round(bal, 6)
            if bal <= 0:
                continue
            price = float(t.get("usd_price") or 0)  # exchange_rate Blockscout
            usd = bal * price
            total += usd
            sym = t.get("symbol") or "?"
            it = {
                "chain": r["chain"], "name": t.get("name") or "Unknown",
                "symbol": sym, "balance": bal, "usd_value": round(usd, 2),
                "usd_price": price, "type": t.get("type") or "ERC-20",
                "contract_address": t.get("contract_address") or "",
                "price_unknown": False, "category": _token_category(sym),
            }
            items.append(it)
    # --- prix courant DefiLlama POUR TOUS les jetons (v2026.09.053) ---
    # L'exchange_rate Blockscout est périmé (cache CMC) et, pour les jetons
    # de staking (stETH/eETH…), exprimé « par part » → valeur sous-évaluée
    # d'un facteur = taux de part. DefiLlama donne le prix de MARCHÉ réel :
    # stETH ≈ ETH (jamais « ETH / taux »). Fallback : exchange_rate.
    need: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        c = it["contract_address"]
        if c:
            key = f"{CHAIN_TO_LLAMA.get(it['chain'], it['chain'])}:{c.lower()}"
        else:
            key = _llama_native_key(it["chain"], it["symbol"])
        if key:
            need.setdefault(key, []).append(i)
    if need:
        llama = _fetch_defillama_current_prices(list(need))
        for key, idxs in need.items():
            entry = llama.get(key.split(":", 1)[-1])
            if not entry or entry["price"] <= 0:
                continue
            for i in idxs:
                it = items[i]
                new_usd = round(it["balance"] * entry["price"], 2)
                total += new_usd - it["usd_value"]
                it["usd_price"] = round(entry["price"], 6)
                it["usd_value"] = new_usd
    for it in items:
        if it["usd_price"] <= 0:
            it["price_unknown"] = True
    items.sort(key=lambda x: x["usd_value"], reverse=True)
    chain_totals: dict[str, float] = {}
    for p in items:
        chain_totals[p["chain"]] = chain_totals.get(p["chain"], 0.0) + p["usd_value"]
    errors = [{"chain": r["chain"], "error": r["error"]} for r in results if r.get("error")]
    return {
        "address": address,
        "total_usd": round(total, 2),
        "tokens": items[:1000],
        "token_count": len(items),
        "chain_count": len(chain_totals),
        "chains": {c: round(v, 2) for c, v in
                   sorted(chain_totals.items(), key=lambda x: x[1], reverse=True)},
        "errors": errors,
    }

# ═══════════════════════════════════════════════════════════════════
# Fetch des transferts (Blockscout, pagination illimitée, dédup)
# ═══════════════════════════════════════════════════════════════════

_MAX_TX_PAGES = 1000
_TX_RETRIES = 3


def fetch_transfers(conn, owner: str, wallet: sqlite3.Row) -> dict:
    """Transferts ERC-20/721/1155 du wallet, toutes chaînes, pagination
    complète. Dédup (wallet, chaîne, tx_hash, log_index) — un swap = 2
    jambes (log_index distincts), jamais fusionnées. Retourne les
    compteurs {inserted, per_chain, truncated}."""
    wid = wallet["id"]
    addr = _norm_addr(wallet["address"])
    inserted = 0
    per_chain: dict[str, int] = {}
    truncated = []
    for chain, host in CHAINS.items():
        chain_new = 0
        url = f"https://{host}/api/v2/addresses/{addr}/token-transfers"
        params = {"type": "ERC-20,ERC-721,ERC-1155"}
        page = 0
        done_chain = False
        while page < _MAX_TX_PAGES:
            sep = "&" if "?" in url else "?"
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            data = _http_json(f"{url}{sep}{qs}", timeout=30)
            if not data:
                break
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                token = item.get("token") or {}
                tx_hash = item.get("transaction_hash") or item.get("tx_hash", "")
                log_index = int(item.get("log_index") or 0)
                contract = (token.get("address") or token.get("address_hash") or "")
                if not tx_hash:
                    continue
                if conn.execute(
                    "SELECT 1 FROM cw_transfers WHERE wallet_id=? AND chain=?"
                    " AND tx_hash=? AND log_index=?",
                    (wid, chain, tx_hash, log_index)).fetchone():
                    continue
                try:
                    raw = (item.get("total") or {}).get("value", "0") or "0"
                    amount = int(str(raw)) / (10 ** int(token.get("decimals") or 18))
                except Exception:
                    amount = 0.0
                if amount <= 0:
                    continue
                symbol = token.get("symbol") or "?"
                name = token.get("name") or "Unknown"
                ts = item.get("timestamp", "")
                to_addr = ((item.get("to") or {}).get("hash") or "").lower()
                direction = "in" if to_addr == addr else "out"
                block_time = ts[:19].replace("T", " ") if ts else ""
                conn.execute(
                    "INSERT INTO cw_transfers (wallet_id, owner, tx_hash, log_index,"
                    " chain, block_time, token_symbol, token_name, token_addr,"
                    " direction, amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (wid, owner, tx_hash, log_index, chain, block_time, symbol,
                     name, contract, direction, round(amount, 12)))
                inserted += 1
                chain_new += 1
            nxt = data.get("next_page_params")
            if not nxt:
                break
            params = {**params, **nxt}
            page += 1
        if page >= _MAX_TX_PAGES:
            truncated.append(chain)
            log.warning("transferts: cap MAX_TX_PAGES atteint chaîne=%s wallet=%s",
                        chain, addr[:10])
        per_chain[chain] = chain_new
        if chain_new:
            conn.commit()
    return {"inserted": inserted, "per_chain": per_chain, "truncated": truncated}


# ═══════════════════════════════════════════════════════════════════
# Mouvements NATIFS (v2026.09.052) — correction du « fantôme ETH »
# ═══════════════════════════════════════════════════════════════════
# Le moteur CWT d'origine ne voyait que les transferts ERC-20 : les envois
# de monnaie native (value), le gaz brûlé (fees) et les mouvements
# internes (contrats) échappaient à l'historique → solde surestimé
# (fantôme). On capture désormais, par chaîne :
#   1) transactions externes : value reçue/envoyée + fee payée ;
#   2) internal transactions (v2 /internal-transactions, filtre to/from).
# Ces jambes sont insérées dans cw_transfers (symbol natif, token_addr '')
# avec des log_index SENTINELLES (≥ 1e9) pour ne jamais entrer en collision
# avec les log_index réels des transferts ERC-20 d'une même tx.

_EXT_IN_SEQ = 1_000_000_000    # jambe « valeur reçue » (tx externe)
_EXT_OUT_SEQ = 1_000_000_001   # jambe « valeur envoyée » (tx externe)
_EXT_FEE_SEQ = 1_000_000_002   # gaz brûlé par une tx partant du wallet
_INT_IN_BASE = 2_000_000_000   # + index (internal → wallet)
_INT_OUT_BASE = 3_000_000_000  # + index (internal ← wallet)


def _native_insert(conn, owner: str, wid: int, chain: str, tx_hash: str,
                   seq: int, ts: str, sym: str, name: str,
                   direction: str, amount: float) -> int:
    """Insère une jambe native si absente (dédup tx_hash+seq). 1 = inséré."""
    if conn.execute(
            "SELECT 1 FROM cw_transfers WHERE wallet_id=? AND chain=?"
            " AND tx_hash=? AND log_index=?",
            (wid, chain, tx_hash, seq)).fetchone():
        return 0
    conn.execute(
        "INSERT INTO cw_transfers (wallet_id, owner, tx_hash, log_index, chain,"
        " block_time, token_symbol, token_name, token_addr, direction, amount)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (wid, owner, tx_hash, seq, chain, ts, sym, name, "", direction,
         round(amount, 12)))
    return 1


def _wei2amt(raw) -> float:
    try:
        return int(str(raw or "0")) / 1e18
    except Exception:
        return 0.0


def fetch_native_movements(conn, owner: str, wallet: sqlite3.Row,
                           scan_chains: list[str] | None = None) -> dict:
    """Capture des mouvements de monnaie native (externes + internes) par
    chaîne présente au scan. Idempotent (sentinel log_index ≥ 1e9).
    Retourne {inserted, per_chain, errors}."""
    wid = wallet["id"]
    addr = _norm_addr(wallet["address"])
    if scan_chains is None:
        # chaînes du dernier scan (toute ligne — le scan ne garde que les
        # chaînes où le wallet a un solde > 0)
        scan_chains = [r["chain"] for r in conn.execute(
            "SELECT DISTINCT chain FROM cw_scans WHERE wallet_id=? AND"
            " scanned_at=(SELECT MAX(scanned_at) FROM cw_scans WHERE wallet_id=?)",
            (wid, wid)).fetchall()]
    inserted = 0
    per_chain: dict[str, int] = {}
    errors: list[dict] = []
    for chain in sorted(set(scan_chains or [])):
        meta = NATIVE_COIN.get(chain)
        host = CHAINS.get(chain)
        if not meta or not host:
            continue
        sym = meta["symbol"].lower()
        name = meta["name"]
        # v2026.09.052 : purge des jambes natives HÉRITÉES du moteur CWT
        # (log_index réel 0, capture partielle — seulement certains IN, ni
        # value OUT, ni fees) pour une représentation unique : seules les
        # jambes sentinelles ≥ 1e9 (externes + internal + gaz) subsistent.
        # Les transferts ERC-20 ont toujours token_addr non vide → intacts.
        conn.execute(
            "DELETE FROM cw_transfers WHERE wallet_id=? AND chain=? AND"
            " token_addr='' AND log_index<1000000000 AND LOWER(token_symbol)=?"
            " AND direction<>''", (wid, chain, sym))
        conn.commit()
        chain_new = 0
        try:
            # 1) transactions externes (value + fee)
            base = f"https://{host}/api/v2/addresses/{addr}/transactions"
            params = {"items_count": "50"}
            for _page in range(_MAX_TX_PAGES * 2):
                sep = "&" if "?" in base else "?"
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                data = _http_json(f"{base}{sep}{qs}", timeout=30)
                if not data:
                    break
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    tx_hash = item.get("hash") or ""
                    ts = (item.get("timestamp") or "")[:19].replace("T", " ")
                    fh = ((item.get("from") or {}).get("hash") or "").lower()
                    th = ((item.get("to") or {}).get("hash") or "").lower()
                    if not tx_hash or (fh != addr and th != addr):
                        continue
                    value = _wei2amt(item.get("value"))
                    fee = _wei2amt((item.get("fee") or {}).get("value"))
                    if fh == addr and th == addr:
                        # auto-envoi : la valeur se compense, seul le gaz brûle
                        if fee > 0:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash, _EXT_FEE_SEQ,
                                ts, sym, name, "out", fee)
                    else:
                        if th == addr and value > 0:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash, _EXT_IN_SEQ,
                                ts, sym, name, "in", value)
                        if fh == addr and value > 0:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash, _EXT_OUT_SEQ,
                                ts, sym, name, "out", value)
                        if fh == addr and fee > 0:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash, _EXT_FEE_SEQ,
                                ts, sym, name, "out", fee)
                nxt = data.get("next_page_params")
                if not nxt:
                    break
                params = {**params, **nxt}
            # 2) internal transactions (2 filtres)
            for filt, base_seq, dirn in (("to", _INT_IN_BASE, "in"),
                                         ("from", _INT_OUT_BASE, "out")):
                base = f"https://{host}/api/v2/addresses/{addr}" \
                       "/internal-transactions"
                params = {"filter": filt, "items_count": "50"}
                for _page in range(_MAX_TX_PAGES * 2):
                    sep = "&" if "?" in base else "?"
                    qs = "&".join(f"{k}={v}" for k, v in params.items())
                    data = _http_json(f"{base}{sep}{qs}", timeout=30)
                    if not data:
                        break
                    items = data.get("items") or []
                    if not items:
                        break
                    for item in items:
                        tx_hash = (item.get("transaction_hash")
                                   or item.get("tx_hash") or "")
                        ts = (item.get("timestamp") or "")[:19].replace("T", " ")
                        fh = ((item.get("from") or {}).get("hash") or "").lower()
                        th = ((item.get("to") or {}).get("hash") or "").lower()
                        idx = item.get("index")
                        if idx is None or not tx_hash:
                            continue  # pas d'index stable → on laisse l'externe
                        try:
                            idx = int(idx)
                        except Exception:
                            continue
                        value = _wei2amt(item.get("value"))
                        if value <= 0:
                            continue
                        if dirn == "in" and th == addr and fh != addr:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash,
                                base_seq + idx, ts, sym, name, "in", value)
                        elif dirn == "out" and fh == addr and th != addr:
                            chain_new += _native_insert(
                                conn, owner, wid, chain, tx_hash,
                                base_seq + idx, ts, sym, name, "out", value)
                    nxt = data.get("next_page_params")
                    if not nxt:
                        break
                    params = {**params, **nxt}
        except Exception as e:
            errors.append({"chain": chain, "error": str(e)[:160]})
            log.warning("native: chaîne %s ignorée (%s)", chain, e)
        per_chain[chain] = chain_new
        if chain_new:
            inserted += chain_new
            conn.commit()
    return {"inserted": inserted, "per_chain": per_chain, "errors": errors}


# ═══════════════════════════════════════════════════════════════════
# Séries de prix historiques (DefiLlama) + cache + enrichissement
# ═══════════════════════════════════════════════════════════════════

_WINDOW_DAYS = 200  # DefiLlama : limite 500 points par batch → 200 j max


def _day_ts_ms(d_iso: str) -> int:
    """Minuit→midi UTC (calendar.timegm — JAMAIS datetime.timestamp(), qui
    est local) puis ms. Alignement avec les séries DefiLlama (UTC)."""
    dt = datetime.datetime.strptime(d_iso, "%Y-%m-%d")
    return (calendar.timegm(dt.timetuple()) + 43200) * 1000


def _load_price_cache(conn, sym_lower: str) -> dict:
    """{ts_ms: price} depuis cw_price_cache (vide si rien)."""
    out = {}
    for r in conn.execute(
            "SELECT date, price_usd FROM cw_price_cache WHERE token_symbol=?"
            " ORDER BY date", (sym_lower,)):
        try:
            out[_day_ts_ms(r["date"])] = r["price_usd"]
        except Exception:
            continue
    return out


def _save_price_cache(conn, sym_lower: str, prices: dict) -> None:
    """INSERT OR REPLACE idempotent (reprise : seules les dates manquantes
    sont re-fetchées)."""
    for ts_ms, price in prices.items():
        d_iso = datetime.datetime.fromtimestamp(ts_ms / 1000,
                                                datetime.timezone.utc).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT OR REPLACE INTO cw_price_cache (token_symbol, date, price_usd)"
            " VALUES (?,?,?)", (sym_lower, d_iso, price))


def _fetch_defillama_series(sym_lower: str, cg_id: str, from_ts: int,
                            to_ts: int) -> tuple[bool, dict]:
    """Série journalière DefiLlama pour UN jeton, fenêtres glissantes de
    200 j, retry 3×. Retourne (ok, {ts_ms: price})."""
    points: dict[int, float] = {}
    ok = False
    window_start = from_ts
    while window_start < to_ts:
        window_end = min(to_ts, window_start + _WINDOW_DAYS * 86400)
        span = max(1, (window_end - window_start) // 86400)
        url = (f"https://coins.llama.fi/chart/coingecko:{cg_id}"
               f"?start={window_start}&span={span}&period=1d")
        data = None
        for attempt in range(3):
            data = _http_json(url, timeout=30, retries=1)
            if data is not None:
                break
            time.sleep(2 ** attempt * (3 if attempt == 0 and data is None else 1))
        if not data:
            return False, {}
        coin = (data.get("coins") or {}).get(f"coingecko:{cg_id}", {})
        for pt in coin.get("prices") or []:
            # DefiLlama renvoie des timestamps en SECONDES
            points[int(pt["timestamp"]) * 1000] = float(pt["price"])
        ok = True
        window_start = window_end
        if window_start < to_ts:
            time.sleep(1.0)
    return ok, points


def fetch_prices_and_enrich(conn, owner: str, wallet_id: int) -> dict:
    """Séries DefiLlama manquantes (cache) + enrichissement usd_price des
    transferts non évalués. Retourne le compte-rendu {mapped, unmapped,
    degraded, calls_ok, calls_failed, enriched}."""
    symbols = [r["sym"] for r in conn.execute(
        "SELECT DISTINCT LOWER(token_symbol) AS sym FROM cw_transfers"
        " WHERE wallet_id=?", (wallet_id,))]
    mapped: dict[str, str] = {}
    unmapped = []
    for s in symbols:
        cg = SYMBOL_TO_CG.get(s)
        if cg:
            mapped[s] = cg
        else:
            unmapped.append(s.upper())
    prices: dict[str, dict] = {}
    calls_ok = calls_failed = 0

    # borne basse : 1er transfert − 1 j (UTC)
    first = conn.execute(
        "SELECT MIN(block_time) AS b FROM cw_transfers WHERE wallet_id=?",
        (wallet_id,)).fetchone()["b"]
    now_ts = int(time.time())
    if first:
        try:
            from_ts = calendar.timegm(time.strptime(first[:10], "%Y-%m-%d")) - 86400
        except Exception:
            from_ts = now_ts - 365 * 86400
    else:
        from_ts = now_ts - 365 * 86400

    # cache d'abord
    for s in list(mapped.keys()):
        cached = _load_price_cache(conn, s)
        if cached:
            prices[s] = cached
            del mapped[s]

    # tri par valeur décroissante (importance → survie aux limites)
    if mapped:
        val_rows = conn.execute(
            "SELECT LOWER(token_symbol) AS s, SUM(usd_value) AS v FROM cw_transfers"
            " WHERE wallet_id=? AND usd_value>0 GROUP BY s", (wallet_id,)).fetchall()
        sym_vals = {r["s"]: r["v"] for r in val_rows}
        mapped = dict(sorted(mapped.items(),
                             key=lambda kv: sym_vals.get(kv[0], 0), reverse=True))

    degraded = []
    for s, cg in mapped.items():
        ok, points = _fetch_defillama_series(s, cg, from_ts, now_ts)
        if ok and points:
            _save_price_cache(conn, s, points)
            prices[s] = points
            calls_ok += 1
        else:
            degraded.append(s.upper())
            calls_failed += 1
        conn.commit()
        time.sleep(1.5)  # politesse DefiLlama entre jetons

    # enrichissement des transferts sans prix (interpolation ≤ timestamp,
    # avant 1er point → prix le plus ancien)
    enriched = 0
    rows = conn.execute(
        "SELECT id, LOWER(token_symbol) AS sym, amount, block_time FROM cw_transfers"
        " WHERE wallet_id=? AND usd_price<=0", (wallet_id,)).fetchall()
    for r in rows:
        series = prices.get(r["sym"])
        if not series:
            continue
        try:
            ts_ms = calendar.timegm(
                time.strptime(r["block_time"][:19], "%Y-%m-%d %H:%M:%S")) * 1000
        except Exception:
            continue
        keys = sorted(series.keys())
        idx = bisect_right(keys, ts_ms) - 1
        price = series[keys[0]] if idx < 0 else series[keys[idx]]
        if not price or price <= 0:
            continue
        conn.execute(
            "UPDATE cw_transfers SET usd_price=?, usd_value=? WHERE id=?",
            (price, round(r["amount"] * price, 2), r["id"]))
        enriched += 1
    conn.commit()
    return {"mapped": sorted(prices.keys()),
            "unmapped": sorted(set(unmapped)),
            "degraded": sorted(set(degraded)),
            "calls_ok": calls_ok, "calls_failed": calls_failed,
            "enriched": enriched}


# ═══════════════════════════════════════════════════════════════════
# Reconstruction de la série journalière (timeline unifiée — port CWT)
# ═══════════════════════════════════════════════════════════════════

def _build_timeline(first_date: str, last_date: str) -> list[str]:
    out = []
    cur = datetime.datetime.strptime(first_date, "%Y-%m-%d")
    end = datetime.datetime.strptime(last_date, "%Y-%m-%d")
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _normalize_prices(timeline: list[str], sym: str, sorted_prices: dict,
                      fallback: dict, current: dict) -> list[float]:
    """Prix alignés sur la timeline (un par jour, jamais NaN/None).
    Sources : série datée → prix tx → prix courant ; backward-extrapolation
    du premier prix connu."""
    result: list[float] = []
    sp = sorted_prices.get(sym, [])
    static = fallback.get(sym, 0.0) or current.get(sym, 0.0)
    idx = 0
    last = 0.0
    for day_str in timeline:
        day_ts = _day_ts_ms(day_str)
        while idx < len(sp) and sp[idx][0] <= day_ts:
            last = sp[idx][1]
            idx += 1
        if last > 0:
            result.append(float(last))
        elif static > 0:
            result.append(float(static))
        else:
            result.append(0.0)
    if sp:
        first_known = sp[0][1] if sp[0][1] > 0 else (static if static > 0 else None)
        if first_known:
            for i in range(len(result)):
                if result[i] > 0:
                    break
                result[i] = float(first_known)
    return result


def rebuild_wallet_series(conn, owner: str, wallet_id: int,
                          scan_items: list | None = None) -> dict:
    """Reconstruit cw_history (agrégat + lignes par jeton) du wallet à
    partir de cw_transfers + cw_price_cache. Idempotent (DELETE + INSERT
    par wallet). `scan_items` = jetons du dernier scan (ancrage : les
    jetons non détenus aujourd'hui n'inflentent pas l'historique ; les
    orphelins — valeur au portfolio sans transfert — sont injectés au
    1er jour, PNL neutre).

    Porté fidèlement de CWT (pnl_service._rebuild_history) : timeline
    contiguë, coût moyen pondéré par JETON (retraits au coût moyen),
    ancrage portfolio, clamp solde ≥ 0, backward-extrapolation prix."""
    rows = conn.execute(
        "SELECT token_symbol, token_name, token_addr, chain, amount, usd_price,"
        " direction, block_time FROM cw_transfers WHERE wallet_id=?"
        " ORDER BY block_time ASC", (wallet_id,)).fetchall()
    if not rows:
        return {"ok": True, "days": 0, "rows": 0, "unmapped": [], "degraded": []}

    # identité par jeton (contrat ; sinon chain:symbol) + séries par symbole
    tid_sym: dict[str, str] = {}
    tid_chain: dict[str, str] = {}
    for tx in rows:
        tid = token_tid(tx["token_symbol"], tx["chain"], tx["token_addr"])
        tid_sym.setdefault(tid, (tx["token_symbol"] or "").lower())
        tid_chain.setdefault(tid, tx["chain"])

    # séries cache (niveau symbole) → triées
    sorted_sym: dict[str, list] = {}
    for s in sorted({v for v in tid_sym.values()}):
        cached = _load_price_cache(conn, s)
        if cached:
            sorted_sym[s] = sorted(cached.items())

    fallback: dict[str, float] = {}
    tx_points: dict[str, dict] = defaultdict(dict)
    for tx in rows:
        if not tx["usd_price"] or tx["usd_price"] <= 0:
            continue
        tid = token_tid(tx["token_symbol"], tx["chain"], tx["token_addr"])
        fallback[tid] = tx["usd_price"]
        try:
            ts = calendar.timegm(time.strptime(tx["block_time"][:19],
                                               "%Y-%m-%d %H:%M:%S")) * 1000
            tx_points[tid][ts] = tx["usd_price"]
        except Exception:
            continue

    sorted_prices: dict[str, list] = {}
    for tid, sym in tid_sym.items():
        if tx_points.get(tid):
            sorted_prices[tid] = sorted(tx_points[tid].items())
        elif sym in sorted_sym:
            sorted_prices[tid] = sorted_sym[sym]

    # ancrage portfolio courant (dernier scan) + orphelins
    current_prices: dict[str, float] = {}
    current_values: dict[str, float] = {}
    current_tids: set = set()
    if scan_items is None:
        scan_items = [dict(r) for r in conn.execute(
            "SELECT chain, symbol, name, token_addr, balance, usd_price,"
            " usd_value FROM cw_scans WHERE wallet_id=?"
            " AND scanned_at=(SELECT MAX(scanned_at) FROM cw_scans WHERE wallet_id=?)",
            (wallet_id, wallet_id)).fetchall()]
    for t in scan_items or []:
        tid = token_tid(t.get("symbol"), t.get("chain"), t.get("token_addr") or "")
        tid_sym.setdefault(tid, (t.get("symbol") or "").lower())
        tid_chain.setdefault(tid, t.get("chain"))
        price = t.get("usd_price") or 0
        val = t.get("usd_value") or 0
        if price > 0:
            current_prices[tid] = price
        if val > 0:
            current_values[tid] = val
            current_tids.add(tid)

    first_date = rows[0]["block_time"][:10]
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    timeline = _build_timeline(first_date, today)
    n_days = len(timeline)

    daily_deltas: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    excluded: set = set()

    def _keep(tid: str) -> bool:
        sym = tid_sym.get(tid, "")
        if SYMBOL_TO_CG.get(sym):
            return True
        if _is_spam(sym):
            return False
        return tid in fallback or current_prices.get(tid, 0) > 0

    tx_tids = {token_tid(tx["token_symbol"], tx["chain"], tx["token_addr"]) for tx in rows}
    for tid, val in current_values.items():
        if tid not in tx_tids and _keep(tid):
            price = current_prices.get(tid, 0)
            if price > 0 and val / price > 0:
                daily_deltas[first_date][tid] += val / price
                fallback.setdefault(tid, price)
    for tx in rows:
        tid = token_tid(tx["token_symbol"], tx["chain"], tx["token_addr"])
        if tid in excluded or not _keep(tid):
            excluded.add(tid)
            continue
        amt = tx["amount"] or 0
        if tx["direction"] == "in":
            daily_deltas[tx["block_time"][:10]][tid] += amt
        else:
            daily_deltas[tx["block_time"][:10]][tid] -= amt

    active: set = set()
    for tid in tx_tids:
        if tid not in excluded:
            active.add(tid)
    for tid in current_values:
        if tid not in excluded:
            active.add(tid)

    price_matrix: dict[str, list[float]] = {}
    missing_slots = 0
    for tid in active:
        p = _normalize_prices(timeline, tid, sorted_prices, fallback, current_prices)
        price_matrix[tid] = p
        missing_slots += sum(1 for x in p if x == 0.0)

    balances: dict[str, float] = defaultdict(float)
    costs: dict[str, float] = defaultdict(float)
    daily_rows: list = []
    values_all: list[float] = []
    skipped: set = set()

    for day_idx, dstr in enumerate(timeline):
        day_deltas = daily_deltas.get(dstr, {})
        for tid, delta in day_deltas.items():
            old = balances[tid]
            day_price = (price_matrix[tid][day_idx] if tid in price_matrix
                         else fallback.get(tid, current_prices.get(tid, 0.0)))
            if delta > 0 and old >= 0:
                costs[tid] += delta * max(0.0, day_price)
            elif delta < 0 and old > 0:
                avg = costs[tid] / old if old > 0 else 0.0
                costs[tid] = max(0.0, costs[tid] - abs(delta) * avg)
            balances[tid] = max(0.0, old + delta)
            if balances[tid] == 0:
                costs[tid] = 0.0
        value = 0.0
        per_tok: dict[str, float] = {}
        for tid in active:
            bal = balances.get(tid, 0.0)
            if bal <= 0 or tid in excluded:
                continue
            if current_tids and tid not in current_tids:
                continue
            if tid in price_matrix:
                p = price_matrix[tid][day_idx]
            else:
                p = fallback.get(tid, current_prices.get(tid, 0.0))
            if p <= 0:
                continue
            tv = round(bal * p, 2)
            if not math.isfinite(tv):
                skipped.add(tid)
                continue
            value += tv
            per_tok[tid] = tv
        if not math.isfinite(value):
            value = 0.0
        values_all.append(value)
        net_flows = 0.0
        for tid, delta in day_deltas.items():
            p = (price_matrix[tid][day_idx] if tid in price_matrix
                 else fallback.get(tid, current_prices.get(tid, 0.0)))
            f = delta * p
            if math.isfinite(f):
                net_flows += f
        cost = sum(costs.values())
        if not math.isfinite(cost) or cost < 0:
            cost = 0.0
        daily_rows.append((wallet_id, owner, dstr, round(value, 2),
                           round(cost, 2), round(net_flows, 2), None, None))
        for tid, tv in per_tok.items():
            daily_rows.append((wallet_id, owner, dstr, tv,
                               round(costs.get(tid, 0.0), 2), 0,
                               tid_sym.get(tid), tid_chain.get(tid)))

    conn.execute("DELETE FROM cw_history WHERE wallet_id=?", (wallet_id,))
    conn.executemany(
        "INSERT INTO cw_history (wallet_id, owner, date, value_usd, cost_usd,"
        " net_flows_usd, token_symbol, chain) VALUES (?,?,?,?,?,?,?,?)",
        daily_rows)
    conn.commit()

    valid = [v for v in values_all if v > 0]
    log.info("cw rebuild wallet=%s jours=%d lignes=%d min=%s max=%s",
             wallet_id, n_days, len(daily_rows),
             round(min(valid), 2) if valid else 0,
             round(max(valid), 2) if valid else 0)
    return {"ok": True, "days": n_days, "rows": len(daily_rows),
            "skipped": sorted(skipped), "missing_price_slots": missing_slots,
            "first_date": first_date, "last_date": today}

# ═══════════════════════════════════════════════════════════════════
# Intégration patrimoine (comptes-auto classe crypto) — miroir cf
# ═══════════════════════════════════════════════════════════════════

CW_ACC_NOTE = ("Valeur calculée par le module Crypto (wallets non-custodial) "
               "— ne pas éditer manuellement.")


def is_cw_account(conn, aid: int) -> bool:
    """Un compte-auto dérivé d'un wallet du module ?"""
    return conn.execute(
        "SELECT 1 FROM cw_wallets WHERE account_id=?", (aid,)).fetchone() is not None


def _month_ends(first_ym: str, today: date) -> list[date]:
    """Fins de mois entre (premier mois) et (mois précédent) inclus ; la fin
    du mois courant n'est incluse QUE si le mois est terminé (aujourd'hui =
    dernier jour). Règle cf : jamais de point futur."""
    y, m = int(first_ym[:4]), int(first_ym[5:7])
    out = []
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = date(ny, nm, 1) - timedelta(days=1)
        if end <= today:
            out.append(end)
        y, m = ny, nm
    return out


def _history_point(conn, wallet_id: int, day: date) -> tuple[float, float] | None:
    """(valeur, coût) agrégats du wallet au jour `day` (dernière ligne
    dispo ≤ day)."""
    r = conn.execute(
        "SELECT value_usd, cost_usd FROM cw_history WHERE wallet_id=?"
        " AND date<=? AND token_symbol IS NULL ORDER BY date DESC LIMIT 1",
        (wallet_id, day.isoformat())).fetchone()
    if r is None or r["value_usd"] is None:
        return None
    return (r["value_usd"] or 0.0), (r["cost_usd"] or 0.0)


def _ensure_fx_usd(conn) -> bool:
    """Taux USD BCE dispo ? Sinon un fetch quotidien unique (threadpool —
    jamais appelé depuis l'event loop). Retourne True si dispo ensuite."""
    if fx.lookup(conn, "USD", date.today().isoformat(), None) is not None:
        return True
    try:
        rates = fx.fetch_daily(UA)
        fx.store_daily(conn, rates)
        return True
    except Exception:
        return False


def refresh_integration(conn, owner: str, today: date | None = None) -> dict:
    """Matérialise chaque wallet en compte-auto de classe crypto (nom
    « {label} (non-custodial) ») : valeur actuelle EUR + grille fin-de-mois
    (séries journalières USD converties au taux BCE ≤ date). Idempotent —
    les valorisations source='cw' sont recréées à chaque passe.

    Retourne {ok, wallets: n, valuations: n} (aucune levée si fx absent :
    la passe est alors sans valorisations EUR, wallet inchangé)."""
    today = today or date.today()
    n_acc = n_val = 0
    for w in conn.execute(
            "SELECT * FROM cw_wallets WHERE owner=? ORDER BY id", (owner,)):
        # série dispo ?
        agg = conn.execute(
            "SELECT MIN(date) AS d0, MAX(date) AS d1 FROM cw_history"
            " WHERE wallet_id=? AND token_symbol IS NULL", (w["id"],)).fetchone()
        if agg is None or not agg["d0"]:
            continue
        # compte-auto (recréé si l'id référence un compte supprimé)
        aid = w["account_id"]
        if aid is None or conn.execute(
                "SELECT id FROM accounts WHERE id=?", (aid,)).fetchone() is None:
            acc_name = w["label"] if w["demo"] else f"{w['label']} (non-custodial)"
            cur = conn.execute(
                "INSERT INTO accounts (owner, name, asset_class, institution,"
                " cost_basis, open_date, notes, valuation_mode)"
                " VALUES (?,?,?,?,?,?,?,'auto')",
                (owner, acc_name, "crypto", "wallet",
                 0, agg["d0"], CW_ACC_NOTE))
            aid = cur.lastrowid
            conn.execute("UPDATE cw_wallets SET account_id=? WHERE id=?",
                         (aid, w["id"]))
        n_acc += 1
        # conversions EUR (rates BCE ; un seul fetch si base fraîche)
        if not _ensure_fx_usd(conn):
            log.warning("cw integration %s: taux USD BCE indisponible — pas de"
                        " valorisations EUR", owner)
            return {"ok": False, "reason": "fx_unavailable",
                    "wallets": n_acc, "valuations": 0}
        today_v = None
        today_cost = None
        last_r = None
        # point « aujourd'hui » : la VALEUR SCANNÉE fait foi (balances
        # on-chain) si le wallet a été rafraîchi aujourd'hui ; sinon le
        # dernier jour de série (refresh du jour) ; jamais de point futur.
        scan_v = (w["last_value_usd"] or 0.0) if w["last_value_usd"] else None
        if scan_v is not None and (w["last_refresh"] or "")[:10] == today.isoformat():
            today_v = round(scan_v, 2)
            today_cost = w["last_cost_usd"] or None
        if today_v is None:
            last_r = conn.execute(
                "SELECT date, value_usd, cost_usd FROM cw_history WHERE wallet_id=?"
                " AND token_symbol IS NULL ORDER BY date DESC LIMIT 1",
                (w["id"],)).fetchone()
            if last_r and last_r["date"] == today.isoformat():
                today_v = last_r["value_usd"]
                today_cost = last_r["cost_usd"]
        # grille fin de mois (mois passés)
        vals: list[tuple[str, float]] = []
        for end in _month_ends(agg["d0"][:7], today):
            pt = _history_point(conn, w["id"], end)
            if pt and pt[0] > 0:
                vals.append((end.isoformat(), pt[0]))
        # coût courant EUR (aujourd'hui) pour le compte
        cost_eur = None
        cost_usd = today_cost
        if cost_usd is None and last_r:
            cost_usd = last_r["cost_usd"]
        if cost_usd is not None:
            cost_eur = _usd_to_eur(conn, cost_usd, today.isoformat())
        conn.execute(
            "UPDATE accounts SET cost_basis=?, updated_at=? WHERE id=?",
            (cost_eur or 0, now_iso(), aid))
        # valorisations recréées (source='cw')
        conn.execute("DELETE FROM valuations WHERE account_id=? AND source='cw'",
                     (aid,))
        if today_v is not None and today_v > 0:
            tv_eur = _usd_to_eur(conn, today_v, today.isoformat())
            if tv_eur is not None:
                conn.execute(
                    "INSERT INTO valuations (account_id, val_date, value, source,"
                    " note) VALUES (?,?,?, 'cw', 'module crypto wallets')",
                    (aid, today.isoformat(), tv_eur))
                n_val += 1
        for d_iso, v_usd in vals:
            v_eur = _usd_to_eur(conn, v_usd, d_iso)
            if v_eur is None:
                continue
            conn.execute(
                "INSERT INTO valuations (account_id, val_date, value, source,"
                " note) VALUES (?,?,?, 'cw', 'module crypto wallets')",
                (aid, d_iso, v_eur))
            n_val += 1
        # dernier état wallet
        conn.execute(
            "UPDATE cw_wallets SET first_date=COALESCE(first_date,?), last_date=?,"
            " status='ok' WHERE id=?",
            (agg["d0"], agg["d1"], w["id"]))
    return {"ok": True, "wallets": n_acc, "valuations": n_val}


def remove_wallet(conn, owner: str, wallet_id: int) -> bool:
    """Supprime un wallet ET son compte-auto dérivé (valuations cascadées).
    Retourne False si absent / pas au owner."""
    w = conn.execute("SELECT * FROM cw_wallets WHERE id=? AND owner=?",
                     (wallet_id, owner)).fetchone()
    if w is None:
        return False
    if w["account_id"]:
        aid = w["account_id"]
        # suppression explicite (indépendante du PRAGMA foreign_keys)
        conn.execute("DELETE FROM valuations WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
    conn.execute("DELETE FROM cw_wallets WHERE id=?", (wallet_id,))
    return True


# ═══════════════════════════════════════════════════════════════════
# Rafraîchissement complet d'un wallet (réseau — threadpool uniquement)
# ═══════════════════════════════════════════════════════════════════

# État de progression par owner (mono-process) — l'event loop lit, le
# thread worker écrit ; dict + Lock.
_BUSY: set[str] = set()
_BUSY_LOCK = Lock()
STATE: dict[str, dict] = {}


def _state_set(owner: str, **kw) -> None:
    s = STATE.setdefault(owner, {"state": "idle"})
    s.update(kw)


def refresh_claimed(owner: str) -> bool:
    with _BUSY_LOCK:
        if owner in _BUSY:
            return False
        _BUSY.add(owner)
    _state_set(owner, state="working")
    return True


def refresh_release(owner: str) -> None:
    with _BUSY_LOCK:
        _BUSY.discard(owner)


def wallet_status(conn, owner: str, wallet_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cw_wallets WHERE id=? AND owner=?",
                        (wallet_id, owner)).fetchone()


def refresh_wallet(conn, owner: str, wallet_id: int,
                   full: bool = True) -> dict:
    """Passe complète d'un wallet : scan → transferts → prix → rebuild →
    intégration. `full=False` = intégration seule (sans réseau)."""
    w = wallet_status(conn, owner, wallet_id)
    if w is None:
        raise ValueError("wallet introuvable")
    if w["demo"]:
        return {"wallet": wallet_id, "demo": True, "ok": True}
    report: dict = {"wallet": wallet_id, "label": w["label"]}
    # 1) scan portfolio courant — la VALEUR COURANTE fait foi (balances
    # réelles on-chain) ; l'historique reconstruit reste une estimation
    _state_set(owner, wallet_id=wallet_id, step="scan")
    scan = scan_portfolio(w["address"])
    conn.execute("DELETE FROM cw_scans WHERE wallet_id=?", (wallet_id,))
    ts = now_iso()
    for t in scan["tokens"]:
        conn.execute(
            "INSERT INTO cw_scans (wallet_id, owner, scanned_at, chain, symbol,"
            " name, category, token_addr, balance, usd_price, usd_value)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wallet_id, owner, ts, t["chain"], t["symbol"], t["name"],
             t["category"], t.get("contract_address") or "", t["balance"],
             t["usd_price"], t["usd_value"]))
    conn.execute(
        "UPDATE cw_wallets SET last_value_usd=?, last_refresh=? WHERE id=?",
        (scan["total_usd"], ts, wallet_id))
    conn.commit()
    report["scan"] = {"total_usd": scan["total_usd"], "tokens": scan["token_count"],
                      "chains": scan["chain_count"], "errors": scan["errors"]}
    # 2) transferts manquants
    _state_set(owner, step="transfers")
    rep_tx = fetch_transfers(conn, owner, w)
    report["transfers"] = rep_tx
    # 2b) mouvements natifs (value + fees + internal) — correction fantôme
    native_chains = sorted({t["chain"] for t in (scan["tokens"] or [])
                            if (t.get("symbol") or "").lower()
                            == NATIVE_COIN.get(t["chain"], {}).get("symbol",
                                                                   "").lower()
                            and t.get("symbol")})
    report["native"] = fetch_native_movements(conn, owner, w, native_chains)
    # 3) prix manquants + enrichissement
    _state_set(owner, step="prices")
    rep_px = fetch_prices_and_enrich(conn, owner, wallet_id)
    report["prices"] = rep_px
    # 4) reconstruction série journalière (ancrée sur le scan)
    _state_set(owner, step="rebuild")
    rep_rb = rebuild_wallet_series(conn, owner, wallet_id, scan["tokens"])
    report["rebuild"] = rep_rb
    # coût courant (dernier point de série) pour l'affichage wallet
    cst = conn.execute(
        "SELECT cost_usd FROM cw_history WHERE wallet_id=? AND token_symbol IS"
        " NULL ORDER BY date DESC LIMIT 1", (wallet_id,)).fetchone()
    if cst is not None:
        conn.execute("UPDATE cw_wallets SET last_cost_usd=? WHERE id=?",
                     (cst["cost_usd"] or 0, wallet_id))
        conn.commit()
    # 5) intégration comptes-auto
    _state_set(owner, step="integration")
    rep_in = refresh_integration(conn, owner)
    report["integration"] = rep_in
    report["ok"] = bool(rep_in.get("ok")) if rep_in else False
    return report


def refresh_owner(db_path: str, owner: str, wallet_id: int | None = None,
                  full: bool = True) -> dict:
    """Workflow worker (sa propre connexion — threadpool/to_thread)."""
    if not refresh_claimed(owner):
        return {"ok": False, "reason": "busy"}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            if wallet_id is not None:
                return refresh_wallet(conn, owner, wallet_id, full=full)
            out = []
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM cw_wallets WHERE owner=?", (owner,))]
            for wid in ids:
                _state_set(owner, wallet_id=wid, step="pending")
                out.append(refresh_wallet(conn, owner, wid, full=full))
            return {"ok": True, "wallets": out}
        finally:
            conn.close()
    except Exception as e:
        log.exception("refresh_owner %s: %s", owner, e)
        _state_set(owner, state="error", error=str(e)[:300])
        return {"ok": False, "reason": str(e)[:300]}
    finally:
        _state_set(owner, state="idle", step="", wallet_id=None)
        refresh_release(owner)


# ═══════════════════════════════════════════════════════════════════
# API métier (helpers appelés par les routes)
# ═══════════════════════════════════════════════════════════════════

def add_wallet(conn, owner: str, label: str, address: str) -> sqlite3.Row:
    """Crée un wallet (adresse EVM valide, pas de doublon par owner).
    Lève ValueError(message) sur erreur."""
    label = (label or "").strip()
    if not label:
        raise ValueError("Le libellé est vide")
    if len(label) > 60:
        raise ValueError("Libellé trop long (60 max)")
    address = _norm_addr(address or "")
    if not is_evm_address(address):
        raise ValueError("Adresse invalide (attendu : 0x + 40 caractères hexadécimaux)")
    dup = conn.execute(
        "SELECT 1 FROM cw_wallets WHERE owner=? AND address=?", (owner, address)
    ).fetchone()
    if dup:
        raise ValueError("Cette adresse est déjà suivie")
    cur = conn.execute(
        "INSERT INTO cw_wallets (owner, label, address, watch_only, demo,"
        " created_at, status) VALUES (?,?,?,1,0,?,'ok')",
        (owner, label, address, now_iso()))
    return wallet_status(conn, owner, cur.lastrowid)


def wallets_rows(conn, owners: list[str]) -> list[dict]:
    """Liste wallets des owners (avec agrégats du dernier état)."""
    if not owners:
        return []
    marks = ",".join("?" * len(owners))
    out = []
    for w in conn.execute(
            f"SELECT * FROM cw_wallets WHERE owner IN ({marks}) ORDER BY owner, id",
            owners):
        d = {"id": w["id"], "owner": w["owner"], "label": w["label"],
             "address": w["address"], "demo": bool(w["demo"]),
             "account_id": w["account_id"], "first_date": w["first_date"],
             "last_date": w["last_date"], "last_value_usd": w["last_value_usd"],
             "last_cost_usd": w["last_cost_usd"], "last_refresh": w["last_refresh"],
             "status": w["status"]}
        chains = conn.execute(
            "SELECT chain, COUNT(*) AS n, SUM(usd_value) AS v FROM cw_scans"
            " WHERE wallet_id=? GROUP BY chain", (w["id"],)).fetchall()
        d["chains"] = {r["chain"]: round(r["v"] or 0, 2) for r in chains}
        d["chain_count"] = len(chains)
        d["token_count"] = conn.execute(
            "SELECT COUNT(*) c FROM cw_scans WHERE wallet_id=? AND usd_value>0",
            (w["id"],)).fetchone()["c"]
        out.append(d)
    return out


def overview_rows(conn, owners: list[str]) -> dict:
    """Agrégats par wallet + totaux (valeurs USD ; les équivalents EUR sont
    calculés côté route quand demandé)."""
    rows = []
    tot_v = tot_c = 0.0
    n_tok = n_chain = 0
    for w in wallets_rows(conn, owners):
        # VALEUR COURANTE = dernier scan (balances réelles) — l'historique
        # reconstruit (transferts ERC-20) est une estimation : l'écart est
        # exposé via `recon_pct` (réconciliation scan vs série)
        scan = conn.execute(
            "SELECT SUM(usd_value) v FROM cw_scans WHERE wallet_id=? AND"
            " usd_value>0 AND scanned_at=(SELECT MAX(scanned_at) FROM cw_scans"
            " WHERE wallet_id=?)", (w["id"], w["id"])).fetchone()
        scan_v = round(scan["v"] or 0, 2) if scan and scan["v"] else None
        if scan_v is None and w["last_value_usd"]:
            scan_v = round(w["last_value_usd"], 2)
        hist = conn.execute(
            "SELECT value_usd, cost_usd, date FROM cw_history WHERE wallet_id=?"
            " AND token_symbol IS NULL ORDER BY date DESC LIMIT 1", (w["id"],)).fetchone()
        hist_v = round(hist["value_usd"] or 0, 2) if hist else None
        val = scan_v if scan_v is not None else (hist_v or 0.0)
        cost = round(hist["cost_usd"] or 0, 2) if hist else 0.0
        recon = None
        if scan_v is not None and hist_v and hist_v > 0:
            recon = round(100 * (scan_v - hist_v) / hist_v, 1)
        w.update({"value_usd": val, "cost_usd": cost,
                  "gain_usd": round(val - cost, 2),
                  "history_last_usd": hist_v,
                  "recon_pct": recon,
                  "tokens": w["token_count"], "chains_n": w["chain_count"]})
        if w["chains"]:
            w["main_chain"] = max(w["chains"], key=w["chains"].get)
        else:
            w["main_chain"] = ""
        rows.append(w)
        tot_v += val
        tot_c += cost
        n_tok += w["token_count"]
        n_chain += w["chain_count"]
    return {"wallets": rows,
            "total_usd": round(tot_v, 2),
            "total_cost_usd": round(tot_c, 2),
            "total_gain_usd": round(tot_v - tot_c, 2),
            "token_count": n_tok, "chain_count": n_chain,
            "wallet_count": len(rows)}


def tokens_rows(conn, owners: list[str], wallet_id: int | None = None) -> list[dict]:
    """Détail du dernier scan (jetons/chaînes) des wallets des owners."""
    if not owners:
        return []
    marks = ",".join("?" * len(owners))
    where = f" AND s.wallet_id=?" if wallet_id else ""
    args: list = owners + ([wallet_id] if wallet_id else [])
    out = []
    for r in conn.execute(
            f"""SELECT s.*, w.label FROM cw_scans s
                JOIN cw_wallets w ON w.id=s.wallet_id
                WHERE s.owner IN ({marks}) AND s.scanned_at=(
                    SELECT MAX(scanned_at) FROM cw_scans s2
                    WHERE s2.wallet_id=s.wallet_id){where}
                ORDER BY s.usd_value DESC LIMIT 1000""", args):
        out.append({
            "wallet_id": r["wallet_id"], "wallet": r["label"], "chain": r["chain"],
            "symbol": r["symbol"], "name": r["name"], "category": r["category"],
            "balance": r["balance"], "usd_price": r["usd_price"],
            "usd_value": r["usd_value"], "token_addr": r["token_addr"],
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# Export / import (coffres & transfert de données)
# ═══════════════════════════════════════════════════════════════════

def export_payload(conn, owner: str) -> dict:
    """Payload JSON du module pour un owner (wallets + transferts + séries
    + scans). Les prix (cache global) ne sont PAS exportés (re-fetchables)."""
    wallets = [dict(r) for r in conn.execute(
        "SELECT * FROM cw_wallets WHERE owner=?", (owner,))]
    ids = [w["id"] for w in wallets]
    if not ids:
        return {"wallets": [], "transfers": [], "history": [], "scans": []}
    marks = ",".join("?" * len(ids))
    transfers = [dict(r) for r in conn.execute(
        f"SELECT * FROM cw_transfers WHERE wallet_id IN ({marks})", ids)]
    history = [dict(r) for r in conn.execute(
        f"SELECT * FROM cw_history WHERE wallet_id IN ({marks})", ids)]
    scans = [dict(r) for r in conn.execute(
        f"SELECT * FROM cw_scans WHERE wallet_id IN ({marks})", ids)]
    return {"wallets": wallets, "transfers": transfers,
            "history": history, "scans": scans}


def do_cw_import(conn, owner: str, body: dict) -> str | None:
    """Restaure le payload d'un owner (remplacement complet). Retourne une
    erreur lisible ou None. Les ids wallets/transferts sont préservés (les
    comptes-auto restaurés par le transfert global référencent les mêmes
    ids d'actifs)."""
    try:
        wallets = body.get("wallets") or []
        transfers = body.get("transfers") or []
        history = body.get("history") or []
        scans = body.get("scans") or []
        if not wallets and not transfers and not history and not scans:
            return None  # module vide : rien à restaurer
        # remplacement owner — PAS de BEGIN/COMMIT ici : la restauration
        # s'exécute dans la transaction du transfert global (transfer.do_import)
        conn.execute("DELETE FROM cw_wallets WHERE owner=?", (owner,))  # cascade
        for w in wallets:
            conn.execute(
                "INSERT INTO cw_wallets (id, owner, label, address, chain,"
                " watch_only, demo, account_id, first_date, last_date,"
                " last_value_usd, last_cost_usd, last_refresh, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (w["id"], owner, w["label"], w["address"], w.get("chain") or "",
                 w.get("watch_only", 1), w.get("demo", 0),
                 w.get("account_id"), w.get("first_date"), w.get("last_date"),
                 w.get("last_value_usd"), w.get("last_cost_usd"),
                 w.get("last_refresh"), w.get("status", "ok"),
                 w.get("created_at") or now_iso()))
        for t in transfers:
            conn.execute(
                "INSERT INTO cw_transfers (id, wallet_id, owner, tx_hash,"
                " log_index, chain, block_time, token_symbol, token_name,"
                " token_addr, direction, amount, usd_price, usd_value)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["wallet_id"], owner, t["tx_hash"],
                 t.get("log_index", 0), t["chain"], t.get("block_time") or "",
                 t.get("token_symbol") or "", t.get("token_name") or "",
                 t.get("token_addr") or "", t["direction"], t.get("amount", 0),
                 t.get("usd_price", 0), t.get("usd_value", 0)))
        for h in history:
            conn.execute(
                "INSERT INTO cw_history (id, wallet_id, owner, date, value_usd,"
                " cost_usd, net_flows_usd, token_symbol, chain)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (h["id"], h["wallet_id"], owner, h["date"], h.get("value_usd"),
                 h.get("cost_usd"), h.get("net_flows_usd", 0),
                 h.get("token_symbol"), h.get("chain")))
        for s in scans:
            conn.execute(
                "INSERT INTO cw_scans (id, wallet_id, owner, scanned_at, chain,"
                " symbol, name, category, token_addr, balance, usd_price,"
                " usd_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], s["wallet_id"], owner, s["scanned_at"], s["chain"],
                 s.get("symbol") or "", s.get("name") or "",
                 s.get("category", "wallet"), s.get("token_addr") or "",
                 s.get("balance", 0), s.get("usd_price", 0),
                 s.get("usd_value", 0)))
    except Exception as e:
        return f"Module crypto : {str(e)[:200]}"
    return None


# ═══════════════════════════════════════════════════════════════════
# Seed démo (déterministe, SANS réseau — wallet synthétique demo=1)
# ═══════════════════════════════════════════════════════════════════

_DEMO_D0 = date(2024, 1, 15)


def _synth_eth_price(d_ordinal: int, n_days: int) -> float:
    """Prix ETH synthétique déterministe : 2 300 $ (2024-01-15) → ~3 100 $
    aujourd'hui, avec ondulations. Aucun aléa d'état : fonction pure de la
    date (ordinal) et de la durée totale."""
    p = max(0.0, min(1.0, (d_ordinal - _DEMO_D0.toordinal()) / max(1, n_days)))
    fade = 1.0 - 0.55 * p
    wave = (math.sin(p * 31.0) * 90 + math.sin(p * 9.0) * 160
            + math.sin(p * 47.0) * 60) * fade
    return round(2300.0 + 800.0 * p + wave, 2)


def seed_demo(conn, owner: str) -> dict:
    """Wallet fictif « Ledger (démo) » : transferts synthétiques (ETH
    ethereum, achats mensuels + frais) + cache prix synthétique → rebuild
    local (aucun appel réseau : wallet demo=1). Idempotent."""
    if conn.execute("SELECT 1 FROM cw_wallets WHERE owner=? AND demo=1",
                    (owner,)).fetchone():
        return {"seeded": False}
    today = date.today()
    d0 = date(2024, 1, 15)
    n_days = (today - d0).days
    # 1) cache prix ETH synthétique (chaque jour)
    cur = conn.execute(
        "SELECT COUNT(*) c FROM cw_price_cache WHERE token_symbol='eth'")
    if cur.fetchone()["c"] == 0:
        for i in range(n_days + 1):
            d = d0 + timedelta(days=i)
            conn.execute(
                "INSERT OR IGNORE INTO cw_price_cache (token_symbol, date,"
                " price_usd) VALUES ('eth',?,?)",
                (d.isoformat(), _synth_eth_price(d.toordinal(), n_days)))
    # 2) wallet démo (adresse fictive déterministe — jamais de réseau)
    cur = conn.execute(
        "INSERT INTO cw_wallets (owner, label, address, chain, watch_only,"
        " demo, status, created_at) VALUES (?,?,'0x' || ?,'ethereum',1,1,'ok',?)",
        (owner, "Ledger (démo)", "d3" * 20, now_iso()))
    wid = cur.lastrowid
    # 3) transferts synthétiques : achat initial + petit achat mensuel +
    #    retrait trimestriel (frais)
    qty = 0.0
    tx = 0
    d = d0
    while d <= today:
        price = _synth_eth_price(d.toordinal(), n_days)
        if d == d0:
            amt = 0.75
        elif d.day == 6:
            amt = 0.012
        elif d.day == 21:
            amt = 0.006
        else:
            amt = 0.0
        if amt > 0:
            qty += amt
            conn.execute(
                "INSERT INTO cw_transfers (wallet_id, owner, tx_hash, log_index,"
                " chain, block_time, token_symbol, token_name, token_addr,"
                " direction, amount, usd_price, usd_value) VALUES"
                " (?,?,?,0,'ethereum',?,'ETH','Ethereum','','in',?,?,?)",
                (wid, owner, f"0x{1000000000 + tx:064x}",
                 f"{d.isoformat()} 12:00:00", amt, price,
                 round(amt * price, 2)))
            tx += 1
        # retrait trimestriel (frais de gestion simulés)
        if d.day == 28 and d.month % 3 == 0 and qty > 0.1:
            out = round(qty * 0.012, 8)
            qty -= out
            conn.execute(
                "INSERT INTO cw_transfers (wallet_id, owner, tx_hash, log_index,"
                " chain, block_time, token_symbol, token_name, token_addr,"
                " direction, amount, usd_price, usd_value) VALUES"
                " (?,?,?,1,'ethereum',?,'ETH','Ethereum','','out',?,?,?)",
                (wid, owner, f"0x{2000000000 + tx:064x}",
                 f"{d.isoformat()} 12:30:00", out, price, round(out * price, 2)))
            tx += 1
        d += timedelta(days=1)
    conn.commit()
    # 4) série locale + intégration (fx synthétique si nécessaire)
    if fx.lookup(conn, "USD", today.isoformat(), None) is None:
        for i in range(0, n_days + 1, 30):
            dd = d0 + timedelta(days=i)
            conn.execute(
                "INSERT OR IGNORE INTO fx_rates (ccy, rate_date, rate, source)"
                " VALUES ('USD',?,1.08,'demo')", (dd.isoformat(),))
    conn.commit()
    rebuild_wallet_series(conn, owner, wid, None)
    refresh_integration(conn, owner, today)
    # 5) scans « cosmétiques » à partir du dernier état de série (le wallet
    #    démo ne fait jamais de réseau) + dernier état wallet
    ts = now_iso()
    agg_v = 0.0
    for r in conn.execute(
            "SELECT token_symbol, chain, value_usd, cost_usd FROM cw_history"
            " WHERE wallet_id=? AND token_symbol IS NOT NULL AND date=(SELECT"
            " MAX(date) FROM cw_history WHERE wallet_id=? AND token_symbol IS"
            " NOT NULL)", (wid, wid)):
        sym = r["token_symbol"]
        price = conn.execute(
            "SELECT price_usd FROM cw_price_cache WHERE token_symbol=?"
            " ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        px = price["price_usd"] if price else 0.0
        if not px:
            txp = conn.execute(
                "SELECT usd_price FROM cw_transfers WHERE wallet_id=? AND"
                " token_symbol=? AND usd_price>0 ORDER BY block_time DESC LIMIT 1",
                (wid, sym)).fetchone()
            px = txp["usd_price"] if txp else 0.0
        val = r["value_usd"] or 0
        agg_v += val
        conn.execute(
            "INSERT INTO cw_scans (wallet_id, owner, scanned_at, chain, symbol,"
            " name, category, token_addr, balance, usd_price, usd_value)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, owner, ts, r["chain"], sym,
             "Ethereum" if sym == "eth" else sym, _token_category(sym), "",
             round(val / px, 6) if px else 0, px, round(val, 2)))
    conn.execute(
        "UPDATE cw_wallets SET last_value_usd=?, last_refresh=?, status='ok'"
        " WHERE id=?", (round(agg_v, 2), ts, wid))
    conn.commit()
    return {"seeded": True, "wallet_id": wid, "transfers": tx}


# ═══════════════════════════════════════════════════════════════════
# Rafraîchissement automatique (boot + quotidien 06:00 Europe/Paris)
# ═══════════════════════════════════════════════════════════════════

def _due_owners(db_path: str, max_age_h: float = 24.0) -> list[str]:
    """Owners ayant au moins un wallet NON démo et jamais rafraîchi ou dont
    le dernier rafraîchissement est plus vieux que max_age_h."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - timedelta(hours=max_age_h)).isoformat()
            rows = conn.execute(
                "SELECT DISTINCT owner FROM cw_wallets WHERE demo=0"
                " AND (last_refresh IS NULL OR last_refresh < ?)", (cutoff,)
            ).fetchall()
            return [r["owner"] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _sleep_until(hour: int, tz_name: str = "Europe/Paris") -> None:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now = datetime.datetime.now(tz)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    time.sleep((nxt - now).total_seconds())


def auto_loop(db_path: str, max_age_h: float = 24.0) -> None:
    """Daemon de rafraîchissement : rattrapage immédiat si un owner est en
    retard (> 24 h depuis le dernier refresh), puis boucle quotidienne à
    06:00 Europe/Paris. Thread daemon — ne bloque jamais l'event loop."""
    while True:
        try:
            for owner in _due_owners(db_path, max_age_h):
                log.info("cw auto: refresh rattrapage owner=%s", owner)
                refresh_owner(db_path, owner)
        except Exception:
            log.exception("cw auto: passe de rattrapage en échec")
        try:
            _sleep_until(6)
        except Exception:
            time.sleep(3600)
        try:
            for owner in _due_owners(db_path, max_age_h):
                refresh_owner(db_path, owner)
        except Exception:
            log.exception("cw auto: passe quotidienne en échec")


# ═══════════════════════════════════════════════════════════════════
# Points mensuels (mini-graphe module) — valeurs agrégées USD
# ═══════════════════════════════════════════════════════════════════

def monthly_history(conn, owners: list[str], wallet_id: int | None = None,
                    limit: int = 120) -> list[dict]:
    """Dernier point de chaque mois (agrégat wallet) : [{ym, date, value_usd,
    cost_usd}]. Tri chronologique, limité aux `limit` derniers mois."""
    if not owners:
        return []
    marks = ",".join("?" * len(owners))
    where = " AND wallet_id=?" if wallet_id else ""
    args: list = owners + ([wallet_id] if wallet_id else [])
    rows = conn.execute(
        f"""SELECT substr(date,1,7) AS ym, date, value_usd, cost_usd,
                   ROW_NUMBER() OVER (PARTITION BY substr(date,1,7)
                       ORDER BY date DESC) AS rn
            FROM cw_history
            WHERE owner IN ({marks}) AND token_symbol IS NULL{where}""",
        args).fetchall()
    out = []
    for r in rows:
        if r["rn"] != 1:
            continue
        out.append({"ym": r["ym"], "date": r["date"],
                    "value_usd": round(r["value_usd"] or 0, 2),
                    "cost_usd": round(r["cost_usd"] or 0, 2)})
    out.sort(key=lambda x: x["date"])
    return out[-limit:]
