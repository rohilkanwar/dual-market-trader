# Runbook: `gated_cross_venue` (clause-matched / settlement-safe paper track)

Paper only. Nothing here places, signs or routes a live order; every entry point
refuses to start when `TRADING_MODE=live` or `ENABLE_LIVE_TRADING=true`.

## What the track is

`gated_cross_venue` measures how many Kalshi/Polymarket pairs are **settlement-
equivalent** and, for those, what a fee- and depth-aware YES/NO lock would earn
on paper. It exists because prior arbAI research (and the basis-risk literature,
arXiv 2601.01706 on semantic non-fungibility) concluded that ungated cross-venue
macro is *not* validated: two contracts with the same headline can settle on
different publishers, periods, tie-break or revision rules.

Per matched pair, in this order and fail-closed:

1. `settlement.gate.settlement_gate` runs eight stages (match confidence, clauses,
   fingerprint, polarity, Fed bucket, interval, hosts, expiry). **Any** failure
   refuses the pair before pricing; every failing stage is recorded.
2. `strategies.paper_edge.compute_paper_edge` walks both books, charges each
   venue's fee schedule on the consumed levels and stops at the first level whose
   marginal contract no longer clears `minimum_net_edge` (default 1c).
3. Two paper orders (cheap YES, dear NO) go through the track's own
   `RiskManager` / `ExecutionEngine` / `PaperLedger`.

`ungated_cross_venue_macro` is the **control**: the same macro pairs traded with
no gate. It is flagged `settlement_risk` whenever it trades a pair the gate
refused, and its PnL is not realisable (legs may settle on different events).

## Run it

```bash
uv sync --extra dev
uv run pytest -q                                   # gate tests included

# Deterministic fixtures (Fed pair admitted, CPI/NBA refused)
uv run python -m apps.measure_all --artifact-dir artifacts

# Live public data, read-only (Kalshi macro series + Polymarket macro search)
uv run python -m apps.measure_all --network --limit 40 --kalshi-env prod --artifact-dir artifacts

# Continuous paper loop (ledgers carry across cycles)
uv run python -m apps.paper_loop --network --limit 40 --kalshi-env prod --interval-seconds 900
```

Outputs (all under `--artifact-dir`):

| File | What to read |
| --- | --- |
| `gate_report_<mode>.json`, `gate_report_latest.json` | `totals` (candidates / gate_admitted / gate_refused / traded / status), `stage_failures`, `pairs[]` with every stage's verdict and details, `vetoed_candidates`, `control` |
| `scoreboard_<mode>.json` | `tracks[]` row `gated_cross_venue`; `findings.gated_cross_venue`; `gate_report.totals` |
| `paper/ledger_gated_cross_venue.json` | fills, marks, equity curve for the track |

The CLI prints a one-line gate headline and the per-stage reject histogram.

## Reading the result

* `status: zero_admits_expected` with `candidates > 0` is the normal, informative
  outcome: live pairs were found and the report says exactly which stage refused
  each one. Do not "fix" this by loosening the policy.
* `gate_admitted > 0, traded == 0` (`priced_but_no_edge`): settlement-equivalent
  pairs exist but books do not cross after fees/depth. Still zero paper orders.
* `traded > 0`: check `pairs[].edge.paper_edge` (levels, VWAP, fees) and confirm
  the fingerprints were supplied by an operator, not inferred.
* A mid-marked YES+NO lock shows the crossed spread plus fees as **negative**
  unrealized PnL until settlement; `expected_locked_pnl_if_settlement_equivalent`
  is the payout the lock guarantees *if* both legs settle on the same event.

Reject reasons, by stage:

| Stage | Reasons | Fix (operator) |
| --- | --- | --- |
| match | `match_low_confidence` | add a `CuratedPair` or pass `curated_pairs=` |
| clauses | `clause_refuse_unreadable`, `clause_refuse_mismatch` | none: venues' texts disagree (e.g. Polymarket "rounded up", Kalshi silent) |
| fingerprint | `fingerprint_indeterminate`, `fingerprint_not_equivalent` | supply `metadata.fingerprint` on **both** markets (see below) |
| polarity | `fingerprint_polarity_conflict`, `polarity_unverified` | fix the pair's `same_polarity` or the fingerprint comparator |
| fed_bucket | `fed_bucket_one_side/domain/union/no_match/unparseable` | none: different FOMC outcome buckets |
| interval | `interval_mismatch/one_side/not_complementary/unparseable` | none: a point bucket is not a threshold |
| hosts | `host_missing/self_referential/tier_not_allowed/conflict/publisher_differs` | none: different resolution publishers |
| expiry | `expiry_one_side/mismatch/unparseable` | none: different close times (> 72 h) |

## Making a live pair admissible

Network markets carry `resolution_text`, `source_url` and `close_time` but **no
fingerprint**, so every live pair is at least `fingerprint_indeterminate`. To
admit one, an operator must attach identical structured fingerprints to both
markets (publisher, release_id, comparator, threshold, reference_period,
revisions_included, fallback_to_prior_period, tie_break), and the venues' own
rule texts must agree on fallback / tie-break / revisions. There is no code path
that infers a fingerprint from text on purpose.

Curated pairs live in `strategies/matching.py::CURATED_PAIRS`; the fixture pair
`fed-rate-cut-september` is the reference for a fully admissible pair.

## Policy

`settlement.gate.STRICT_POLICY`: heuristic confidence >= 0.6, hosts must be
`official` tier and the same registrable domain on both sides, close times within
72 hours. Loosening any knob is a `GatePolicy(name=...)` whose name is written
into every report (`meta.policy`), so a loosened run can never pass as strict.

## Publish to the dashboard

```bash
cd dashboard
npm ci
npm run sync-artifacts    # copies scoreboard_*, gate_report_*, run records; rebuilds experiments_index.json
npm run typecheck && npm run build
git add public/artifacts && git commit -m "Refresh paper snapshots"
```

The experiments index joins each `gate_report_<mode>.json` to its run by
`meta.run_id`; the run detail shows `gate a/n admitted` and links the report.

## Never

* Do not set `TRADING_MODE=live` / `ENABLE_LIVE_TRADING=true` for this track.
* Do not treat `ungated_cross_venue_macro` PnL as achievable.
* Do not add `fed_bucket` / `fingerprint` metadata you have not read from the
  venues' published rules.
