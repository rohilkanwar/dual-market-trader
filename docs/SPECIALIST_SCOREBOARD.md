# `category_specialist` — category specialist scoreboard (paper lane)

Paper-only measurement of one pre-registered hypothesis:

> Large traders who have been good **in one category** (positive rolling ROI
> *and* positive directional Brier skill, scored on that category alone) go on
> to beat the market mid on their next bets **in that same category**.

Nothing in this lane places, signs or routes an order. Paper follows are
simulated against a frozen public book by the same engine as every other
track and booked on the lane's own `PaperLedger`. All data is free and public:
the Polymarket Data API (leaderboard, per-wallet closed and open positions,
UMA resolution feed) and the CLOB book endpoint. Kalshi publishes no
per-trader data, so the lane is **Polymarket-only** by construction and says
so in `metrics.venue_scope`.

Status legend follows `docs/ASSUMPTIONS.md`: **PASS** = validated by code plus
a test or reproducible run here; **UNKNOWN** = cannot be validated from this
repository (what evidence would be needed is stated).

## Pre-registration

Registered 2026-09-14, before any network follow had resolved. Emitted verbatim
in every artifact under `preregistration` (`strategies/specialist.py::preregistration`).

| Item | Value |
| --- | --- |
| Unit of analysis | one paper-followed bet, pooled across categories |
| Primary metric | `mean_excess_vs_mid` = mean over resolved follows of `1[won] − p_dir`, where `p_dir` is the YES mid at follow time expressed for the followed direction |
| Secondary metric | mean directional Brier skill of the followed direction against the mid at follow time |
| Test | one-sided exact binomial sign test on hit rate (H0 = 0.5) **and** `mean_excess_vs_mid > 0` |
| N | **30** resolved follows |
| α | 0.05 (at N = 30 the sign test needs ≥ 20 wins) |
| Underpowered rule | fewer than 30 resolved follows → `evaluation_status = underpowered`; never `pass`, never `fail` |
| Per-category readouts | descriptive only; not individually powered |

**Sample-size status (honest):** as of the committed 2026-09-14 network
snapshot the pooled follow log holds **0 resolved follows**. The fixture path
resolves 3. Both are reported as `pending_resolutions` / `underpowered`; no
pass or fail has been declared, and none can be until the paper loop has
accumulated 30 resolved follows across cycles. The parameters below are
operator choices made before seeing outcomes, not calibrated values.

Parameters (`SpecialistParameters`): `window_bets=20`, `min_resolved_bets=10`,
`min_category_notional=1000`, `top_fraction=0.1`, `forecast_shade=0.5`,
`follow_quantity=10`, `max_spread=0.10`, `preregistered_n=30`, `alpha=0.05`.

## Measurement

All prices are probabilities of the market's first outcome (YES). A bet has a
`direction` (YES or NO), an `entry_price` paid for that direction and a `size`.

```text
cost            = size * entry_price
pnl             = size * (1[won] - entry_price)            # or venue-reported realizedPnl when present
roi             = sum(pnl) / sum(cost)                     # over the in-category window

# directional Brier skill for one resolved bet against a YES benchmark m
o     = 1 if the market resolved YES else 0
f     = m + shade * (1 - m)      if direction is YES
      = m - shade * m            if direction is NO
skill = (m - o)^2 - (f - o)^2                              # > 0: tilting toward the bet beat the benchmark
```

`m` is the market mid at entry when known (fixtures) and the trader's
YES-equivalent entry price otherwise (network; counted in `mid_proxy_bets`).

### Scoring (per trader, per category)

* **Rolling window:** the trader's most recent `window_bets = 20` resolved bets
  **in that category**. Bets in other categories never enter the score; they
  only appear in `specialization` (share of the trader's window notional in
  this category).
* **Scored** ("large" with enough history): `resolved_bets >= 10` and window
  notional `>= 1000`.
* **Promotion:** within a category, scored traders are ranked by ROI (Brier
  skill breaks ties). The top `ceil(0.1 * n_scored)` ranks that also have
  `roi > 0` **and** `brier_skill > 0` are the specialists. A category with one
  scored trader promotes at most that one, and only if it is positive.
* The taxonomy's catch-all `other` is scored for the record but never promoted
  (`unclassified_category`).

Reasons a row is not promoted (all listed, first blocking one is not special):
`insufficient_history`, `below_notional`, `negative_roi`, `negative_brier`,
`below_top_decile`, `unclassified_category`.

### Follow leg (first blocking reason wins)

For every currently open bet in the batch:

