/** Scoreboard artifact types — keep in sync with public/artifacts/schema.md */

export type ArtifactSource = 'sample' | 'measured' | 'synced'
export type MeasureMode = 'network' | 'fixtures'
export type VenueId = 'kalshi' | 'polymarket' | string

export type TrackId =
  | 'gated_cross_venue'
  | 'gated_cross_venue_macro'
  | 'ungated_cross_venue_macro'
  | 'single_venue_fair_value'
  | 'sports_cross_venue'
  | 'small_deliberate_bet'
  | 'news_underreaction'
  | 'polymarket_rebalancing_arb'
  | 'polymarket_negrisk_arb'
  | 'polymarket_combinatorial_arb'
  | string

/**
 * Strategy lane a track belongs to. Assigned by scripts/track-families.mjs when
 * the experiments index is built; the UI never classifies ids itself.
 */
export type TrackFamilyId =
  | 'negrisk'
  | 'kalshi_flb'
  | 'xv_gated'
  | 'xv_ungated'
  | 'cross_venue'
  | 'single_venue'
  | 'news'
  | 'tennis_copy'
  | 'other'
  | string

export interface DivergenceFinding {
  observed: number
  sample_size: number
  label: string
  note?: string
}

export interface ScoreboardMeta {
  source: ArtifactSource
  label: string
  paper_only: boolean
  mode: MeasureMode
  measured_at: string
  generated_at?: string
  venues: VenueId[]
  markets_per_venue?: number
  primary_track?: TrackId
  refresh?: string
  /** Present on ledger-backed artifacts (schema >= 1.2.0). */
  pnl_source?: string
  venue_focus?: string
  kalshi_env?: string | null
  cycle?: number | null
  run_id?: string
  note?: string
  /** Set by family-specific CLIs (e.g. `polymarket_arb`); absent on the full board. */
  track_family?: string | null
}

/** True only for artifacts whose PnL was produced by the paper ledger. */
export function hasLedgerPnl(meta: ScoreboardMeta): boolean {
  return meta.source !== 'sample' && typeof meta.pnl_source === 'string'
}

export interface NewsUnderreactionFinding {
  status: string
  signal_source?: string | null
  signals: number
  mapped: number
  unmapped: number
  paper_fills: number
  reaction_ratio_observed_mean?: number | null
  reaction_ratio_literature?: number | null
  literature_reference?: string | null
  /** Always false: the signal→probability mapping is not validated. */
  mapping_validated: boolean
  note?: string
}

/**
 * Admissibility headline of the settlement-safe `gated_cross_venue` track
 * (schema >= 1.3.0). `gate_admitted` counts pairs that passed every gate stage;
 * `traded` counts those that also had a fee-positive paper edge through depth.
 * Zero is the expected state, not a failure.
 */
export interface GateSummary {
  track: TrackId
  policy: string | null
  candidates: number
  gate_admitted: number
  gate_refused: number
  priced_but_no_edge: number
  traded: number
  paper_fills: number
  primary_reject_reasons: Record<string, number>
  all_stage_reject_reasons: Record<string, number>
  status: 'zero_admits_expected' | 'admits_present_verify_fingerprints' | string | null
}

export interface ScoreboardFindings {
  fed_exact_divergences?: DivergenceFinding
  macro_admitted_bucket_divergences?: DivergenceFinding
  live_network_cross_venue_candidates?: number
  arbai_summary?: string
  news_underreaction?: NewsUnderreactionFinding
  gated_cross_venue?: GateSummary
}

export interface ScoreboardTotals {
  candidates: number
  admitted: number
  rejects: number
  paper_fills: number
  fill_rate: number | null
  settlement_risk_pairs: number
  paper_pnl: number
  avg_edge_bps?: number | null
  proposed_orders?: number
  realized_pnl?: number
  unrealized_pnl?: number
  fees_paid?: number
}

export interface VenueBreakdown {
  markets?: number
  candidates?: number
  admitted?: number
  fills?: number
}

