import type { ScoreboardArtifact, TrackSummary } from '../types'
import { formatNum, formatPct, trackHasSettlementRisk } from '../types'
import { BarChart } from './BarChart'

function EmptyCrossVenue({ data }: { data: ScoreboardArtifact }) {
  const f = data.findings
  return (
    <div className="empty-state">
      <h3>Why cross-venue is empty</h3>
      <p>
        Live network cross-venue candidates in this snapshot:{' '}
        <strong>{f?.live_network_cross_venue_candidates ?? 0}</strong>.
      </p>
      <ul>
        {f?.fed_exact_divergences && (
          <li>
            <strong>{f.fed_exact_divergences.label}</strong> — {f.fed_exact_divergences.note}
          </li>
        )}
        {f?.macro_admitted_bucket_divergences && (
          <li>
            <strong>{f.macro_admitted_bucket_divergences.label}</strong> —{' '}
            {f.macro_admitted_bucket_divergences.note}
          </li>
        )}
        <li>
          {f?.arbai_summary ??
            'arbAI settlement-clause / host / bucket matching finds no safe executable cross-venue pairs when divergence risk eats the thin edge.'}
        </li>
      </ul>
      <p className="muted">
        This is an expected measurement outcome, not a dashboard failure. Single-venue fair value
        remains the primary paper track until safer cross-venue pairs appear.
      </p>
    </div>
  )
}

