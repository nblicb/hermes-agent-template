from pathlib import Path

import financial_evidence
from financial_evidence import build_latest_financial_evidence_prefix
from rate_limit import _inject_reference_prefix
from ticker_resolver import resolve_query_tickers


NVDA_ROW = {
    "date": "2026-04-26",
    "fiscalYear": "2027",
    "period": "Q1",
    "revenue": 81_615_000_000,
    "grossProfit": 61_157_000_000,
    "operatingIncome": 53_536_000_000,
    "netIncome": 58_321_000_000,
    "epsDiluted": 2.39,
    "reportedCurrency": "USD",
}
NVDA_TRANSCRIPT = {
    "symbol": "NVDA",
    "period": "Q1",
    "year": 2027,
    "date": "2026-05-20",
    "content": (
        "Colette Kress: Total revenue was $82 billion and data center revenue "
        "was $75 billion. Jen-Hsun Huang: Agentic AI demand has gone parabolic."
    ),
}
TSM_ROW = {
    **NVDA_ROW,
    "date": "2026-06-30",
    "fiscalYear": "2026",
    "period": "Q2",
    "reportedCurrency": "TWD",
    "revenue": 1_270_381_000_000,
    "epsDiluted": 136.25,
}
TSM_TRANSCRIPT = {
    "symbol": "TSM",
    "period": "Q2",
    "year": 2026,
    "date": "2026-07-16",
    "content": (
        "Wendell Huang: For third quarter 2026, revenue is expected between "
        "$44.6 billion and $45.8 billion. Gross margin is expected between "
        "65% and 67%, and operating margin between 56% and 58%."
    ),
}


def test_resolves_popular_chinese_company_name_for_evidence_routing():
    assert resolve_query_tickers("英伟达最新财报电话会议讲了什么？") == ["NVDA"]


def test_latest_call_gets_deterministic_current_quarter_anchor():
    prefix = build_latest_financial_evidence_prefix(
        "英伟达最新财报电话会议讲了什么？",
        api_key="test-key",
        fetcher=lambda symbol, _key: NVDA_ROW if symbol == "NVDA" else None,
        transcript_fetcher=lambda symbol, year, quarter, _key: (
            NVDA_TRANSCRIPT
            if (symbol, year, quarter) == ("NVDA", "2027", 1)
            else None
        ),
    )

    assert "latestQuarter=FY2027 Q1" in prefix
    assert "periodEnd=2026-04-26" in prefix
    assert "revenue=$81.615B" in prefix
    assert "dilutedEPS=2.39" in prefix
    assert "Never label any earnings release or call older than periodEnd as latest" in prefix
    assert "matchingQuarter=FY2027 Q1" in prefix
    assert "callDate=2026-05-20" in prefix
    assert "Total revenue was $82 billion" in prefix
    assert "sequentialComparisonQuarter=FY2026 Q4" in prefix
    assert "yearOverYearComparisonQuarter=FY2026 Q1" in prefix
    assert "nextQuarter=FY2027 Q2" in prefix
    assert "twoQuartersAhead=FY2027 Q3" in prefix
    assert "threeQuartersAhead=FY2027 Q4" in prefix
    assert "fourQuartersAhead=FY2028 Q1" in prefix
    assert "最终答案必须使用中文" in prefix


def test_explicit_historical_period_does_not_get_latest_anchor():
    assert build_latest_financial_evidence_prefix(
        "NVDA 2025 Q2 财报",
        api_key="test-key",
        fetcher=lambda _symbol, _key: NVDA_ROW,
    ) == ""


def test_non_us_statement_preserves_currency_and_rejects_adr_eps_as_issuer_eps():
    prefix = build_latest_financial_evidence_prefix(
        "TSM latest financials",
        api_key="test-key",
        fetcher=lambda _symbol, _key: TSM_ROW,
        transcript_fetcher=lambda _symbol, _year, _quarter, _key: None,
    )
    assert "reportingCurrency=TWD" in prefix
    assert "revenue=TWD 1270.381B" in prefix
    assert "ADR-equivalent EPS" in prefix
    assert "dilutedEPS=136.25" not in prefix


