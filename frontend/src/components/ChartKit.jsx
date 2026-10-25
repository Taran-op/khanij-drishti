import { useId } from 'react'

/** Shared liquid-glass pieces for Recharts: gradients, glass tooltip, glass legend chips, glass dots. */

// Recharts needs ids that are valid in url(#…); useId() returns ":r1:"-style ids.
export function useChartId() {
  return useId().replace(/:/g, '')
}

/** Vertical fade fill (top strong → bottom transparent) and a glossy horizontal bar fill. */
export function ChartDefs({ id, colors = {} }) {
  return (
    <defs>
      {Object.entries(colors).map(([name, c]) => (
        <linearGradient key={`v-${name}`} id={`${id}-v-${name}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" style={{ stopColor: c, stopOpacity: 0.42 }} />
          <stop offset="100%" style={{ stopColor: c, stopOpacity: 0.04 }} />
        </linearGradient>
      ))}
      {Object.entries(colors).map(([name, c]) => (
        <linearGradient key={`h-${name}`} id={`${id}-h-${name}`} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" style={{ stopColor: c, stopOpacity: 0.55 }} />
          <stop offset="100%" style={{ stopColor: c, stopOpacity: 0.95 }} />
        </linearGradient>
      ))}
      {Object.entries(colors).map(([name, c]) => (
        <linearGradient key={`b-${name}`} id={`${id}-b-${name}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" style={{ stopColor: c, stopOpacity: 0.95 }} />
          <stop offset="100%" style={{ stopColor: c, stopOpacity: 0.45 }} />
        </linearGradient>
      ))}
    </defs>
  )
}

// Gradient-filled series report their colour as url(#…); pick the first plain colour instead.
const solid = (...cs) => cs.find((c) => typeof c === 'string' && !c.startsWith('url(')) || 'var(--c-blue)'

/** Tooltip content: frosted card, swatch per series. */
export function GlassTooltip({ active, payload, label, labelFormatter, formatter }) {
  if (!active || !payload?.length) return null
  const rows = payload.filter((p) => p.value != null && p.fill !== 'transparent' && p.color !== 'transparent'
    && !(Array.isArray(p.value) && p.value.every((x) => x == null)))
  if (!rows.length) return null
  return (
    <div className="gtip">
      <div className="gtip-h">{labelFormatter ? labelFormatter(label) : label}</div>
      {rows.map((p) => {
        const [v, name] = formatter ? formatter(p.value, p.name, p) : [p.value, p.name]
        return (
          <div key={p.dataKey ?? p.name} className="gtip-r">
            <span className="gtip-sw" style={{ background: solid(p.payload?.swatch, p.stroke, p.color, p.fill) }} />
            <span className="gtip-n">{name}</span>
            <b>{v}</b>
          </div>
        )
      })}
    </div>
  )
}

/** Legend content: glass chips. Series with legendType="none" are skipped. */
export function GlassLegend({ payload }) {
  if (!payload?.length) return null
  return (
    <div className="glegend">
      {payload.filter((p) => p.type !== 'none').map((p) => (
        <span key={p.value} className="glegend-i">
          <span className={`glegend-sw ${p.payload?.strokeDasharray ? 'dash' : ''} ${p.type === 'rect' || p.type === 'square' ? 'box' : ''}`}
                style={{ '--c': solid(p.payload?.stroke, p.color, p.payload?.fill) }} />
          {p.value}
        </span>
      ))}
    </div>
  )
}

/** Active dot rendered as a small glass bead in the series colour. */
export const glassDot = (color) => ({ r: 5, fill: 'var(--surface)', stroke: color, strokeWidth: 2.5, className: 'gdot' })

export const axisTick = { fontSize: 11, fill: 'var(--muted)' }
