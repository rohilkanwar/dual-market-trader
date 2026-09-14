// Copy ledger-backed runtime artifacts from ../artifacts into public/artifacts.
//
// Only files that parse as JSON and carry meta/tracks/totals are copied, and a
// file whose meta.source is "sample" is never copied: generated artifacts are
// always "measured"/"synced", and committed samples stay hand-written.
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const dashboardRoot = resolve(here, '..')
const repoRoot = resolve(dashboardRoot, '..')
const runtimeRoot = process.env.ARTIFACT_DIR
  ? resolve(process.env.ARTIFACT_DIR)
  : join(repoRoot, 'artifacts')
const publicRoot = join(dashboardRoot, 'public', 'artifacts')

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
