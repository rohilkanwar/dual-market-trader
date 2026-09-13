import type { ScoreboardTotals } from '../types'
import { formatNum } from '../types'

/** Origin-style: few big numbers, tiny labels — max 4. */
export function KpiStrip({ totals }: { totals: ScoreboardTotals }) {
  const items = [
    { label: 'Candidates', value: formatNum(totals.candidates), tone: 'neutral' as const },
    { label: 'Admitted', value: formatNum(totals.admitted), tone: 'accent' as const },
    { label: 'Paper fills', value: formatNum(totals.paper_fills), tone: 'accent' as const },
    {
      label: 'Settlement risk pairs',
      value: formatNum(totals.settlement_risk_pairs),
      tone: totals.settlement_risk_pairs > 0 ? ('warn' as const) : ('neutral' as const),
    },
  ]

  return (
    <section className="stat-strip" aria-label="Summary numbers">
      {items.map((item) => (
        <div key={item.label} className={`stat ${item.tone}`}>
          <div className="stat-value">{item.value}</div>
          <div className="stat-label">{item.label}</div>
        </div>
      ))}
    </section>
  )
}
