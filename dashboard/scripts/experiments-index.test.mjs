// node --test scripts/
//
// Proves the experiments index and the sync script keep working when parallel
// strategy branches add track ids the dashboard has never seen, and that lanes
// report `missing` instead of inventing numbers when a family has no artifact.
import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

import { buildExperimentsIndex } from './build-experiments-index.mjs'
import { FAMILIES, LANE_FAMILIES, WEATHER_TRACKS, familyOf } from './track-families.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const run = promisify(execFile)

const track = (id, extra = {}) => ({
  track: id,
  candidates: 4,
  admitted: 2,
  rejects: 2,
  proposed_orders: 2,
  paper_fills: 1,
  fill_rate: 0.5,
  edge_bps: 120,
  settlement_risk_flag: false,
  ledger: { ledger_id: id, starting_cash: 1000, equity: 1001.5, realized_pnl: 1, unrealized_pnl: 0.5, total_pnl: 1.5, fees_paid: 0.1, fills: 1 },
  ...extra,
})

function scoreboard({ runId, measuredAt, mode = 'network', source = 'measured', tracks }) {
  const meta = {
    source,
    label: `${source.toUpperCase()} / ${mode.toUpperCase()}`,
    paper_only: true,
    mode,
    measured_at: measuredAt,
    venues: ['kalshi', 'polymarket'],
    primary_track: 'single_venue_fair_value',
    run_id: runId,
  }
  if (source !== 'sample') meta.pnl_source = 'core.ledger.PaperLedger'
  return {
    schema_version: '1.2.0',
    meta,
    totals: {
      candidates: tracks.length * 4,
      admitted: tracks.length * 2,
      rejects: tracks.length * 2,
      paper_fills: tracks.length,
      paper_pnl: tracks.length * 1.5,
    },
    tracks,
  }
}

// Runs `fn` with one fresh temp dir per name and removes them afterwards.
async function withTempDirs(names, fn) {
  const dirs = {}
  for (const name of names) dirs[name] = await mkdtemp(join(tmpdir(), `dmt-${name}-`))
  try {
    return await fn(dirs)
  } finally {
    await Promise.all(Object.values(dirs).map((dir) => rm(dir, { recursive: true, force: true })))
  }
}

test('familyOf: known ids, keyword heuristics, declared family, fallback', () => {
  assert.equal(familyOf('gated_cross_venue'), 'xv_gated')
  assert.equal(familyOf('gated_cross_venue_macro'), 'xv_gated')
  assert.equal(familyOf('small_deliberate_bet'), 'xv_gated')
  assert.equal(familyOf('ungated_cross_venue_macro'), 'xv_ungated')
  assert.equal(familyOf('sports_cross_venue'), 'xv_ungated')
  assert.equal(familyOf('single_venue_fair_value'), 'single_venue')
  assert.equal(familyOf('news_underreaction'), 'news')

  assert.equal(familyOf('polymarket_negrisk'), 'negrisk')
  assert.equal(familyOf('headline_drift_kalshi'), 'news')
  assert.equal(familyOf('polymarket_negrisk_combinatorial'), 'negrisk')
  assert.equal(familyOf('neg_risk_arb'), 'negrisk')
  assert.equal(familyOf('kalshi_maker_flb'), 'kalshi_flb')
  assert.equal(familyOf('kalshi_flb'), 'kalshi_flb')
  assert.equal(familyOf('gated_cross_venue_sports'), 'xv_gated')
  assert.equal(familyOf('cross_venue_experimental'), 'cross_venue')
  assert.equal(familyOf('xv_gated_crypto'), 'xv_gated')
  assert.equal(familyOf('kalshi_fair_value_canary'), 'single_venue')
  assert.equal(familyOf('tennis_whale_copy_30s'), 'tennis_copy')
  assert.equal(familyOf('tennis_whale_copy_2m'), 'tennis_copy')
  assert.equal(familyOf('polymarket_tennis_copy'), 'tennis_copy')
  assert.equal(familyOf('whale_copy_nba'), 'tennis_copy')
  assert.equal(familyOf({ track: 'mystery', metrics: { family: 'tennis_copy' } }), 'tennis_copy')
  assert.equal(familyOf('tennis_basis'), 'tennis_basis')
  assert.equal(familyOf('tennis_basis_polymarket_only'), 'tennis_basis')
  assert.equal(familyOf('weather_dead_bucket'), 'weather')
  assert.equal(familyOf('weather_bucket_edge'), 'weather')
  assert.equal(familyOf('something_entirely_new'), 'other')

  assert.equal(familyOf({ track: 'mystery', family: 'negrisk' }), 'negrisk')
  assert.equal(familyOf({ track: 'mystery', metrics: { track_family: 'kalshi_flb' } }), 'kalshi_flb')
  assert.equal(familyOf({ track: 'gated_cross_venue_macro', family: 'not_a_family' }), 'xv_gated')
  assert.equal(familyOf(''), 'other')
  assert.equal(familyOf(undefined), 'other')
})

