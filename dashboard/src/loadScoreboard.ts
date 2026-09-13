import type { ScoreboardArtifact } from './types'

const CANDIDATES = [
  '/artifacts/scoreboard_latest.json',
  '/artifacts/scoreboard_network.json',
  '/artifacts/scoreboard_sample.json',
] as const

export type LoadResult = {
  data: ScoreboardArtifact
  url: string
}

export async function loadScoreboard(): Promise<LoadResult> {
  const errors: string[] = []
  for (const url of CANDIDATES) {
    try {
      const res = await fetch(url, { cache: 'no-store' })
      if (!res.ok) {
        errors.push(`${url} → HTTP ${res.status}`)
        continue
      }
      const data = (await res.json()) as ScoreboardArtifact
      if (!data?.meta || !Array.isArray(data.tracks) || !data.totals) {
        errors.push(`${url} → missing meta/tracks/totals`)
        continue
      }
      return { data, url }
    } catch (e) {
      errors.push(`${url} → ${e instanceof Error ? e.message : 'fetch failed'}`)
    }
  }
  throw new Error(`Could not load scoreboard artifact. Tried: ${errors.join('; ')}`)
}
