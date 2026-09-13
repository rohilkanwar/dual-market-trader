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
