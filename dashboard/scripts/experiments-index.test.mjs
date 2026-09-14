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
import { FAMILIES, LANE_FAMILIES, familyOf } from './track-families.mjs'

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
  assert.equal(familyOf('something_entirely_new'), 'other')

  assert.equal(familyOf({ track: 'mystery', family: 'negrisk' }), 'negrisk')
  assert.equal(familyOf({ track: 'mystery', metrics: { track_family: 'kalshi_flb' } }), 'kalshi_flb')
  assert.equal(familyOf({ track: 'gated_cross_venue_macro', family: 'not_a_family' }), 'xv_gated')
  assert.equal(familyOf(''), 'other')
  assert.equal(familyOf(undefined), 'other')
})

test('registry: lane families exist and every family has a label', () => {
  assert.deepEqual(LANE_FAMILIES, ['negrisk', 'kalshi_flb', 'xv_gated'])
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

  assert.equal(index.schema_version, '1.1.0')
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

test('index: empty directory yields an honest empty index with missing lanes', () => withTempDirs(['empty'], async ({ empty }) => {
  const index = await buildExperimentsIndex(empty)
  assert.equal(index.counts.total, 0)
  assert.deepEqual(index.runs, [])
  assert.deepEqual(index.ledgers, [])
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

  const { stdout } = await run(process.execPath, [join(here, 'sync-artifacts.mjs')], {
    env: { ...process.env, ARTIFACT_DIR: runtime.dir, PUBLIC_ARTIFACT_DIR: publicDir.dir },
  })
  // 2 boards + 2 ledgers; the sample board, the broken file and the non-paper ledger are refused.
  assert.match(stdout, /Synced 4 precomputed scoreboard artifacts/)

  const manifest = JSON.parse(await readFile(join(publicDir.dir, 'manifest.json'), 'utf8'))
  assert.deepEqual(Object.keys(manifest.artifacts).sort(), [
    'paper/ledger_kalshi_maker_flb.json',
    'paper/ledger_single_venue_fair_value.json',
    'scoreboard_flb.json',
    'scoreboard_network.json',
  ])
  assert.equal(manifest.paper_only, true)

  const record = JSON.parse(await readFile(join(publicDir.dir, 'runs', 'sync-run.json'), 'utf8'))
  assert.equal(record.primary_track, newTrack)
  assert.equal(record.tracks.find((t) => t.track === newTrack).family, 'kalshi_flb')
  assert.equal(record.tracks.find((t) => t.track === 'single_venue_fair_value').family, undefined)
  assert.equal(record.pnl_source, 'core.ledger.PaperLedger')

  const index = JSON.parse(await readFile(join(publicDir.dir, 'experiments_index.json'), 'utf8'))
  const lanes = Object.fromEntries(index.lanes.map((l) => [l.family, l]))
  assert.equal(lanes.kalshi_flb.status, 'measured')
  assert.equal(lanes.kalshi_flb.run_id, 'flb-run', 'the newer mode-specific board wins the lane')
  assert.equal(lanes.negrisk.status, 'missing')
  assert.deepEqual(
    index.ledgers.map((l) => [l.track, l.family]).sort(),
    [['kalshi_maker_flb', 'kalshi_flb'], ['single_venue_fair_value', 'single_venue']],
  )
  assert.ok(!index.runs.some((r) => r.kind === 'sample'), 'runtime sample board is never published')
}))