| Reason | Condition |
| --- | --- |
| `trader_not_promoted` | the trader is not a specialist in any category |
| `out_of_category` | the trader is a specialist, but not in this bet's category |
| `already_followed` | this (trader, market, direction) is already in the follow log |
| `duplicate_market_direction` | another specialist's bet on the same market/direction was followed this run |
| `market_not_in_snapshot` | no book could be fetched for the market |
| `market_inactive` | market flagged inactive |
| `market_past_end_date` | the position's `endDate` has passed (stale book, resolution pending) |
| `no_two_sided_book` | no bid or no ask: there is no mid to beat |
| `spread_too_wide` | `ask − bid > max_spread` (0.10): the mid is not a meaningful benchmark |
| `no_ask_for_direction`, `touch_at_bound` | nothing to lift, or the touch is 0/1 |
| `position_already_held` | the ledger already holds this market |
| `risk_*`, `no_fill` | `RiskManager` refusal on submit; book too thin |

An admitted bet becomes a limit **buy of the specialist's direction at the
touch for 10 contracts** through `TrackRuntime.submit` → `ExecutionEngine`
→ `RiskManager` → paper fill simulator. The `FollowedBet` records the YES mid
at follow time (`mid_at_follow`), the fill, and later the outcome.

### Settlement and evaluation

At the start of every run, pending follows of the **current mode** are checked
against the resolution oracle (fixtures: `paper_settlement_outcome`; network:
`GET /v2/resolutions?condition=` — `resolved` with UMA price `1e18` → YES,
`0` → NO; split payouts stay unresolved). Resolved markets are settled on the
ledger at 1/0 (`order_id = settlement`). The follow log then feeds
`evaluate_follows` (pooled and per category).

Fixture and network follows share one state file
(`artifacts/paper/specialist_follow_state.json`) but are settled and evaluated
separately: synthetic fixture outcomes never count toward the network test
(`follow_log.other_modes` shows how many follows belong to the other mode).

## Data

| Source | Endpoint | What it gives | Notes |
| --- | --- | --- | --- |
| `PolymarketDataApiSource` | `GET /v2/leaderboard?time_period=month&sort_by=VOLUME&limit=25` | the "large traders" | selection by volume, not PnL, to avoid picking winners |
| | `GET /closed-positions?user=&sortBy=TIMESTAMP&limit=50` (≤ 2 pages) | resolved bets: `avgPrice`, `totalBought`, `realizedPnl`, `curPrice` ∈ {0,1} | `curPrice` outside {0,1} = exited before resolution → excluded; the row's `timestamp` is the close time (used as `resolved_at`; placement time is unknown) |
| | `GET /positions?user=&limit=100` | open bets (candidate "next" bets) **and** resolved-but-unredeemed positions (`curPrice` ∈ {0,1}), which count as resolved bets | dropping unredeemed positions scored only the winners a wallet bothered to redeem: a live canary promoted a "100 % hit rate" wallet that disappeared once its unredeemed losers counted |
| `polymarket_book_fetcher` | `POST clob.polymarket.com/books` | YES book for each candidate market | the follow fills against this book |
| `PolymarketResolutionOracle` | `GET /v2/resolutions?condition=` | settlement of followed markets | |
| `FixtureTraderSource` | `research/fixtures/specialist_trades.json` | **synthetic** histories aligned to `venues/polymarket/fixtures/markets.json` | proves the arithmetic and every rail |
| `NullTraderSource` | — | nothing | `--specialist-traders 0`; the lane reports `no_trader_source` |

Every source records failures on `TraderHistoryBatch.errors` and never raises
into the scoreboard. One network run with 25 wallets is 76 unauthenticated
GETs (~15 s).

### Category taxonomy

