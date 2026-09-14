import { useState } from 'react'
import type { ExperimentEntry, ExperimentsIndex } from '../types'
import { formatNum, formatSigned } from '../types'

const THIN_ARCHIVE_RUNS = 5

function shortDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function sourcePill(run: ExperimentEntry): { label: string; kind: string } {
  if (run.kind === 'sample') return { label: 'Sample', kind: 'sample' }
  if (run.kind === 'backtest') return { label: 'Backtest', kind: 'primary' }
  return { label: 'Measured', kind: 'primary' }
}

function primaryTrackLabel(run: ExperimentEntry): string {
  const primary = run.tracks.find((t) => t.track === run.primary_track)
  if (primary) return primary.label
  const busiest = [...run.tracks].sort((a, b) => b.paper_fills - a.paper_fills)[0]
  return busiest?.label ?? '—'
}

function pnlCell(run: ExperimentEntry): string {
  return run.pnl_source ? formatSigned(run.totals.paper_pnl) : '—'
}

function archiveNote(index: ExperimentsIndex): string | null {
  const { measured, backtest } = index.counts
  if (measured === 0) return 'No measured runs yet — sample only.'
  if (backtest === 0 && measured < THIN_ARCHIVE_RUNS) {
    return `Thin archive — ${measured} measured paper ${measured === 1 ? 'run' : 'runs'}, no backtests yet.`
  }
  if (backtest === 0) return 'Paper runs only — no backtests yet.'
  return null
}

function RunDetail({ run }: { run: ExperimentEntry }) {
  const bits: string[] = []
  if (run.cycle != null) bits.push(`cycle ${run.cycle}`)
  if (run.venues.length) bits.push(run.venues.join(' + '))
  if (run.kalshi_env) bits.push(`kalshi ${run.kalshi_env}`)
  if (run.pnl_source) bits.push(`PnL from ${run.pnl_source}`)
  else if (run.kind === 'sample') bits.push('numbers are placeholders')

  return (
    <div className="experiment-detail">
      <table className="data-table detail-table">
        <thead>
          <tr>
            <th>Track</th>
            <th className="align-right">In</th>
            <th className="align-right">Fills</th>
            <th className="align-right">PnL</th>
          </tr>
        </thead>
        <tbody>
          {run.tracks.map((t) => (
            <tr key={t.track}>
              <td className="track-name">{t.label}</td>
              <td className="num">{t.admitted}</td>
              <td className="num">{t.paper_fills}</td>
              <td className="num">{run.pnl_source ? formatSigned(t.paper_pnl) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="meta-line detail-meta">
        <span className="mono">{run.run_id}</span>
        {bits.length > 0 && ` · ${bits.join(' · ')}`}
        {' · '}
        <a href={run.detail} target="_blank" rel="noreferrer">
          JSON
        </a>
      </p>
    </div>
  )
}

export function Experiments({ index }: { index: ExperimentsIndex | null }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!index) return null

  const { runs, counts } = index
  const note = archiveNote(index)
  const summary = [
    counts.measured > 0 && `${counts.measured} measured`,
    counts.sample > 0 && `${counts.sample} sample`,
    counts.backtest > 0 && `${counts.backtest} backtest`,
  ]
    .filter(Boolean)
    .join(' · ')

  const toggle = (id: string) => setOpen((current) => (current === id ? null : id))

  return (
    <section className="card" aria-label="Experiments">
      <div className="card-head">
        <h2 className="card-title">Experiments</h2>
        <span className="card-count">{summary}</span>
      </div>
      {runs.length === 0 ? (
        <p className="meta-line">No runs recorded yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data-table experiments-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Mode</th>
                <th className="align-right">Fills</th>
                <th className="align-right">PnL</th>
                <th className="col-track">Track</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const pill = sourcePill(run)
                const isOpen = open === run.run_id
                const detailId = `run-${run.run_id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
                return [
                  <tr
                    key={run.run_id}
                    className={`experiment-row${isOpen ? ' is-open' : ''}`}
                    role="button"
                    tabIndex={0}
                    aria-expanded={isOpen}
                    aria-controls={detailId}
                    onClick={() => toggle(run.run_id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        toggle(run.run_id)
                      }
                    }}
                  >
                    <td className="date-cell">
                      {run.is_latest && <span className="dot-latest" title="Latest run" />}
                      {shortDate(run.measured_at)}
                    </td>
                    <td className="mode-cell">{run.mode}</td>
                    <td className="num">{formatNum(run.totals.paper_fills)}</td>
                    <td className={`num${run.pnl_source && run.totals.paper_pnl != null && run.totals.paper_pnl < 0 ? ' neg' : ''}`}>
                      {pnlCell(run)}
                    </td>
                    <td className="col-track track-cell">{primaryTrackLabel(run)}</td>
                    <td>
                      <span className={`status-pill ${pill.kind}`}>{pill.label}</span>
                    </td>
                  </tr>,
                  isOpen ? (
                    <tr key={`${run.run_id}-detail`} className="experiment-detail-row" id={detailId}>
                      <td colSpan={6}>
                        <RunDetail run={run} />
                      </td>
                    </tr>
                  ) : null,
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      {note && <p className="meta-line archive-note">{note}</p>}
    </section>
  )
}