test('familyOf: weather ids are reserved and win over broader keyword matchers', () => {
  assert.deepEqual(WEATHER_TRACKS, ['weather_bucket_edge', 'weather_dead_bucket', 'weather_calibrated_ensemble'])
  for (const id of WEATHER_TRACKS) assert.equal(familyOf(id), 'weather', id)
  // Variants a strategy branch might pick before the ids settle.
  assert.equal(familyOf('polymarket_weather_bucket'), 'weather')
  assert.equal(familyOf('metar_late_day_kill'), 'weather')
  assert.equal(familyOf('nws_temperature_ensemble'), 'weather')
  assert.equal(familyOf('dead_bucket_metar'), 'weather')
  // Ids that also contain a broader family's keyword still land in weather.
  assert.equal(familyOf('weather_maker_quote'), 'weather', 'not kalshi_flb')
  assert.equal(familyOf('weather_fair_value_ensemble'), 'weather', 'not single_venue')
  assert.equal(familyOf('weather_basis_metar'), 'weather', 'not tennis_basis')
  assert.equal(familyOf('weather_whale_copy'), 'weather', 'not tennis_copy')
  assert.equal(familyOf('weather_headline_drift'), 'weather', 'not news')
  // Existing families are untouched by the new rule.
  assert.equal(familyOf('category_specialist'), 'specialist')
  assert.equal(familyOf('kalshi_maker_quote'), 'kalshi_flb')
  assert.equal(familyOf('polymarket_negrisk_arb'), 'negrisk')
  // A declared family still wins over the id.
  assert.equal(familyOf({ track: 'weather_bucket_edge', family: 'other' }), 'other')
})

test('registry: lane families exist and every family has a label', () => {
  assert.deepEqual(LANE_FAMILIES, ['negrisk', 'kalshi_flb', 'xv_gated', 'weather'])
  for (const f of FAMILIES) assert.ok(f.label && f.description, f.id)
})

