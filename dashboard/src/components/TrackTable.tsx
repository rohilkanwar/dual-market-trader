import { useMemo, useState } from 'react'
import type { TrackSummary } from '../types'
import { formatNum, formatPct, trackHasSettlementRisk } from '../types'

type SortKey =
  | 'track'
  | 'candidates'
  | 'admitted'
  | 'rejects'
  | 'paper_fills'
  | 'fill_rate'
  | 'edge_bps'
  | 'settlement_risk'

function cmp(a: number | string | boolean | null | undefined, b: number | string | boolean | null | undefined) {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  if (typeof a === 'string' && typeof b === 'string') return a.localeCompare(b)
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(a) - Number(b)
  return Number(a) - Number(b)
}

export function TrackTable({ tracks }: { tracks: TrackSummary[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('candidates')
  const [asc, setAsc] = useState(false)

  const sorted = useMemo(() => {
    const rows = [...tracks]
    rows.sort((ra, rb) => {
      const riskA = trackHasSettlementRisk(ra)
      const riskB = trackHasSettlementRisk(rb)
      const va =
        sortKey === 'settlement_risk'
          ? riskA
          : sortKey === 'rejects'
            ? (ra.rejects ?? ra.candidates - ra.admitted)
            : ra[sortKey as keyof TrackSummary]
      const vb =
        sortKey === 'settlement_risk'
          ? riskB
          : sortKey === 'rejects'
            ? (rb.rejects ?? rb.candidates - rb.admitted)
            : rb[sortKey as keyof TrackSummary]
      const d = cmp(va as never, vb as never)
      return asc ? d : -d
    })
    return rows
  }, [tracks, sortKey, asc])

  const onSort = (key: SortKey) => {
    if (key === sortKey) setAsc(!asc)
    else {
      setSortKey(key)
      setAsc(key === 'track')
    }
  }

  const th = (key: SortKey, label: string, align: 'left' | 'right' = 'right') => (
    <th className={`sortable align-${align}`} onClick={() => onSort(key)}>
      {label}
      {sortKey === key ? (asc ? ' ↑' : ' ↓') : ''}
    </th>
  )

  return (
    <div className="table-wrap">
      <table className="data-table track-table">
        <thead>
          <tr>
            {th('track', 'Track', 'left')}
            {th('candidates', 'Candidates')}
            {th('admitted', 'Admitted')}
            {th('rejects', 'Rejects')}
            {th('paper_fills', 'Fills')}
            {th('fill_rate', 'Fill rate')}
            {th('edge_bps', 'Edge bps')}
            {th('settlement_risk', 'Settlement risk')}
            <th className="align-left">Notes</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((t) => {
            const risk = trackHasSettlementRisk(t)
            const rejects = t.rejects ?? Math.max(0, t.candidates - t.admitted)
            return (
              <tr key={t.track}>
                <td className="track-name">
                  <div className="track-id">{t.label ?? t.track}</div>
                  <div className="track-slug">{t.track}</div>
                </td>
                <td className="num">{t.candidates}</td>
                <td className="num">{t.admitted}</td>
                <td className="num">{rejects}</td>
                <td className="num">{t.paper_fills}</td>
                <td className="num">{formatPct(t.fill_rate)}</td>
                <td className="num">{formatNum(t.edge_bps, 0)}</td>
                <td>
                  <span className={`risk-pill ${risk ? 'risk-yes' : 'risk-no'}`}>
                    {risk ? 'yes' : 'no'}
                  </span>
                </td>
                <td className="notes">{t.notes ?? '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
