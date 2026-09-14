import { useMemo, useState } from 'react'
import type {
  ExperimentEntry,
  ExperimentTrack,
  ExperimentsIndex,
  LaneSummary,
  TrackFamilyId,
  TrackFamilySummary,
} from '../types'
import { OTHER_FAMILY, formatNum, formatSigned, trackFamily } from '../types'

const THIN_ARCHIVE_RUNS = 5
const ALL = 'all'

type Filter = typeof ALL | TrackFamilyId

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

function runFamilies(run: ExperimentEntry): TrackFamilyId[] {
  return run.families ?? [...new Set(run.tracks.map(trackFamily))].sort()
}

function tracksIn(run: ExperimentEntry, filter: Filter): ExperimentTrack[] {
  return filter === ALL ? run.tracks : run.tracks.filter((t) => trackFamily(t) === filter)
}

/** Fills and PnL for the visible tracks. PnL is only summed when every row has one. */
function subtotal(run: ExperimentEntry, filter: Filter): { fills: number; pnl: number | null } {
  if (filter === ALL) {
    return { fills: run.totals.paper_fills, pnl: run.pnl_source ? run.totals.paper_pnl : null }
  }
  const rows = tracksIn(run, filter)
  const fills = rows.reduce((acc, t) => acc + t.paper_fills, 0)
  const pnlKnown = Boolean(run.pnl_source) && rows.length > 0 && rows.every((t) => t.paper_pnl != null)
  const pnl = pnlKnown ? Math.round(rows.reduce((acc, t) => acc + (t.paper_pnl ?? 0), 0) * 100) / 100 : null
  return { fills, pnl }
}