test('index: new track ids land in lanes; families without artifacts stay missing', () => withTempDirs(['index'], async ({ index: dir }) => {
  const known = ['gated_cross_venue_macro', 'ungated_cross_venue_macro', 'single_venue_fair_value']

  await writeFile(
    join(dir, 'scoreboard_network.json'),
    JSON.stringify(scoreboard({
      runId: 'run-old',
      measuredAt: '2026-09-14T10:00:00+00:00',
      tracks: [...known, 'polymarket_negrisk_combinatorial', 'weird_new_track'].map((id) => track(id)),
    })),
  )
  await writeFile(
    join(dir, 'scoreboard_latest.json'),
    JSON.stringify(scoreboard({
      runId: 'run-old',
      measuredAt: '2026-09-14T10:00:00+00:00',
      tracks: [...known, 'polymarket_negrisk_combinatorial', 'weird_new_track'].map((id) => track(id)),
    })),
  )
  await writeFile(
    join(dir, 'scoreboard_sample.json'),
    JSON.stringify(scoreboard({
      runId: undefined,
      source: 'sample',
      mode: 'network',
      measuredAt: '2026-09-01T00:00:00+00:00',
      tracks: [...known, 'kalshi_maker_flb'].map((id) => track(id)),
    })),
  )
  // A newer compact run record from a sister branch that only measures its own tracks.
  await mkdir(join(dir, 'runs'))
  await writeFile(
    join(dir, 'runs', 'run-new.json'),
    JSON.stringify({
      kind: 'paper_run_record',
      paper_only: true,
      run_id: 'run-new',
      mode: 'network',
      measured_at: '2026-09-14T12:00:00+00:00',
      pnl_source: 'core.ledger.PaperLedger',
      totals: { candidates: 8, admitted: 4, rejects: 4, paper_fills: 2, paper_pnl: 3 },
      tracks: [track('polymarket_negrisk'), track('single_venue_fair_value'), { label: 'no id' }],
    }),
  )

  const index = await buildExperimentsIndex(dir)

  assert.equal(index.schema_version, '1.3.0')
  assert.equal(index.paper_only, true)
  assert.deepEqual(index.counts, { total: 3, measured: 2, sample: 1, backtest: 0 })
  assert.equal(index.latest_run_id, 'run-old')

  const byId = Object.fromEntries(index.runs.map((r) => [r.run_id, r]))
  assert.deepEqual(index.runs.map((r) => r.run_id), ['run-new', 'run-old', 'sample:scoreboard_sample.json'])
  assert.deepEqual(byId['run-new'].families, ['negrisk', 'single_venue'])
  assert.equal(byId['run-new'].tracks.length, 2, 'row without an id is dropped, not fatal')
  assert.deepEqual(byId['run-old'].families, ['negrisk', 'other', 'single_venue', 'xv_gated', 'xv_ungated'])
  assert.equal(byId['run-old'].tracks.find((t) => t.track === 'weird_new_track').family, 'other')
  assert.deepEqual(byId['run-old'].artifacts, ['scoreboard_network.json', 'scoreboard_latest.json'])

  const sample = byId['sample:scoreboard_sample.json']
  assert.equal(sample.kind, 'sample')
  assert.equal(sample.pnl_source, null)
  assert.ok(sample.tracks.every((t) => t.paper_pnl === null), 'sample tracks never carry PnL')

  const lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.deepEqual(Object.keys(lanes), LANE_FAMILIES)

  assert.equal(lanes.negrisk.status, 'measured')
  assert.equal(lanes.negrisk.run_id, 'run-new', 'newest measured run carrying the family wins')
  assert.deepEqual(lanes.negrisk.tracks, ['polymarket_negrisk'])
  assert.equal(lanes.negrisk.paper_fills, 1)
  assert.equal(lanes.negrisk.paper_pnl, 1.5)
  assert.equal(lanes.negrisk.detail, '/artifacts/runs/run-new.json')

  assert.equal(lanes.kalshi_flb.status, 'sample_only', 'only the sample carries the FLB track')
  assert.equal(lanes.kalshi_flb.paper_fills, null)
  assert.equal(lanes.kalshi_flb.paper_pnl, null)
  assert.deepEqual(lanes.kalshi_flb.tracks, ['kalshi_maker_flb'])

  assert.equal(lanes.xv_gated.status, 'measured')
  assert.equal(lanes.xv_gated.run_id, 'run-old')

  const families = Object.fromEntries(index.families.map((f) => [f.id, f]))
  assert.deepEqual(families.negrisk.tracks, ['polymarket_negrisk', 'polymarket_negrisk_combinatorial'])
  assert.equal(families.negrisk.measured_runs, 2)
  assert.equal(families.kalshi_flb.measured_runs, 0)
  assert.equal(families.kalshi_flb.sample_runs, 1)
  assert.deepEqual(families.other.tracks, ['weird_new_track'])
}))

