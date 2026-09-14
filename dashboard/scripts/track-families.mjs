// Track families group scoreboard track ids into the strategy lanes the
// dashboard filters and summarises by. The registry is the single source of
// truth: build-experiments-index.mjs stamps `family` onto every indexed track
// and the React app only reads those stamps, so a new track id from a sister
// branch lands in a lane without a UI change.
//
// Resolution order for a track:
//   1. an explicit `family` (or `metrics.family` / `metrics.track_family`) on the
//      artifact, when it names a registered family
//   2. KNOWN_TRACKS — ids the Python scoreboard emits today
//   3. keyword heuristics on the id (negrisk / flb / gated cross-venue …)
//   4. `other`
//
// Families with `lane: true` are pinned in the lanes strip even when no run has
// measured them yet, so the empty state is visible before sister PRs merge.

export const FAMILIES = [
  {
    id: 'negrisk',
    label: 'NegRisk',
    lane: true,
    description: 'Polymarket NegRisk / combinatorial / YES+NO rebalancing',
  },
  {
    id: 'kalshi_flb',
    label: 'Kalshi FLB',
    lane: true,
    description: 'Kalshi maker / FLB',
  },
  {
    id: 'xv_gated',
    label: 'XV gated',
    lane: true,
    description: 'Cross-venue pairs that passed the settlement gate',
  },
  {
    id: 'xv_ungated',
    label: 'XV ungated',
    lane: false,
    description: 'Cross-venue pairs traded without the settlement gate (measurement only)',
  },
  {
    id: 'cross_venue',
    label: 'Cross-venue',
    lane: false,
    description: 'Cross-venue tracks that do not state whether they are gated',
  },
  {
    id: 'single_venue',
    label: 'Single venue',
    lane: false,
    description: 'Single-venue fair value',
  },
  {
    id: 'news',
    label: 'News',
    lane: false,
    description: 'News / underreaction residual (signal mapping not validated)',
  },
  {
    id: 'other',
    label: 'Other',
    lane: false,
    description: 'Tracks not matched to a family',
  },
]

export const FAMILY_IDS = new Set(FAMILIES.map((f) => f.id))
export const LANE_FAMILIES = FAMILIES.filter((f) => f.lane).map((f) => f.id)

export const KNOWN_TRACKS = {
  gated_cross_venue_macro: 'xv_gated',
  small_deliberate_bet: 'xv_gated',
  ungated_cross_venue_macro: 'xv_ungated',
  sports_cross_venue: 'xv_ungated',
  single_venue_fair_value: 'single_venue',
  news_underreaction: 'news',
  polymarket_rebalancing_arb: 'negrisk',
  polymarket_negrisk_arb: 'negrisk',
  polymarket_combinatorial_arb: 'negrisk',
}

const tokensOf = (id) =>
  String(id ?? '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)

/** Family id for a track row (`{ track, family?, metrics? }`) or a bare track id. */
export function familyOf(trackOrId) {
  const row = typeof trackOrId === 'string' ? { track: trackOrId } : (trackOrId ?? {})
  const declared = row.family ?? row.metrics?.family ?? row.metrics?.track_family
  if (typeof declared === 'string' && FAMILY_IDS.has(declared)) return declared

  const id = String(row.track ?? '')
  if (KNOWN_TRACKS[id]) return KNOWN_TRACKS[id]

  const tokens = new Set(tokensOf(id))
  const has = (...words) => words.every((w) => tokens.has(w))
  const any = (...words) => words.some((w) => tokens.has(w))
  const joined = [...tokens].join('_')

  if (any('negrisk', 'combinatorial', 'combo') || has('neg', 'risk')) return 'negrisk'
  if (any('flb', 'maker')) return 'kalshi_flb'
  if (any('news', 'underreaction', 'headline')) return 'news'
  const crossVenue = any('xv') || has('cross', 'venue') || joined.includes('crossvenue')
  if (crossVenue) {
    if (tokens.has('ungated')) return 'xv_ungated'
    if (tokens.has('gated')) return 'xv_gated'
    return 'cross_venue'
  }
  if (has('single', 'venue') || has('fair', 'value')) return 'single_venue'
  return 'other'
}

export function familyLabel(id) {
  return FAMILIES.find((f) => f.id === id)?.label ?? id
}
