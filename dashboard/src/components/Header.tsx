import type { ScoreboardArtifact } from '../types'

export function Header({ data }: { data: ScoreboardArtifact }) {
  const { meta } = data
  const measured = meta.measured_at
    ? new Date(meta.measured_at).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '—'

  return (
    <header className="app-header">
      <div className="header-row">
        <h1>Scoreboard</h1>
        <div className="badge-row">
          {meta.paper_only && <span className="badge paper">Paper</span>}
          {meta.source === 'sample' && <span className="badge sample">Sample</span>}
        </div>
      </div>
      <p className="meta-line">{measured}</p>
    </header>
  )
}