def test_matching_transcript_can_correct_statement_provider_eps():
    row = {
        **NVDA_ROW,
        "date": "2026-06-27",
        "fiscalYear": "2026",
        "period": "Q3",
        "revenue": 109_417_000_000,
        "epsDiluted": 2.03,
    }
    transcript = {
        "symbol": "AAPL",
        "period": "Q3",
        "year": 2026,
        "date": "2026-07-30",
        "content": "Diluted earnings per share was $2.02, up 29% year-over-year.",
    }
    prefix = build_latest_financial_evidence_prefix(
        "苹果最新财务数据如何？",
        api_key="test-key",
        fetcher=lambda symbol, _key: row if symbol == "AAPL" else None,
        transcript_fetcher=lambda symbol, year, quarter, _key: (
            transcript if (symbol, year, quarter) == ("AAPL", "2026", 3) else None
        ),
    )
    assert "dilutedEPS=2.03 (statement-provider value" in prefix
    assert "the issuer value wins" in prefix
    assert "Diluted earnings per share was $2.02" in prefix


def test_guidance_question_prefetches_matching_transcript_context():
    prefix = build_latest_financial_evidence_prefix(
        "台积电最新财务数据和下一季度指引是什么？",
        api_key="test-key",
        fetcher=lambda symbol, _key: TSM_ROW if symbol == "TSM" else None,
        transcript_fetcher=lambda symbol, year, quarter, _key: (
            TSM_TRANSCRIPT
            if (symbol, year, quarter) == ("TSM", "2026", 2)
            else None
        ),
    )
    assert "matchingQuarter=FY2026 Q2" in prefix
    assert "$44.6 billion and $45.8 billion" in prefix
    assert "65% and 67%" in prefix
    assert "nextQuarter=FY2026 Q3" in prefix


def test_existing_evidence_is_not_injected_twice():
    already_injected = (
        "(ref: NVDA = NVIDIA)\n"
        "(latest-financial-evidence: existing anchor)\n"
        "英伟达最新财报"
    )
    assert build_latest_financial_evidence_prefix(
        already_injected,
        api_key="test-key",
        fetcher=lambda _symbol, _key: NVDA_ROW,
    ) == ""


def test_missing_matching_transcript_explicitly_forbids_older_call_fallback():
    prefix = build_latest_financial_evidence_prefix(
        "NVDA latest earnings call",
        api_key="test-key",
        fetcher=lambda _symbol, _key: NVDA_ROW,
        transcript_fetcher=lambda _symbol, _year, _quarter, _key: None,
    )
    assert "No transcript matching" in prefix
    assert "Do not summarize or substitute any older earnings call" in prefix


def test_shared_message_injection_is_idempotent(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(
        financial_evidence,
        "_fetch_latest_income_statement",
        lambda symbol, _key: NVDA_ROW if symbol == "NVDA" else None,
    )
    monkeypatch.setattr(
        financial_evidence,
        "_fetch_matching_transcript",
        lambda symbol, year, quarter, _key: (
            NVDA_TRANSCRIPT
            if (symbol, year, quarter) == ("NVDA", "2027", 1)
            else None
        ),
    )

    first = _inject_reference_prefix("英伟达最新财报电话会议讲了什么？")
    second = _inject_reference_prefix(first)

    assert first.count("(earnings-analysis-guide:") == 1
    assert first.count("(latest-financial-evidence:") == 1
    assert first.count("(latest-call-evidence:") == 1
    assert second.count("(earnings-analysis-guide:") == 1
    assert second.count("(latest-financial-evidence:") == 1
    assert second.count("(latest-call-evidence:") == 1


def test_financial_evidence_module_is_copied_into_production_image():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert "COPY financial_evidence.py /app/financial_evidence.py" in dockerfile.read_text()
