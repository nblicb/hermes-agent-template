"""Fund / institution alias resolver — counterpart to ticker_resolver.

Why: queries like "段永平的13F" / "巴菲特持仓" need to be answered by calling
the institutional-ownership FMP tool, which requires a 10-digit CIK. The LLM
does not know that "段永平" → H&H International, CIK 0001759760 (and there
are ~80 other famous-investor aliases). Without an authoritative map, the
LLM either makes up a CIK or punts with "please provide the CIK".

Strategy: scan the user message for any alias from config/aliases_funds.json
and inject a compact reference prefix:

    (fund-ref: 段永平=H&H International CIK 0001759760)
    段永平的13F

Then the LLM can call the institutional-ownership tool with the right CIK
on the first turn.

Maintainer: keep config/aliases_funds.json in sync with the source-of-truth
in nblicb/tg-invest-bot-v2/config/aliases_funds.json. Adding a new famous
investor is a single-line PR in both repos.
"""
from __future__ import annotations

import json
from pathlib import Path

_ALIASES_PATH = Path(__file__).resolve().parent / "config" / "aliases_funds.json"

# {alias_lowercase: (display_alias, fund_name, cik)}
# Only entries with both `name` and `cik` are kept — aliases that exist
# without a CIK (e.g. ARK family, which is an ETF, not a 13F filer) are
# intentionally skipped because the LLM can't act on them anyway.
_FUND_ALIASES: dict[str, tuple[str, str, str]] = {}


def _load() -> None:
    global _FUND_ALIASES
    if not _ALIASES_PATH.exists():
        return
    try:
        raw = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    out: dict[str, tuple[str, str, str]] = {}
    for alias, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        cik = entry.get("cik")
        name = entry.get("name") or ""
        if not cik or not name:
            continue
        out[alias.lower()] = (alias, name, cik)
    _FUND_ALIASES = out


_load()


def resolve_and_inject(msg: str) -> str:
    """Return a compact reference prefix for fund aliases in `msg`, or "".

    - Case-insensitive substring match (works for both Chinese and English).
    - Longest-alias-first to avoid matching "桥水" inside "桥水基金达利欧".
    - Dedup by CIK (one fund's many aliases all collapse to one ref).
    - Empty message / no matches → empty string (no injection).
    """
    if not msg or not _FUND_ALIASES:
        return ""
    msg_lower = msg.lower()
    seen_ciks: set[str] = set()
    refs: list[str] = []
    # Sort by length desc so "桥水基金" wins over "桥水" when both alias
    # forms exist in the dict (and so we don't double-emit a ref).
    sorted_aliases = sorted(
        _FUND_ALIASES.items(), key=lambda x: len(x[0]), reverse=True
    )
    for alias_lower, (display, name, cik) in sorted_aliases:
        if cik in seen_ciks:
            continue
        if alias_lower in msg_lower:
            seen_ciks.add(cik)
            refs.append(f"{display}={name} CIK {cik}")
    if not refs:
        return ""
    return f"(fund-ref: {'; '.join(refs)})\n"


if __name__ == "__main__":
    # Quick manual test
    import sys
    tests = sys.argv[1:] or [
        "段永平的13F",
        "巴菲特最新持仓",
        "桥水持仓变化",
        "ARK Invest 买了什么",  # ARK has no CIK in the map → no ref
        "聊聊天气",                # no fund mentioned → empty
        "段永平 vs 巴菲特",       # two funds → two refs
    ]
    for m in tests:
        print(f"INPUT:  {m!r}")
        print(f"PREFIX: {resolve_and_inject(m)!r}")
        print()