test('index: weather lane is missing on a core board, sample_only with a sample, measured once a weather run lands', () => withTempDirs(['weather'], async ({ weather: dir }) => {
  const core = ['gated_cross_venue', 'single_venue_fair_value', 'category_specialist']
  const weatherFinding = {
    status: 'measured', source: 'open-meteo', tracks: ['weather_bucket_edge', 'weather_dead_bucket'],
    markets: 6, buckets: 30, cities: ['Chicago', 'Dallas', 7], stations: 4, stations_parsed: 3, station_parse_rate: 0.75,
    ensemble_edge_n: 12, ensemble_edge_mean_bps: 85, dead_bucket_candidates: 5, dead_bucket_kills: 2,
    calibration_n: 40, preregistered_n: 30, evaluation_status: 'underpowered', hypothesis_validated: 'yes',
  }

  // 1. A board written before any weather branch merged: no weather block anywhere.
  await writeFile(
    join(dir, 'scoreboard_network.json'),
    JSON.stringify(scoreboard({ runId: 'run-core', measuredAt: '2026-09-15T10:00:00+00:00', tracks: core.map((id) => track(id)) })),
  )
  let index = await buildExperimentsIndex(dir)
  assert.equal(index.schema_version, '1.3.0')
  let lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.deepEqual(Object.keys(lanes), ['negrisk', 'kalshi_flb', 'xv_gated', 'weather'])
  assert.equal(lanes.weather.status, 'missing')
  assert.equal(lanes.weather.paper_fills, null)
  assert.equal(index.runs[0].weather, null)
  assert.equal(index.families.find((f) => f.id === 'weather').runs, 0)

  // 2. A hand-written sample weather board: lane is sample_only, no PnL, no headline.
  const sample = scoreboard({
    runId: undefined, source: 'sample', measuredAt: '2026-09-01T00:00:00+00:00',
    tracks: WEATHER_TRACKS.map((id) => track(id, { family: 'weather' })),
  })
  sample.findings = { weather: weatherFinding }
  await writeFile(join(dir, 'scoreboard_weather_sample.json'), JSON.stringify(sample))
  index = await buildExperimentsIndex(dir)
  lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.equal(lanes.weather.status, 'sample_only')
  assert.deepEqual(lanes.weather.tracks, [...WEATHER_TRACKS].sort())
  assert.equal(lanes.weather.paper_pnl, null)
  const sampleRun = index.runs.find((r) => r.kind === 'sample')
  assert.equal(sampleRun.weather, null, 'sample headlines are placeholders and never indexed')
  assert.ok(sampleRun.tracks.every((t) => t.family === 'weather' && t.paper_pnl === null))

  // 3. A measured weather run (own board + compact run record) carries the headline.
  const measured = scoreboard({
    runId: 'run-weather', measuredAt: '2026-09-15T12:00:00+00:00',
    tracks: [track('weather_bucket_edge', { paper_fills: 1 }), track('weather_dead_bucket', { paper_fills: 0 })],
  })
  measured.meta.track_family = 'weather'
  measured.meta.primary_track = 'weather_bucket_edge'
  measured.findings = { weather: weatherFinding }
  await writeFile(join(dir, 'scoreboard_weather.json'), JSON.stringify(measured))
  await mkdir(join(dir, 'runs'))
  await writeFile(
    join(dir, 'runs', 'run-weather.json'),
    JSON.stringify({
      kind: 'paper_run_record', paper_only: true, run_id: 'run-weather', mode: 'network',
      measured_at: '2026-09-15T12:00:00+00:00', pnl_source: 'core.ledger.PaperLedger', track_family: 'weather',
      totals: { candidates: 8, admitted: 4, rejects: 4, paper_fills: 1, paper_pnl: 3 },
      tracks: [track('weather_bucket_edge'), track('weather_dead_bucket')],
      weather: weatherFinding,
    }),
  )
  index = await buildExperimentsIndex(dir)
  lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.equal(lanes.weather.status, 'measured')
  assert.equal(lanes.weather.run_id, 'run-weather')
  assert.equal(lanes.weather.paper_fills, 1)
  assert.equal(lanes.weather.paper_pnl, 3)
  assert.equal(lanes.weather.detail, '/artifacts/scoreboard_weather.json', 'the board beats the compact record')

  const run = index.runs.find((r) => r.run_id === 'run-weather')
  assert.deepEqual(run.families, ['weather'])
  assert.deepEqual(run.artifacts, ['scoreboard_weather.json', 'runs/run-weather.json'])
  assert.equal(run.weather.status, 'measured')
  assert.deepEqual(run.weather.cities, ['Chicago', 'Dallas'], 'non-string cities are dropped')
  assert.equal(run.weather.station_parse_rate, 0.75)
  assert.equal(run.weather.dead_bucket_kills, 2)
  assert.equal(run.weather.calibration_n, 40)
  assert.equal(run.weather.evaluation_status, 'underpowered')
  assert.equal(run.weather.hypothesis_validated, false, 'only a boolean true validates')
  assert.equal(run.gate, null)

  // A half-filled headline from an in-progress branch is normalised, never fatal.
  const partial = scoreboard({ runId: 'run-partial', measuredAt: '2026-09-15T13:00:00+00:00', tracks: [track('weather_calibrated_ensemble')] })
  partial.findings = { weather: { status: 'no_weather_markets' } }
  await writeFile(join(dir, 'scoreboard_weather_partial.json'), JSON.stringify(partial))
  index = await buildExperimentsIndex(dir)
  const partialRun = index.runs.find((r) => r.run_id === 'run-partial')
  assert.deepEqual(partialRun.weather, {
    status: 'no_weather_markets', source: null, tracks: [], markets: 0, buckets: 0, cities: [], stations: 0, stations_parsed: 0,
    station_parse_rate: null, ensemble_edge_n: 0, ensemble_edge_mean_bps: null, dead_bucket_candidates: 0, dead_bucket_kills: 0,
    calibration_n: 0, preregistered_n: null, evaluation_status: 'not_run', hypothesis_validated: false,
  })
  const families = Object.fromEntries(index.families.map((f) => [f.id, f]))
  assert.deepEqual(families.weather.tracks, [...WEATHER_TRACKS].sort())
  assert.equal(families.weather.measured_runs, 2)
  assert.equal(families.weather.sample_runs, 1)
}))

