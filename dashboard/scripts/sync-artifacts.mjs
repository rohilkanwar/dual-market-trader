// Copy ledger-backed runtime artifacts from ../artifacts into public/artifacts.
//
// Only files that parse as JSON and carry meta/tracks/totals are copied, and a
// file whose meta.source is "sample" is never copied: generated artifacts are
// always "measured"/"synced", and committed samples stay hand-written.
//
// Every run manifest under ../artifacts/paper/runs/ is also reduced to a compact
// record in public/artifacts/runs/<run_id>.json (counts and ledger totals per
// track, no per-fill rows) so the experiments history survives scoreboard_latest
// being overwritten by the next run. Finally the experiments index is rebuilt.
import { copyFile, mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { writeExperimentsIndex } from './build-experiments-index.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const dashboardRoot = resolve(here, '..')
const repoRoot = resolve(dashboardRoot, '..')
const runtimeRoot = process.env.ARTIFACT_DIR
  ? resolve(process.env.ARTIFACT_DIR)
  : join(repoRoot, 'artifacts')
const publicRoot = join(dashboardRoot, 'public', 'artifacts')
// Newest run records kept in the public tree; an always-on paper loop writes one per cycle.
const MAX_RUN_RECORDS = Number(process.env.MAX_RUN_RECORDS ?? 200)
const LEDGER_PNL_SOURCE = 'core.ledger.PaperLedger'

const CANDIDATES = [
  'scoreboard_latest.json',
  'scoreboard_network.json',
  'scoreboard_fixtures.json',
  'paper_loop_latest.json',
  'paper/ledger_single_venue_fair_value.json',
]

function isScoreboard(doc) {
  return Boolean(doc?.meta) && Array.isArray(doc?.tracks) && Boolean(doc?.totals)
}

await mkdir(publicRoot, { recursive: true })
const copied = {}
for (const relative of CANDIDATES) {
  const source = join(runtimeRoot, relative)
  let doc
  try {
    doc = JSON.parse(await readFile(source, 'utf8'))
  } catch (error) {
    if (error?.code !== 'ENOENT') console.warn(`skip ${relative}: ${error.message}`)
    continue
  }
  const isBoard = relative.startsWith('scoreboard_')
  if (isBoard && !isScoreboard(doc)) {
    console.warn(`skip ${relative}: missing meta/tracks/totals`)
    continue
  }
  if (isBoard && doc.meta.source === 'sample') {
    console.warn(`skip ${relative}: refusing to publish meta.source=sample as a runtime artifact`)
    continue
  }
  if (relative.startsWith('paper/') && doc.paper_only !== true) {
    console.warn(`skip ${relative}: ledger is not marked paper_only`)
    continue
  }
  const target = join(publicRoot, relative.replace('paper/', 'paper_'))
  await copyFile(source, target)
  copied[relative] = {
    target: target.replace(`${dashboardRoot}/`, ''),
    source: doc.meta?.source ?? (doc.paper_only ? 'ledger' : 'unknown'),
    measured_at: doc.meta?.measured_at ?? doc.completed_at ?? doc.updated_at ?? null,
    paper_pnl: doc.totals?.paper_pnl ?? null,
  }
}

if (Object.keys(copied).length > 0) {
  const manifestPath = join(publicRoot, 'manifest.json')
  let existingArtifacts = null
  try {
    existingArtifacts = JSON.parse(await readFile(manifestPath, 'utf8')).artifacts
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
  if (JSON.stringify(existingArtifacts) !== JSON.stringify(copied)) {
    await writeFile(
      manifestPath,
      `${JSON.stringify(
        {
          generated_at: new Date().toISOString(),
          source: 'precomputed-paper-artifacts',
          paper_only: true,
          artifacts: copied,
        },
        null,
        2,
      )}\n`,
    )
    console.log(`Synced ${Object.keys(copied).length} precomputed scoreboard artifacts.`)
  } else {
    console.log('Precomputed scoreboard artifacts are already current.')
  }
} else {
  console.log('No runtime artifacts found; keeping committed static snapshots.')
}

// ---------------------------------------------------------------------------
// Run records: artifacts/paper/runs/<run_id>.json (+ paper_loop_history.jsonl)
// ---------------------------------------------------------------------------

const num = (v) => {
  if (v == null) return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}
const round2 = (v) => (v == null ? null : Math.round(v * 100) / 100)
const sum = (rows, pick) => round2(rows.reduce((acc, row) => acc + (num(pick(row)) ?? 0), 0))

async function loadLoopCycles() {
  const cycles = new Map()
  let text
  try {
    text = await readFile(join(runtimeRoot, 'paper_loop_history.jsonl'), 'utf8')
  } catch (error) {
    if (error?.code !== 'ENOENT') console.warn(`skip paper_loop_history.jsonl: ${error.message}`)
    return cycles
  }
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try {
      const row = JSON.parse(line)
      if (row?.run_id) cycles.set(row.run_id, row)
    } catch {
      // A torn final line from a loop that is still writing is not an error.
    }
  }
  return cycles
}

function compactTrack(track) {
  const ledger = track.ledger ?? {}
  return {
    track: track.track,
    label: track.label ?? track.track,
    candidates: num(track.candidates) ?? 0,
    admitted: num(track.admitted) ?? 0,
    rejects: num(track.rejects) ?? 0,
    proposed_orders: num(track.proposed_orders) ?? 0,
    paper_fills: num(track.paper_fills) ?? 0,
    fill_rate: num(track.fill_rate),
    edge_bps: num(track.edge_bps),
    settlement_risk: Boolean(track.settlement_risk ?? track.settlement_risk_flag),
    reject_reasons: track.reject_reasons ?? track.refused_by_reason ?? {},
    notes: track.notes ?? '',
    ledger: {
      ledger_id: ledger.ledger_id ?? null,
      starting_cash: num(ledger.starting_cash),
      cash: num(ledger.cash),
      equity: num(ledger.equity),
      realized_pnl: round2(num(ledger.realized_pnl)),
      unrealized_pnl: round2(num(ledger.unrealized_pnl)),
      total_pnl: round2(num(ledger.total_pnl)),
      fees_paid: round2(num(ledger.fees_paid)),
      fills: num(ledger.fills) ?? 0,
      open_positions: num(ledger.open_positions) ?? 0,
      max_drawdown: num(ledger.max_drawdown),
    },
  }
}

function compactRunRecord(file, manifest, cycle) {
  const tracks = (manifest.tracks ?? []).map(compactTrack)
  const ledgerBacked = tracks.length > 0 && tracks.every((t) => t.ledger.ledger_id)
  const derived = [`paper/runs/${file}`]
  if (cycle) derived.push('paper_loop_history.jsonl')
  return {
    schema_version: '1.0.0',
    kind: 'paper_run_record',
    paper_only: true,
    derived_from: derived,
    run_id: manifest.run_id,
    mode: manifest.mode ?? cycle?.mode ?? 'unknown',
    measured_at: manifest.measured_at ?? cycle?.completed_at ?? null,
    completed_at: cycle?.completed_at ?? null,
    cycle: cycle?.cycle ?? null,
    duration_seconds: num(cycle?.duration_seconds),
    primary_track: cycle?.primary_track ?? 'single_venue_fair_value',
    pnl_source: ledgerBacked ? LEDGER_PNL_SOURCE : null,
    totals: {
      candidates: sum(tracks, (t) => t.candidates),
      admitted: sum(tracks, (t) => t.admitted),
      rejects: sum(tracks, (t) => t.rejects),
      proposed_orders: sum(tracks, (t) => t.proposed_orders),
      paper_fills: sum(tracks, (t) => t.paper_fills),
      paper_pnl: ledgerBacked ? sum(tracks, (t) => t.ledger.total_pnl) : null,
      realized_pnl: ledgerBacked ? sum(tracks, (t) => t.ledger.realized_pnl) : null,
      unrealized_pnl: ledgerBacked ? sum(tracks, (t) => t.ledger.unrealized_pnl) : null,
      fees_paid: ledgerBacked ? sum(tracks, (t) => t.ledger.fees_paid) : null,
    },
    tracks,
  }
}

async function syncRunRecords() {
  const runsDir = join(runtimeRoot, 'paper', 'runs')
  let files
  try {
    files = (await readdir(runsDir)).filter((f) => f.endsWith('.json')).sort()
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
    return 0
  }
  if (files.length > MAX_RUN_RECORDS) {
    console.warn(`paper/runs has ${files.length} manifests; keeping the newest ${MAX_RUN_RECORDS}`)
    files = files.slice(-MAX_RUN_RECORDS)
  }
  const cycles = await loadLoopCycles()
  const targetDir = join(publicRoot, 'runs')
  await mkdir(targetDir, { recursive: true })
  let written = 0
  for (const file of files) {
    let manifest
    try {
      manifest = JSON.parse(await readFile(join(runsDir, file), 'utf8'))
    } catch (error) {
      console.warn(`skip paper/runs/${file}: ${error.message}`)
      continue
    }
    if (manifest?.paper_only !== true || !manifest.run_id || !Array.isArray(manifest.tracks)) {
      console.warn(`skip paper/runs/${file}: not a paper run manifest`)
      continue
    }
    const record = compactRunRecord(file, manifest, cycles.get(manifest.run_id))
    const target = join(targetDir, `${manifest.run_id}.json`)
    const next = `${JSON.stringify(record, null, 2)}\n`
    let current = null
    try {
      current = await readFile(target, 'utf8')
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error
    }
    if (current !== next) {
      await writeFile(target, next)
      written += 1
    }
  }
  console.log(`Run records: ${files.length} found, ${written} written to public/artifacts/runs/.`)
  return written
}

await syncRunRecords()
await writeExperimentsIndex(publicRoot)
