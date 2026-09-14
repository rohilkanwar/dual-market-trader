from core.types import Market, Venue
from strategies.matching import CURATED_PAIRS, MarketMatcher, consistent_reference, same_title_polarity


def test_curated_pairs_win_and_are_reported_as_curated() -> None:
    kalshi = [Market(Venue.KALSHI, "KX-FED-SEP-CUT", "Will the Fed cut rates at the September meeting?")]
    poly = [Market(Venue.POLYMARKET, "0xfixture-fed-september", "Will the Fed cut rates in September?")]

    pairs = MarketMatcher().match(kalshi, poly)

    assert len(pairs) == 1
    assert pairs[0].method == "curated"
    assert pairs[0].confidence == 1.0
    assert pairs[0].pair_id == CURATED_PAIRS[0].pair_id


def test_heuristic_match_on_similar_titles_keeps_polarity() -> None:
    kalshi = Market(Venue.KALSHI, "K", "Will the Fed cut rates in September 2026?")
    polymarket = Market(Venue.POLYMARKET, "P", "Fed rate cut in September 2026?")

    pair = MarketMatcher(curated_pairs=()).match([kalshi], [polymarket])[0]

    assert pair.method == "heuristic"
    assert pair.same_polarity
    assert same_title_polarity(kalshi.title, polymarket.title)


def test_heuristic_can_use_slug_and_event_context() -> None:
    kalshi = Market(
        venue=Venue.KALSHI,
        market_id="K-RATE",
        title="Contract A",
        metadata={"event_title": "Federal Reserve September rate decision"},
    )
    polymarket = Market(
        venue=Venue.POLYMARKET,
        market_id="P-RATE",
        title="Market B",
        metadata={"slug": "fed-september-rate-decision"},
    )

    pair = MarketMatcher(curated_pairs=()).match([kalshi], [polymarket])[0]

    assert pair.method == "heuristic"


def test_unrelated_markets_do_not_match() -> None:
    kalshi = Market(Venue.KALSHI, "K", "Will New York beat Boston?")
    polymarket = Market(Venue.POLYMARKET, "P", "Will August CPI come in above 3.0%?")

    assert MarketMatcher(curated_pairs=()).match([kalshi], [polymarket]) == []


def test_negated_title_flips_polarity() -> None:
    assert not same_title_polarity("Will CPI be above 3%?", "Will CPI fail to exceed 3%?")


def test_heuristic_candidates_that_disagree_on_month_are_vetoed_and_recorded() -> None:
    kalshi = Market(Venue.KALSHI, "K", "Will the Fed cut rates in September 2026?")
    december = Market(Venue.POLYMARKET, "P", "Fed rate cut in December 2026?")
    matcher = MarketMatcher(curated_pairs=())

    assert matcher.match([kalshi], [december]) == []
    assert [v.as_dict() for v in matcher.vetoed] == [
        {"kalshi": "K", "polymarket": "P", "confidence": 0.5, "reason": "period_disagrees"}
    ]


def test_heuristic_candidates_that_disagree_on_year_or_threshold_are_vetoed() -> None:
    matcher = MarketMatcher(curated_pairs=())
    kalshi = Market(Venue.KALSHI, "K", "Will the Fed cut rates in September 2026?")
    next_year = Market(Venue.POLYMARKET, "P", "Fed rate cut in September 2027?")
    assert matcher.match([kalshi], [next_year]) == []
    assert matcher.vetoed[0].reason == "period_disagrees"

    cpi = Market(Venue.KALSHI, "K2", "Will August CPI be above 3.0%?")
    other_threshold = Market(Venue.POLYMARKET, "P2", "Will August CPI be above 3.5%?")
    assert matcher.match([cpi], [other_threshold]) == []
    assert matcher.vetoed[0].reason == "threshold_disagrees"


def test_consistent_reference_tolerates_silence_on_one_side() -> None:
    # One side names no period/threshold: not a veto here; the gate's fingerprint stage decides.
    kalshi = Market(Venue.KALSHI, "K", "Will the Fed cut rates in September 2026?")
    silent = Market(Venue.POLYMARKET, "P", "Will the Fed cut rates at the next meeting?")
    assert consistent_reference(kalshi, silent) == (True, "ok")
    assert MarketMatcher(curated_pairs=()).match([kalshi], [silent])