export interface TrackSummary {
  track: TrackId
  label?: string
  candidates: number
  admitted: number
  rejects?: number
  paper_fills: number
  fill_rate?: number | null
  edge_bps?: number | null
  settlement_risk?: boolean
  settlement_risk_flag?: boolean
  proposed_orders?: number
  estimated_fees_buffer?: number
  notes?: string
  reject_reasons?: Record<string, number>
  metrics?: {
    panel?: string
    venue_breakdown?: Record<string, VenueBreakdown>
    hit_rate?: number | null
    host_conflicts?: number
    fed_exact_divergences?: string
    bucket_divergences?: string
    risk?: string
    notional_usd?: number
    [key: string]: unknown
  }
}

export interface FillRow {
  rank: number
  track: TrackId
  venue: VenueId
  market: string
  side: string
  outcome: string
  qty: number
  price: number
  edge_bps: number
  paper_pnl: number
  filled_at: string
}

export interface EdgeRow {
  rank: number
  track: TrackId
  venue: VenueId
  market: string
  edge_bps: number
  admitted: boolean
  filled: boolean
  fair_value?: number
  mid?: number
}

export interface PortfolioConcentration {
  venue: VenueId
  weight: number
}

export interface LedgerTrackSummary {
  starting_cash?: number
  cash?: number
  equity?: number
  realized_pnl?: number
  unrealized_pnl?: number
  total_pnl?: number
  fees_paid?: number
  gross_notional?: number
  max_drawdown?: number
  open_positions?: number
  fills?: number
  equity_points?: number
}

export interface PortfolioSummary {
  paper_only: boolean
  open_positions: number
  gross_notional: number
  net_exposure: number
  realized_pnl: number
  unrealized_pnl: number
  max_drawdown: number
  settlement_risk_pairs: number
  concentration: PortfolioConcentration[]
  risk_flags: string[]
  /** Ledger-backed extras (schema >= 1.2.0). */
  source?: string
  starting_cash?: number
  cash?: number
  equity?: number
  total_pnl?: number
  fees_paid?: number
  primary_track?: string
  primary?: LedgerTrackSummary & { ledger_id?: string; unmarked_positions?: number }
  by_track?: Record<string, LedgerTrackSummary>
}

export interface ChartPoint {
  track?: string
  venue?: string
  value: number
}

export interface ScoreboardCharts {
  candidates_by_track?: ChartPoint[]
  fills_by_track?: ChartPoint[]
  venue_fills?: ChartPoint[]
}

export interface ScoreboardArtifact {
  schema_version: string
  meta: ScoreboardMeta
  findings?: ScoreboardFindings
  totals: ScoreboardTotals
  tracks: TrackSummary[]
  top_fills?: FillRow[]
  top_edges?: EdgeRow[]
  portfolio?: PortfolioSummary
  charts?: ScoreboardCharts
  /** Pointer to the per-pair gate report written alongside this run (schema >= 1.3.0). */
  gate_report?: { file: string; totals: GateSummary }
}

/** Experiments index — built from public/artifacts by scripts/build-experiments-index.mjs */

export type ExperimentKind = 'paper_run' | 'sample' | 'backtest'

export interface ExperimentTrack {
  track: TrackId
  label: string
  /** Absent on 1.0.0 indexes; treat as `other`. */
  family?: TrackFamilyId
  candidates: number
  admitted: number
  paper_fills: number
  edge_bps: number | null
  settlement_risk: boolean
  paper_pnl: number | null
}

export interface ExperimentTotals {
  candidates: number
  admitted: number
  rejects: number
  paper_fills: number
  paper_pnl: number | null
  realized_pnl: number | null
  unrealized_pnl: number | null
  fees_paid: number | null
}

export interface ExperimentEntry {
  run_id: string
  kind: ExperimentKind
  source: ArtifactSource | string
  label: string | null
  mode: MeasureMode | 'harvest' | 'sample' | string
  cycle: number | null
  measured_at: string | null
  generated_at: string | null
  venues: VenueId[]
  venue_focus: string | null
  kalshi_env: string | null
  primary_track: TrackId | null
  track_family?: string | null
  /** Null on sample files and anything not produced by the paper ledger. */
  pnl_source: string | null
  note: string | null
  totals: ExperimentTotals
  tracks: ExperimentTrack[]
  /** Distinct families present in `tracks` (1.1.0+). */
  families?: TrackFamilyId[]
  /** Admissibility headline of gated_cross_venue; null on samples and pre-1.3.0 runs. */
  gate?: GateSummary | null
  /** URL of the per-pair gate report when one is on disk for this run. */
  gate_report?: string
  /** Files under public/artifacts that describe this run (deduped by run_id). */
  artifacts: string[]
  /** URL of the richest artifact for this run. */
  detail: string
  is_latest: boolean
}

