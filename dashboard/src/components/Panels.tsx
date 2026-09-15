import type { ScoreboardArtifact } from '../types'
import { formatNum, formatPct, formatSigned, hasLedgerPnl } from '../types'

export function DetailsSection({ data }: { data: ScoreboardArtifact }) {
  const f = data.findings
  const p = data.portfolio
  const xv = f?.live_network_cross_venue_candidates ?? 0
  const gate = data.gate_report?.totals ?? f?.gated_cross_venue
  const ledger = hasLedgerPnl(data.meta) ? p : undefined
  const primary = ledger?.primary
  // Weather headline is optional: boards without weather tracks never carry it.
  const weather = f?.weather
  const weatherValue = weather
    ? [
        (weather.stations ?? 0) > 0 ? `parse ${formatPct(weather.station_parse_rate, 0)}` : null,
        (weather.dead_bucket_kills ?? 0) > 0 ? `${formatNum(weather.dead_bucket_kills)} kills` : null,
        (weather.calibration_n ?? 0) > 0 ? `n ${formatNum(weather.calibration_n)}` : null,
      ]
        .filter(Boolean)
        .join(' · ') || weather.evaluation_status
    : null

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
            {gate && (
              <div className="mini-stat">
                <dt>Gate admitted</dt>
                <dd>
                  {gate.gate_admitted}/{gate.candidates}
                  {gate.traded > 0 ? ` · ${gate.traded} traded` : ''}
                </dd>
              </div>
            )}
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
            {weatherValue && (
              <div className="mini-stat">
                <dt>Weather</dt>
                <dd>{weatherValue}</dd>
              </div>
            )}
            {ledger ? (
              <>
                <div className="mini-stat">
                  <dt>Realized</dt>
                  <dd>{formatSigned(ledger.realized_pnl)}</dd>
                </div>
                <div className="mini-stat">
                  <dt>Unrealized</dt>
                  <dd>{formatSigned(ledger.unrealized_pnl)}</dd>
                </div>
                <div className="mini-stat">
                  <dt>Fees</dt>
                  <dd>{formatNum(ledger.fees_paid, 2)}</dd>
                </div>
                <div className="mini-stat">
                  <dt>Max drawdown</dt>
                  <dd>{formatNum(ledger.max_drawdown, 2)}</dd>
                </div>
                <div className="mini-stat">
                  <dt>Positions</dt>
                  <dd>{formatNum(ledger.open_positions)}</dd>
                </div>
                {primary && (
                  <div className="mini-stat">
                    <dt>Primary equity</dt>
                    <dd>
                      {formatNum(primary.equity, 2)}
                      {primary.starting_cash != null && ` / ${formatNum(primary.starting_cash, 0)}`}
                    </dd>
                  </div>
                )}
              </>
            ) : (
              p && (
                <div className="mini-stat">
                  <dt>PnL</dt>
                  <dd>sample — not measured</dd>
                </div>
              )
            )}
          </dl>
          {data.meta.pnl_source && (
            <p className="meta-line">
              PnL from {data.meta.pnl_source}
              {data.meta.run_id ? ` · run ${data.meta.run_id}` : ''}
              {data.meta.mode ? ` · ${data.meta.mode}` : ''}
            </p>
          )}
        </div>
      </details>
    </div>
  )
}
