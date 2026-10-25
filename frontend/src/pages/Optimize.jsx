import { useEffect, useRef, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, LabelList, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useApi } from '../api'
import ForecastChart from '../components/ForecastChart'
import GlassSelect from '../components/GlassSelect'
import { ChartDefs, GlassLegend, GlassTooltip, axisTick, useChartId } from '../components/ChartKit'
import { useAuth } from '../auth'
import { Card, ErrorBox, Kpi, Loading, RiskBadge } from '../components/ui'
import { n0, pct, signed, t } from '../format'

const MINES = [['BLG', 'Balaghat'], ['UKW', 'Ukwa'], ['TRD', 'Tirodi'], ['DGB', 'Dongri Buzurg'], ['CHK', 'Chikla'], ['KDR', 'Kandri']]

export default function Optimize() {
  return (
    <div className="page">
      <div className="page-h">
        <div>
          <h1>Optimization & Recommendation</h1>
          <p className="muted">What-if scenario analysis and prescriptive corrective actions, both scored by the same forecasting model.</p>
        </div>
      </div>
      <PrescriptiveGate />
      <WhatIf />
    </div>
  )
}

function PrescriptiveGate() {
  const { can, user } = useAuth()
  if (can('optimize_run')) return <Prescriptive />
  return (
    <Card title="Prescriptive plan" sub="Budget-constrained corrective-action recommendations">
      <p className="muted">Generating corrective-action plans is reserved for Operations and Management. As <b>{user.role_label}</b> you can explore scenarios with the what-if simulator below.</p>
    </Card>
  )
}

