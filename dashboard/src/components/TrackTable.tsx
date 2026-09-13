import type { TrackId, TrackSummary } from '../types'
import { trackHasSettlementRisk } from '../types'

function humanName(t: TrackSummary): string {
  if (t.label) return t.label
  return t.track.replace(/_/g, ' ')
}

function statusFor(t: TrackSummary, primary?: TrackId): { label: string; kind: string } {
  const isPrimary = (primary ?? 'single_venue_fair_value') === t.track
  if (isPrimary) return { label: 'Primary', kind: 'primary' }
  if (t.candidates === 0 || trackHasSettlementRisk(t)) return { label: 'Quiet', kind: 'quiet' }
  if (t.admitted > 0) return { label: 'Active', kind: 'primary' }
  return { label: 'Idle', kind: 'quiet' }
}

export function TrackTable({
  tracks,
  primaryTrack,
}: {
  tracks: TrackSummary[]
  primaryTrack?: TrackId
}) {
  return (
    <div className="table-wrap">
      <table className="data-table track-table">
        <thead>
          <tr>
            <th>Track</th>
            <th></th>
            <th className="align-right">In</th>
            <th className="align-right">Fills</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((t) => {
            const status = statusFor(t, primaryTrack)
            return (
              <tr key={t.track}>
                <td className="track-name">{humanName(t)}</td>
                <td>
                  <span className={`status-pill ${status.kind}`}>{status.label}</span>
                </td>
                <td className="num">{t.admitted}</td>
                <td className="num">{t.paper_fills}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
