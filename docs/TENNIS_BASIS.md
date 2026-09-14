# `tennis_basis` — cross-platform tennis basis vs. free public odds

Paper-only measurement track. It asks one pre-registered question: **when a
Kalshi or Polymarket tennis match-winner mid drifts ≥ 3¢ away from a free public
consensus line, does the venue mid move back toward that line before the match
starts?** While a gap is open the track paper-leans toward the sharp side; the
lean is unwound at the last pre-start mid, so no paper position is ever exposed
to a tennis settlement.

$0 and paper-only by construction: no paid odds vendor, no scraping, no venue
credentials, no order ever leaves the process. Status legend follows
`docs/ASSUMPTIONS.md` (**PASS** = code + test or reproducible run here;
**UNKNOWN** = cannot be validated from this repository, evidence needed stated).

## Pre-registration

| Item | Value | Where |
| --- | --- | --- |
| Gap | `gap_t = venue_mid_t − outside_p_t` for the market's YES player, both contemporaneous, YES probabilities | `strategies/tennis_basis.py::gap` |
| Open | `abs(gap_0) ≥ 0.03` on a market that passed the settlement-basis filter, paired to exactly one outside event, with a two-sided book, before the scheduled start | `research/tennis_basis.py::run_tennis_basis_cycle` |
| Lean | `gap > 0` → sell YES at the bid; `gap < 0` → buy YES at the ask; built by the primary track's fair-value engine with the outside line as the prior (fee buffer Kalshi 0.02 / Polymarket 0.01, 10-contract cap, `RiskManager` capacity) | `TennisBasisLean` |
| Final observation | the last observation **strictly before** the scheduled start; in-play prints are recorded but never enter the measurement | `_close_record` |
| Closure | `closure_fraction = (gap_0 − gap_T) / gap_0`; `≥ 0.5` counts as closed; `> 1` (crossed through the line) counts as closed and is reported as an overshoot; `< 0` widened | `closure_fraction` |
| Sample | records `closed_at_start` **and** `settlement_status = confirmed` (see filter below) | `verdict` |
| **Pass** | `n ≥ 30` and `closed_half / n ≥ 0.60`; `n < 30` → `insufficient_sample`; a Wilson 95 % interval is reported, not gating | `verdict` |
| Secondary | the same rule per venue (`by_venue`) and including still-pending settlements (`verdict_provisional_including_pending`) | report |

Parameters live in `BasisParameters` and are echoed into every artifact
(`metrics.parameters`, `verdict.pre_registered`). The CLI exposes them under
"pre-registered parameters (override only for sensitivity checks)".

## The free outside line

**Source: The Odds API** (`https://the-odds-api.com`), official keyed API with a
free tier. Facts read from its v4 documentation on 2026-09-14:

* Starter plan: **500 credits / month**, API key by e-mail sign-up, no payment
  details. Personal / non-commercial use.
* `GET /v4/sports` is free of quota and lists in-season sport keys; tennis keys
  are per tournament (`tennis_atp_us_open`, `tennis_wta_wuhan_open`, …;
  Grand Slams, ATP/WTA 1000 and 500).
* `GET /v4/sports/{key}/odds?regions=eu&markets=h2h&oddsFormat=decimal` costs
  `markets × regions` = **1 credit per tournament per call** and returns every
  `eu` bookmaker's match-winner odds. **Pinnacle** is in the `eu` region
  (`key = pinnacle`; the docs note its odds come from the public website and
  "may incur a delay").
* Response headers `x-requests-remaining` / `x-requests-used` carry the budget.

`research/tennis_odds.py::OddsApiSource` spends at most `--max-credits` per run
(default 6) and stops when `x-requests-remaining` would fall below
`--min-remaining` (default 20). With ~4 in-season tennis keys, one run costs 4
credits; **a run every 6 h ≈ 480 credits / month**, inside the free tier.

Consensus (`strategies/tennis_basis.py::consensus_line`): each book's two-way
odds are de-vigged multiplicatively (`p_a = (1/o_a) / (1/o_a + 1/o_b)`), quotes
older than `max_quote_age_seconds` (3600) are dropped, and the line is
**Pinnacle when quoted, else the median across ≥ 2 books**
(`consensus_insufficient_books` otherwise). Each record stores which method and
which books produced its opening line.

