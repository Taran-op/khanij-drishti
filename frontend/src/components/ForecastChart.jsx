import { Area, CartesianGrid, ComposedChart, Legend, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { n0, shortDate } from '../format'
import { ChartDefs, GlassLegend, GlassTooltip, axisTick, glassDot, useChartId } from './ChartKit'

/** History (actual vs target) + forecast band (P10–P90), P50, and an optional what-if scenario. */
export default function ForecastChart({ history = [], forecast = [], scenario, asOf, height = 320 }) {
  const id = useChartId()
  const byDate = new Map()
  history.forEach((h) => byDate.set(h.date, { date: h.date, actual: h.actual_t, target: h.target_t }))
  forecast.forEach((f) => {
    const row = byDate.get(f.date) || { date: f.date }
    Object.assign(row, { band: [f.p10, f.p90], p50: f.p50, target: f.target_t })
    byDate.set(f.date, row)
  })
  scenario?.forEach((f) => {
    const row = byDate.get(f.date)
    if (row) row.scenario = f.p50
  })
  const data = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date))
  return (
    <div className="chart-glass">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: 12, right: 14, bottom: 0, left: 0 }}>
          <ChartDefs id={id} colors={{ blue: 'var(--c-blue)', ink: 'var(--ink)', green: 'var(--c-green)' }} />
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
          <XAxis dataKey="date" tickFormatter={shortDate} minTickGap={28} tick={axisTick} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={n0} width={52} tick={axisTick} axisLine={false} tickLine={false} />
          <Tooltip
            cursor={{ stroke: 'var(--c-blue)', strokeOpacity: 0.35, strokeWidth: 1 }}
            content={<GlassTooltip labelFormatter={shortDate}
                                   formatter={(v, name) => [Array.isArray(v) ? `${n0(v[0])} – ${n0(v[1])} t` : `${n0(v)} t`, name]} />}
          />
          <Legend content={<GlassLegend />} />
          <Area dataKey="band" name="Forecast P10–P90" fill={`url(#${id}-v-blue)`} stroke="var(--c-blue)" strokeOpacity={0.35}
                strokeWidth={1} activeDot={false} isAnimationActive={false} />
          <Area dataKey="actual" name="Actual" fill={`url(#${id}-v-ink)`} fillOpacity={0.35} stroke="var(--ink)" strokeWidth={1.5}
                className="gline" activeDot={glassDot('var(--ink)')} isAnimationActive={false} />
          <Line dataKey="p50" name="Forecast P50" stroke="var(--c-blue)" strokeWidth={2.5} dot={false} className="gline glow-blue"
                activeDot={glassDot('var(--c-blue)')} isAnimationActive={false} />
          {scenario && <Line dataKey="scenario" name="What-if scenario" stroke="var(--c-green)" strokeWidth={2.5} dot={false}
                             className="gline glow-green" activeDot={glassDot('var(--c-green)')} isAnimationActive={false} />}
          <Line dataKey="target" name="Daily target" stroke="var(--c-red)" strokeDasharray="6 5" strokeWidth={1.6} dot={false}
                className="gline glow-red" activeDot={false} isAnimationActive={false} />
          {asOf && <ReferenceLine x={asOf} stroke="var(--muted)" strokeDasharray="2 4"
                                  label={{ value: 'today', fontSize: 11, fill: 'var(--ink-2)', position: 'insideTopRight', className: 'gref' }} />}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
