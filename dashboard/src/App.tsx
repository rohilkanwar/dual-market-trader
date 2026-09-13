import { useEffect, useState } from 'react'
import './App.css'
import { Header } from './components/Header'
import { KpiStrip } from './components/KpiStrip'
import {
  CrossVenuePanel,
  DeliberatePanel,
  OverviewPanel,
  PortfolioPanel,
  SingleVenuePanel,
  SportsPanel,
} from './components/Panels'
import { TrackTable } from './components/TrackTable'
import { loadScoreboard } from './loadScoreboard'
import type { ScoreboardArtifact, TabId } from './types'

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'cross_venue', label: 'Cross-venue settlement' },
  { id: 'single_venue', label: 'Single-venue fair value' },
  { id: 'sports', label: 'Sports' },
  { id: 'deliberate', label: 'Deliberate bets' },
  { id: 'portfolio', label: 'Portfolio & risk' },
]

export default function App() {
  const [data, setData] = useState<ScoreboardArtifact | null>(null)
  const [sourceUrl, setSourceUrl] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<TabId>('overview')

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const result = await loadScoreboard()
        if (cancelled) return
        setData(result.data)
        setSourceUrl(result.url)
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'failed to load scoreboard')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return (
      <main className="desk">
        <p className="error-banner">Could not load artifacts: {error}</p>
      </main>
    )
  }

  if (!data) {
    return (
      <main className="desk">
        <p className="loading">Loading scoreboard…</p>
      </main>
    )
  }

  return (
    <main className="desk">
      <Header data={data} sourceUrl={sourceUrl} />
      {data.meta.source === 'sample' && (
        <div className="sample-banner" role="status">
          SAMPLE artifact — numbers are a labeled Sep 2026 paper last-run snapshot, not a live
          feed. Refresh via measure_all → sync → deploy (see <code>artifacts/schema.md</code>).
        </div>
      )}
      <KpiStrip totals={data.totals} />

      <section className="panel-block">
        <div className="section-title">Track comparison</div>
        <TrackTable tracks={data.tracks} />
      </section>

      <nav className="tab-bar" aria-label="Scoreboard panels">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={tab === t.id ? 'tab active' : 'tab'}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <section className="panel-block tab-panel">
        {tab === 'overview' && <OverviewPanel data={data} />}
        {tab === 'cross_venue' && <CrossVenuePanel data={data} />}
        {tab === 'single_venue' && <SingleVenuePanel data={data} />}
        {tab === 'sports' && <SportsPanel data={data} />}
        {tab === 'deliberate' && <DeliberatePanel data={data} />}
        {tab === 'portfolio' && <PortfolioPanel data={data} />}
      </section>

      <footer className="desk-footer">
        <span>schema {data.schema_version}</span>
        <span>source file {sourceUrl}</span>
        <span>paper_only={String(data.meta.paper_only)}</span>
      </footer>
    </main>
  )
}
