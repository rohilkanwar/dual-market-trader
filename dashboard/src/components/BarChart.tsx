/** Charts intentionally unused in the Summary-first redesign — kept as a no-op stub. */
type Bar = { label: string; value: number; accent?: string }

export function BarChart(_props: {
  title: string
  bars: Bar[]
  height?: number
}) {
  return null
}
