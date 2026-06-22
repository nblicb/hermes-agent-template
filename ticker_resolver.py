"""Ticker → company name resolver with token-minimizing injection.

Why: LLM training data lacks tickers that IPO'd after cutoff (e.g. CRWV =
CoreWeave, Inc., IPO 2025-03). Without an authoritative name map, the LLM
confabulates (CRWV → CRISPR Therapeutics is wrong, that's CRSP). We inject
a compact "(ref: CRWV=CoreWeave,Inc.)" prefix so the LLM sees the correct
company before generating.

Strategy:
1. POPULAR_TICKERS whitelist (~30 mega-caps): skip injection (0 token).
2. Lookup in aliases_cn_us.json (~27k rows, ships with image): compact inject.
3. Optional fallback: FMP /profile HTTP call with 24h in-memory cache for new
   IPOs not yet in the JSON snapshot. Disabled by default; enable with
   HERMES_FMP_PROFILE_FALLBACK_ENABLED=1 only when bandwidth allows it.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _alias_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _compact_alias_key(text: str) -> str:
    return re.sub(r"[\s_\-./]+", "", _alias_key(text))


# ── Config ────────────────────────────────────────────────────────────────────

_ALIASES_PATH = Path(__file__).resolve().parent / "config" / "aliases_cn_us.json"
_FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 4.0  # seconds
_CACHE_TTL = 86400  # 24h
_MAX_REF_COUNT = 3
_MAX_REF_NAME_CHARS = 48

# LLM training data covers these thoroughly — skip injection to save tokens.
# Maintenance: missing a mega-cap here only costs ~7 tokens extra, not a bug.
_POPULAR_TICKERS = {
    # Mag 7
    "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    # Index heavyweights
    "BRK.A", "BRK.B", "JPM", "V", "WMT", "UNH", "JNJ", "XOM", "MA", "HD",
    "BAC", "KO", "PEP", "DIS", "NFLX", "ADBE", "ORCL", "CRM", "AMD", "INTC",
    # Major ETFs
    "SPY", "QQQ", "VOO", "VTI", "IWM", "DIA",
    # China ADRs (LLM has deep training coverage)
    "BABA", "PDD", "JD", "BIDU", "NIO",
}

# Ticker candidates:
# - US tickers: AAPL, BRK.B
# - Crypto USD pairs: BTCUSD, USDTUSD
# - HK/JP/KR/TW listings: 0700.HK, 7203.T, 005930.KS, 2330.TW, 5274.TWO
# Word boundary ensures we don't pull tickers out of random ALLCAPS noise.
_TICKER_RE = re.compile(
    r"\b([A-Z0-9]{2,14}USD|\d{4}\.(?:TWO|TW|HK|T)|\d{6}\.(?:KS|KQ)|[A-Z]{1,6}(?:\.[A-Z])?)\b"
)

# Common English all-caps words that match the regex but aren't tickers.
# Must be a superset so we never confuse them with real tickers.
_NOISE_WORDS = frozenset({
    "A", "I", "AI", "USA", "US", "UK", "EU", "CEO", "CFO", "CTO", "COO",
    "IPO", "API", "SEC", "FDA", "FED", "GDP", "CPI", "PPI", "PMI", "ISM",
    "ETF", "REIT", "IRA", "LLC", "LP", "LTD", "INC", "CORP", "CO", "PLC",
    "THE", "AND", "OR", "BUT", "IF", "IS", "AM", "BE", "AS", "AT", "ON",
    "IN", "OF", "TO", "BY", "FOR", "WAS", "NOT", "HAS", "HAD", "HAVE",
    "DO", "DOES", "DID", "CAN", "MAY", "WHY", "HOW", "WHO", "WHAT", "WHEN",
    "OK", "NO", "YES", "ALL", "ANY", "NEW", "OLD", "TOP", "BIG", "LOW",
    "PE", "PB", "PS", "PEG", "ROE", "ROA", "ROIC", "EPS", "EBIT", "EBITDA",
    "FCF", "DCF", "TTM", "YOY", "QOQ", "YTD", "YTM", "MOM", "ATH", "ATL",
    "MA", "SMA", "EMA", "RSI", "MACD", "KDJ", "OBV", "ATR", "ADX", "CCI",
    "VWAP", "NYSE", "NASDAQ", "OTC", "ADR", "GDR", "CFD",
    "JSON", "HTTP", "HTTPS", "URL", "HTML", "CSS", "JS", "TS", "SQL",
    "GMT", "UTC", "EST", "PST", "HKT",
    "HI", "HEY", "OH", "AH", "UM", "UH", "WOW",
    # Chinese-mixed noise
    "QA", "FAQ", "DM", "PM", "AM",
    # Comparison / conjunction words that collide with real tickers
    "VS", "VERSUS", "AKA", "ETC", "IE", "EG",
    # Other small words that look like tickers but usually aren't
    "GO", "UP", "DOWN", "OUT", "OFF", "SO", "IT", "ME", "MY", "WE", "US",
})

_CRYPTO_SUFFIXES = ("币", "coin", "token")
_CRYPTO_CONTEXT_RE = re.compile(
    r"加密|加密货币|虚拟货币|数字货币|链上|区块链|代币|币价|币种|"
    r"\b(crypto|cryptocurrency|coin|token|web3|defi|onchain|on-chain)\b",
    re.IGNORECASE,
)

# Curated alias catalog. It lives in code, not in the prompt; only matched
# assets are injected. Conflict-prone abbreviations such as SOL/LINK/ON are
# context-only so they do not hijack US stock tickers.
_CRYPTO_CATALOG: dict[str, dict[str, tuple[str, ...] | str]] = {
    "BTCUSD": {"name": "Bitcoin", "aliases": ("btc", "bitcoin", "比特币")},
    "ETHUSD": {"name": "Ethereum", "aliases": ("eth", "ethereum", "ether", "以太坊")},
    "BNBUSD": {"name": "BNB", "aliases": ("bnb", "binance coin", "币安币")},
    "XRPUSD": {"name": "XRP", "aliases": ("xrp", "ripple", "瑞波", "瑞波币")},
    "SOLUSD": {"name": "Solana", "aliases": ("solana", "sol", "sol币")},
    "DOGEUSD": {"name": "Dogecoin", "aliases": ("doge", "dogecoin", "狗狗币")},
    "ADAUSD": {"name": "Cardano", "aliases": ("ada", "cardano", "艾达币")},
    "TRXUSD": {"name": "TRON", "aliases": ("trx", "tron", "波场")},
    "AVAXUSD": {"name": "Avalanche", "aliases": ("avax", "avalanche", "雪崩")},
    "TONUSD": {"name": "Toncoin", "aliases": ("ton", "toncoin")},
    "SHIBUSD": {"name": "Shiba Inu", "aliases": ("shib", "shiba inu", "柴犬币")},
    "DOTUSD": {"name": "Polkadot", "aliases": ("dot", "polkadot", "波卡")},
    "LINKUSD": {"name": "Chainlink", "aliases": ("chainlink", "link", "link币")},
    "LTCUSD": {"name": "Litecoin", "aliases": ("ltc", "litecoin", "莱特币")},
    "BCHUSD": {"name": "Bitcoin Cash", "aliases": ("bch", "bitcoin cash", "比特币现金")},
    "XLMUSD": {"name": "Stellar", "aliases": ("xlm", "stellar", "恒星币")},
    "SUIUSD": {"name": "Sui", "aliases": ("sui", "sui币")},
    "HYPEUSD": {"name": "Hyperliquid", "aliases": ("hype", "hyperliquid")},
    "UNIUSD": {"name": "Uniswap", "aliases": ("uniswap", "uni", "uni币")},
    "NEARUSD": {"name": "NEAR Protocol", "aliases": ("near protocol", "near币")},
    "AAVEUSD": {"name": "Aave", "aliases": ("aave",)},
    "FILUSD": {"name": "Filecoin", "aliases": ("fil", "filecoin", "fil币")},
    "ICPUSD": {"name": "Internet Computer", "aliases": ("icp", "internet computer")},
    "APTUSD": {"name": "Aptos", "aliases": ("aptos", "apt币")},
    "PEPEUSD": {"name": "Pepe", "aliases": ("pepe", "pepe币")},
}

_CRYPTO_SAFE_ABBREVS = frozenset({
    "BTC", "ETH", "BNB", "XRP", "DOGE", "ADA", "TRX", "AVAX", "TON",
    "SHIB", "DOT", "LTC", "BCH", "XLM", "SUI", "HYPE", "AAVE", "FIL",
    "ICP", "PEPE",
})
_CRYPTO_CONTEXT_ONLY_ABBREVS = frozenset({
    "SOL", "LINK", "UNI", "NEAR", "APT", "OP", "ETC", "FLOW", "CORE", "SAFE",
    "MOVE", "MASK",
})

_CRYPTO_CATALOG.update({
    "USDTUSD": {"name": "Tether", "aliases": ("usdt", "tether", "泰达币")},
    "USDCUSD": {"name": "USD Coin", "aliases": ("usdc", "usd coin", "circle usd", "USDC")},
    "DAIUSD": {"name": "Dai", "aliases": ("dai", "dai stablecoin")},
    "FDUSDUSD": {"name": "First Digital USD", "aliases": ("fdusd", "first digital usd")},
    "MATICUSD": {"name": "Polygon", "aliases": ("matic", "polygon", "Polygon")},
    "POLUSD": {"name": "POL", "aliases": ("pol", "polygon ecosystem token", "pol币")},
    "ATOMUSD": {"name": "Cosmos", "aliases": ("atom", "cosmos", "atom币")},
    "ARBUSD": {"name": "Arbitrum", "aliases": ("arb", "arbitrum", "arb币")},
    "OPUSD": {"name": "Optimism", "aliases": ("optimism", "op币")},
    "FTMUSD": {"name": "Fantom", "aliases": ("ftm", "fantom", "ftm币")},
    "ETCUSD": {"name": "Ethereum Classic", "aliases": ("ethereum classic", "以太经典", "etc币")},
    "HBARUSD": {"name": "Hedera", "aliases": ("hbar", "hedera", "hashgraph")},
    "VETUSD": {"name": "VeChain", "aliases": ("vet", "vechain", "唯链")},
    "MANAUSD": {"name": "Decentraland", "aliases": ("mana", "decentraland", "mana币")},
    "SANDUSD": {"name": "The Sandbox", "aliases": ("sand", "sandbox", "沙盒")},
    "XMRUSD": {"name": "Monero", "aliases": ("xmr", "monero", "门罗币")},
    "ALGOUSD": {"name": "Algorand", "aliases": ("algo", "algorand", "阿尔戈兰德")},
    "GRTUSD": {"name": "The Graph", "aliases": ("the graph", "grt币")},
    "THETAUSD": {"name": "Theta", "aliases": ("theta", "theta token")},
    "XTZUSD": {"name": "Tezos", "aliases": ("xtz", "tezos", "xtz币")},
    "NEOUSD": {"name": "NEO", "aliases": ("neo coin", "小蚁")},
    "CHZUSD": {"name": "Chiliz", "aliases": ("chz", "chiliz", "chz币")},
    "ENSUSD": {"name": "Ethereum Name Service", "aliases": ("ens", "ethereum name service", "以太坊域名服务")},
    "LDOUSD": {"name": "Lido DAO", "aliases": ("ldo", "lido", "lido dao")},
    "INJUSD": {"name": "Injective", "aliases": ("inj", "injective", "injective protocol")},
    "RUNEUSD": {"name": "THORChain", "aliases": ("rune", "thorchain", "rune币")},
    "SNXUSD": {"name": "Synthetix", "aliases": ("snx", "synthetix", "snx币")},
    "COMPUSD": {"name": "Compound", "aliases": ("compound", "comp币")},
    "CRVUSD": {"name": "Curve DAO", "aliases": ("crv", "curve", "curve dao")},
    "DYDXUSD": {"name": "dYdX", "aliases": ("dydx", "dydx chain")},
    "FLOKIUSD": {"name": "FLOKI", "aliases": ("floki", "floki币")},
    "BONKUSD": {"name": "Bonk", "aliases": ("bonk", "bonk币")},
    "WLDUSD": {"name": "Worldcoin", "aliases": ("wld", "worldcoin", "世界币")},
    "SEIUSD": {"name": "Sei", "aliases": ("sei", "sei network", "sei币")},
    "TIAUSD": {"name": "Celestia", "aliases": ("tia", "celestia", "tia币")},
    "JUPUSD": {"name": "Jupiter", "aliases": ("jup", "jupiter", "jupiter exchange")},
    "STXUSD": {"name": "Stacks", "aliases": ("stx", "stacks", "stx币")},
    "KASUSD": {"name": "Kaspa", "aliases": ("kas", "kaspa", "kas币")},
    "RENDERUSD": {"name": "Render", "aliases": ("render", "render token", "rndr")},
    "TAOUSD": {"name": "Bittensor", "aliases": ("tao", "bittensor", "tao币")},
    "ONDOUSD": {"name": "Ondo", "aliases": ("ondo", "ondo finance")},
    "IMXUSD": {"name": "Immutable", "aliases": ("imx", "immutable", "immutable x")},
    "PENDLEUSD": {"name": "Pendle", "aliases": ("pendle",)},
    "PYTHUSD": {"name": "Pyth Network", "aliases": ("pyth", "pyth network")},
    "JTOUSD": {"name": "Jito", "aliases": ("jto", "jito")},
    "RAYUSD": {"name": "Raydium", "aliases": ("raydium", "ray币")},
    "AXSUSD": {"name": "Axie Infinity", "aliases": ("axs", "axie", "axie infinity")},
    "GALAUSD": {"name": "GALA", "aliases": ("gala", "gala币")},
    "ROSEUSD": {"name": "Oasis", "aliases": ("rose", "oasis network")},
    "KAVAUSD": {"name": "Kava", "aliases": ("kava",)},
    "MINAUSD": {"name": "Mina", "aliases": ("mina", "mina protocol")},
    "IOTAUSD": {"name": "IOTA", "aliases": ("iota",)},
    "CELOUSD": {"name": "Celo", "aliases": ("celo",)},
    "ZECUSD": {"name": "Zcash", "aliases": ("zec", "zcash", "大零币")},
    "DASHUSD": {"name": "Dash", "aliases": ("dash coin", "达世币")},
    "DCRUSD": {"name": "Decred", "aliases": ("dcr", "decred")},
    "ZILUSD": {"name": "Zilliqa", "aliases": ("zil", "zilliqa")},
    "RVNUSD": {"name": "Ravencoin", "aliases": ("rvn", "ravencoin", "渡鸦币")},
    "ANKRUSD": {"name": "Ankr", "aliases": ("ankr",)},
    "CKBUSD": {"name": "Nervos", "aliases": ("ckb", "nervos")},
    "KSMUSD": {"name": "Kusama", "aliases": ("ksm", "kusama")},
    "BATUSD": {"name": "Basic Attention Token", "aliases": ("bat token", "basic attention token", "注意力币")},
    "1INCHUSD": {"name": "1inch", "aliases": ("1inch", "1inch token")},
    "ENAUSD": {"name": "Ethena", "aliases": ("ena", "ethena", "ena币")},
    "FETUSD": {"name": "Artificial Superintelligence Alliance", "aliases": ("fet", "fetch ai", "asi", "人工超级智能联盟")},
    "BSVUSD": {"name": "Bitcoin SV", "aliases": ("bsv", "bitcoin sv", "比特币sv")},
    "CROUSD": {"name": "Cronos", "aliases": ("cro", "cronos", "crypto.com coin")},
    "CFXUSD": {"name": "Conflux", "aliases": ("cfx", "conflux", "树图")},
    "OKBUSD": {"name": "OKB", "aliases": ("okb", "okx token")},
    "LEOUSD": {"name": "LEO Token", "aliases": ("leo token", "unus sed leo")},
    "KCSUSD": {"name": "KuCoin Token", "aliases": ("kcs", "kucoin", "kucoin token")},
    "GTUSD": {"name": "GateToken", "aliases": ("gate token", "gatetoken", "gt币")},
    "BGBUSD": {"name": "Bitget Token", "aliases": ("bgb", "bitget token")},
    "PIUSD": {"name": "Pi Network", "aliases": ("pi network", "pi币")},
    "PAXGUSD": {"name": "PAX Gold", "aliases": ("paxg", "pax gold")},
    "XAUTUSD": {"name": "Tether Gold", "aliases": ("xaut", "tether gold")},
})

_CRYPTO_SAFE_ABBREVS = frozenset(set(_CRYPTO_SAFE_ABBREVS) | {
    "USDT", "USDC", "BNB", "AAVE", "OKB", "KCS", "BGB", "XRP", "DOGE", "ADA",
    "AVAX", "DOT", "LTC", "BCH", "XLM", "ATOM", "ARB", "HBAR", "VET", "XMR",
    "ALGO", "CHZ", "ENS", "LDO", "INJ", "RUNE", "SNX", "DYDX", "FLOKI",
    "BONK", "WLD", "TON", "SUI", "SEI", "TIA", "JUP", "STX", "KAS",
    "RENDER", "TAO", "ONDO", "IMX", "PENDLE", "PYTH", "JTO", "AXS", "GALA",
    "KAVA", "MINA", "IOTA", "CELO", "ZEC", "DCR", "ZIL", "RVN", "ANKR",
    "CKB", "KSM", "1INCH", "ENA", "FET", "BSV", "CRO", "CFX", "XAUT",
    "PAXG", "FDUSD", "DAI",
})

_CRYPTO_CATALOG["SOLUSD"] = {
    "name": "Solana",
    "aliases": ("solana", "solona", "sol", "sol币"),
}
_CRYPTO_CATALOG["HYPEUSD"] = {
    "name": "Hyperliquid",
    "aliases": ("hype", "hyperliquid", "hyper liquid", "hyperliquied", "hpyeliquied"),
}


def _extend_crypto_aliases(symbol: str, *aliases: str) -> None:
    meta = _CRYPTO_CATALOG.get(symbol)
    if not meta:
        return
    existing = tuple(str(a) for a in meta["aliases"])
    meta["aliases"] = tuple(dict.fromkeys((*existing, *aliases)))


for _symbol, _aliases in {
    "BTCUSD": (
        "比特幣", "大饼", "大餅", "btc coin", "btc币", "비트코인", "ビットコイン",
    ),
    "ETHUSD": (
        "以太幣", "以太币", "eth coin", "eth币", "이더리움", "イーサリアム",
    ),
    "USDTUSD": (
        "泰達幣", "tether usd", "usdt币", "tether stablecoin", "테더", "テザー",
    ),
    "USDCUSD": (
        "usdc币", "usd coin stablecoin", "circle", "circle stablecoin", "유에스디코인",
    ),
    "BNBUSD": (
        "幣安幣", "bnb coin", "bnb币", "binance token", "币安平台币", "幣安平台幣",
    ),
    "SOLUSD": (
        "solana coin", "solana币", "sol幣", "솔라나", "ソラナ",
    ),
    "XRPUSD": (
        "瑞波幣", "xrp币", "xrp coin", "리플", "リップル",
    ),
    "DOGEUSD": (
        "狗狗幣", "doge coin", "doge币", "도지코인", "ドージコイン",
    ),
    "ADAUSD": ("艾達幣", "ada coin", "ada币"),
    "TRXUSD": ("波場", "tron coin", "trx币"),
    "AVAXUSD": ("avax coin", "avax币", "avalanche coin"),
    "TONUSD": ("ton coin", "ton币", "the open network"),
    "SHIBUSD": ("柴犬幣", "shiba", "shiba coin", "shib币"),
    "DOTUSD": ("dot coin", "dot币", "polkadot coin"),
    "LINKUSD": ("chainlink coin", "link coin", "link幣"),
    "LTCUSD": ("萊特幣", "ltc coin", "ltc币"),
    "BCHUSD": ("比特幣現金", "bch coin", "bch币"),
    "XLMUSD": ("xlm coin", "xlm币", "stellar lumen"),
    "XMRUSD": ("門羅幣", "xmr coin", "xmr币"),
    "ETCUSD": ("以太經典", "etc coin"),
    "FILUSD": ("文件幣", "fil coin"),
    "ICPUSD": ("icp coin", "icp币"),
    "APTUSD": ("aptos coin", "aptos币", "apt幣"),
    "SUIUSD": ("sui coin", "sui幣"),
    "HYPEUSD": ("hype币", "hype幣", "hyperliquid coin", "hyperliquid币"),
    "AAVEUSD": ("aave coin", "aave币", "aave幣"),
    "UNIUSD": ("uni coin", "uni幣", "uniswap coin"),
    "NEARUSD": ("near protocol", "near coin", "near幣"),
    "PEPEUSD": ("pepe coin", "pepe幣"),
    "WLDUSD": ("world coin", "worldcoin币", "世界幣"),
    "OKBUSD": ("okb coin", "okb币", "okb幣"),
    "KCSUSD": ("kcs coin", "kcs币", "kcs幣"),
    "BGBUSD": ("bgb coin", "bgb币", "bgb幣"),
    "RENDERUSD": ("render network", "render币", "render幣", "rndr币"),
    "TAOUSD": ("tao coin", "tao币", "tao幣"),
    "ONDOUSD": ("ondo coin", "ondo币", "ondo幣"),
    "ARBUSD": ("arb coin", "arb幣"),
    "OPUSD": ("op token", "op币", "op幣"),
    "TIAUSD": ("tia coin", "tia幣"),
    "JUPUSD": ("jup coin", "jup币", "jup幣"),
    "KASUSD": ("kas coin", "kas幣"),
    "PENDLEUSD": ("pendle coin", "pendle币", "pendle幣"),
    "PYTHUSD": ("pyth coin", "pyth币", "pyth幣"),
    "INJUSD": ("inj coin", "inj币", "inj幣"),
}.items():
    _extend_crypto_aliases(_symbol, *_aliases)

_GLOBAL_COMPANY_ALIASES: tuple[tuple[str, str, str], ...] = (
    # Hong Kong
    ("腾讯", "0700.HK", "Tencent Holdings"),
    ("腾讯控股", "0700.HK", "Tencent Holdings"),
    ("tencent", "0700.HK", "Tencent Holdings"),
    ("美团", "3690.HK", "Meituan"),
    ("meituan", "3690.HK", "Meituan"),
    ("小米", "1810.HK", "Xiaomi"),
    ("xiaomi", "1810.HK", "Xiaomi"),
    ("港交所", "0388.HK", "Hong Kong Exchanges and Clearing"),
    ("香港交易所", "0388.HK", "Hong Kong Exchanges and Clearing"),
    ("hkex", "0388.HK", "Hong Kong Exchanges and Clearing"),
    ("中芯国际", "0981.HK", "SMIC"),
    ("中芯國際", "0981.HK", "SMIC"),
    ("smic", "0981.HK", "SMIC"),
    ("快手", "1024.HK", "Kuaishou Technology"),
    ("kuaishou", "1024.HK", "Kuaishou Technology"),
    ("百度港股", "9888.HK", "Baidu"),
    ("百度集团", "9888.HK", "Baidu"),
    ("京东港股", "9618.HK", "JD.com"),
    ("京东集团", "9618.HK", "JD.com"),
    ("网易港股", "9999.HK", "NetEase"),
    ("网易", "9999.HK", "NetEase"),
    ("比亚迪股份", "1211.HK", "BYD"),
    ("中国移动", "0941.HK", "China Mobile"),
    ("中國移動", "0941.HK", "China Mobile"),
    ("中移动", "0941.HK", "China Mobile"),
    ("中移動", "0941.HK", "China Mobile"),
    ("中国海油", "0883.HK", "CNOOC"),
    ("中國海油", "0883.HK", "CNOOC"),
    ("中国海洋石油", "0883.HK", "CNOOC"),
    ("招商银行港股", "3968.HK", "China Merchants Bank"),
    ("药明生物", "2269.HK", "WuXi Biologics"),
    ("理想汽车港股", "2015.HK", "Li Auto"),
    ("蔚来港股", "9866.HK", "NIO"),
    ("携程港股", "9961.HK", "Trip.com"),
    # Japan
    ("丰田", "7203.T", "Toyota Motor"),
    ("豐田", "7203.T", "Toyota Motor"),
    ("トヨタ", "7203.T", "Toyota Motor"),
    ("丰田汽车", "7203.T", "Toyota Motor"),
    ("toyota", "7203.T", "Toyota Motor"),
    ("索尼", "6758.T", "Sony Group"),
    ("ソニー", "6758.T", "Sony Group"),
    ("索尼集团", "6758.T", "Sony Group"),
    ("sony", "6758.T", "Sony Group"),
    ("软银", "9984.T", "SoftBank Group"),
    ("軟銀", "9984.T", "SoftBank Group"),
    ("ソフトバンク", "9984.T", "SoftBank Group"),
    ("softbank", "9984.T", "SoftBank Group"),
    ("任天堂", "7974.T", "Nintendo"),
    ("ニンテンドー", "7974.T", "Nintendo"),
    ("nintendo", "7974.T", "Nintendo"),
    ("三菱ufj", "8306.T", "Mitsubishi UFJ Financial Group"),
    ("三菱UFJ", "8306.T", "Mitsubishi UFJ Financial Group"),
    ("东京电子", "8035.T", "Tokyo Electron"),
    ("東京電子", "8035.T", "Tokyo Electron"),
    ("東京エレクトロン", "8035.T", "Tokyo Electron"),
    ("日立", "6501.T", "Hitachi"),
    ("三井住友", "8316.T", "Sumitomo Mitsui Financial Group"),
    ("三菱商事", "8058.T", "Mitsubishi Corporation"),
    ("keyence", "6861.T", "Keyence"),
    ("基恩士", "6861.T", "Keyence"),
    ("recruit", "6098.T", "Recruit Holdings"),
    ("瑞可利", "6098.T", "Recruit Holdings"),
    ("东京海上", "8766.T", "Tokio Marine Holdings"),
    ("三井物产", "8031.T", "Mitsui"),
    ("伊藤忠", "8001.T", "Itochu"),
    ("信越化学", "4063.T", "Shin-Etsu Chemical"),
    ("信越化學", "4063.T", "Shin-Etsu Chemical"),
    ("本田", "7267.T", "Honda Motor"),
    ("honda", "7267.T", "Honda Motor"),
    ("日产", "7201.T", "Nissan Motor"),
    ("nissan", "7201.T", "Nissan Motor"),
    # Korea
    ("三星", "005930.KS", "Samsung Electronics"),
    ("三星电子", "005930.KS", "Samsung Electronics"),
    ("三星電子", "005930.KS", "Samsung Electronics"),
    ("삼성전자", "005930.KS", "Samsung Electronics"),
    ("samsung electronics", "005930.KS", "Samsung Electronics"),
    ("sk海力士", "000660.KS", "SK hynix"),
    ("SK 海力士", "000660.KS", "SK hynix"),
    ("sk hynix", "000660.KS", "SK hynix"),
    ("에스케이하이닉스", "000660.KS", "SK hynix"),
    ("하이닉스", "000660.KS", "SK hynix"),
    ("sk hynix", "000660.KS", "SK hynix"),
    ("现代汽车", "005380.KS", "Hyundai Motor"),
    ("現代汽車", "005380.KS", "Hyundai Motor"),
    ("현대차", "005380.KS", "Hyundai Motor"),
    ("현대자동차", "005380.KS", "Hyundai Motor"),
    ("hyundai motor", "005380.KS", "Hyundai Motor"),
    ("naver", "035420.KS", "NAVER"),
    ("네이버", "035420.KS", "NAVER"),
    ("kakao", "035720.KS", "Kakao"),
    ("카카오", "035720.KS", "Kakao"),
    ("lg化学", "051910.KS", "LG Chem"),
    ("lg新能源", "373220.KS", "LG Energy Solution"),
    ("lg 新能源", "373220.KS", "LG Energy Solution"),
    ("엘지에너지솔루션", "373220.KS", "LG Energy Solution"),
    ("lg energy solution", "373220.KS", "LG Energy Solution"),
    ("三星sdi", "006400.KS", "Samsung SDI"),
    ("삼성sdi", "006400.KS", "Samsung SDI"),
    ("samsung sdi", "006400.KS", "Samsung SDI"),
    ("三星物产", "028260.KS", "Samsung C&T"),
    ("samsung c&t", "028260.KS", "Samsung C&T"),
    ("韩华航空", "012450.KS", "Hanwha Aerospace"),
    ("hanwha aerospace", "012450.KS", "Hanwha Aerospace"),
    ("kb金融", "105560.KS", "KB Financial Group"),
    ("kb financial", "105560.KS", "KB Financial Group"),
    ("kakao corp", "035720.KS", "Kakao"),
    ("celltrion", "068270.KS", "Celltrion"),
    ("赛尔群", "068270.KS", "Celltrion"),
    ("起亚", "000270.KS", "Kia"),
    ("起亞", "000270.KS", "Kia"),
    ("기아", "000270.KS", "Kia"),
    ("kia", "000270.KS", "Kia"),
    ("posco", "005490.KS", "POSCO Holdings"),
    ("浦项", "005490.KS", "POSCO Holdings"),
    ("hyundai mobis", "012330.KS", "Hyundai Mobis"),
    ("现代摩比斯", "012330.KS", "Hyundai Mobis"),
    # Taiwan
    ("台积电", "2330.TW", "Taiwan Semiconductor Manufacturing"),
    ("台積電", "2330.TW", "Taiwan Semiconductor Manufacturing"),
    ("台湾积体电路", "2330.TW", "Taiwan Semiconductor Manufacturing"),
    ("台灣積體電路", "2330.TW", "Taiwan Semiconductor Manufacturing"),
    ("tsmc taiwan", "2330.TW", "Taiwan Semiconductor Manufacturing"),
    ("联发科", "2454.TW", "MediaTek"),
    ("聯發科", "2454.TW", "MediaTek"),
    ("mediatek", "2454.TW", "MediaTek"),
    ("鸿海", "2317.TW", "Hon Hai Precision"),
    ("富士康", "2317.TW", "Hon Hai Precision"),
    ("foxconn", "2317.TW", "Hon Hai Precision"),
    ("台达电", "2308.TW", "Delta Electronics"),
    ("台達電", "2308.TW", "Delta Electronics"),
    ("中华电信", "2412.TW", "Chunghwa Telecom"),
    ("中華電信", "2412.TW", "Chunghwa Telecom"),
    ("富邦金", "2881.TW", "Fubon Financial"),
    ("delta electronics", "2308.TW", "Delta Electronics"),
    ("台塑", "1301.TW", "Formosa Plastics"),
    ("南亚", "1303.TW", "Nan Ya Plastics"),
    ("国泰金", "2882.TW", "Cathay Financial"),
    ("國泰金", "2882.TW", "Cathay Financial"),
    ("中信金", "2891.TW", "CTBC Financial"),
    ("玉山金", "2884.TW", "E.SUN Financial"),
    ("广达", "2382.TW", "Quanta Computer"),
    ("廣達", "2382.TW", "Quanta Computer"),
    ("广达电脑", "2382.TW", "Quanta Computer"),
    ("quanta", "2382.TW", "Quanta Computer"),
    ("华硕", "2357.TW", "ASUSTeK Computer"),
    ("華碩", "2357.TW", "ASUSTeK Computer"),
    ("asus", "2357.TW", "ASUSTeK Computer"),
    ("研华", "2395.TW", "Advantech"),
    ("advantech", "2395.TW", "Advantech"),
    ("统一企业", "1216.TW", "Uni-President Enterprises"),
    ("长荣", "2603.TW", "Evergreen Marine"),
    ("長榮", "2603.TW", "Evergreen Marine"),
    ("长荣海运", "2603.TW", "Evergreen Marine"),
    ("長榮海運", "2603.TW", "Evergreen Marine"),
    ("阳明", "2609.TW", "Yang Ming Marine Transport"),
    ("日月光", "3711.TW", "ASE Technology"),
    ("ase technology", "3711.TW", "ASE Technology"),
    ("信骅", "5274.TWO", "ASPEED Technology"),
    ("信驊", "5274.TWO", "ASPEED Technology"),
    ("aspeed", "5274.TWO", "ASPEED Technology"),
)
_GLOBAL_COMPANY_ALIASES_SORTED = tuple(
    sorted(_GLOBAL_COMPANY_ALIASES, key=lambda row: len(row[0]), reverse=True)
)
_CRYPTO_ALIAS_TO_SYMBOL: dict[str, str] = {}
for _symbol, _meta in _CRYPTO_CATALOG.items():
    for _alias in _meta["aliases"]:
        _key = _alias_key(str(_alias))
        _CRYPTO_ALIAS_TO_SYMBOL[_key] = _symbol
        _compact_key = _compact_alias_key(str(_alias))
        if _compact_key != _key:
            _CRYPTO_ALIAS_TO_SYMBOL[_compact_key] = _symbol

# ── In-memory state ───────────────────────────────────────────────────────────

_by_ticker: dict[str, dict] = {}  # {"CRWV": {"name_en": "CoreWeave, Inc...", "name_zh": "..."}}
_alias_to_ticker: dict[str, str] = {}
_stock_alias_keys: set[str] = set()
_fmp_cache: dict[str, tuple[float, dict | None]] = {}  # ticker → (expiry_ts, profile)


def _load_aliases() -> None:
    global _by_ticker, _alias_to_ticker, _stock_alias_keys
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[ticker_resolver] aliases load failed: {e}", flush=True)
        return
    for entry in data:
        t = (entry.get("ticker") or "").strip().upper()
        if not t:
            continue
        name_en = (entry.get("name_en") or "").strip()
        name_zh = (entry.get("name_zh") or "").strip()
        _by_ticker[t] = {
            "name_en": name_en,
            "name_zh": name_zh,
        }
        if name_zh:
            zh_key = _alias_key(name_zh)
            _stock_alias_keys.add(zh_key)
            _alias_to_ticker.setdefault(zh_key, t)
        if name_en:
            lowered = name_en.lower()
            en_key = _alias_key(lowered)
            _stock_alias_keys.add(en_key)
            _alias_to_ticker.setdefault(en_key, t)
            short = lowered.split(",")[0].split("(")[0].strip()
            if short.endswith(".com"):
                short = short[:-4].strip()
            for suffix in (
                " inc.", " corp.", " ltd.", " co.", " plc", " corporation",
                " limited", " company", " holdings", " group", " technologies",
                " technology", " platforms", " semiconductor", " systems",
                " software", " services", " international", " automotive",
                " n.v.", " s.a.", " ag", " se", " incorporated",
            ):
                if short.endswith(suffix):
                    short = short[: -len(suffix)].strip()
                    break
            if short and len(short) > 4:
                short_key = _alias_key(short)
                _stock_alias_keys.add(short_key)
                _alias_to_ticker.setdefault(short_key, t)
    print(f"[ticker_resolver] loaded {len(_by_ticker)} US tickers", flush=True)


_load_aliases()


# ── Name compaction ───────────────────────────────────────────────────────────

_NAME_TAIL_SUFFIXES = (
    " Class A Common Stock", " Class B Common Stock", " Class C Capital Stock",
    " Common Stock", " Common Shares", " Ordinary Shares",
    " American Depositary Shares", " ADRs", " ADS",
)


def _compact_name(name_en: str) -> str:
    """Strip boilerplate suffixes to save tokens.

    'CoreWeave, Inc. Class A Common Stock' → 'CoreWeave, Inc.'
    """
    n = name_en
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_TAIL_SUFFIXES:
            if n.endswith(suffix):
                n = n[: -len(suffix)].rstrip()
                changed = True
                break
    return n


# ── FMP fallback ──────────────────────────────────────────────────────────────

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _fmp_profile(ticker: str) -> dict | None:
    """HTTP GET /stable/profile?symbol=X. Returns {name_en, name_zh} or None."""
    if not _FMP_API_KEY or not _env_bool("HERMES_FMP_PROFILE_FALLBACK_ENABLED", False):
        return None
    url = (
        f"{_FMP_BASE}/profile?symbol={urllib.parse.quote(ticker)}"
        f"&apikey={_FMP_API_KEY}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "investlog-hermes/1.0"})
        with urllib.request.urlopen(req, timeout=_FMP_TIMEOUT) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except Exception as e:
        print(f"[ticker_resolver] FMP fallback failed for {ticker}: {e}", flush=True)
        return None
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    name = (row.get("companyName") or "").strip()
    if not name:
        return None
    return {"name_en": name, "name_zh": name}


def _cached_fmp(ticker: str) -> dict | None:
    now = time.time()
    hit = _fmp_cache.get(ticker)
    if hit and hit[0] > now:
        return hit[1]
    result = _fmp_profile(ticker)
    _fmp_cache[ticker] = (now + _CACHE_TTL, result)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def extract_tickers(text: str) -> list[str]:
    """Extract candidate ticker symbols from text.

    Order-preserving, deduplicated. Filters noise words. Uppercases input
    selectively so 'crwv' and 'CRWV' both resolve.
    """
    seen: set[str] = set()
    out: list[str] = []
    # Match uppercase candidates in original text (strict).
    for m in _TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in _NOISE_WORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    # Also try tokens that are lowercase but resolve to known tickers
    # (handles user typing "crwv" instead of "CRWV").
    for tok in re.findall(
        r"\b(?:[A-Za-z0-9]{2,14}USD|\d{4}\.(?:TWO|TW|HK|T)|\d{6}\.(?:KS|KQ)|[A-Za-z]{2,6}(?:\.[A-Za-z])?)\b",
        text,
        flags=re.IGNORECASE,
    ):
        u = tok.upper()
        if u in seen or u in _NOISE_WORDS:
            continue
        if u in _by_ticker or re.match(r"^([A-Z0-9]{2,14}USD|\d{4}\.(TWO|TW|HK|T)|\d{6}\.(KS|KQ))$", u):
            seen.add(u)
            out.append(u)
    return out


def lookup_ticker(ticker: str) -> dict | None:
    """Return {name_en, name_zh} for ticker, or None.

    Order: in-memory JSON → FMP profile (24h cached) → None.
    """
    t = ticker.upper()
    if t in _CRYPTO_CATALOG:
        name = str(_CRYPTO_CATALOG[t]["name"])
        return {"name_en": name, "name_zh": name}
    for _, symbol, name in _GLOBAL_COMPANY_ALIASES:
        if symbol == t:
            return {"name_en": name, "name_zh": name}
    if t in _by_ticker:
        return _by_ticker[t]
    return _cached_fmp(t)


def _ascii_word_in_text(text_key: str, alias_key: str) -> bool:
    return re.search(
        rf"(?<![a-z0-9]){re.escape(alias_key)}(?![a-z0-9])",
        text_key,
    ) is not None


def _alias_in_text(text: str, alias: str) -> bool:
    text_key = _alias_key(text)
    alias_key = _alias_key(alias)
    if not alias_key:
        return False
    compact_text = _compact_alias_key(text)
    compact_alias = _compact_alias_key(alias)
    if compact_alias and (compact_alias != alias_key or not alias_key.isascii()) and compact_alias in compact_text:
        return True
    if alias_key.isascii():
        return _ascii_word_in_text(text_key, alias_key)
    return alias_key in text_key


def _split_query_parts(text: str) -> list[str]:
    """Split mixed CN/ASCII input without losing compact phrases like btc价格."""
    raw_parts = re.split(r"[\s,，。？?!！、;；:：/()（）]+", text or "")
    parts: list[str] = []
    for part in raw_parts:
        if not part:
            continue
        sub_parts = re.split(
            r"(?<=[^\x00-\x7f])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[^\x00-\x7f])",
            part,
        )
        for sub in sub_parts:
            cleaned = sub.strip(" \t\r\n'\"`“”‘’[]{}<>")
            if cleaned:
                parts.append(cleaned)
    return parts


def _clean_crypto_token(token: str) -> tuple[str, bool]:
    key = _alias_key(token.strip("?？.!！,，。；;:：'\"`“”‘’"))
    had_suffix = False
    for suffix in _CRYPTO_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[: -len(suffix)].strip()
            had_suffix = True
            break
    return key, had_suffix


def _matched_name_refs(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for alias, ticker, name in _GLOBAL_COMPANY_ALIASES_SORTED:
        if ticker in seen:
            continue
        if _alias_in_text(text, alias):
            seen.add(ticker)
            refs.append((ticker, name))
            if len(refs) >= _MAX_REF_COUNT:
                break
    return refs


def _matched_stock_alias_refs(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(ticker: str) -> None:
        if ticker in seen or ticker in _POPULAR_TICKERS or len(refs) >= _MAX_REF_COUNT:
            return
        info = lookup_ticker(ticker)
        if not info:
            return
        name = info.get("name_en") or info.get("name_zh") or ""
        if not name:
            return
        seen.add(ticker)
        refs.append((ticker, name))

    whole = _alias_key(text)
    if whole in _alias_to_ticker:
        add(_alias_to_ticker[whole])

    for part in _split_query_parts(text):
        key = _alias_key(part)
        if not key or key.upper() in _NOISE_WORDS:
            continue
        ticker = _alias_to_ticker.get(key)
        if ticker:
            add(ticker)
    return refs


def _matched_crypto_refs(text: str) -> list[tuple[str, str]]:
    text_key = _alias_key(text)
    compact_text = _compact_alias_key(text)
    has_crypto_context = bool(_CRYPTO_CONTEXT_RE.search(text))
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(symbol: str) -> None:
        if symbol in seen or len(refs) >= _MAX_REF_COUNT:
            return
        meta = _CRYPTO_CATALOG.get(symbol)
        if not meta:
            return
        seen.add(symbol)
        refs.append((symbol, str(meta["name"])))

    # Full Chinese/English names first. English aliases use word boundaries so
    # "link" does not fire inside "linked"; conflict-prone one-word aliases are
    # gated below unless there is crypto context.
    for alias_key, symbol in sorted(_CRYPTO_ALIAS_TO_SYMBOL.items(), key=lambda x: len(x[0]), reverse=True):
        abbr = symbol[:-3] if symbol.endswith("USD") else symbol
        if abbr in _CRYPTO_CONTEXT_ONLY_ABBREVS and alias_key == abbr.lower() and not has_crypto_context:
            continue
        if alias_key in _stock_alias_keys and not has_crypto_context:
            continue
        if alias_key.isascii():
            hit = _ascii_word_in_text(text_key, alias_key)
        else:
            hit = alias_key in text_key
        if (
            not hit
            and alias_key in compact_text
            and (not alias_key.isascii() or len(alias_key) >= 5 or any(ch.isdigit() for ch in alias_key))
        ):
            hit = True
        if hit:
            add(symbol)

    for part in _split_query_parts(text):
        key, had_suffix = _clean_crypto_token(part)
        if not key:
            continue
        upper = key.upper()
        if upper in _CRYPTO_SAFE_ABBREVS:
            if upper in _by_ticker and not (had_suffix or has_crypto_context):
                continue
            add(f"{upper}USD")
            continue
        if upper in _CRYPTO_CONTEXT_ONLY_ABBREVS and (had_suffix or has_crypto_context):
            add(f"{upper}USD")
            continue
        symbol = _CRYPTO_ALIAS_TO_SYMBOL.get(key)
        if symbol:
            abbr = symbol[:-3] if symbol.endswith("USD") else symbol
            if abbr in _CRYPTO_CONTEXT_ONLY_ABBREVS and not (had_suffix or has_crypto_context or key != abbr.lower()):
                continue
            add(symbol)

    return refs


def _append_ref(refs: list[str], seen: set[str], ticker: str, name: str | None = None) -> None:
    if len(refs) >= _MAX_REF_COUNT:
        return
    t = ticker.upper()
    if t in seen:
        return
    if not name:
        info = lookup_ticker(t)
        if not info:
            return
        name = info.get("name_en") or info.get("name_zh") or ""
    compact_name = _compact_name(name).replace(", ", ",").strip()
    if not compact_name:
        return
    if len(compact_name) > _MAX_REF_NAME_CHARS:
        compact_name = compact_name[:_MAX_REF_NAME_CHARS].rstrip()
    seen.add(t)
    refs.append(f"{t}={compact_name}")


def resolve_and_inject(msg: str) -> str:
    """Return a compact reference prefix for tickers needing LLM disambiguation.

    Skips mega-caps (LLM already knows them). Returns empty string if no
    injection needed. Format: '(ref: CRWV=CoreWeave,Inc.; RIVN=Rivian)\\n'
    """
    if not msg:
        return ""
    refs: list[str] = []
    seen: set[str] = set()

    for t in extract_tickers(msg):
        if t in _POPULAR_TICKERS:
            continue
        _append_ref(refs, seen, t)
        if len(refs) >= _MAX_REF_COUNT:
            break

    if len(refs) < _MAX_REF_COUNT:
        for ticker, name in _matched_name_refs(msg):
            _append_ref(refs, seen, ticker, name)
            if len(refs) >= _MAX_REF_COUNT:
                break

    if len(refs) < _MAX_REF_COUNT:
        for ticker, name in _matched_stock_alias_refs(msg):
            _append_ref(refs, seen, ticker, name)
            if len(refs) >= _MAX_REF_COUNT:
                break

    if len(refs) < _MAX_REF_COUNT:
        for ticker, name in _matched_crypto_refs(msg):
            _append_ref(refs, seen, ticker, name)
            if len(refs) >= _MAX_REF_COUNT:
                break

    if not refs:
        return ""
    return f"(ref: {'; '.join(refs)})\n"


if __name__ == "__main__":
    # Quick manual test
    import sys
    test_msgs = sys.argv[1:] or [
        "CRWV 最近内部人员交易情况",
        "TSLA 今天多少钱",
        "Compare CRWV vs RIVN vs TSLA",
        "crwv insider trading",
        "随便聊聊",
        "AAPL earnings",
    ]
    for m in test_msgs:
        print(f"INPUT:  {m!r}")
        print(f"PREFIX: {resolve_and_inject(m)!r}")
        print()