test('index: empty directory yields an honest empty index with missing lanes', () => withTempDirs(['empty'], async ({ empty }) => {
  const index = await buildExperimentsIndex(empty)
  assert.equal(index.counts.total, 0)
  assert.deepEqual(index.runs, [])
  assert.deepEqual(index.ledgers, [])
  assert.deepEqual(index.gate_reports, [])
  assert.ok(index.lanes.every((l) => l.status === 'missing' && l.paper_pnl === null && l.paper_fills === null))
  assert.ok(index.families.every((f) => f.runs === 0 && f.tracks.length === 0))
}))

test('sync: discovers new scoreboards, per-track ledgers and run manifests', () => withTempDirs(['runtime', 'public'], async (dirs) => {
  const runtime = { dir: dirs.runtime }
  const publicDir = { dir: dirs.public }
  const newTrack = 'kalshi_maker_flb'

  await mkdir(join(runtime.dir, 'paper', 'runs'), { recursive: true })
  await writeFile(
    join(runtime.dir, 'scoreboard_network.json'),
    JSON.stringify(scoreboard({ runId: 'sync-run', measuredAt: '2026-09-14T13:00:00+00:00', tracks: [track('single_venue_fair_value'), track(newTrack)] })),
  )
  await writeFile(
    join(runtime.dir, 'scoreboard_flb.json'),
    JSON.stringify(scoreboard({ runId: 'flb-run', mode: 'network', measuredAt: '2026-09-14T13:30:00+00:00', tracks: [track(newTrack)] })),
  )
  await writeFile(
    join(runtime.dir, 'scoreboard_sample.json'),
    JSON.stringify(scoreboard({ runId: 'sample-run', source: 'sample', measuredAt: '2026-09-14T13:00:00+00:00', tracks: [track(newTrack)] })),
  )
  await writeFile(join(runtime.dir, 'scoreboard_broken.json'), '{ not json')
  // Weather reports: the contract is kind + meta + paper_only; samples and synthetic
  // fixture replays are never published, and an unrelated shape is refused.
  const weatherReport = (extra) => JSON.stringify({
    kind: 'weather_report', paper_only: true, totals: { markets: 3 },
    meta: { source: 'measured', mode: 'network', measured_at: '2026-09-14T14:00:00+00:00', run_id: 'weather-run', ...extra },
  })
  await writeFile(join(runtime.dir, 'weather_report_network.json'), weatherReport({}))
  await writeFile(join(runtime.dir, 'weather_report_latest.json'), weatherReport({}))
  await writeFile(join(runtime.dir, 'weather_report_fixtures.json'), weatherReport({ status: 'fixture_synthetic' }))
  await writeFile(join(runtime.dir, 'weather_report_sample.json'), weatherReport({ source: 'sample' }))
  await writeFile(join(runtime.dir, 'weather_report_bogus.json'), JSON.stringify({ kind: 'something_else', meta: {}, paper_only: true }))
  for (const id of ['single_venue_fair_value', newTrack]) {
    await writeFile(
      join(runtime.dir, 'paper', `ledger_${id}.json`),
      JSON.stringify({ paper_only: true, ledger_id: id, summary: { fills: 1, equity: 1001.5, total_pnl: 1.5, updated_at: '2026-09-14T13:00:01+00:00' } }),
    )
  }
  await writeFile(
    join(runtime.dir, 'paper', 'ledger_live_leak.json'),
    JSON.stringify({ paper_only: false, ledger_id: 'live_leak', summary: {} }),
  )
  await writeFile(
    join(runtime.dir, 'paper', 'runs', 'sync-run.json'),
    JSON.stringify({
      run_id: 'sync-run',
      paper_only: true,
      mode: 'network',
      measured_at: '2026-09-14T13:00:00+00:00',
      primary_track: newTrack,
      tracks: [track('single_venue_fair_value'), track(newTrack, { family: 'kalshi_flb' })],
    }),
  )
  // A weather run manifest as persist_run writes it: the headline rides on the manifest.
  const weatherHeadline = { status: 'measured', tracks: ['weather_dead_bucket'], stations: 2, stations_parsed: 2, dead_bucket_kills: 1, evaluation_status: 'no_candidates', hypothesis_validated: false }
  await writeFile(
    join(runtime.dir, 'paper', 'runs', 'weather-run.json'),
    JSON.stringify({
      run_id: 'weather-run',
      paper_only: true,
      mode: 'network',
      measured_at: '2026-09-14T14:00:00+00:00',
      primary_track: 'weather_dead_bucket',
      track_family: 'weather',
      weather: weatherHeadline,
      tracks: [track('weather_dead_bucket')],
    }),
  )

  const { stdout } = await run(process.execPath, [join(here, 'sync-artifacts.mjs')], {
    env: { ...process.env, ARTIFACT_DIR: runtime.dir, PUBLIC_ARTIFACT_DIR: publicDir.dir },
  })
  // 2 boards + 2 ledgers + 2 weather reports; the sample board, the broken file, the
  // non-paper ledger and the synthetic / sample / bogus weather reports are refused.
  assert.match(stdout, /Synced 6 precomputed scoreboard artifacts/)

  const manifest = JSON.parse(await readFile(join(publicDir.dir, 'manifest.json'), 'utf8'))
  assert.deepEqual(Object.keys(manifest.artifacts).sort(), [
    'paper/ledger_kalshi_maker_flb.json',
    'paper/ledger_single_venue_fair_value.json',
    'scoreboard_flb.json',
    'scoreboard_network.json',
    'weather_report_latest.json',
    'weather_report_network.json',
  ])
  assert.equal(manifest.paper_only, true)
  assert.equal(manifest.artifacts['weather_report_network.json'].source, 'measured')

  const record = JSON.parse(await readFile(join(publicDir.dir, 'runs', 'sync-run.json'), 'utf8'))
  assert.equal(record.primary_track, newTrack)
  assert.equal(record.tracks.find((t) => t.track === newTrack).family, 'kalshi_flb')
  assert.equal(record.tracks.find((t) => t.track === 'single_venue_fair_value').family, undefined)
  assert.equal(record.pnl_source, 'core.ledger.PaperLedger')
  assert.equal(record.weather, undefined, 'no weather block is invented for a core run')

  const weatherRecord = JSON.parse(await readFile(join(publicDir.dir, 'runs', 'weather-run.json'), 'utf8'))
  assert.deepEqual(weatherRecord.weather, weatherHeadline)
  assert.equal(weatherRecord.track_family, 'weather')

  const index = JSON.parse(await readFile(join(publicDir.dir, 'experiments_index.json'), 'utf8'))
  const lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.equal(lanes.kalshi_flb.status, 'measured')
  assert.equal(lanes.kalshi_flb.run_id, 'flb-run', 'the newer mode-specific board wins the lane')
  assert.equal(lanes.negrisk.status, 'missing')
  assert.equal(lanes.weather.status, 'measured')
  assert.equal(lanes.weather.run_id, 'weather-run')
  assert.deepEqual(lanes.weather.tracks, ['weather_dead_bucket'])
  const weatherRun = index.runs.find((r) => r.run_id === 'weather-run')
  assert.equal(weatherRun.weather.dead_bucket_kills, 1)
  assert.equal(weatherRun.weather.evaluation_status, 'no_candidates')
  assert.deepEqual(
    index.ledgers.map((l) => [l.track, l.family]).sort(),
    [['kalshi_maker_flb', 'kalshi_flb'], ['single_venue_fair_value', 'single_venue']],
  )
  assert.ok(!index.runs.some((r) => r.kind === 'sample'), 'runtime sample board is never published')
}))

