import type { ScoreboardTotals } from '../types'
import { formatNum, formatPct } from '../types'

export function KpiStrip({ totals }: { totals: ScoreboardTotals }) {
  const items = [
    { label: 'Candidates', value: formatNum(totals.candidates), tone: 'neutral' },
    { label: 'Admitted', value: formatNum(totals.admitted), tone: 'good' },
    { label: 'Paper fills', value: formatNum(totals.paper_fills), tone: 'good' },
    { label: 'Fill rate', value: formatPct(totals.fill_rate), tone: 'neutral' },
    {
      label: 'Settlement-risk pairs',
      value: formatNum(totals.settlement_risk_pairs),
      tone: totals.settlement_risk_pairs > 0 ? 'warn' : 'neutral',
    },
    {
      label: 'Paper PnL',
      value: totals.paper_pnl >= 0 ? `+${formatNum(totals.paper_pnl, 2)}` : formatNum(totals.paper_pnl, 2),
      tone: totals.paper_pnl >= 0 ? 'good' : 'bad',
    },
  ] as const

  return (
    <section className="kpi-strip" aria-label="Key performance indicators">
      {items.map((item) => (
        <div key={item.label} className={`kpi-card tone-${item.tone}`}>
          <div className="kpi-label">{item.label}</div>
          <div className="kpi-value">{item.value}</div>
        </div>
      ))}
    </section>
  )
}
