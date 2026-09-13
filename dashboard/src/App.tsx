import { useEffect, useState } from 'react'
import './App.css'
import { Header } from './components/Header'
import { KpiStrip } from './components/KpiStrip'
import { DetailsSection } from './components/Panels'
import { TrackTable } from './components/TrackTable'
import { loadScoreboard } from './loadScoreboard'
import type { ScoreboardArtifact } from './types'

export default function App() {
  const [data, setData] = useState<ScoreboardArtifact | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const result = await loadScoreboard()
        if (!cancelled) setData(result.data)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Load failed')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return (
      <main className="app">
        <p className="error-banner">{error}</p>
      </main>
    )
  }

  if (!data) {
    return (
      <main className="app">
        <p className="loading">Loading…</p>
      </main>
    )
  }

  return (
    <main className="app">
      <Header data={data} />
      <KpiStrip totals={data.totals} />
      <section className="card" aria-label="Tracks">
        <TrackTable tracks={data.tracks} primaryTrack={data.meta.primary_track} />
      </section>
      <DetailsSection data={data} />
    </main>
  )
}
