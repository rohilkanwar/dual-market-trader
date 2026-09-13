# Design — Dual-market scoreboard

A locked design system for this dashboard. Every page redesign reads this file before
emitting code. Do not regenerate per page — extend or amend this file when the
system needs to grow.

## Pre-flight

Existing: Vite + React 19, TypeScript, sample/network artifacts under
`public/artifacts/`. Prior UI was a dense dark trading-desk aesthetic (radial
blooms, 6 KPI cards, 6-tab farm, bar charts) — removed in favour of Origin-inspired
modern-minimal paper. Data layer (`loadScoreboard.ts`, `types.ts`, artifacts JSON)
is preserved; numbers are never invented.

## Genre

modern-minimal

## Macrostructure family

- Marketing pages: n/a (this project is app-only)
- App pages: **Summary-first** — one health/summary sentence → short “what to look at”
  list → 3–4 big numbers → one clean table → optional collapsed Details. Not Workbench
  screenshot tours; not a tab farm.
- Content pages: n/a

## Theme

Coral-adjacent warmth on soft warm-white paper. Single restrained **soft green**
accent (calm “paper measurement” signal — not neon trading green).

- `--color-paper`     oklch(98.5% 0.006 85)
- `--color-paper-2`   oklch(96.5% 0.008 85)
- `--color-paper-3`   oklch(94% 0.01 85)
- `--color-ink`       oklch(22% 0.012 60)
- `--color-ink-2`     oklch(42% 0.01 60)
- `--color-muted`     oklch(55% 0.012 60)
- `--color-rule`      oklch(88% 0.008 85)
- `--color-accent`    oklch(58% 0.11 155)
- `--color-accent-ink` oklch(98% 0.01 155)
- `--color-focus`     oklch(55% 0.12 155)
- `--color-warn`      oklch(62% 0.12 75)
- `--color-warn-soft` oklch(95% 0.03 85)
- `--color-danger`    oklch(55% 0.14 25)

## Typography

- Display / body: **Inter**, weight 400–650, roman only (no italic headers)
- Mono: ui-monospace stack for codes / slugs only
- Display tracking: `-0.025em`
- Type scale anchor: `--text-display` = `clamp(1.75rem, 2.5vw + 0.75rem, 2.25rem)`
- Max content measure: ~720–880px centered

## Spacing

4-point named scale in `:root` / `tokens` block. Pages must use named tokens
(`var(--space-md)`), never raw values in new rules.

## Motion

- Cut / minimal — no scroll reveals, no chart animations
- Easings: `--ease-out: cubic-bezier(0.16, 1, 0.3, 1)` for expand/collapse only
- Reduced-motion: opacity-only, ≤ 150 ms

## Microinteractions stance

- Silent success
- Hover delay 800 ms · focus delay 0 ms (if tooltips appear)
- Details expand is the only intentional motion

## CTA voice

- Primary: soft filled accent pill, calm labels (“Show details”, not “Unleash”)
- Secondary: hairline-border pill on paper

## Per-page allowances

- App pages MUST NOT use enrichment — function carries the page
- Charts: prefer none; BarChart kept only if a single spark earns its place (default off)
- Honest copy only — keep SAMPLE / PAPER banners; never invent live trading numbers

## What pages MUST share

- Soft warm-white paper + near-black ink + one soft-green accent
- Inter single-family stack
- Summary-first rhythm
- Soft light cards with subtle borders; generous air
- Content max-width ~800px centered

## What pages MAY differ on

- Which Details subsections are open by default (default: all collapsed)
- Which 3–4 KPIs surface (must stay ≤ 4)

## Voice

Plain English, short labels, no jargon walls. Prefer “Single-venue fair value” over
snake_case track ids in the primary table.

## Anti-patterns banned here

Glassmorphism · gradient text · purple/cyan AI gradients · neon · dense dark
trading-terminal aesthetic · 6+ KPI cards · tab farms · chart clutter · card-in-card ·
italic headers · invented metrics

## Exports

### tokens.css

See `src/index.css` `:root` block — source of truth for this project.