function TrackCards({ tracks }: { tracks: TrackSummary[] }) {
  return (
    <div className="track-cards">
      {tracks.map((t) => (
        <article key={t.track} className="track-card">
          <header>
            <h4>{t.label ?? t.track}</h4>
            <span className={`risk-pill ${trackHasSettlementRisk(t) ? 'risk-yes' : 'risk-no'}`}>
              {trackHasSettlementRisk(t) ? 'settlement risk' : 'no settlement risk'}
            </span>
          </header>
          <dl className="mini-metrics">
            <div>
              <dt>Candidates</dt>
              <dd>{t.candidates}</dd>
            </div>
            <div>
              <dt>Admitted</dt>
              <dd>{t.admitted}</dd>
            </div>
            <div>
              <dt>Fills</dt>
              <dd>{t.paper_fills}</dd>
            </div>
            <div>
              <dt>Fill rate</dt>
              <dd>{formatPct(t.fill_rate)}</dd>
            </div>
            <div>
              <dt>Edge bps</dt>
              <dd>{formatNum(t.edge_bps)}</dd>
            </div>
          </dl>
          <p className="notes">{t.notes}</p>
          {t.reject_reasons && Object.keys(t.reject_reasons).length > 0 && (
            <div className="reject-box">
              <div className="chart-title">Reject reasons</div>
              <ul>
                {Object.entries(t.reject_reasons).map(([k, v]) => (
                  <li key={k}>
                    <code>{k}</code>: {v}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </article>
      ))}
    </div>
  )
}

function FillsEdges({ data }: { data: ScoreboardArtifact }) {
  return (
    <div className="two-col">
      <div className="table-wrap">
        <div className="section-title">Top paper fills</div>
        <table className="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Venue</th>
              <th>Market</th>
              <th>Side</th>
              <th className="align-right">Qty</th>
              <th className="align-right">Price</th>
              <th className="align-right">Edge bps</th>
              <th className="align-right">PnL</th>
            </tr>
          </thead>
          <tbody>
            {(data.top_fills ?? []).map((f) => (
              <tr key={`${f.rank}-${f.market}`}>
                <td>{f.rank}</td>
                <td>
                  <span className={`pill venue-${f.venue}`}>{f.venue}</span>
                </td>
                <td className="mono">{f.market}</td>
                <td>
                  {f.side} {f.outcome}
                </td>
                <td className="num">{f.qty}</td>
                <td className="num">{f.price.toFixed(2)}</td>
                <td className="num">{f.edge_bps}</td>
                <td className="num">{f.paper_pnl.toFixed(2)}</td>
              </tr>
            ))}
            {(data.top_fills ?? []).length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No paper fills in this snapshot.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <div className="section-title">Top edges</div>
        <table className="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Venue</th>
              <th>Market</th>
              <th className="align-right">Edge bps</th>
              <th>Admitted</th>
              <th>Filled</th>
            </tr>
          </thead>
          <tbody>
            {(data.top_edges ?? []).map((e) => (
              <tr key={`${e.rank}-${e.market}`}>
                <td>{e.rank}</td>
                <td>
                  <span className={`pill venue-${e.venue}`}>{e.venue}</span>
                </td>
                <td className="mono">{e.market}</td>
                <td className="num">{e.edge_bps}</td>
                <td>{e.admitted ? 'yes' : 'no'}</td>
                <td>{e.filled ? 'yes' : 'no'}</td>
              </tr>
            ))}
            {(data.top_edges ?? []).length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No ranked edges in this snapshot.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function OverviewPanel({ data }: { data: ScoreboardArtifact }) {
  const cand = (data.charts?.candidates_by_track ?? data.tracks.map((t) => ({
    track: t.track,
    value: t.candidates,
  }))).map((p) => ({
    label: (p.track ?? '').replace(/_/g, ' ').split(' ').slice(0, 2).join(' '),
    value: p.value,
    accent: '#3d8bfd',
  }))
  const fills = (data.charts?.fills_by_track ?? data.tracks.map((t) => ({
    track: t.track,
    value: t.paper_fills,
  }))).map((p) => ({
    label: (p.track ?? '').replace(/_/g, ' ').split(' ').slice(0, 2).join(' '),
    value: p.value,
    accent: '#3ecf8e',
  }))
  const venues = (data.charts?.venue_fills ?? []).map((p) => ({
    label: p.venue ?? 'venue',
    value: p.value,
    accent: p.venue === 'kalshi' ? '#f0b429' : '#a78bfa',
  }))

  return (
    <div className="panel-stack">
      <div className="chart-row">
        <BarChart title="Candidates by track" bars={cand} />
        <BarChart title="Paper fills by track" bars={fills} />
        {venues.length > 0 && <BarChart title="Fills by venue" bars={venues} />}
      </div>
      <FillsEdges data={data} />
    </div>
  )
}

export function CrossVenuePanel({ data }: { data: ScoreboardArtifact }) {
  const tracks = data.tracks.filter(
    (t) =>
      t.track === 'gated_cross_venue_macro' || t.track === 'ungated_cross_venue_macro',
  )
  const empty = tracks.every((t) => t.candidates === 0)
  return (
    <div className="panel-stack">
      {empty && <EmptyCrossVenue data={data} />}
      <TrackCards tracks={tracks} />
    </div>
  )
}

export function SingleVenuePanel({ data }: { data: ScoreboardArtifact }) {
  const tracks = data.tracks.filter((t) => t.track === 'single_venue_fair_value')
  const t = tracks[0]
  const breakdown = t?.metrics?.venue_breakdown
  return (
    <div className="panel-stack">
      <TrackCards tracks={tracks} />
      {breakdown && (
        <div className="chart-row">
          <BarChart
            title="Admitted by venue"
            bars={Object.entries(breakdown).map(([venue, v]) => ({
              label: venue,
              value: v.admitted ?? 0,
              accent: venue === 'kalshi' ? '#f0b429' : '#a78bfa',
            }))}
          />
          <BarChart
            title="Fills by venue"
            bars={Object.entries(breakdown).map(([venue, v]) => ({
              label: venue,
              value: v.fills ?? 0,
              accent: venue === 'kalshi' ? '#f0b429' : '#a78bfa',
            }))}
          />
        </div>
      )}
      <FillsEdges
        data={{
          ...data,
          top_fills: (data.top_fills ?? []).filter((f) => f.track === 'single_venue_fair_value'),
          top_edges: (data.top_edges ?? []).filter((e) => e.track === 'single_venue_fair_value'),
        }}
      />
    </div>
  )
}

export function SportsPanel({ data }: { data: ScoreboardArtifact }) {
  const tracks = data.tracks.filter((t) => t.track === 'sports_cross_venue')
  const empty = tracks.every((t) => t.candidates === 0)
  return (
    <div className="panel-stack">
      {empty && (
        <div className="empty-state">
          <h3>Sports cross-venue idle</h3>
          <p>
            No host-aligned sports pairs in this network snapshot. Settlement-source / host
            conflicts are treated as hard risk — empty is safer than forced matching.
          </p>
        </div>
      )}
      <TrackCards tracks={tracks} />
    </div>
  )
}

export function DeliberatePanel({ data }: { data: ScoreboardArtifact }) {
  const tracks = data.tracks.filter((t) => t.track === 'small_deliberate_bet')
  return (
    <div className="panel-stack">
      <div className="empty-state info">
        <h3>Deliberate micro-bets</h3>
        <p>
          Process-validation sizing only. Not a production edge source — used to confirm paper
          fill plumbing and fee buffers under tiny notional.
        </p>
      </div>
      <TrackCards tracks={tracks} />
    </div>
  )
}

export function PortfolioPanel({ data }: { data: ScoreboardArtifact }) {
  const p = data.portfolio
  if (!p) {
    return (
      <div className="empty-state">
        <h3>No portfolio block</h3>
        <p>Artifact has no <code>portfolio</code> section yet.</p>
      </div>
    )
  }
  return (
    <div className="panel-stack">
      <section className="kpi-strip compact">
        {[
          ['Open positions', formatNum(p.open_positions)],
          ['Gross notional', formatNum(p.gross_notional, 2)],
          ['Net exposure', formatNum(p.net_exposure, 2)],
          ['Realized PnL', formatNum(p.realized_pnl, 2)],
          ['Unrealized PnL', formatNum(p.unrealized_pnl, 2)],
          ['Max drawdown', formatNum(p.max_drawdown, 2)],
          ['Settlement-risk pairs', formatNum(p.settlement_risk_pairs)],
        ].map(([label, value]) => (
          <div key={label} className="kpi-card tone-neutral">
            <div className="kpi-label">{label}</div>
            <div className="kpi-value">{value}</div>
          </div>
        ))}
      </section>
      <div className="chart-row">
        <BarChart
          title="Venue concentration"
          bars={p.concentration.map((c) => ({
            label: String(c.venue),
            value: Math.round(c.weight * 100),
            accent: c.venue === 'kalshi' ? '#f0b429' : '#a78bfa',
          }))}
        />
      </div>
      <div className="reject-box">
        <div className="chart-title">Risk flags</div>
        <ul>
          {p.risk_flags.map((f) => (
            <li key={f}>
              <code>{f}</code>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
