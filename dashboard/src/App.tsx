import { useEffect, useState } from 'react'
import './App.css'
import { Header } from './components/Header'
import { KpiStrip } from './components/KpiStrip'
import { DetailsSection } from './components/Panels'
import { TrackTable } from './components/TrackTable'
import { loadScoreboard } from './loadScoreboard'
import type { ScoreboardArtifact, TrackSummary } from './types'

function primaryTrackLabel(data: ScoreboardArtifact): string {
  const id = data.meta.primary_track ?? 'single_venue_fair_value'
  const t = data.tracks.find((x) => x.track === id)
  return t?.label ?? 'Single-venue fair value'
}

function LookAt({ data }: { data: ScoreboardArtifact }) {
  const primary = primaryTrackLabel(data)
  const xvCandidates = data.findings?.live_network_cross_venue_candidates ?? 0
  const riskPairs = data.totals.settlement_risk_pairs

  return (
    <aside className="callout" aria-label="What to look at">
      <h2>What to look at</h2>
      <ul className="callout-list">
        <li>
          <strong>{primary}</strong> is the primary paper track — measuring fair-value edges
          inside one venue at a time, with no cross-venue settlement risk by construction.
        </li>
        <li>
          Cross-venue is quiet
          {xvCandidates === 0 ? ' (0 live candidates in this snapshot)' : ` (${xvCandidates} live candidates)`}
          : settlement-clause / host matching keeps pairs dark when divergence risk would eat a thin edge.
        </li>
        {riskPairs > 0 && (
          <li>
            <strong>{riskPairs}</strong> settlement-risk pair{riskPairs === 1 ? '' : 's'} flagged for operator visibility — not executable paper fills.
          </li>
        )}
      </ul>
    </aside>
  )
}

export default function App() {
  const [data, setData] = useState<ScoreboardArtifact | null>(null)
  const [sourceUrl, setSourceUrl] = useState<string>('')
  const [error, setError] = useState<string | null>(null)

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
      <main className="app">
        <p className="error-banner">Could not load artifacts: {error}</p>
      </main>
    )
  }

  if (!data) {
    return (
      <main className="app">
        <p className="loading">Loading scoreboard…</p>
      </main>
    )
  }

  const isSample = data.meta.source === 'sample'
  const tracks: TrackSummary[] = data.tracks

  return (
    <main className="app">
      <Header data={data} sourceUrl={sourceUrl} />

      {isSample && (
        <div className="sample-banner" role="status">
          SAMPLE artifact — labeled paper last-run snapshot, not a live feed. Refresh via
          measure_all → sync → deploy (see <code>artifacts/schema.md</code>).
        </div>
      )}

      <LookAt data={data} />

      <KpiStrip totals={data.totals} />

      <section className="card" aria-labelledby="tracks-heading">
        <h2 id="tracks-heading" className="card-title">
          Tracks
        </h2>
        <TrackTable tracks={tracks} primaryTrack={data.meta.primary_track} />
      </section>

      <DetailsSection data={data} />

      <footer className="app-footer">
        <span>schema {data.schema_version}</span>
        <span>source {sourceUrl}</span>
        <span>paper_only={String(data.meta.paper_only)}</span>
      </footer>
    </main>
  )
}
