from pathlib import Path


def test_fmp_tools_cannot_be_silently_disabled_by_legacy_flag():
    start_script = (Path(__file__).parents[1] / "start.sh").read_text()

    assert 'if [ -n "$FMP_API_KEY" ]; then' in start_script
    assert "FMP_MCP_ENABLED_NORMALIZED" not in start_script
    assert "FMP MCP server disabled by FMP_MCP_ENABLED" not in start_script
    assert 'url: "https://financialmodelingprep.com/mcp?apikey=${FMP_API_KEY}"' in start_script


def test_earnings_and_transcript_tool_contract_is_explicit():
    start_script = (Path(__file__).parents[1] / "start.sh").read_text()

    assert "`statements` tool" in start_script
    assert "`earningsTranscript`" in start_script
    assert "`transcripts-dates-by-symbol`" in start_script
    assert "`search-transcripts`" in start_script


def test_latest_financial_data_cannot_mix_ttm_with_quarterly_statements():
    start_script = (Path(__file__).parents[1] / "start.sh").read_text()

    assert "Query `income-statement` first" in start_script
    assert "Treat `quarter`, `year`, and `TTM` as different scopes" in start_script
    assert "Never label TTM key" in start_script
    assert "same newest quarterly income-statement row" in start_script
    assert "Never move a decimal point during localization" in start_script


def test_transcript_numbers_preserve_period_and_language_contract():
    start_script = (Path(__file__).parents[1] / "start.sh").read_text()

    assert 'Match the user\'s language' in start_script
    assert 'Chinese: "以上内容仅供参考，不构成投资建议。"' in start_script
    assert "Preserve the source's time scope for every transcript number" in start_script
    assert 'Never rewrite' in start_script
    assert 'as "this quarter"' in start_script
    assert "Cross-check transcript claims" in start_script
    assert "matching quarterly" in start_script
    assert 'never use a bare relative label such as "this' in start_script
    assert "`FQ3 actual`, `FQ4 guidance`, `FY2026 plan`" in start_script
    assert "Keep reported-quarter actuals, forward guidance" in start_script
    assert "call the quarterly statements for the matching period" in start_script
    assert "Every earnings-call answer must use this order" in start_script
    assert "reported fiscal-quarter actuals" in start_script
    assert "full-year" in start_script
    assert "cumulative amounts and period-end balances" in start_script
    assert "Before sending, scan the draft" in start_script
    assert "Chinese disclaimer, never the English disclaimer" in start_script
