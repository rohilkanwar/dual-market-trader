// Build public/artifacts/experiments_index.json from the JSON already in
// public/artifacts. Nothing is fetched or measured here: the index only lists
// runs that exist on disk, so a Vercel build (which never sees ../artifacts)
// produces the same index as a local one.
//
// Inputs (all optional):
//   scoreboard_*.json   full dashboard artifacts; one run each (deduped by meta.run_id)
//   gate_report_*.json  per-pair admissibility verdicts; joined to a run by meta.run_id
//   runs/*.json         compact run records written by sync-artifacts.mjs
//   paper_ledger_*.json ledger snapshots (listed separately; they carry no run_id)
//
// Every track is stamped with a `family` (see track-families.mjs) so the UI can
// filter or group by strategy lane. Unknown track ids never fail the build: they
// fall back to keyword matching and finally to the `other` family.
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { FAMILIES, LANE_FAMILIES, familyOf } from './track-families.mjs'

const INDEX_FILE = 'experiments_index.json'
// 1.1.0: tracks carry `family`; runs carry `families[]`; root gains `families[]`
// and `lanes[]`; ledgers carry `track` + `family`. 1.0.0 readers still work.
// 1.2.0: runs carry `gate` (+ `gate_report` URL) and root gains `gate_reports[]`.
// 1.3.0: runs carry `weather` (compact headline of the weather tracks, or null).
const INDEX_SCHEMA = '1.3.0'
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
    family: familyOf(t),
    candidates: finite(t.candidates) ?? 0,
    admitted: finite(t.admitted) ?? 0,
    paper_fills: finite(t.paper_fills) ?? 0,
    edge_bps: finite(t.edge_bps),
    settlement_risk: Boolean(t.settlement_risk ?? t.settlement_risk_flag),
    paper_pnl: ledger ? round2(finite(ledger.total_pnl)) : null,
  }
}

// Admissibility headline for the settlement-safe track. Counts only; never
// defaulted to anything other than what the artifact states.
function gateSummary(raw) {
  if (!raw || typeof raw !== 'object') return null
  return {
    track: raw.track ?? 'gated_cross_venue',
    policy: raw.policy ?? null,
    candidates: finite(raw.candidates) ?? 0,
    gate_admitted: finite(raw.gate_admitted) ?? 0,
    gate_refused: finite(raw.gate_refused) ?? 0,
    priced_but_no_edge: finite(raw.priced_but_no_edge) ?? 0,
    traded: finite(raw.traded) ?? 0,
    paper_fills: finite(raw.paper_fills) ?? 0,
    primary_reject_reasons: raw.primary_reject_reasons ?? {},
    all_stage_reject_reasons: raw.all_stage_reject_reasons ?? {},
    status: raw.status ?? null,
  }
}

// Headline of the weather tracks (`findings.weather` on a board, `weather` on a
// run record). Counts only, copied as stated; `hypothesis_validated` is never
// defaulted to true. Returns null when the run carries no weather block, which
// is the normal state of every board written before the weather branches merge.
function weatherSummary(raw) {
  if (!raw || typeof raw !== 'object') return null
  const cities = Array.isArray(raw.cities) ? raw.cities.filter((c) => typeof c === 'string') : []
  return {
    status: typeof raw.status === 'string' ? raw.status : 'unknown',
    source: raw.source ?? null,
    tracks: Array.isArray(raw.tracks) ? raw.tracks.filter((t) => typeof t === 'string') : [],
    markets: finite(raw.markets) ?? 0,
    buckets: finite(raw.buckets) ?? 0,
    cities,
    stations: finite(raw.stations) ?? 0,
    stations_parsed: finite(raw.stations_parsed) ?? 0,
    station_parse_rate: finite(raw.station_parse_rate),
    ensemble_edge_n: finite(raw.ensemble_edge_n) ?? 0,
    ensemble_edge_mean_bps: finite(raw.ensemble_edge_mean_bps),
    dead_bucket_candidates: finite(raw.dead_bucket_candidates) ?? 0,
    dead_bucket_kills: finite(raw.dead_bucket_kills) ?? 0,
    calibration_n: finite(raw.calibration_n) ?? 0,
    preregistered_n: finite(raw.preregistered_n),
    evaluation_status: typeof raw.evaluation_status === 'string' ? raw.evaluation_status : 'not_run',
    hypothesis_validated: raw.hypothesis_validated === true,
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
    track_family: meta.track_family ?? null,
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
    tracks: validTracks(doc.tracks, file).map((tr) => {
      const row = trackRow(tr, byTrack)
      return measuredPnl ? row : { ...row, paper_pnl: null }
    }),
    gate: isSample ? null : gateSummary(doc.gate_report?.totals ?? doc.findings?.gated_cross_venue),
    weather: isSample ? null : weatherSummary(doc.findings?.weather),
    artifacts: [file],
    detail: `/artifacts/${file}`,
  }
}

// A track row needs a string id to be indexed; anything else is reported and dropped
// so one malformed row from an in-progress branch cannot hide the whole run.
function validTracks(tracks, file) {
  const rows = Array.isArray(tracks) ? tracks : []
  const kept = rows.filter((tr) => typeof tr?.track === 'string' && tr.track.length > 0)
  if (kept.length !== rows.length) console.warn(`${file}: dropped ${rows.length - kept.length} track row(s) without an id`)
  return kept
}

const distinctFamilies = (tracks) => [...new Set(tracks.map((t) => t.family))].sort()

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
    track_family: doc.track_family ?? null,
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
    tracks: validTracks(doc.tracks, `runs/${file}`).map((tr) => trackRow(tr)),
    gate: gateSummary(doc.gate),
    weather: weatherSummary(doc.weather),
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
    track_family: richer.track_family ?? other.track_family,
    gate: richer.gate ?? other.gate ?? null,
    weather: richer.weather ?? other.weather ?? null,
    artifacts: [...new Set([...existing.artifacts, ...incoming.artifacts])],
  }
}