function Prescriptive() {
  const [budget, setBudget] = useState(40)
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const run = (b = budget) => {
    setBusy(true); setErr(null)
    api.optimize({ budget_lakh: b }).then(setRes).catch(setErr).finally(() => setBusy(false))
  }
  useEffect(() => { run(40) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const names = Object.fromEntries(MINES)
  return (
    <Card title="Prescriptive plan: minimise expected month-end shortfall"
          sub="Greedy budget-constrained search over corrective actions. Each candidate is re-forecast; picks best tonnes recovered per ₹ lakh."
          right={
            <div className="row gap center">
              <label className="small muted">Budget ₹{budget} lakh</label>
              <input type="range" min="5" max="100" step="5" value={budget} onChange={(e) => setBudget(+e.target.value)} />
              <button className="btn primary" disabled={busy} onClick={() => run()}>{busy ? 'Optimising…' : 'Recommend'}</button>
            </div>
          }>
      {err && <ErrorBox error={err} />}
      {!res ? <Loading label="Optimising" /> : (
        <>
          <div className="kpis small-kpis">
            <Kpi label="Expected shortfall before" value={t(res.expected_shortfall_before_t)} tone="red" />
            <Kpi label="After recommended plan" value={t(res.expected_shortfall_after_t)} tone="green" />
            <Kpi label="Tonnes recovered" value={t(res.recovered_t)} sub={`${pct(res.recovered_t / Math.max(1, res.expected_shortfall_before_t))} of expected shortfall`} />
            <Kpi label="Budget used" value={`₹${res.spent_lakh} lakh`} sub={`of ₹${res.budget_lakh} lakh`} />
          </div>
          <PlanCharts res={res} names={names} />
          <div className="grid-2">
            <div className="table-wrap glass-table">
              <table className="tbl">
                <thead><tr><th>#</th><th>Mine</th><th>Action</th><th className="r">₹ lakh</th><th className="r">Recovered</th><th className="r">t / lakh</th></tr></thead>
                <tbody>
                  {res.plan.map((p) => (
                    <tr key={p.step}>
                      <td>{p.step}</td><td className="nowrap"><b>{p.mine}</b></td>
                      <td><b>{p.title}</b><div className="muted small">{p.category}: {p.detail}</div></td>
                      <td className="r mono">{p.cost_lakh}</td><td className="r mono">{n0(p.recovered_t)} t</td><td className="r mono">{p.t_per_lakh}</td>
                    </tr>
                  ))}
                  {!res.plan.length && <tr><td colSpan="6" className="muted">No action recovers tonnage within this budget.</td></tr>}
                </tbody>
              </table>
            </div>
            <div>
            <div className="table-wrap glass-table">
              <table className="tbl">
                <thead><tr><th>Mine</th><th className="r">Projected before → after</th><th>Risk before</th><th>Risk after</th></tr></thead>
                <tbody>
                  {Object.keys(res.before).map((id) => (
                    <tr key={id}>
                      <td className="nowrap"><b>{names[id]}</b><div className="muted small">target {n0(res.before[id].target_t)} t</div></td>
                      <td className="r mono">{n0(res.before[id].projected_t)} → <b>{n0(res.after[id].projected_t)}</b></td>
                      <td><RiskBadge p={res.before[id].prob_miss} /></td>
                      <td><RiskBadge p={res.after[id].prob_miss} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
              <p className="muted small">Constraints checked: {res.constraints_checked.join(' · ')}.</p>
              {res.notes.map((n) => <p key={n} className="note">⚠ {n}</p>)}
            </div>
          </div>
        </>
      )}
    </Card>
  )
}

const SHORT = { maintenance: 'Maint.', redeploy: 'Dumpers', overtime: 'Overtime', blasting: 'Blasting', dewatering: 'Dewater', resequence: 'Re-seq.' }

/** Waterfall (shortfall → each action → remaining) and per-mine before/after, both liquid glass. */
function PlanCharts({ res, names }) {
  const id = useChartId()
  let running = res.expected_shortfall_before_t
  const fall = [{ label: 'Before', base: 0, val: running, kind: 'red', name: 'Expected shortfall before plan' }]
  res.plan.forEach((p) => {
    running -= p.recovered_t
    fall.push({ label: `${p.step}. ${p.mine_id} ${SHORT[p.action] ?? ''}`, base: Math.max(0, running), val: p.recovered_t, kind: 'green',
                name: `${p.mine}: ${p.title} (₹${p.cost_lakh} lakh)` })
  })
  fall.push({ label: 'After', base: 0, val: res.expected_shortfall_after_t, kind: 'blue', name: 'Expected shortfall after plan' })
  fall.forEach((r) => { r.swatch = `var(--c-${r.kind})` })
  const mines = Object.keys(res.before).map((m) => ({
    name: names[m], before: res.before[m].expected_shortfall_t, after: res.after[m].expected_shortfall_t,
  }))
  const tip = (v) => `${n0(v)} t`
  return (
    <div className="grid-2">
      <div>
        <h4 className="chart-h">How the plan closes the gap</h4>
        <div className="chart-glass">
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={fall} margin={{ top: 22, right: 10, left: 0, bottom: 0 }} barCategoryGap="22%">
              <ChartDefs id={id} colors={{ red: 'var(--c-red)', green: 'var(--c-green)', blue: 'var(--c-blue)' }} />
              <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
              <XAxis dataKey="label" tick={{ ...axisTick, fill: 'var(--ink-2)', fontWeight: 600 }} interval={0} axisLine={false} tickLine={false} />
              <YAxis tickFormatter={n0} width={52} tick={axisTick} axisLine={false} tickLine={false} />
              <Tooltip cursor={{ fill: 'var(--hover)', radius: 12 }}
                       content={<GlassTooltip labelFormatter={(l) => fall.find((r) => r.label === l)?.name ?? l}
                                              formatter={(v, _n, p) => [tip(v), p.payload.kind === 'green' ? 'Recovered' : 'Shortfall']} />} />
              <Bar dataKey="base" stackId="w" fill="transparent" legendType="none" isAnimationActive={false} />
              <Bar dataKey="val" stackId="w" radius={[10, 10, 10, 10]} className="gbar" isAnimationActive={false}>
                {/* LabelList reads the bar's own value; a plain label on a stacked bar shows the stack top */}
                <LabelList dataKey="val" position="top" fill="var(--ink-2)" fontSize={11} fontWeight={700}
                           formatter={(v) => (v ? `${(v / 1000).toFixed(1)}k` : '')} />
                {fall.map((r) => (
                  <Cell key={r.label} fill={`url(#${id}-b-${r.kind})`}
                        style={{ filter: `drop-shadow(0 4px 10px color-mix(in srgb, var(--c-${r.kind}) 45%, transparent))` }} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div>
        <h4 className="chart-h">Expected shortfall by mine</h4>
        <div className="chart-glass">
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={mines} margin={{ top: 12, right: 10, left: 0, bottom: 0 }} barCategoryGap="26%" barGap={3}>
              <ChartDefs id={`${id}m`} colors={{ red: 'var(--c-red)', green: 'var(--c-green)' }} />
              <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
              <XAxis dataKey="name" tick={{ ...axisTick, fill: 'var(--ink-2)', fontWeight: 600 }} interval={0} axisLine={false} tickLine={false} />
              <YAxis tickFormatter={n0} width={52} tick={axisTick} axisLine={false} tickLine={false} />
              <Tooltip cursor={{ fill: 'var(--hover)', radius: 12 }} content={<GlassTooltip formatter={(v, n) => [tip(v), n]} />} />
              <Legend content={<GlassLegend />} />
              <Bar dataKey="before" name="Before plan" fill={`url(#${id}m-b-red)`} stroke="var(--c-red)" radius={[99, 99, 4, 4]}
                   className="gbar glow-red-bar" isAnimationActive={false} />
              <Bar dataKey="after" name="After plan" fill={`url(#${id}m-b-green)`} stroke="var(--c-green)" radius={[99, 99, 4, 4]}
                   className="gbar glow-green-bar" isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}

const SLIDERS = [
  { key: 'excavator_avail', label: 'Excavator availability', min: 0.4, max: 0.98, step: 0.01, fmt: (v) => pct(v) },
  { key: 'equip_from_day', label: 'Equipment change effective from day', min: 0, max: 14, step: 1, fmt: (v) => `day ${v}` },
  { key: 'fleet_ratio', label: 'Dumper fleet available', min: 0.4, max: 1, step: 0.01, fmt: (v) => pct(v) },
  { key: 'manpower_ratio', label: 'Manpower attendance', min: 0.6, max: 1.05, step: 0.01, fmt: (v) => pct(v) },
  { key: 'blasts', label: 'Blast rounds per day', min: 0, max: 4, step: 1, fmt: (v) => v },
  { key: 'rain_mult', label: 'Rainfall vs IMD forecast', min: 0, max: 2, step: 0.1, fmt: (v) => `×${v.toFixed(1)}` },
  { key: 'extra_eff', label: 'Schedule / face re-sequencing gain', min: 0, max: 0.08, step: 0.01, fmt: (v) => `+${Math.round(v * 100)}%` },
]

function WhatIf() {
  const [mine, setMine] = useState('DGB')
  const base = useApi(() => api.production(mine), [mine])
  const [o, setO] = useState({})
  const [res, setRes] = useState(null)
  const timer = useRef()

  // Slider start positions = the baseline scenario. Only levers moved away from these are sent.
  const defaults = (d) => {
    const first = d.forecast.daily[0]
    return { excavator_avail: +first.excavator_avail.toFixed(2), equip_from_day: 0, fleet_ratio: +first.fleet_ratio.toFixed(2),
      manpower_ratio: +first.manpower_ratio.toFixed(2), blasts: d.mine.method === 'opencast' ? 2 : 3, rain_mult: 1, extra_eff: 0 }
  }
  const reset = () => { clearTimeout(timer.current); setO(defaults(base.data)); setRes(null) }
  useEffect(() => { if (base.data) reset() }, [base.data]) // eslint-disable-line react-hooks/exhaustive-deps

  const update = (k, v) => {
    const next = { ...o, [k]: v }
    setO(next)
    const d = defaults(base.data)
    const changed = Object.fromEntries(Object.entries(next).filter(([key, val]) => val !== d[key]))
    if (changed.equip_from_day != null && changed.excavator_avail == null) delete changed.equip_from_day
    clearTimeout(timer.current)
    if (!Object.keys(changed).length) { setRes(null); return }
    timer.current = setTimeout(() => api.whatif({ mine_id: mine, ...changed }).then(setRes), 250)
  }

  return (
    <Card title="What-if scenario analysis" sub="Move a lever and the model re-forecasts the next 30 days for that mine."
          right={<GlassSelect ariaLabel="Mine" value={mine} onChange={setMine} align="right" minMenuWidth={170} options={MINES.map(([k, n]) => ({ value: k, label: n }))} />}>
      {base.error && <ErrorBox error={base.error} />}
      {!base.data ? <Loading /> : (
        <div className="whatif">
          <div className="sliders">
            {SLIDERS.map((s) => (
              <label key={s.key} className="slider">
                <span className="s-l">{s.label}<b>{o[s.key] != null ? s.fmt(o[s.key]) : '—'}</b></span>
                <input type="range" min={s.min} max={s.max} step={s.step} value={o[s.key] ?? s.min} onChange={(e) => update(s.key, +e.target.value)} />
              </label>
            ))}
            <button className="btn sm" onClick={reset}>Reset to baseline</button>
          </div>
          <div className="whatif-r">
            <div className="kpis small-kpis">
              <Kpi label="Month projected" value={t((res?.scenario ?? base.data.forecast).month.projected_t)}
                   sub={res ? <>change <b className={res.delta_month_t >= 0 ? 'pos' : 'neg'}>{signed(res.delta_month_t)} t</b></> : 'baseline'} />
              <Kpi label="Risk of missing target" value={pct((res?.scenario ?? base.data.forecast).month.prob_miss)}
                   sub={`baseline ${pct(base.data.forecast.month.prob_miss)}`} />
              <Kpi label="30-day output" value={t((res?.scenario ?? base.data.forecast).horizon.forecast_t)}
                   sub={res ? <>change {signed(res.delta_horizon_t)} t</> : 'baseline'} />
            </div>
            <ForecastChart history={base.data.history.slice(-30)} forecast={base.data.forecast.daily} scenario={res?.scenario.daily} asOf={base.data.forecast.as_of} height={280} />
          </div>
        </div>
      )}
    </Card>
  )
}