test('index: gate reports join their run by run_id and never invent a run', () => withTempDirs(['gate'], async ({ gate: dir }) => {
  await writeFile(
    join(dir, 'scoreboard_network.json'),
    JSON.stringify(scoreboard({
      runId: 'run-gate',
      measuredAt: '2026-09-14T12:00:00+00:00',
      tracks: [track('gated_cross_venue', { candidates: 8, admitted: 0, paper_fills: 0 }), track('single_venue_fair_value')],
    })),
  )
  const totals = {
    track: 'gated_cross_venue', policy: 'strict', candidates: 8, gate_admitted: 0, gate_refused: 8,
    priced_but_no_edge: 0, traded: 0, paper_fills: 0,
    primary_reject_reasons: { clause_refuse_mismatch: 5, match_low_confidence: 3 },
    all_stage_reject_reasons: { clause_refuse_mismatch: 5, fingerprint_indeterminate: 8 },
    status: 'zero_admits_expected',
  }
  const report = (runId) => JSON.stringify({
    schema_version: '1.0.0', kind: 'gate_report',
    meta: { source: 'measured', paper_only: true, mode: 'network', measured_at: '2026-09-14T12:00:00+00:00', run_id: runId, policy: { name: 'strict' } },
    totals, stage_failures: {}, vetoed_candidates: [], pairs: new Array(8).fill({}), control: null,
  })
  await writeFile(join(dir, 'gate_report_network.json'), report('run-gate'))
  await writeFile(join(dir, 'gate_report_fixtures.json'), report('run-not-on-disk'))
  await writeFile(join(dir, 'gate_report_sample.json'), JSON.stringify({ kind: 'gate_report', meta: { source: 'sample', run_id: 'run-gate' }, totals, pairs: [] }))

  const index = await buildExperimentsIndex(dir)
  assert.equal(index.runs.length, 1, 'a report for an unknown run does not create a run')
  const run = index.runs[0]
  assert.equal(run.gate.gate_admitted, 0)
  assert.equal(run.gate.candidates, 8)
  assert.equal(run.gate.status, 'zero_admits_expected')
  assert.equal(run.gate_report, '/artifacts/gate_report_network.json')
  assert.ok(run.artifacts.includes('gate_report_network.json'))
  assert.equal(run.tracks.find((t) => t.track === 'gated_cross_venue').family, 'xv_gated')
  assert.deepEqual(index.gate_reports.map((g) => [g.file, g.run_id, g.pairs]), [
    ['gate_report_network.json', 'run-gate', 8],
    ['gate_report_fixtures.json', 'run-not-on-disk', 8],
  ], 'sample reports are never indexed')
}))
