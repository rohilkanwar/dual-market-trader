import type { ScoreboardArtifact } from '../types'

export function Header({ data, sourceUrl }: { data: ScoreboardArtifact; sourceUrl: string }) {
  const { meta } = data
  const isSample = meta.source === 'sample'
  const measured = meta.measured_at
    ? new Date(meta.measured_at).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '—'

  const venues = (meta.venues ?? ['kalshi', 'polymarket']).join(' · ')

  return (
    <header className="app-header">
      <h1>Dual-market scoreboard</h1>
      <p className="lede">
        Paper measurement of prediction-market strategies across venues
        {isSample ? ' — sample snapshot, not live trading.' : '.'}
      </p>
      <div className="badge-row">
        {meta.paper_only && <span className="badge paper">Paper only</span>}
        {isSample && <span className="badge sample">{meta.label || 'Sample'}</span>}
        {!isSample && <span className="badge">{meta.label || meta.source}</span>}
        <span className="badge">{meta.mode}</span>
      </div>
      <p className="meta-line" title={sourceUrl}>
        Last run <strong>{measured}</strong>
        <span aria-hidden="true"> · </span>
        {venues}
      </p>
    </header>
  )
}
