"""Ticker → company name resolver with token-minimizing injection.

Why: LLM training data lacks tickers that IPO'd after cutoff (e.g. CRWV =
CoreWeave, Inc., IPO 2025-03). Without an authoritative name map, the LLM
confabulates (CRWV → CRISPR Therapeutics is wrong, that's CRSP). We inject
a compact "(ref: CRWV=CoreWeave,Inc.)" prefix so the LLM sees the correct
company before generating.

Strategy:
1. POPULAR_TICKERS whitelist (~30 mega-caps): skip injection (0 token).
2. Lookup in aliases_cn_us.json (~27k rows, ships with image): compact inject.
3. Fallback: FMP /profile HTTP call with 24h in-memory cache (for new IPOs
   not yet in the JSON snapshot).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

_ALIASES_PATH = Path(__file__).resolve().parent / "config" / "aliases_cn_us.json"
_FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 4.0  # seconds
_CACHE_TTL = 86400  # 24h

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

# Ticker candidates: 1-5 uppercase letters, optional .X.X suffix (BRK.A).
# Word boundary ensures we don't pull tickers out of random ALLCAPS noise.
_TICKER_RE = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")

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

# ── In-memory state ───────────────────────────────────────────────────────────

_by_ticker: dict[str, dict] = {}  # {"CRWV": {"name_en": "CoreWeave, Inc...", "name_zh": "..."}}
_fmp_cache: dict[str, tuple[float, dict | None]] = {}  # ticker → (expiry_ts, profile)


def _load_aliases() -> None:
    global _by_ticker
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
        _by_ticker[t] = {
            "name_en": (entry.get("name_en") or "").strip(),
            "name_zh": (entry.get("name_zh") or "").strip(),
        }
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

def _fmp_profile(ticker: str) -> dict | None:
    """HTTP GET /stable/profile?symbol=X. Returns {name_en, name_zh} or None."""
    if not _FMP_API_KEY:
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
    for tok in re.findall(r"\b[A-Za-z]{2,5}(?:\.[A-Za-z])?\b", text):
        u = tok.upper()
        if u in seen or u in _NOISE_WORDS:
            continue
        if u in _by_ticker:
            seen.add(u)
            out.append(u)
    return out


def lookup_ticker(ticker: str) -> dict | None:
    """Return {name_en, name_zh} for ticker, or None.

    Order: in-memory JSON → FMP profile (24h cached) → None.
    """
    t = ticker.upper()
    if t in _by_ticker:
        return _by_ticker[t]
    return _cached_fmp(t)


def resolve_and_inject(msg: str) -> str:
    """Return a compact reference prefix for tickers needing LLM disambiguation.

    Skips mega-caps (LLM already knows them). Returns empty string if no
    injection needed. Format: '(ref: CRWV=CoreWeave,Inc.; RIVN=Rivian)\\n'
    """
    if not msg:
        return ""
    candidates = extract_tickers(msg)
    if not candidates:
        return ""
    refs: list[str] = []
    for t in candidates:
        if t in _POPULAR_TICKERS:
            continue
        info = lookup_ticker(t)
        if not info:
            continue
        name = _compact_name(info.get("name_en") or info.get("name_zh") or "")
        if not name:
            continue
        # Drop spaces around commas to save tokens: "CoreWeave, Inc." → "CoreWeave,Inc."
        compact = name.replace(", ", ",")
        refs.append(f"{t}={compact}")
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
