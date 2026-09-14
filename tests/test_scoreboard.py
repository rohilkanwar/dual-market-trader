from research.scoreboard import measure_all


async def test_parallel_scoreboard_keeps_tracks_isolated() -> None:
    summaries = {summary.track: summary for summary in await measure_all()}

    assert set(summaries) == {
        "gated_cross_venue",
        "gated_cross_venue_macro",
        "ungated_cross_venue_macro",
        "single_venue_fair_value",
        "sports_cross_venue",
        "small_deliberate_bet",
        "news_underreaction",
        "polymarket_rebalancing_arb",
        "polymarket_negrisk_arb",
        "polymarket_combinatorial_arb",
        "kalshi_longshot_fade",
        "kalshi_maker_quote",
        "category_specialist",
    }

    strict = summaries["gated_cross_venue"]
    assert strict.candidates == 3  # every matched pair, all categories
    assert strict.admitted == 1  # only the Fed pair passes all eight stages
    assert strict.refused_by_reason == {"clause_refuse_mismatch": 1, "fingerprint_indeterminate": 1}
    assert strict.metrics["gate_reject_reasons_all"]["host_tier_not_allowed"] == 1  # NBA: media host
    assert strict.proposed_orders == 2
    assert not strict.settlement_risk_flag
    assert strict.metrics["gate_policy"]["name"] == "strict"

    gated = summaries["gated_cross_venue_macro"]
    assert gated.candidates == 2
    assert gated.admitted == 1
    assert gated.refused_by_reason == {"clause_refuse_mismatch": 1}
    assert gated.proposed_orders == 2
    assert (
        gated.metrics["refused_pairs"]["august-cpi-over-3"]["fingerprint_relation"]
        == "indeterminate"
    )

    ungated = summaries["ungated_cross_venue_macro"]
    assert ungated.admitted == 2
    assert ungated.proposed_orders == 4
    assert ungated.paper_fills == 4
    assert ungated.settlement_risk_flag

    fair_value = summaries["single_venue_fair_value"]
    assert fair_value.metrics["venue_breakdown"]["kalshi"]["fills"] == 2
    assert fair_value.metrics["venue_breakdown"]["polymarket"]["fills"] == 2
    assert fair_value.metrics["hit_rate"] is not None

    sports = summaries["sports_cross_venue"]
    assert sports.candidates == 1
    assert sports.metrics["host_conflicts"] == 1
    assert sports.paper_fills == 2

    small_bet = summaries["small_deliberate_bet"]
    assert small_bet.admitted == 1
    assert small_bet.paper_fills == 2
    assert small_bet.estimated_fees_buffer < gated.estimated_fees_buffer

    news = summaries["news_underreaction"]
    assert news.candidates == 8 and news.admitted == 3 and news.paper_fills == 3
    assert news.metrics["status"] == "fixture_synthetic"
    assert not news.settlement_risk_flag
    # The news lane trades the same fixture markets as the fair-value track but
    # on its own ledger: fills never leak between tracks.
    assert news.ledger["ledger_id"] == "news_underreaction"
    assert fair_value.ledger["fills"] == 4 and news.ledger["fills"] == 3

    specialist = summaries["category_specialist"]
    assert specialist.candidates == 6 and specialist.admitted == 3 and specialist.paper_fills == 3
    assert specialist.metrics["status"] == "fixture_synthetic"
    assert specialist.ledger["ledger_id"] == "category_specialist" and specialist.ledger["fills"] == 3
    # The lane fills on the same Polymarket fixture markets as the fair-value and
    # news tracks, on its own ledger and its own snapshot object.
    assert not specialist.settlement_risk_flag
