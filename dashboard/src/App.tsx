import { useEffect, useState } from 'react'
import './App.css'
import { Header } from './components/Header'
import { KpiStrip } from './components/KpiStrip'
import { DetailsSection } from './components/Panels'
import { Experiments } from './components/Experiments'
import { TrackTable } from './components/TrackTable'
import { loadExperiments } from './loadExperiments'
import { loadScoreboard } from './loadScoreboard'
import type { ExperimentsIndex, ScoreboardArtifact } from './types'

export default function App() {
  const [data, setData] = useState<ScoreboardArtifact | null>(null)
  const [experiments, setExperiments] = useState<ExperimentsIndex | null>(null)
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
    // History is optional: a missing index hides the section, never the scoreboard.
    void loadExperiments().then((index) => {
      if (!cancelled) setExperiments(index)
    })
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
      <KpiStrip totals={data.totals} meta={data.meta} />
      <section className="card" aria-label="Tracks">
        <TrackTable tracks={data.tracks} primaryTrack={data.meta.primary_track} />
      </section>
      <Experiments index={experiments} />
      <DetailsSection data={data} />
    </main>
  )
}
