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

  return (
    <header className="desk-header">
      <div className="header-left">
        <h1>Dual-market scoreboard</h1>
        <div className="header-badges">
          <span className="badge paper">PAPER ONLY</span>
          {isSample && <span className="badge sample">{meta.label || 'SAMPLE'}</span>}
          {!isSample && <span className="badge measured">{meta.label || meta.source}</span>}
          <span className="badge mode">{meta.mode}</span>
        </div>
      </div>
      <div className="header-right">
        <div className="venue-pills">
          {(meta.venues ?? ['kalshi', 'polymarket']).map((v) => (
            <span key={v} className={`pill venue-${v}`}>
              {v}
            </span>
          ))}
        </div>
        <div className="timestamp" title={sourceUrl}>
          Last run: <strong>{measured}</strong>
        </div>
      </div>
    </header>
  )
}