Other inputs accepted by the same parser: `--odds-file` (a saved v4 `/odds`
response; the report then says the provenance is the operator's) and the fixture
replay. **No other site is read.** Without `ODDS_API_KEY` or `--odds-file` a
network run is an honest empty: `status = no_outside_source`,
`network_status = UNKNOWN: …`, zero gaps, zero fills.

## Venue universe (verified on the public APIs, 2026-09-14)

| Venue | Discovery | YES player | Start time | Rules text |
| --- | --- | --- | --- | --- |
| Kalshi | `GET /markets?series_ticker=KXATPMATCH|KXWTAMATCH&status=open` (public, no key). One market per player; ticker `KXWTAMATCH-26SEP15STETJE-TJE`, `yes_sub_title = "Janice Tjen"`, event title `"Stephens vs Tjen"` from `GET /events/{event_ticker}` | `yes_sub_title`; opponent parsed from the event title | `occurrence_datetime` — a **session** time (several same-day matches share `21:00Z`) and it can be stale after a rain postponement | `rules_primary + rules_secondary` |
| Polymarket | Gamma `GET /markets?tag_id=864&active=true&closed=false` (public), kept only when `sportsMarketType == "moneyline"` and the two `outcomes` are player names (28 of the top 100 tennis markets; set winners, totals, handicaps and futures are dropped) | `outcomes[0]` (the YES/first CLOB token) | `gameStartTime` (exact) | `description` |

Only one side of a Kalshi match is measured per run (the first listed; the other
is `duplicate_side`) so a match is never counted twice. Pairing with the outside
feed requires the YES player to match exactly one side of exactly one event
(surname plus first-initial agreement, accents stripped), the opponent to match
the other side, and the start times to agree within 48 h; two candidates →
`ambiguous_match`, none → `no_outside_match`. The record's start is the outside
`commence_time` when paired, else the venue field; it is revised on later runs
if the feed moves it (`start_revisions`).

Live 2026-09-14 capture: **50 Kalshi and 30 Polymarket** match-winner markets,
every rules text classified (Kalshi `walkover = fair_price`, Polymarket
`walkover = fifty_fifty`, both `retirement = advancing_player`), 0 pairings
because no outside line was configured.

## Settlement-mismatch filter (mandatory)

Two stages, both fail-closed, both recorded on every record.

**At admission** (`strategies/tennis_basis.py::classify_settlement_basis`, run
before pairing or pricing):

| Reads | Kalshi text (verbatim) | Polymarket text (verbatim) | Result |
| --- | --- | --- | --- |
| retirement | "If *Player* wins the … match … **after a ball has been played**, then the market resolves to Yes" (defers to the tour's official result, which awards a retirement to the opponent) | "one player advances due to the opponent's **retirement, default, or disqualification**, this market will resolve to the player who **advances**" | `advancing_player` |
| walkover | "If the match does not occur (signaled by a ball being played) due to a player injury, **walkover**, forfeiture, or any other cancellation …, the market will resolve to a **fair price**" | "If the match ends in a **walkover** …, this market will resolve to **50-50**" | `fair_price` / `fifty_fifty` |
| ITF | series `KXITFMATCH` or "ITF" in the text | "ITF" in the text | **refused** `settlement_basis_itf` (ITF walkovers settle at a flat 0.50 on both venues; the free feed does not cover ITF) |
| silence | no readable retirement or walkover clause | | **refused** `settlement_basis_unreadable` |
| anything else | e.g. walkover → void | | **refused** `settlement_basis_mismatch` |

**After the start** (`research/tennis_basis.py::classify_settlement`): a closed
record is `settlement_pending` until a later run sees the venue's result —
Kalshi `GET /markets/{ticker}` (`status = finalized`, `result ∈ {yes, no}` →
**confirmed**; finalized with any other result = fair-price / cancellation →
**excluded**), Polymarket Gamma `GET /markets?slug=` (`closed` with prices
`[0,1]`/`[1,0]` → **confirmed**; `[0.5,0.5]` or anything non-binary →
**excluded**). An optional `--results-file`
(`{outside_event_id: completed|retired|walkover|cancelled}`) marks
**retirements**, which venue results cannot reveal because both venues pay the
advancing player. Only **confirmed** records enter the primary verdict.

**Bookmaker basis (documented, not modelled).** A match-winner bet is void on a
walkover and, at most books including Pinnacle, void on retirement; the venue
contract pays the advancing player. The outside line is therefore
`P(win | match completed)` while the venue mid prices `P(advances)`. The
difference is bounded by the withdrawal/retirement rate and can legitimately
produce a persistent gap. It is reported on every record (`bookmaker_basis`) and
listed as UNKNOWN below.

## Lifecycle of a record

```text
run k   : admitted + paired + |gap| >= 0.03 + before start  -> record opened, lean submitted (fills booked on the ledger)
run k+1…: observation appended (mid, outside, gap, method, pre_start); start revised if the feed moved it;
          outside line missing this run -> dated observation with gap = None; market delisted -> "market_missing"
start   : first run with as_of >= start closes the record on the last pre-start observation,
          unwinds the position at that mid (order_id = event_start_unwind, venue fee applied),
          computes closure_fraction / closed_half / overshoot, sets settlement_status = pending
later   : settlement check -> confirmed | excluded (pending until the venue reports)
```

Records with no observation between opening and start are `no_pre_start_observation`
and excluded from `n` (counted in `register`). The register is persisted at
`artifacts/tennis_basis/register.json` and reloaded before every run; the ledger
at `artifacts/paper/ledger_tennis_basis.json`. `--reset` starts both fresh.

## Run it

```bash
uv run python -m apps.measure_tennis_basis                          # fixture replay (deterministic, no network)
uv run python -m apps.measure_tennis_basis --network --kalshi-env prod   # public venue books; no key -> honest empty
ODDS_API_KEY=… uv run python -m apps.measure_tennis_basis --network --kalshi-env prod --max-credits 6
uv run python -m apps.measure_tennis_basis --network --kalshi-env prod --odds-file saved_v4_odds.json
uv run python -m apps.measure_tennis_basis --network --kalshi-env prod --results-file data/tennis/results.json
uv run python -m apps.measure_tennis_basis --help
```

A measurement needs **repeated runs**: one opens gaps, later ones observe them,
the first run after each start closes them, and a run after the matches settle
confirms or excludes them. Schedule it (cron / the always-on host from the
README) every few hours with the same `--artifact-dir`; the credit maths above
assumes 4 runs a day.

## Artifacts

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_tennis_basis.json` | dashboard artifact (schema 1.3.0, `meta.source=measured`, `meta.track_family=tennis_basis`, `pnl_source=core.ledger.PaperLedger`); `scoreboard_latest.json` only with `--publish-latest` |
| `artifacts/tennis_basis_latest.json` | full report: pre-registered rule, `verdict` (confirmed) and `verdict_provisional_including_pending`, `by_venue`, per-market `measurements[]` with reason and settlement basis, every `records[]` with observations and paper details, ledger, `network_status`, `not_validated[]` |
| `artifacts/tennis_basis/register.json` | the gap register |
| `artifacts/paper/ledger_tennis_basis.json`, `equity_curve_tennis_basis.jsonl`, `paper/runs/<run_id>.json` | ledger and run manifest |

`npm run sync-artifacts` copies the scoreboard, the report and the ledger into
`dashboard/public/artifacts/`; the track is registered in the `tennis_basis`
family (`dashboard/scripts/track-families.mjs`).

## Fixture replay (`research/fixtures/tennis_basis_replay.json`)

Five synthetic steps (10:00, 14:00, 17:00, 21:30 on 2026-09-15; 12:00 on the
16th) replayed through the live code path. Pinnacle odds are exact-devig pairs
(1.80/2.20 → 0.55, 1.25/5.00 → 0.80, 1.60/2.40 → 0.60, 2.00/2.00 → 0.50). The
outside `commence_time` (20:00) overrides Kalshi's 21:00 session time.

| Record | Opens | `gap_0` → `gap_T` | closure | Settlement | Exercises |
| --- | --- | --- | --- | --- | --- |
| Kalshi Tjen (Stephens vs Tjen) | t0, sell 10 @ 0.60 | +0.06 → +0.02 | 0.667 ✓ | `finalized/yes` → confirmed | narrowing; in-play 0.75 print ignored; unwind at 0.57; Kalshi fees 0.17 + 0.18 |
| Kalshi Parks (Parks vs Bejlek) | t0, sell 10 @ 0.23 | +0.04 → +0.07 | −0.75 ✗ | `finalized/""` → **excluded** (fair-price walkover) | widening; walkover exclusion |
| Polymarket Maria vs Townsend | t0, buy 10 @ 0.36 | −0.05 → +0.03 | 1.6 ✓ overshoot | `[0,1]` → confirmed | crossing the line; 5 % sports taker fee |
| Polymarket Bouzas Maneiro vs Salkova | t1 (below threshold at t0), buy 10 @ 0.61 | −0.0506 → −0.0206 | 0.593 ✓ | operator `retired` → **excluded** | median of 3 books; retirement exclusion |
| Polymarket Gray vs Maloney | t0, sell 10 @ 0.55 | +0.06 → +0.03 | 0.500 ✓ | `closed=false` → pending | exactly half; still pre-start at t3; delisted at t4 (`market_missing`) |

Refused every step: Polymarket ITF market (`settlement_basis_itf`), Kalshi
market without a walkover clause (`settlement_basis_unreadable`), Kenin vs
Sakkari listed twice in the feed (`ambiguous_match`), Sharipov vs Ivashka absent
from the feed (`no_outside_match`), Delaney vs Shiraishi with one non-sharp book
(`consensus_insufficient_books`), Sell vs Mmoh with an asks-only book
(`no_two_sided_mid`), Zverev vs Shelton already started (`event_started`), the
mirror Kalshi sides (`duplicate_side`), and a Fed market in the same snapshot is
not a tennis market at all. Result: 63 venue-market evaluations, 5 gaps, 5 leans
filled, 5 unwinds, everything flat, `equity == 1000 + realized + unrealized`;
verdict `n = 2` confirmed (both closed) → `insufficient_sample`; provisional
`n = 3`; 2 excluded; 1 pending. These numbers are hand-written and say nothing
about the hypothesis. Asserted in `tests/test_tennis_basis.py`.

## Assumption audit

| # | Assumption | Status | Evidence / what would be needed |
| --- | --- | --- | --- |
| T.1 | De-vig, consensus, gap, closure and the pass rule are implemented as pre-registered | **PASS** | `test_multiplicative_devig_is_exact_on_vig_free_and_vigged_pairs`, `test_consensus_prefers_the_sharp_book_then_the_median_and_drops_stale_quotes`, `test_closure_fraction_and_sharp_side_math`, `test_verdict_applies_the_pre_registered_rule` (PASS at 20/30, FAIL at 17/30, `insufficient_sample` at 29, exactly-half counts). |
| T.2 | The settlement-basis filter admits the live Kalshi / Polymarket texts and refuses silence, ITF and void clauses | **PASS** | verbatim 2026-09-14 texts in `test_kalshi_and_polymarket_rule_texts_are_classified_and_admitted`; `test_settlement_basis_filter_is_fail_closed`; live run: 80/80 markets classified, Kalshi `fair_price`, Polymarket `fifty_fifty`. |
| T.3 | Post-start settlements are confirmed only on a binary venue result; walkovers, 50-50, fair-price and operator-reported retirements are excluded | **PASS** | `test_post_start_settlement_classification`; replay records Parks (fair price) and Bouzas (retired) excluded, Tjen / Maria confirmed, Gray pending. Field shapes verified live: Kalshi finalized market `result: "yes"`, Polymarket resolved `outcomePrices ["0","1"]`. |
| T.4 | In-play prints never enter the measurement and the lean is unwound at the last pre-start mid | **PASS** | replay: Tjen closes on the 17:00 observation (+0.02) although 21:30 shows +0.20; unwind fill at 0.57 (`test_replay_measures_every_scripted_branch`). |
| T.5 | Leans go through the same engine, risk rails and ledger as the primary track; PnL is ledger-backed and isolated | **PASS** | `TennisBasisLean` delegates to `CalibratedFairValueStrategy`; `TrackRuntime.submit` → `ExecutionEngine` → `RiskManager`; `PaperLedger(ledger_id="tennis_basis")`; equity identity asserted after the replay; artifact `meta.pnl_source` (`test_scoreboard_artifact_is_measured_and_ledger_backed`). |
| T.6 | The register survives a process restart and a resumed replay equals a straight one | **PASS** | `test_register_round_trips_and_a_resumed_replay_matches_a_straight_one`; a re-run over a closed register opens nothing (`test_replaying_a_closed_register_again_opens_nothing_new`). |
| T.7 | A network run without a key is an honest empty and a failing feed cannot kill the run | **PASS** | `test_network_path_without_a_key_is_an_honest_empty`, `test_exploding_outside_source_does_not_take_the_run_down`, `test_outside_source_failures_are_reported_not_raised`; live 2026-09-14: `status=no_outside_source`, 0 gaps, equity 1000. |
| T.8 | The Odds API client spends within budget, reads the quota headers and only tennis h2h keys | **PASS (against the documented shape)** | `test_odds_api_source_lists_tennis_keys_spends_within_budget_and_reads_headers`; the v4 response shape is taken from the public docs, not from a live call (no key in the build environment). |
| T.9 | Venue tennis universes are readable without keys and normalise to one YES player each | **PASS** | live 2026-09-14: 50 Kalshi (`KXWTAMATCH`; `KXATPMATCH` had no open markets that evening) + 30 Polymarket moneyline markets; `test_venue_markets_are_normalised_to_one_yes_player_each`, `test_pairing_is_fail_closed_on_ambiguity_opponent_and_time_window`; pairing verified live with a local operator odds file (both Kalshi sides + the Polymarket market of Maria vs Townsend paired; started match refused). |
| T.10 | **Live gap distribution and closure rate on Kalshi / Polymarket vs. The Odds API** | **UNKNOWN** | No free-tier key was available where this was built, so `network_status` is UNKNOWN. Needed: an operator sets `ODDS_API_KEY` (free sign-up) and runs the CLI every few hours for weeks until `verdict.n ≥ 30` confirmed records. |
| T.11 | The bookmaker line (`P(win | completed)`) and the venue mid (`P(advances)`) price the same thing | **UNKNOWN / known to differ** | Bookmakers void on walkover and (Pinnacle) retirement; venues pay the advancing player. The gap can carry a legitimate retirement premium of a few cents. Not modelled; `BOOKMAKER_BASIS` is attached to every record. Evidence: a resolved sample large enough to estimate the premium by tour level. |
| T.12 | Pinnacle on the free tier is the sharp line | **UNKNOWN** | The docs say its odds come from the public website and may be delayed; `max_quote_age_seconds` drops quotes older than an hour and each record stores `consensus_method_open` / `books_open` so a median-only sample can be separated. |
| T.13 | Kalshi's `occurrence_datetime` is the first serve | **FAIL as a start time / handled** | Several same-day matches share `21:00Z`; Maria vs Townsend showed `2026-09-13T19:00Z` on Kalshi vs `2026-09-14T21:05Z` on Polymarket after a postponement. The paired outside `commence_time` is used and revised per run; unpaired Kalshi markets never open a record. |
| T.14 | Retirements are detected | **UNKNOWN without an operator file** | Both venues pay the advancing player, so their results do not reveal a retirement; The Odds API `/scores` costs credits and does not flag it. `--results-file` is the extension point. |
| T.15 | Fixture closure rates are evidence | **FAIL as evidence** | Hand-written; they prove branches, not the hypothesis. |

## Not in v1 (deliberately)

* No paid odds vendor (TickOdds or otherwise), no scraping of bookmaker or
  aggregator sites, no `/scores` calls.
* No retirement-premium model (T.11); no per-book sharpness model beyond
  "Pinnacle first" (T.12).
* Set / game / handicap markets, doubles, ITF and Challenger events not covered
  by the free feed are out of scope (`no_outside_match` / `settlement_basis_itf`).
* Not part of `apps.measure_all`'s ten-track board; it is its own family with
  its own CLI, like the Polymarket arb tracks.
