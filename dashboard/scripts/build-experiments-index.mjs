// Build public/artifacts/experiments_index.json from the JSON already in
// public/artifacts. Nothing is fetched or measured here: the index only lists
// runs that exist on disk, so a Vercel build (which never sees ../artifacts)
// produces the same index as a local one.
//
// Inputs (all optional):
//   scoreboard_*.json   full dashboard artifacts; one run each (deduped by meta.run_id)
//   runs/*.json         compact run records written by sync-artifacts.mjs
//   paper_ledger_*.json ledger snapshots (listed separately; they carry no run_id)
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const INDEX_FILE = 'experiments_index.json'
const INDEX_SCHEMA = '1.0.0'
const KNOWN_MODES = new Set(['fixtures', 'network', 'harvest', 'sample'])

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : v == null ? null : Number(v))
const finite = (v) => {
  const n = num(v)
  return n == null || Number.isNaN(n) ? null : n
}
const round2 = (v) => (v == null ? null : Math.round(v * 100) / 100)

async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    if (error?.code !== 'ENOENT') console.warn(`skip ${path}: ${error.message}`)
    return null
  }
}

async function listFiles(dir) {
  try {
    return (await readdir(dir)).sort()
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw error
  }
}

// A run is a "backtest" only when the artifact itself says so. Nothing in the
// repo emits this yet, so the flag exists to keep the UI honest, not to claim it.
function kindOf(meta) {
  if (meta?.source === 'sample') return 'sample'
  if (meta?.kind === 'backtest' || meta?.backtest === true) return 'backtest'
  return 'paper_run'
}

function trackRow(t, ledgerByTrack) {
  const ledger = t.ledger ?? ledgerByTrack?.[t.track] ?? null
  return {
    track: t.track,
    label: t.label ?? t.track.replace(/_/g, ' '),
    candidates: finite(t.candidates) ?? 0,
    admitted: finite(t.admitted) ?? 0,
    paper_fills: finite(t.paper_fills) ?? 0,
    edge_bps: finite(t.edge_bps),
    settlement_risk: Boolean(t.settlement_risk ?? t.settlement_risk_flag),
    paper_pnl: ledger ? round2(finite(ledger.total_pnl)) : null,
  }
}

function entryFromScoreboard(file, doc) {
  const meta = doc.meta
  const kind = kindOf(meta)
  const isSample = kind === 'sample'
  const measuredPnl = !isSample && typeof meta.pnl_source === 'string'
  const t = doc.totals ?? {}
  const byTrack = doc.portfolio?.by_track
  return {
    run_id: meta.run_id ?? `${kind}:${file}`,
    kind,
    source: meta.source ?? 'unknown',
    label: meta.label ?? null,
    mode: KNOWN_MODES.has(meta.mode) ? meta.mode : (meta.mode ?? 'unknown'),
    cycle: meta.cycle ?? null,
    measured_at: meta.measured_at ?? null,
    generated_at: meta.generated_at ?? null,
    venues: Array.isArray(meta.venues) ? meta.venues : [],
    venue_focus: meta.venue_focus ?? null,
    kalshi_env: meta.kalshi_env ?? null,
    primary_track: meta.primary_track ?? null,
    pnl_source: measuredPnl ? meta.pnl_source : null,
    note: meta.note ?? null,
    totals: {
      candidates: finite(t.candidates) ?? 0,
      admitted: finite(t.admitted) ?? 0,
      rejects: finite(t.rejects) ?? 0,
      paper_fills: finite(t.paper_fills) ?? 0,
      // Sample files carry schema placeholders, never earnings: index them as null.
      paper_pnl: measuredPnl ? round2(finite(t.paper_pnl)) : null,
      realized_pnl: measuredPnl ? round2(finite(t.realized_pnl)) : null,
      unrealized_pnl: measuredPnl ? round2(finite(t.unrealized_pnl)) : null,
      fees_paid: measuredPnl ? round2(finite(t.fees_paid)) : null,
    },
    tracks: (doc.tracks ?? []).map((tr) => {
      const row = trackRow(tr, byTrack)
      return measuredPnl ? row : { ...row, paper_pnl: null }
    }),
    artifacts: [file],
    detail: `/artifacts/${file}`,
  }
}

function entryFromRunRecord(file, doc) {
  const t = doc.totals ?? {}
  const measuredPnl = typeof doc.pnl_source === 'string'
  return {
    run_id: doc.run_id ?? `run:${file}`,
    kind: kindOf(doc),
    source: 'measured',
    label: doc.label ?? `MEASURED / ${String(doc.mode ?? 'run').toUpperCase()}`,
    mode: doc.mode ?? 'unknown',
    cycle: doc.cycle ?? null,
    measured_at: doc.measured_at ?? doc.completed_at ?? null,
    generated_at: doc.completed_at ?? null,
    venues: Array.isArray(doc.venues) ? doc.venues : [],
    venue_focus: doc.venue_focus ?? null,
    kalshi_env: doc.kalshi_env ?? null,
    primary_track: doc.primary_track ?? null,
    pnl_source: measuredPnl ? doc.pnl_source : null,
    note: null,
    totals: {
      candidates: finite(t.candidates) ?? 0,
      admitted: finite(t.admitted) ?? 0,
      rejects: finite(t.rejects) ?? 0,
      paper_fills: finite(t.paper_fills) ?? 0,
      paper_pnl: measuredPnl ? round2(finite(t.paper_pnl)) : null,
      realized_pnl: measuredPnl ? round2(finite(t.realized_pnl)) : null,
      unrealized_pnl: measuredPnl ? round2(finite(t.unrealized_pnl)) : null,
      fees_paid: measuredPnl ? round2(finite(t.fees_paid)) : null,
    },
    tracks: (doc.tracks ?? []).map((tr) => trackRow(tr)),
    artifacts: [`runs/${file}`],
    detail: `/artifacts/runs/${file}`,
  }
}

