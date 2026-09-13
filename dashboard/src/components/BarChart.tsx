type Bar = { label: string; value: number; accent?: string }

export function BarChart({
  title,
  bars,
  height = 120,
}: {
  title: string
  bars: Bar[]
  height?: number
}) {
  const max = Math.max(1, ...bars.map((b) => b.value))
  const width = Math.max(240, bars.length * 56)
  const pad = 28
  const chartH = height - pad
  const barW = Math.min(36, (width - 16) / Math.max(bars.length, 1) - 12)

  return (
    <div className="chart-card">
      <div className="chart-title">{title}</div>
      <svg viewBox={`0 0 ${width} ${height}`} className="bar-svg" role="img" aria-label={title}>
        {bars.map((b, i) => {
          const h = (b.value / max) * (chartH - 8)
          const x = 20 + i * ((width - 24) / bars.length)
          const y = chartH - h
          return (
            <g key={b.label}>
              <rect
                x={x}
                y={y}
                width={barW}
                height={Math.max(h, b.value > 0 ? 2 : 0)}
                rx={3}
                fill={b.accent ?? '#3d8bfd'}
                opacity={0.9}
              />
              <text x={x + barW / 2} y={chartH + 14} textAnchor="middle" className="bar-label">
                {b.label.length > 10 ? `${b.label.slice(0, 9)}…` : b.label}
              </text>
              <text x={x + barW / 2} y={y - 4} textAnchor="middle" className="bar-value">
                {b.value}
              </text>
            </g>
          )
        })}
        <line x1={8} y1={chartH} x2={width - 8} y2={chartH} stroke="#2a3548" strokeWidth={1} />
      </svg>
    </div>
  )
}