Data API rows carry no category. `specialist_category()` maps Gamma tag
labels (when present), then the event-slug prefix (`nfl-`, `atp-`, `lol-`,
`fifwc-`, `lal-`, …), then title keywords, then a club-abbreviation heuristic
for association-football fixtures ("Will CA Platense win on …", "Spread:
Genoa CFC (−1.5)") to one of: `tennis`, `soccer`, `basketball`,
`american_football`, `baseball`, `hockey`, `mma`, `esports`, `motorsport`,
`golf`, `cricket`, `crypto`, `macro`, `politics`, `weather`, `entertainment`,
`other`. `taxonomy_coverage` in every artifact shows how many bets landed in
each bucket; on the 2026-09-14 snapshot 130 of 4 695 bets (2.8 %) were `other`.

## Fixtures

`research/fixtures/specialist_trades.json` (synthetic; the file says so in
`_note`): eight traders, 100 resolved bets, six open bets. Expected result
(asserted in `tests/test_specialist.py` and `tests/test_scoreboard.py`):

| Trader / category | Window | Verdict | Why |
| --- | --- | --- | --- |
| `fx-macro-alpha` / macro | 14 bets, ROI +0.62, Brier +0.11 | **PROMOTED** (rank 1 of 3) | two bets without a recorded mid exercise the entry-price proxy |
| `fx-macro-beta` / macro | 12, +0.33, +0.02 | `below_top_decile` | positive on both, ranked 2nd; decile of three is one |
| `fx-macro-gamma` / macro | 10, −0.20, −0.10 | `negative_roi`, `negative_brier`, `below_top_decile` | |
| `fx-macro-theta` / macro | 12, +0.50, +0.06 | `below_notional` | 5-contract bets, notional 30 |
| `fx-hoops-delta` / basketball | 12, +0.49, +0.06 | **PROMOTED** | also 6 tennis bets → `insufficient_history` there |
| `fx-hoops-epsilon` / basketball | 11 longshots at 0.20, ROI +0.36, Brier −0.11 | `negative_brier`, `below_top_decile` | ROI-positive but the directional tilt lost |
| `fx-tennis-zeta` / tennis | 15, +0.42, +0.05 | **PROMOTED** | open bet is on a market outside the snapshot |
| `fx-crypto-eta` / crypto | 8 | `insufficient_history` | |

Follow leg, run 1: 6 open bets → 3 followed (Fed YES @ 0.43 vs mid 0.42, CPI
NO @ 0.61 vs YES mid 0.405, NBA YES @ 0.53 vs mid 0.52), refused
`out_of_category` 1 (alpha's NBA bet), `trader_not_promoted` 1 (beta),
`market_not_in_snapshot` 1 (zeta). Ledger: 3 fills, unrealized −0.35 (paid the
touch, marked at mid), `equity == 1000 + realized + unrealized`.

Run 2 (carried state and ledger): the fixture oracle resolves all three
(YES / NO / YES) → 3 wins, realized +14.30, `mean_excess_vs_mid = 0.4883`,
sign-test p = 0.125, **`underpowered`** ("27 more needed"). Those three wins
are a property of hand-written fixtures, not a result.

## Run it

```bash
python -m research.specialist_scoreboard                               # fixtures; state+ledger under artifacts/paper/
python -m research.specialist_scoreboard --network --traders 25        # public Data API + CLOB books
python -m research.specialist_scoreboard --network --artifact-dir artifacts --json /tmp/spec.json
python -m apps.measure_all --network --kalshi-env prod --specialist-traders 25   # all 11 tracks
python -m apps.paper_loop --network --interval-seconds 900             # accumulate follows + settlements
python -m apps.measure_all --network --specialist-traders 0            # lane disabled: no_trader_source
```

Artifacts: the lane is a normal track row (`tracks[]`, `portfolio.by_track`,
`charts.*`); `findings.category_specialist` carries the headline
(`evaluation_status`, `hit_rate`, `mean_excess_vs_mid`, `sign_test_p`,
`hypothesis_validated`); the full board — per-trader-category scores,
promotions, informative follow attempts, follow log, pre-registration,
`not_validated[]` — is `specialist_scoreboard_<mode>.json` /
`specialist_scoreboard_latest.json`. Whenever the current mode's follow log is
non-empty and the pooled test has not passed the board carries the risk flag
`specialist_hypothesis_not_validated`.

## 2026-09-14 network snapshot (committed)

25 wallets by 30-day volume; 4 695 bets (3 954 resolved, 741 open); 132
(trader, category) rows, 58 scored. Promoted: one `american_football`
specialist (20 bets, ROI +0.25, Brier +0.03, hit 0.70) and one `tennis`
specialist (20 bets, ROI +0.76, Brier +0.17, hit 0.80). Neither had an
admissible open bet in its category at capture time (their 11 open bets were
`out_of_category`), so **0 follows, `evaluation_status = no_follows`**. An
earlier canary the same day, before unredeemed positions were counted,
promoted five wallets and followed 21 bets against live CLOB books (fills at
the touch, mids recorded); that run is not committed because its scoring was
survivorship-biased.

## Assumption audit

| # | Assumption | Status | Evidence / what would be needed |
| --- | --- | --- | --- |
| S.1 | ROI, directional Brier skill, rolling window and promotion are implemented as specified | **PASS** | `test_directional_brier_skill_hand_calculation`, `test_bet_pnl_roi_and_brier_proxy`, `test_rolling_window_is_count_based_and_in_category_only`, `test_scoring_reasons`, `test_promotion_is_top_decile_per_category_and_positive`, `test_promotion_breaks_roi_ties_on_brier_skill`, `test_unclassified_category_is_scored_but_never_promoted`. |
| S.2 | Promotion is in-category only | **PASS** | A trader ranked first in tennis and last in soccer is promoted in tennis only; the same trader's soccer bets are refused `out_of_category` (`test_promotion_is_top_decile_per_category_and_positive`, `test_fixture_track_scores_promotes_and_follows`). |
| S.3 | The pre-registered test is evaluated exactly as registered and never declares pass/fail below N | **PASS** | `test_binomial_tail_and_wins_required` (exact tail, 20 wins at N = 30), `test_evaluation_statuses_follow_the_preregistration` (no_follows / pending / underpowered / pass / fail, including significant-but-negative-excess → fail; per-category never powered). |
| S.4 | Follows go through the shared execution and risk path onto their own ledger; settlement books 1/0 | **PASS** | `TrackRuntime.submit` → `ExecutionEngine.submit` → `RiskManager`; `PaperLedger(ledger_id="category_specialist")`; `test_fixture_track_scores_promotes_and_follows` (equity identity), `test_carried_state_settles_follows_and_stays_underpowered` (realized +14.30 = 10·(1−0.43)+10·(1−0.61)+10·(1−0.53)). |
| S.5 | Fixture and network follows never share an evaluation | **PASS** | `test_fixture_follows_never_enter_the_network_evaluation`. |
| S.6 | Source failures cannot kill the scoreboard; a network run without a source is an honest empty | **PASS** | `test_exploding_source_does_not_take_the_scoreboard_down`, `test_data_api_leaderboard_failure_is_reported_not_raised`, `test_null_source_is_an_honest_empty_lane`. |
| S.7 | Data API rows are parsed correctly (direction from `outcomeIndex`, resolution from `curPrice`, token ordering, exited positions excluded, unredeemed positions counted) | **PASS (mechanism)** | `test_parse_closed_positions_from_data_api_rows`, `test_parse_open_positions_from_data_api_rows`, `test_parse_leaderboard_accepts_v2_and_v1_shapes`, `test_parse_resolution_reads_uma_price`; live 2026-09-14: 76 requests, 0 errors. |
| S.8 | Stale and unpriceable markets are refused before any follow | **PASS** | `test_follow_refuses_stale_markets_and_wide_books`. Live canary motivated both rails (open positions weeks past `endDate`; a 40 c-wide book). |
| S.9 | **The hypothesis** (in-category specialists beat the mid on their next bets) | **UNKNOWN — underpowered** | 0 resolved network follows vs N = 30. Requires the paper loop to run across resolutions; the artifact says `underpowered` until then and `pass`/`fail` afterwards. |
| S.10 | Category taxonomy assigns the right category | **UNKNOWN / heuristic** | Keyword, slug-prefix and club-abbreviation tables; 2.8 % `other` on the snapshot, but no ground truth for the rest. Gamma tags would be authoritative when present (the source consults them); a per-market Gamma lookup is the upgrade path. |
| S.11 | Entry price is an acceptable Brier benchmark proxy on network | **UNKNOWN** | The Data API has no mid-at-entry. With the proxy, skill measures whether tilting from the trader's own price toward their direction reduced squared error. Fixtures carry true mids; `mid_proxy_bets` is reported per row. |
| S.12 | "Next bet" = currently open position | **UNKNOWN / operational** | `/positions` carries no placement timestamp, so an open bet may predate the scoring window. Outcomes of open bets are unknown, so this cannot leak outcomes into the score; it can weaken the "next" semantics. `/trades?user=` would give placement times at one more request per wallet. |
| S.13 | `totalBought` × `avgPrice` is the right cost basis and venue `realizedPnl` the right PnL | **UNKNOWN / venue-defined** | Positions with partial exits before resolution are excluded (`curPrice` ∉ {0,1}); for resolved-unredeemed rows PnL is recomputed from held size because `realizedPnl` excludes the payoff. |
| S.14 | Follow PnL is realistic | **UNKNOWN / pre-fee** | Data API rows carry no `feeType`, so followed network markets are filled fee-free; the ledger PnL pays the spread but not taker fees. The pre-registered metric is against the mid and does not depend on this. |
| S.15 | Volume-leaderboard wallets are "large traders" | **UNKNOWN / operator choice** | Top 25 by 30-day both-sides volume; cap 50 per page. Market makers dominate this list; their closed positions are still scored like anyone else's. |
| S.16 | Literature anchor | **context only** | Domain-specific persistence of forecaster skill (superforecasters; Mellers et al. 2015; Tetlock & Gardner 2015). No published estimate for Polymarket wallets by category; nothing is validated here. |

## Not in v1 (deliberately)

* No Kalshi leg: no per-trader public data exists.
* No placement-time reconstruction from `/trades`; see S.12.
* No fee model on followed network markets; see S.14.
* No Gamma per-market category lookup; see S.10.
* No live or demo-authenticated path; the lane inherits every rail from
  `docs/ASSUMPTIONS.md` §3.
