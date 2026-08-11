"""Deterministic latest-quarter evidence injected before Hermes tool planning."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from ticker_resolver import resolve_query_tickers


_FINANCIAL_INTENT_RE = re.compile(
    r"(财报|財報|财务|財務|业绩|業績|电话会|電話會|法说会|法說會|"
    r"earnings|financial|revenue|eps|guidance|transcript|conference call)",
    re.IGNORECASE,
)
_LATEST_RE = re.compile(r"(最新|最近|当前|當前|latest|current|most recent)", re.IGNORECASE)
_EXPLICIT_PERIOD_RE = re.compile(
    r"(?:20\d{2}|FY\s*\d{2,4}|F?Q[1-4]|第[一二三四1234]季度)",
    re.IGNORECASE,
)
_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _is_latest_financial_question(message: str) -> bool:
    if not _FINANCIAL_INTENT_RE.search(message):
        return False
    if _LATEST_RE.search(message):
        return True
    return not _EXPLICIT_PERIOD_RE.search(message)


def _fetch_latest_income_statement(symbol: str, api_key: str) -> dict | None:
    with _cache_lock:
        cached = _cache.get(symbol)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    query = urllib.parse.urlencode({
        "symbol": symbol,
        "period": "quarter",
        "limit": 1,
        "apikey": api_key,
    })
    request = urllib.request.Request(
        f"https://financialmodelingprep.com/stable/income-statement?{query}",
        headers={"User-Agent": "InvestLog-Hermes/1.0"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    row = payload[0]
    with _cache_lock:
        _cache[symbol] = (time.monotonic(), row)
    return row


def _money(value, currency: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unavailable"
    absolute = abs(number)
    prefix = "$" if currency == "USD" else f"{currency} "
    if absolute >= 1_000_000_000:
        return f"{prefix}{number / 1_000_000_000:.3f}B"
    if absolute >= 1_000_000:
        return f"{prefix}{number / 1_000_000:.3f}M"
    return f"{prefix}{number:.2f}"


def build_latest_financial_evidence_prefix(
    message: str,
    *,
    api_key: str | None = None,
    fetcher: Callable[[str, str], dict | None] | None = None,
) -> str:
    """Return a compact freshness anchor for one-company latest earnings queries."""
    if not isinstance(message, str) or not _is_latest_financial_question(message):
        return ""
    if "(latest-financial-evidence:" in message[:2000]:
        return ""
    symbols = resolve_query_tickers(message)
    if len(symbols) != 1:
        return ""
    key = api_key if api_key is not None else os.environ.get("FMP_API_KEY", "")
    if not key:
        return ""
    try:
        row = (fetcher or _fetch_latest_income_statement)(symbols[0], key)
    except Exception:
        return ""
    if not row:
        return ""

    symbol = symbols[0]
    fiscal_year = row.get("fiscalYear") or "unknown"
    period = row.get("period") or "unknown"
    period_end = row.get("date") or "unknown"
    currency = str(row.get("reportedCurrency") or "USD").upper()
    diluted_eps = row.get("epsDiluted", "unavailable")
    if currency != "USD":
        diluted_eps = (
            "use official issuer release (FMP may report ADR-equivalent EPS for "
            "non-US issuers)"
        )
    evidence = (
        f"symbol={symbol}; latestQuarter=FY{fiscal_year} {period}; periodEnd={period_end}; "
        f"reportingCurrency={currency}; revenue={_money(row.get('revenue'), currency)}; "
        f"grossProfit={_money(row.get('grossProfit'), currency)}; "
        f"operatingIncome={_money(row.get('operatingIncome'), currency)}; "
        f"netIncome={_money(row.get('netIncome'), currency)}; dilutedEPS={diluted_eps}"
    )
    return (
        "(latest-financial-evidence: Deterministic current quarterly statement anchor; "
        f"{evidence}. Never label any earnings release or call older than periodEnd as latest. "
        "For a latest call, use the matching-period transcript or official investor-relations "
        "release/prepared remarks; if unavailable, state that limitation without substituting "
        "an older call.)\n"
    )
