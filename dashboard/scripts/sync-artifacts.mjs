// Copy ledger-backed runtime artifacts from ../artifacts into public/artifacts.
//
// Candidates are discovered: every scoreboard_*.json in ../artifacts, every
// paper/ledger_*.json (one per track, so new tracks from parallel branches
// appear without editing this file) and paper_loop_latest.json. Only files that
// parse as JSON and carry meta/tracks/totals are copied, and a file whose
// meta.source is "sample" is never copied: generated artifacts are always
// "measured"/"synced", and committed samples stay hand-written.
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
// PUBLIC_ARTIFACT_DIR exists for tests; deploys always publish into public/artifacts.
const publicRoot = process.env.PUBLIC_ARTIFACT_DIR
  ? resolve(process.env.PUBLIC_ARTIFACT_DIR)
  : join(dashboardRoot, 'public', 'artifacts')
// Newest run records kept in the public tree; an always-on paper loop writes one per cycle.
const MAX_RUN_RECORDS = Number(process.env.MAX_RUN_RECORDS ?? 200)
const LEDGER_PNL_SOURCE = 'core.ledger.PaperLedger'

// Discovered rather than listed: a sister branch that writes scoreboard_<mode>.json
// for a new mode, or paper/ledger_<track>.json for a new track, is picked up with
// no change here. Validation below still decides what is publishable.
async function listCandidates() {
  // weather_calibration_latest.json from apps.measure_weather_calibration
  // flb_report_latest.json is the Kalshi FLB report written by apps.measure_flb;
  // tennis_whale_report_latest.json the tennis whale copy report from apps.measure_tennis_whale;
  // tennis_basis_latest.json the tennis basis report from apps.measure_tennis_basis;
  // weather_report_<mode>.json the weather report the weather branches are expected to
  // write next to scoreboard_weather.json (kind "weather_report"; optional);
  // weather_buckets_latest.json the weather bucket-edge report from apps.measure_weather_buckets.
  const names = new Set([
    'paper_loop_latest.json',
    'polymarket_arb_latest.json',
    'flb_report_latest.json',
    'tennis_whale_report_latest.json',
    'tennis_basis_latest.json',
    'weather_buckets_latest.json',
    'weather_calibration_latest.json',
  ])
  for (const file of await safeReaddir(runtimeRoot)) {
    if (/^scoreboard_.*\.json$/.test(file)) names.add(file)
    if (/^gate_report_.*\.json$/.test(file)) names.add(file)
    if (/^specialist_scoreboard_.*\.json$/.test(file)) names.add(file)
    if (/^weather_report_.*\.json$/.test(file)) names.add(file)
  }
  for (const file of await safeReaddir(join(runtimeRoot, 'paper'))) {
    if (/^ledger_.*\.json$/.test(file)) names.add(`paper/${file}`)
  }
  return [...names].sort()
}

async function safeReaddir(dir) {
  try {
    return await readdir(dir)
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw error
  }
}

function isScoreboard(doc) {
  return Boolean(doc?.meta) && Array.isArray(doc?.tracks) && Boolean(doc?.totals)
}

// Per-pair admissibility verdicts for the gated_cross_venue track. Emitted on
// every run, including zero-candidate runs, so an empty pairs[] is expected.
function isGateReport(doc) {
  return doc?.kind === 'gate_report' && Boolean(doc?.meta) && Array.isArray(doc?.pairs) && Boolean(doc?.totals)
}

// Category specialist board: per-trader-category scores, promotions, follow log
// and the pre-registered evaluation. Emitted on every run; an empty follow log
// with evaluation_status "no_follows" is a valid, expected state.
function isSpecialistBoard(doc) {
  return (
    doc?.kind === 'specialist_scoreboard' &&
    Boolean(doc?.meta) &&
    Array.isArray(doc?.scoreboard) &&
    Array.isArray(doc?.follow_log) &&
    Boolean(doc?.totals) &&
    Boolean(doc?.preregistration)
  )
}

