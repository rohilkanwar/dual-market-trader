import type { ExperimentsIndex } from './types'

export const EXPERIMENTS_INDEX_URL = '/artifacts/experiments_index.json'

/**
 * Loads the experiments index. Resolves to null when the file is missing or
 * malformed so the history section simply stays hidden instead of failing the
 * whole scoreboard.
 */
export async function loadExperiments(): Promise<ExperimentsIndex | null> {
  try {
    const res = await fetch(EXPERIMENTS_INDEX_URL, { cache: 'no-store' })
    if (!res.ok) return null
    const data = (await res.json()) as ExperimentsIndex
    if (!Array.isArray(data?.runs) || !data.counts) return null
    return data
  } catch {
    return null
  }
}
