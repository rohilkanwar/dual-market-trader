import type { ScoreboardArtifact } from '../types'
import { formatNum } from '../types'

export function DetailsSection({ data }: { data: ScoreboardArtifact }) {
  const f = data.findings
  const p = data.portfolio
  const xv = f?.live_network_cross_venue_candidates ?? 0

  return (
    <div className="details-stack">
      <details className="details-block">
        <summary>
          <span>More</span>
          <span className="details-chevron" aria-hidden="true">
            +
          </span>
        </summary>
        <div className="details-body">
          <dl className="mini-grid">
            <div className="mini-stat">
              <dt>Cross-venue</dt>
              <dd>{xv}</dd>
            </div>
            {f?.fed_exact_divergences && (
              <div className="mini-stat">
                <dt>Fed</dt>
                <dd>{f.fed_exact_divergences.label}</dd>
              </div>
            )}
            {f?.macro_admitted_bucket_divergences && (
              <div className="mini-stat">
                <dt>Macro</dt>
                <dd>{f.macro_admitted_bucket_divergences.label}</dd>
              </div>
            )}
            {p && (
              <>
                <div className="mini-stat">
                  <dt>PnL</dt>
                  <dd>{formatNum(p.realized_pnl, 2)}</dd>
                </div>
                <div className="mini-stat">
                  <dt>Positions</dt>
                  <dd>{formatNum(p.open_positions)}</dd>
                </div>
              </>
            )}
          </dl>
        </div>
      </details>
    </div>
  )
}
