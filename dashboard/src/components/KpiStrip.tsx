import type { ScoreboardMeta, ScoreboardTotals } from '../types'
import { formatNum, formatSigned, hasLedgerPnl } from '../types'

export function KpiStrip({ totals, meta }: { totals: ScoreboardTotals; meta: ScoreboardMeta }) {
  const items: { label: string; value: string; tone: 'neutral' | 'accent' | 'warn' }[] = [
    { label: 'Candidates', value: formatNum(totals.candidates), tone: 'neutral' },
    { label: 'Admitted', value: formatNum(totals.admitted), tone: 'accent' },
    { label: 'Fills', value: formatNum(totals.paper_fills), tone: 'accent' },
    {
      label: 'Risk',
      value: formatNum(totals.settlement_risk_pairs),
      tone: totals.settlement_risk_pairs > 0 ? 'warn' : 'neutral',
    },
  ]
  // PnL is only shown when the artifact's numbers come from the paper ledger.
  // Sample files carry schema placeholders, never earnings.
  if (hasLedgerPnl(meta)) {
    items.push({
      label: 'Paper PnL',
      value: formatSigned(totals.paper_pnl),
      tone: totals.paper_pnl < 0 ? 'warn' : totals.paper_pnl > 0 ? 'accent' : 'neutral',
    })
  }

  return (
    <section className="stat-strip" aria-label="Summary">
      {items.map((item) => (
        <div key={item.label} className={`stat ${item.tone}`}>
          <div className="stat-value">{item.value}</div>
          <div className="stat-label">{item.label}</div>
        </div>
      ))}
    </section>
  )
}