// Weather report contract (research/weather_tracks.py): `kind: "weather_report"`,
// `paper_only: true`, `meta.source` measured/synced. Everything else about its
// shape is the strategy branch's choice; the board only needs the headline that
// the scoreboard already carries under findings.weather.
function isWeatherReport(doc) {
  return doc?.kind === 'weather_report' && Boolean(doc?.meta) && doc?.paper_only === true
}

await mkdir(publicRoot, { recursive: true })
const copied = {}
for (const relative of await listCandidates()) {
  const source = join(runtimeRoot, relative)
  let doc
  try {
    doc = JSON.parse(await readFile(source, 'utf8'))
  } catch (error) {
    if (error?.code !== 'ENOENT') console.warn(`skip ${relative}: ${error.message}`)
    continue
  }
  const isBoard = relative.startsWith('scoreboard_')
  const isGate = relative.startsWith('gate_report_')
  const isSpecialist = relative.startsWith('specialist_scoreboard_')
  const isWeather = relative.startsWith('weather_report_')
  if (isBoard && !isScoreboard(doc)) {
    console.warn(`skip ${relative}: missing meta/tracks/totals`)
    continue
  }
  if (isGate && !isGateReport(doc)) {
    console.warn(`skip ${relative}: not a gate report (kind/meta/pairs/totals)`)
    continue
  }
  if (isSpecialist && !isSpecialistBoard(doc)) {
    console.warn(`skip ${relative}: not a specialist scoreboard (kind/meta/scoreboard/follow_log/totals/preregistration)`)
    continue
  }
  if (isWeather && !isWeatherReport(doc)) {
    console.warn(`skip ${relative}: not a paper-only weather_report (kind/meta/paper_only)`)
    continue
  }
  if ((isBoard || isGate || isSpecialist || isWeather) && doc.meta.source === 'sample') {
    console.warn(`skip ${relative}: refusing to publish meta.source=sample as a runtime artifact`)
    continue
  }
  if (isWeather && (doc.meta.status === 'fixture_synthetic' || doc.status === 'fixture_synthetic')) {
    console.warn(`skip ${relative}: refusing to publish a synthetic-fixture weather report as a runtime artifact`)
    continue
  }
  if (isBoard && doc.meta.paper_only === false) {
    console.warn(`skip ${relative}: refusing to publish a scoreboard that is not paper_only`)
    continue
  }
  if (relative.startsWith('paper/') && doc.paper_only !== true) {
    console.warn(`skip ${relative}: ledger is not marked paper_only`)
    continue
  }
  if (
    (relative === 'polymarket_arb_latest.json' || relative === 'tennis_basis_latest.json') &&
    (doc.paper_only !== true || doc.source !== 'measured')
  ) {
    console.warn(`skip ${relative}: report must be paper_only and measured`)
    continue
  }
  const isFlbReport = relative === 'flb_report_latest.json'
  if (isFlbReport && (doc.kind !== 'kalshi_flb_report' || doc.paper_only !== true)) {
    console.warn(`skip ${relative}: not a paper-only kalshi_flb_report`)
    continue
  }
  // tennis_whale_report_latest.json is written by apps.measure_tennis_whale; a
  // fixture replay is labelled status=fixture_synthetic and is never published.
  const isTennisReport = relative === 'tennis_whale_report_latest.json'
  if (isTennisReport && (doc.kind !== 'tennis_whale_copy_report' || doc.paper_only !== true)) {
    console.warn(`skip ${relative}: not a paper-only tennis_whale_copy_report`)
    continue
  }
  if (isTennisReport && doc.status === 'fixture_synthetic') {
    console.warn(`skip ${relative}: refusing to publish a synthetic-fixture tennis report as a runtime artifact`)
    continue
  }
  const isHeadlineReport = isFlbReport || isTennisReport
  const target = join(publicRoot, relative.replace('paper/', 'paper_'))
  await copyFile(source, target)
  copied[relative] = {
    target: target.replace(`${dashboardRoot}/`, ''),
    // Reports carry source/measured_at at top level (polymarket_arb / tennis_basis) or are headline reports.
    source: doc.meta?.source ?? doc.source ?? (isHeadlineReport ? 'measured' : doc.paper_only ? 'ledger' : 'unknown'),
    measured_at: doc.meta?.measured_at ?? doc.measured_at ?? doc.completed_at ?? doc.updated_at ?? null,
    paper_pnl: doc.totals?.paper_pnl ?? null,
    ...(isHeadlineReport ? { headline: doc.headline ?? null } : {}),
    ...(isTennisReport ? { status: doc.status ?? null, overall_verdict: doc.overall_verdict ?? null } : {}),
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
  // A track may declare its lane; the index builder falls back to keyword
  // matching on the id when it does not (see track-families.mjs).
  const family = track.family ?? track.metrics?.family ?? track.metrics?.track_family
  return {
    track: track.track,
    label: track.label ?? track.track,
    ...(typeof family === 'string' ? { family } : {}),
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

// Admissibility headline of the gated_cross_venue track, read from the track's
// own metrics so the record does not depend on gate_report_latest.json (which
// the next run overwrites).
function compactGate(manifest) {
  const gated = (manifest.tracks ?? []).find((t) => t.track === 'gated_cross_venue')
  if (!gated) return null
  const m = gated.metrics ?? {}
  const refused = num(m.gate_refused) ?? 0
  return {
    track: gated.track,
    policy: m.gate_policy?.name ?? null,
    candidates: num(gated.candidates) ?? 0,
    gate_admitted: num(m.gate_admitted) ?? Math.max(0, (num(gated.candidates) ?? 0) - refused),
    gate_refused: refused,
    priced_but_no_edge: Object.keys(m.priced_but_no_edge ?? {}).length,
    traded: num(gated.admitted) ?? 0,
    paper_fills: num(gated.paper_fills) ?? 0,
    primary_reject_reasons: gated.reject_reasons ?? gated.refused_by_reason ?? {},
    all_stage_reject_reasons: m.gate_reject_reasons_all ?? {},
    status: (num(gated.admitted) ?? 0) === 0 ? 'zero_admits_expected' : 'admits_present_verify_fingerprints',
  }
}

function compactRunRecord(file, manifest, cycle) {
  const rows = (manifest.tracks ?? []).filter((t) => typeof t?.track === 'string' && t.track.length > 0)
  if (rows.length !== (manifest.tracks ?? []).length) {
    console.warn(`paper/runs/${file}: dropped ${(manifest.tracks ?? []).length - rows.length} track row(s) without an id`)
  }
  const tracks = rows.map(compactTrack)
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
    label: manifest.label ?? null,
    venues: Array.isArray(manifest.venues) ? manifest.venues : [],
    venue_focus: manifest.venue_focus ?? null,
    kalshi_env: manifest.kalshi_env ?? null,
    track_family: manifest.track_family ?? null,
    primary_track: manifest.primary_track ?? cycle?.primary_track ?? 'single_venue_fair_value',
    ...(manifest.label ? { label: manifest.label } : {}),
    ...(manifest.kind ? { run_kind: manifest.kind } : {}),
    ...(Array.isArray(manifest.venues) ? { venues: manifest.venues } : {}),
    ...(manifest.venue_focus ? { venue_focus: manifest.venue_focus } : {}),
    ...(manifest.kalshi_env ? { kalshi_env: manifest.kalshi_env } : {}),
    ...(manifest.flb ? { flb: manifest.flb } : {}),
    ...(manifest.tennis_whale_copy ? { tennis_whale_copy: manifest.tennis_whale_copy } : {}),
    // Weather headline as the manifest states it (persist_run copies findings.weather
    // onto the manifest); absent on every run that carries no weather track.
    ...(manifest.weather && typeof manifest.weather === 'object' ? { weather: manifest.weather } : {}),
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
    gate: compactGate(manifest),
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