// Same run seen in several files (scoreboard_latest + scoreboard_network, or a
// scoreboard plus its run record). Keep the richer entry, remember every file.
function merge(existing, incoming) {
  const richer = existing.artifacts[0].startsWith('runs/') ? incoming : existing
  const other = richer === existing ? incoming : existing
  return {
    ...richer,
    cycle: richer.cycle ?? other.cycle,
    venues: richer.venues.length ? richer.venues : other.venues,
    venue_focus: richer.venue_focus ?? other.venue_focus,
    kalshi_env: richer.kalshi_env ?? other.kalshi_env,
    primary_track: richer.primary_track ?? other.primary_track,
    artifacts: [...new Set([...existing.artifacts, ...incoming.artifacts])],
  }
}

function ledgerEntry(file, doc) {
  const s = doc.summary ?? doc
  return {
    ledger_id: doc.ledger_id ?? s.ledger_id ?? file,
    updated_at: doc.updated_at ?? s.updated_at ?? null,
    mark_method: doc.mark_method ?? s.mark_method ?? null,
    fills: finite(s.fills) ?? (Array.isArray(doc.fills) ? doc.fills.length : 0),
    equity_points: finite(s.equity_points) ?? (doc.equity_curve?.length ?? 0),
    starting_cash: finite(s.starting_cash),
    equity: finite(s.equity),
    total_pnl: round2(finite(s.total_pnl)),
    max_drawdown: finite(s.max_drawdown),
    artifact: `/artifacts/${file}`,
  }
}

export async function buildExperimentsIndex(publicRoot) {
  const byRun = new Map()
  const ledgers = []
  let latestRunId = null

  const files = await listFiles(publicRoot)
  // Mode-specific boards before scoreboard_latest so the deduped entry points
  // at the stable filename and "latest" is only a flag.
  const boards = files
    .filter((f) => /^scoreboard_.*\.json$/.test(f))
    .sort((a, b) => Number(a === 'scoreboard_latest.json') - Number(b === 'scoreboard_latest.json'))
  for (const file of boards) {
    const doc = await readJson(join(publicRoot, file))
    if (!doc?.meta || !Array.isArray(doc.tracks) || !doc.totals) {
      console.warn(`skip ${file}: missing meta/tracks/totals`)
      continue
    }
    const entry = entryFromScoreboard(file, doc)
    if (file === 'scoreboard_latest.json') latestRunId = entry.run_id
    byRun.set(entry.run_id, byRun.has(entry.run_id) ? merge(byRun.get(entry.run_id), entry) : entry)
  }

  for (const file of await listFiles(join(publicRoot, 'runs'))) {
    if (!file.endsWith('.json')) continue
    const doc = await readJson(join(publicRoot, 'runs', file))
    if (!doc?.run_id || !Array.isArray(doc.tracks) || doc.paper_only !== true) {
      console.warn(`skip runs/${file}: not a paper run record`)
      continue
    }
    const entry = entryFromRunRecord(file, doc)
    byRun.set(entry.run_id, byRun.has(entry.run_id) ? merge(byRun.get(entry.run_id), entry) : entry)
  }

  for (const file of files.filter((f) => /^paper_ledger_.*\.json$/.test(f))) {
    const doc = await readJson(join(publicRoot, file))
    if (doc?.paper_only === true) ledgers.push(ledgerEntry(file, doc))
  }

  const runs = [...byRun.values()]
    .map((e) => ({ ...e, is_latest: e.run_id === latestRunId }))
    .sort((a, b) => {
      const ta = a.measured_at ?? ''
      const tb = b.measured_at ?? ''
      if (ta !== tb) return ta < tb ? 1 : -1
      return a.run_id < b.run_id ? 1 : -1
    })

  const counts = { total: runs.length, measured: 0, sample: 0, backtest: 0 }
  const modes = {}
  for (const r of runs) {
    if (r.kind === 'sample') counts.sample += 1
    else counts.measured += 1
    if (r.kind === 'backtest') counts.backtest += 1
    modes[r.mode] = (modes[r.mode] ?? 0) + 1
  }

  return {
    schema_version: INDEX_SCHEMA,
    paper_only: true,
    source: 'public/artifacts',
    counts,
    modes,
    latest_run_id: latestRunId,
    runs,
    ledgers,
  }
}

function stable(index) {
  const { generated_at: _ignored, ...rest } = index
  return JSON.stringify(rest)
}

export async function writeExperimentsIndex(publicRoot) {
  const index = await buildExperimentsIndex(publicRoot)
  const target = join(publicRoot, INDEX_FILE)
  const existing = await readJson(target)
  if (existing && stable(existing) === stable(index)) {
    console.log(`Experiments index is current (${index.counts.total} runs).`)
    return { index, changed: false }
  }
  await writeFile(
    target,
    `${JSON.stringify({ generated_at: new Date().toISOString(), ...index }, null, 2)}\n`,
  )
  console.log(
    `Wrote ${INDEX_FILE}: ${index.counts.total} runs (${index.counts.measured} measured, ${index.counts.sample} sample, ${index.counts.backtest} backtest), ${index.ledgers.length} ledgers.`,
  )
  return { index, changed: true }
}

const here = dirname(fileURLToPath(import.meta.url))
export const defaultPublicRoot = join(resolve(here, '..'), 'public', 'artifacts')

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await writeExperimentsIndex(defaultPublicRoot)
}
