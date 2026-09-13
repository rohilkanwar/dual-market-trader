import type { ScoreboardArtifact } from '../types'
import { formatNum } from '../types'

/** Collapsed-by-default secondary details — replaces the old 6-tab farm. */
export function DetailsSection({ data }: { data: ScoreboardArtifact }) {
  return (
    <div className="details-stack">
      <SettlementDetails data={data} />
      <PortfolioDetails data={data} />
    </div>
  )
}

function SettlementDetails({ data }: { data: ScoreboardArtifact }) {
  const f = data.findings
  const xv = f?.live_network_cross_venue_candidates ?? 0

  return (
    <details className="details-block">
      <summary>
        <span>Settlement &amp; cross-venue</span>
        <span className="details-chevron" aria-hidden="true">
          +
        </span>
      </summary>
      <div className="details-body">
        <p>
          Live network cross-venue candidates in this snapshot: <strong>{xv}</strong>.
        </p>
        <ul>
          {f?.fed_exact_divergences && (
            <li>
              <strong>{f.fed_exact_divergences.label}</strong>
              {f.fed_exact_divergences.note ? ` — ${f.fed_exact_divergences.note}` : ''}
            </li>
          )}
          {f?.macro_admitted_bucket_divergences && (
            <li>
              <strong>{f.macro_admitted_bucket_divergences.label}</strong>
              {f.macro_admitted_bucket_divergences.note
                ? ` — ${f.macro_admitted_bucket_divergences.note}`
                : ''}
            </li>
          )}
          <li>
            {f?.arbai_summary ??
              'Settlement-clause / host / bucket matching finds no safe executable cross-venue pairs when divergence risk eats the thin edge.'}
          </li>
        </ul>
        <p className="muted">
          Empty cross-venue is an expected measurement outcome, not a dashboard failure.
          Single-venue fair value stays primary until safer pairs appear.
        </p>
      </div>
    </details>
  )
}

function PortfolioDetails({ data }: { data: ScoreboardArtifact }) {
  const p = data.portfolio

  return (
    <details className="details-block">
      <summary>
        <span>Portfolio &amp; risk</span>
        <span className="details-chevron" aria-hidden="true">
          +
        </span>
      </summary>
      <div className="details-body">
        {!p ? (
          <p className="muted">
            No portfolio block in this artifact yet.
          </p>
        ) : (
          <>
            <dl className="mini-grid">
              {(
                [
                  ['Open positions', formatNum(p.open_positions)],
                  ['Gross notional', formatNum(p.gross_notional, 2)],
                  ['Net exposure', formatNum(p.net_exposure, 2)],
                  ['Realized PnL', formatNum(p.realized_pnl, 2)],
                  ['Unrealized PnL', formatNum(p.unrealized_pnl, 2)],
                  ['Max drawdown', formatNum(p.max_drawdown, 2)],
                  ['Settlement-risk pairs', formatNum(p.settlement_risk_pairs)],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="mini-stat">
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            {p.concentration?.length > 0 && (
              <p>
                Venue mix:{' '}
                {p.concentration
                  .map((c) => `${c.venue} ${Math.round(c.weight * 100)}%`)
                  .join(' · ')}
              </p>
            )}
            {p.risk_flags?.length > 0 && (
              <>
                <p>
                  <strong>Risk flags</strong>
                </p>
                <ul className="flag-list">
                  {p.risk_flags.map((flag) => (
                    <li key={flag}>
                      <code>{flag}</code>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {p.paper_only && (
              <p className="muted">Paper-only portfolio — not live capital.</p>
            )}
          </>
        )}
      </div>
    </details>
  )
}

/* Legacy panel exports kept as thin wrappers so old imports don't break. */
export function OverviewPanel({ data }: { data: ScoreboardArtifact }) {
  return <DetailsSection data={data} />
}
export function CrossVenuePanel({ data }: { data: ScoreboardArtifact }) {
  return <SettlementDetails data={data} />
}
export function SingleVenuePanel(_props: { data: ScoreboardArtifact }) {
  return null
}
export function SportsPanel(_props: { data: ScoreboardArtifact }) {
  return null
}
export function DeliberatePanel(_props: { data: ScoreboardArtifact }) {
  return null
}
export function PortfolioPanel({ data }: { data: ScoreboardArtifact }) {
  return <PortfolioDetails data={data} />
}
