import { useEffect, useState } from 'react'
import './App.css'

type Track = {
  track?: string
  candidates?: number
  admitted?: number
  paper_fills?: number
  settlement_risk_flag?: boolean
  notes?: string
}

export default function App() {
  const [tracks, setTracks] = useState<Track[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch('/artifacts/scoreboard_network.json')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        const list = data.tracks || data.summaries || (Array.isArray(data) ? data : [])
        setTracks(list)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'failed to load scoreboard')
      }
    }
    void load()
  }, [])

  return (
    <main style={{ fontFamily: 'ui-sans-serif, system-ui', padding: 24, maxWidth: 960, margin: '0 auto' }}>
      <h1>Dual-market scoreboard</h1>
      <p>Static paper measurement view for Kalshi + Polymarket tracks.</p>
      {error && <p style={{ color: 'crimson' }}>Could not load artifacts: {error}</p>}
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th align="left">Track</th>
            <th align="right">Candidates</th>
            <th align="right">Admitted</th>
            <th align="right">Fills</th>
            <th align="left">Risk</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((t) => (
            <tr key={String(t.track)}>
              <td>{t.track}</td>
              <td align="right">{t.candidates ?? 0}</td>
              <td align="right">{t.admitted ?? 0}</td>
              <td align="right">{t.paper_fills ?? 0}</td>
              <td>{t.settlement_risk_flag ? 'yes' : 'no'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  )
}
