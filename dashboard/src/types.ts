/** Scoreboard artifact types — keep in sync with public/artifacts/schema.md */

export type ArtifactSource = 'sample' | 'measured' | 'synced'
export type MeasureMode = 'network' | 'fixtures'
export type VenueId = 'kalshi' | 'polymarket' | string

export type TrackId =
  | 'gated_cross_venue_macro'
  | 'ungated_cross_venue_macro'
  | 'single_venue_fair_value'
  | 'sports_cross_venue'
  | 'small_deliberate_bet'
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
}

export interface ScoreboardFindings {
  fed_exact_divergences?: DivergenceFinding
  macro_admitted_bucket_divergences?: DivergenceFinding
  live_network_cross_venue_candidates?: number
  arbai_summary?: string
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

export function shortTrack(id: string): string {
  return id
    .replace(/_cross_venue_/g, ' xv ')
    .replace(/_/g, ' ')
}