export interface GateReportEntry extends GateSummary {
  file: string
  run_id: string | null
  mode: string
  measured_at: string | null
  pairs: number
  artifact: string
}

export interface LedgerSnapshotEntry {
  ledger_id: string
  /** Track the ledger belongs to (ledger files are one per track). 1.1.0+. */
  track?: TrackId
  family?: TrackFamilyId
  updated_at: string | null
  mark_method: string | null
  fills: number
  equity_points: number
  starting_cash: number | null
  equity: number | null
  total_pnl: number | null
  max_drawdown: number | null
  artifact: string
}

export interface TrackFamilySummary {
  id: TrackFamilyId
  label: string
  description: string | null
  /** Pinned in the lanes strip even when no run has measured it yet. */
  lane: boolean
  /** Track ids seen under this family across every indexed run. */
  tracks: TrackId[]
  runs: number
  measured_runs: number
  sample_runs: number
}

export type LaneStatus = 'measured' | 'sample_only' | 'missing'

/**
 * Newest measured run that carries at least one track of the family, reduced
 * to those tracks. Numbers are null unless status is `measured`, and PnL is
 * null unless that run was ledger-backed.
 */
export interface LaneSummary {
  family: TrackFamilyId
  label: string
  description: string | null
  status: LaneStatus
  run_id: string | null
  measured_at: string | null
  mode: string | null
  pnl_source: string | null
  tracks: TrackId[]
  candidates: number | null
  admitted: number | null
  paper_fills: number | null
  paper_pnl: number | null
  detail: string | null
}

export interface ExperimentsIndex {
  schema_version: string
  generated_at?: string
  paper_only: boolean
  source: string
  counts: { total: number; measured: number; sample: number; backtest: number }
  modes: Record<string, number>
  latest_run_id: string | null
  /** 1.1.0+: every registered family, including those with no runs yet. */
  families?: TrackFamilySummary[]
  /** 1.1.0+: pinned lanes (NegRisk / Kalshi FLB / XV gated). */
  lanes?: LaneSummary[]
  runs: ExperimentEntry[]
  ledgers: LedgerSnapshotEntry[]
  gate_reports?: GateReportEntry[]
}

/** "0/7 admitted" style readout; null when the run carries no gate summary. */
export function gateLine(gate: GateSummary | null | undefined): string | null {
  if (!gate) return null
  const base = `gate ${gate.gate_admitted}/${gate.candidates} admitted`
  return gate.traded > 0 ? `${base} · ${gate.traded} traded` : base
}

export const OTHER_FAMILY: TrackFamilyId = 'other'

export function trackFamily(t: ExperimentTrack): TrackFamilyId {
  return t.family ?? OTHER_FAMILY
}

export type TabId =
  | 'overview'
  | 'cross_venue'
  | 'single_venue'
  | 'sports'
  | 'deliberate'
  | 'portfolio'

export function trackHasSettlementRisk(t: TrackSummary): boolean {
  return Boolean(t.settlement_risk ?? t.settlement_risk_flag)
}

export function formatPct(rate: number | null | undefined, digits = 1): string {
  if (rate == null || Number.isNaN(rate)) return '—'
  return `${(rate * 100).toFixed(digits)}%`
}

export function formatNum(n: number | null | undefined, digits = 0): string {
  if (n == null || Number.isNaN(n)) return '—'
  return Number.isInteger(n) && digits === 0 ? String(n) : n.toFixed(digits)
}

export function formatSigned(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  const fixed = Math.abs(n).toFixed(digits)
  if (n > 0) return `+${fixed}`
  if (n < 0) return `−${fixed}`
  return fixed
}

export function shortTrack(id: string): string {
  return id
    .replace(/_cross_venue_/g, ' xv ')
    .replace(/_/g, ' ')
}