function ledgerEntry(file, doc) {
  const s = doc.summary ?? doc
  // measure_all names each ledger after its track (paper/ledger_<track>.json,
  // ledger_id = track), so the filename is the fallback when ledger_id is absent.
  const track = doc.track ?? doc.ledger_id ?? s.ledger_id ?? file.replace(/^paper_ledger_/, '').replace(/\.json$/, '')
  return {
    ledger_id: doc.ledger_id ?? s.ledger_id ?? file,
    track,
    family: familyOf(track),
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

  // Gate reports are joined to the run they were measured with. A report whose
  // run is not on disk is listed under gate_reports[] but never invents a run.
  const gateReports = []
  for (const file of files.filter((f) => /^gate_report_.*\.json$/.test(f))) {
    const doc = await readJson(join(publicRoot, file))
    if (doc?.kind !== 'gate_report' || !doc.meta || !Array.isArray(doc.pairs) || !doc.totals) {
      console.warn(`skip ${file}: not a gate report`)
      continue
    }
    if (doc.meta.source === 'sample') continue
    const summary = gateSummary(doc.totals)
    gateReports.push({
      file,
      run_id: doc.meta.run_id ?? null,
      mode: doc.meta.mode ?? 'unknown',
      measured_at: doc.meta.measured_at ?? null,
      policy: doc.meta.policy?.name ?? null,
      pairs: doc.pairs.length,
      ...summary,
      artifact: `/artifacts/${file}`,
    })
    const run = doc.meta.run_id ? byRun.get(doc.meta.run_id) : null
    if (run) {
      run.gate = summary
      run.gate_report = `/artifacts/${file}`
      if (!run.artifacts.includes(file)) run.artifacts.push(file)
    }
  }

  for (const file of files.filter((f) => /^paper_ledger_.*\.json$/.test(f))) {
    const doc = await readJson(join(publicRoot, file))
    if (doc?.paper_only === true) ledgers.push(ledgerEntry(file, doc))
  }

  const runs = [...byRun.values()]
    .map((e) => ({ ...e, families: distinctFamilies(e.tracks), is_latest: e.run_id === latestRunId }))
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
    families: familySummaries(runs),
    lanes: LANE_FAMILIES.map((family) => laneFor(family, runs)),
    runs,
    ledgers,
    gate_reports: gateReports.sort((a, b) => ((a.measured_at ?? '') < (b.measured_at ?? '') ? 1 : -1)),
  }
}

// Every registered family, with the track ids seen under it and how many runs
// touched it. Families with zero runs stay listed so the UI can render empty lanes.
function familySummaries(runs) {
  return FAMILIES.map((f) => {
    const tracks = new Set()
    let measured = 0
    let sample = 0
    for (const run of runs) {
      const mine = run.tracks.filter((t) => t.family === f.id)
      if (mine.length === 0) continue
      for (const t of mine) tracks.add(t.track)
      if (run.kind === 'sample') sample += 1
      else measured += 1
    }
    return {
      id: f.id,
      label: f.label,
      description: f.description,
      lane: f.lane,
      tracks: [...tracks].sort(),
      runs: measured + sample,
      measured_runs: measured,
      sample_runs: sample,
    }
  })
}

// Lane = the newest measured run that carries at least one track of the family,
// reduced to that family's tracks. Nothing is estimated: when no measured run
// has the family the lane reports `missing` (or `sample_only`) with null numbers.
function laneFor(family, runs) {
  const f = FAMILIES.find((x) => x.id === family)
  const base = { family, label: f?.label ?? family, description: f?.description ?? null }
  const carrying = runs.filter((run) => run.tracks.some((t) => t.family === family))
  const measured = carrying.find((run) => run.kind !== 'sample')
  if (!measured) {
    return {
      ...base,
      status: carrying.length > 0 ? 'sample_only' : 'missing',
      run_id: null,
      measured_at: null,
      mode: null,
      pnl_source: null,
      tracks: [...new Set(carrying.flatMap((r) => r.tracks.filter((t) => t.family === family).map((t) => t.track)))].sort(),
      candidates: null,
      admitted: null,
      paper_fills: null,
      paper_pnl: null,
      detail: null,
    }
  }
  const mine = measured.tracks.filter((t) => t.family === family)
  const pnlKnown = Boolean(measured.pnl_source) && mine.every((t) => t.paper_pnl != null)
  const total = (pick) => mine.reduce((acc, t) => acc + (pick(t) ?? 0), 0)
  return {
    ...base,
    status: 'measured',
    run_id: measured.run_id,
    measured_at: measured.measured_at,
    mode: measured.mode,
    pnl_source: measured.pnl_source,
    tracks: mine.map((t) => t.track),
    candidates: total((t) => t.candidates),
    admitted: total((t) => t.admitted),
    paper_fills: total((t) => t.paper_fills),
    paper_pnl: pnlKnown ? round2(total((t) => t.paper_pnl)) : null,
    detail: measured.detail,
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
  const lanes = index.lanes.map((l) => `${l.label}=${l.status}`).join(', ')
  console.log(
    `Wrote ${INDEX_FILE}: ${index.counts.total} runs (${index.counts.measured} measured, ${index.counts.sample} sample, ${index.counts.backtest} backtest), ${index.ledgers.length} ledgers; lanes: ${lanes}.`,
  )
  return { index, changed: true }
}

const here = dirname(fileURLToPath(import.meta.url))
export const defaultPublicRoot = join(resolve(here, '..'), 'public', 'artifacts')

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await writeExperimentsIndex(defaultPublicRoot)
}