function headlineTrack(run: ExperimentEntry, filter: Filter): string {
  const rows = tracksIn(run, filter)
  const primary = rows.find((t) => t.track === run.primary_track)
  if (primary) return primary.label
  const busiest = [...rows].sort((a, b) => b.paper_fills - a.paper_fills)[0]
  return busiest?.label ?? '—'
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

/**
 * Families available as filters: the index's registry order when present
 * (1.1.0+), otherwise whatever the runs carry. Only families with runs count.
 */
function filterableFamilies(index: ExperimentsIndex): TrackFamilySummary[] {
  if (index.families) return index.families.filter((f) => f.runs > 0)
  const seen = new Map<TrackFamilyId, number>()
  for (const run of index.runs) for (const f of runFamilies(run)) seen.set(f, (seen.get(f) ?? 0) + 1)
  return [...seen.entries()].map(([id, runs]) => ({
    id,
    label: id === OTHER_FAMILY ? 'Other' : id.replace(/_/g, ' '),
    description: null,
    lane: false,
    tracks: [],
    runs,
    measured_runs: runs,
    sample_runs: 0,
  }))
}

function familyLabel(index: ExperimentsIndex, id: TrackFamilyId): string {
  return index.families?.find((f) => f.id === id)?.label ?? (id === OTHER_FAMILY ? 'Other' : id.replace(/_/g, ' '))
}

function LaneStrip({
  lanes,
  active,
  onPick,
  filterable,
}: {
  lanes: LaneSummary[]
  active: Filter
  onPick: (family: TrackFamilyId) => void
  filterable: Set<TrackFamilyId>
}) {
  return (
    <div className="lane-strip" role="group" aria-label="Strategy lanes (paper)">
      {lanes.map((lane) => {
        const canFilter = filterable.has(lane.family)
        const isActive = active === lane.family
        const measured = lane.status === 'measured'
        const detail = measured
          ? `${formatNum(lane.paper_fills)} ${lane.paper_fills === 1 ? 'fill' : 'fills'} · ${
              lane.pnl_source ? `${formatSigned(lane.paper_pnl)} paper` : 'PnL —'
            }`
          : lane.status === 'sample_only'
            ? 'Sample only'
            : 'Not measured yet'
        const when = measured ? `${shortDate(lane.measured_at)}${lane.mode ? ` · ${lane.mode}` : ''}` : '—'
        return (
          <button
            key={lane.family}
            type="button"
            className={`lane${measured ? '' : ' is-empty'}${isActive ? ' is-active' : ''}`}
            title={lane.description ?? undefined}
            aria-pressed={canFilter ? isActive : undefined}
            disabled={!canFilter}
            onClick={() => canFilter && onPick(lane.family)}
          >
            <span className="lane-label">{lane.label}</span>
            <span className={`lane-value${measured && lane.paper_pnl != null && lane.paper_pnl < 0 ? ' neg' : ''}`}>
              {detail}
            </span>
            <span className="lane-meta">{when}</span>
          </button>
        )
      })}
    </div>
  )
}

function RunDetail({ run, filter, index }: { run: ExperimentEntry; filter: Filter; index: ExperimentsIndex }) {
  const bits: string[] = []
  if (run.cycle != null) bits.push(`cycle ${run.cycle}`)
  if (run.venues.length) bits.push(run.venues.join(' + '))
  if (run.kalshi_env) bits.push(`kalshi ${run.kalshi_env}`)
  if (run.pnl_source) bits.push(`PnL from ${run.pnl_source}`)
  else if (run.kind === 'sample') bits.push('numbers are placeholders')

  const rows = tracksIn(run, filter)
  const groups = new Map<TrackFamilyId, ExperimentTrack[]>()
  for (const t of rows) {
    const f = trackFamily(t)
    groups.set(f, [...(groups.get(f) ?? []), t])
  }
  const showGroupRows = groups.size > 1

  return (
    <div className="experiment-detail">
      <table className="data-table detail-table">
        <thead>
          <tr>
            <th>Track</th>
            <th className="align-right">In</th>
            <th className="align-right">Fills</th>
            <th className="align-right">Paper PnL</th>
          </tr>
        </thead>
        <tbody>
          {[...groups.entries()].map(([family, tracks]) => [
            showGroupRows ? (
              <tr key={`${family}-head`} className="family-row">
                <td colSpan={4}>{familyLabel(index, family)}</td>
              </tr>
            ) : null,
            ...tracks.map((t) => (
              <tr key={t.track}>
                <td className="track-name">{t.label}</td>
                <td className="num">{t.admitted}</td>
                <td className="num">{t.paper_fills}</td>
                <td className="num">{run.pnl_source ? formatSigned(t.paper_pnl) : '—'}</td>
              </tr>
            )),
          ])}
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
  const [filter, setFilter] = useState<Filter>(ALL)
  const families = useMemo(() => (index ? filterableFamilies(index) : []), [index])
  if (!index) return null

  const { counts } = index
  const note = archiveNote(index)
  const summary = [
    counts.measured > 0 && `${counts.measured} measured`,
    counts.sample > 0 && `${counts.sample} sample`,
    counts.backtest > 0 && `${counts.backtest} backtest`,
  ]
    .filter(Boolean)
    .join(' · ')

  const filterable = new Set(families.map((f) => f.id))
  const effectiveFilter: Filter = filter !== ALL && !filterable.has(filter) ? ALL : filter
  const runs = index.runs.filter((run) => effectiveFilter === ALL || runFamilies(run).includes(effectiveFilter))
  const lanes = index.lanes ?? []

  const toggle = (id: string) => setOpen((current) => (current === id ? null : id))
  const pick = (family: TrackFamilyId) => setFilter((current) => (current === family ? ALL : family))

  return (
    <section className="card" aria-label="Experiments">
      <div className="card-head">
        <h2 className="card-title">Experiments</h2>
        <span className="card-count">{summary}</span>
      </div>
      {lanes.length > 0 && (
        <LaneStrip lanes={lanes} active={effectiveFilter} onPick={pick} filterable={filterable} />
      )}
      {families.length > 1 && (
        <div className="filter-row" role="group" aria-label="Filter runs by track family">
          <button
            type="button"
            className={`filter-pill${effectiveFilter === ALL ? ' is-active' : ''}`}
            aria-pressed={effectiveFilter === ALL}
            onClick={() => setFilter(ALL)}
          >
            All
          </button>
          {families.map((f) => (
            <button
              key={f.id}
              type="button"
              className={`filter-pill${effectiveFilter === f.id ? ' is-active' : ''}`}
              aria-pressed={effectiveFilter === f.id}
              title={f.description ?? undefined}
              onClick={() => pick(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
      )}
      {runs.length === 0 ? (
        <p className="meta-line">
          {effectiveFilter === ALL
            ? 'No runs recorded yet.'
            : `No runs carry ${familyLabel(index, effectiveFilter)} tracks yet.`}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="data-table experiments-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Mode</th>
                <th className="align-right">Fills</th>
                <th className="align-right">Paper PnL</th>
                <th className="col-track">Track</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const pill = sourcePill(run)
                const isOpen = open === run.run_id
                const detailId = `run-${run.run_id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
                const { fills, pnl } = subtotal(run, effectiveFilter)
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
                    <td className="num">{formatNum(fills)}</td>
                    <td className={`num${pnl != null && pnl < 0 ? ' neg' : ''}`}>{pnl != null ? formatSigned(pnl) : '—'}</td>
                    <td className="col-track track-cell">{headlineTrack(run, effectiveFilter)}</td>
                    <td>
                      <span className={`status-pill ${pill.kind}`}>{pill.label}</span>
                    </td>
                  </tr>,
                  isOpen ? (
                    <tr key={`${run.run_id}-detail`} className="experiment-detail-row" id={detailId}>
                      <td colSpan={6}>
                        <RunDetail run={run} filter={effectiveFilter} index={index} />
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
