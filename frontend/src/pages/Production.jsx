import { useNavigate, useParams } from 'react-router-dom'
import { Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useApi } from '../api'
import ForecastChart from '../components/ForecastChart'
import { ChartDefs, GlassLegend, GlassTooltip, axisTick, glassDot, useChartId } from '../components/ChartKit'
import { Card, ErrorBox, Kpi, Loading, RiskBadge, Segmented } from '../components/ui'
import LiveEquipment from '../components/LiveEquipment'
import { kt, n0, pct, shortDate, t } from '../format'
import { Leaderboard } from '../components/ModelRegistry'

export default function Production() {
  const { mineId = 'DGB' } = useParams()
  const nav = useNavigate()
  const all = useApi(api.forecast, [])
  const d = useApi(() => api.production(mineId), [mineId])
  const mines = all.data ? Object.values(all.data.mines) : []

  return (
    <div className="page">
      <div className="page-h">
        <div>
          <h1>Production & Shortfall Prediction</h1>
          <p className="muted">Driver-based forecasting ({({ gbm: "gradient boosting", lstm: "LSTM", prophet: "Prophet" })[all.data?.model?.champion] ?? "champion model"} serving, chosen by backtest): equipment, fleet, manpower, blasting, rainfall &amp; ground wetness → P10/P50/P90 daily output and month-end risk.</p>
        </div>
      </div>
      {all.error && <ErrorBox error={all.error} retry={all.reload} />}
      <div className="mine-strip">
        {mines.map((f) => (
          <button key={f.mine_id} className={`mine-chip ${f.mine_id === mineId ? 'on' : ''}`} onClick={() => nav(`/production/${f.mine_id}`)}>
            <span className="mc-n">{f.mine_id}</span>
            <RiskBadge p={f.month.prob_miss} />
          </button>
        ))}
      </div>

      {d.error && <ErrorBox error={d.error} retry={d.reload} />}
      {!d.data ? <Loading label="Forecasting" /> : <MineView d={d.data} card={all.data?.model} />}
    </div>
  )
}

function MineView({ d, card }) {
  const { mine, forecast: f, history, drivers } = d
  const cid = useChartId()
  const mo = f.month
  const ops = [...history.slice(-60).map((h) => ({ date: h.date, eq: h.excavator_avail, rain: h.rain_mm })),
    ...f.daily.map((x) => ({ date: x.date, eqF: x.excavator_avail, rain: x.rain_mm, fc: true }))]
  return (
    <>
      <div className="kpis">
        <Kpi label={`${mine.name}: ${mo.month} projected`} value={t(mo.projected_t)} sub={<>P10–P90 {n0(mo.p10_t)}–{n0(mo.p90_t)} t</>} tone={mo.prob_miss >= 0.8 ? 'red' : mo.prob_miss >= 0.4 ? 'amber' : 'green'} />
        <Kpi label="Month target" value={t(mo.target_t)} sub={<>MTD actual {t(mo.mtd_actual_t)}</>} />
        <Kpi label="Probability of missing target" value={pct(mo.prob_miss)} sub={<>expected shortfall {t(mo.expected_shortfall_t)}</>} tone={mo.prob_miss >= 0.8 ? 'red' : mo.prob_miss >= 0.4 ? 'amber' : 'green'} />
        <Kpi label="Next 30 days" value={kt(f.horizon.forecast_t)} sub={<>vs plan {kt(f.horizon.target_t)} · risk {pct(f.horizon.prob_miss)}</>} />
        <Kpi label="Mine" value={<span className="cap">{mine.method}</span>} sub={<>{n0(mine.capacity_tpd)} t/day capacity · {mine.dumpers} dumpers</>} />
      </div>
      <LiveEquipment mineId={mine.id} />
      <Card title="Daily production: last 120 days and 30-day forecast" sub="Band = P10–P90. Drivers from the IMD extended-range forecast, mean-reverting equipment availability, and the blasting plan.">
        <ForecastChart history={history} forecast={f.daily} asOf={f.as_of} />
      </Card>
      <div className="grid-2">
        <Card title="What's driving the shortfall" sub="Extra output over the next 30 days if each driver were at nominal (model-estimated)">
          <div className="chart-glass">
            <ResponsiveContainer width="100%" height={210}>
              <BarChart data={drivers.drivers} layout="vertical" margin={{ left: 8, right: 56, top: 6, bottom: 0 }} barCategoryGap="28%">
                <ChartDefs id={cid} colors={{ blue: 'var(--c-blue)' }} />
                <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" horizontal={false} />
                <XAxis type="number" tickFormatter={n0} tick={axisTick} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="driver" width={150} tick={{ fontSize: 12, fill: 'var(--ink)', fontWeight: 600 }} axisLine={false} tickLine={false} />
                <Tooltip cursor={{ fill: 'var(--hover)', radius: 10 }} content={<GlassTooltip formatter={(v) => [t(v), 'Recoverable']} />} />
                <Bar dataKey="impact_t" name="Recoverable" fill={`url(#${cid}-h-blue)`} radius={[0, 99, 99, 0]} className="gbar glow-blue"
                     background={{ fill: 'var(--grid)', radius: 99 }} isAnimationActive={false}
                     label={{ position: 'right', formatter: (v) => `${n0(v)} t`, fill: 'var(--ink-2)', fontSize: 11, fontWeight: 600 }} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="muted small">Nominal = 91% excavator availability, 95% fleet, 93% attendance, no rain.</p>
        </Card>
        <Card title="Operational drivers" sub="Excavator availability (line) and rainfall (bars). Dashed/lighter = forecast scenario">
          <div className="chart-glass">
            <ResponsiveContainer width="100%" height={230}>
              <ComposedChart data={ops} margin={{ left: -8, right: 4, top: 10 }}>
                <ChartDefs id={cid} colors={{ rain: '#38bdf8', rainF: '#7dd3fc', violet: 'var(--c-violet)' }} />
                <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
                <XAxis dataKey="date" tickFormatter={shortDate} minTickGap={30} tick={axisTick} axisLine={false} tickLine={false} />
                <YAxis yAxisId="a" domain={[0.3, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} tick={axisTick} width={44} axisLine={false} tickLine={false} />
                <YAxis yAxisId="r" orientation="right" tick={axisTick} width={34} axisLine={false} tickLine={false} />
                <Tooltip cursor={{ stroke: 'var(--c-violet)', strokeOpacity: 0.35 }}
                         content={<GlassTooltip labelFormatter={shortDate} formatter={(v, n) => [n.includes('Rain') ? `${v.toFixed(1)} mm` : pct(v), n]} />} />
                <Legend content={<GlassLegend />} />
                <Bar yAxisId="r" dataKey="rain" name="Rain (mm)" barSize={5} radius={[99, 99, 0, 0]} className="gbar" isAnimationActive={false}>
                  {ops.map((o) => <Cell key={o.date} fill={`url(#${cid}-b-${o.fc ? 'rainF' : 'rain'})`} />)}
                </Bar>
                <Area yAxisId="a" dataKey="eq" name="Excavator availability" stroke="var(--c-violet)" strokeWidth={2} fill={`url(#${cid}-v-violet)`}
                      baseValue={0.3} className="gline glow-violet" activeDot={glassDot('var(--c-violet)')} isAnimationActive={false} />
                <Line yAxisId="a" dataKey="eqF" name="Availability (scenario)" stroke="var(--c-violet)" strokeDasharray="5 4" strokeWidth={2}
                      dot={false} className="gline glow-violet" activeDot={glassDot('var(--c-violet)')} isAnimationActive={false} />
                <ReferenceLine yAxisId="a" x={f.as_of} stroke="var(--muted)" strokeDasharray="2 4"
                               label={{ value: 'today', fontSize: 11, fill: 'var(--ink-2)', position: 'insideTopRight' }} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
      {card && (
        <Card title="Model card: shortfall forecaster" sub={card.algorithm}>
          <div className="kpis small-kpis">
            <Kpi label="30-day total MAPE (backtest)" value={`${card.backtest.model_mape_30d}%`} sub={`seasonal-naive baseline ${card.backtest.naive_mape_30d}%`} />
            <Kpi label="Daily MAE (backtest)" value={`${card.backtest.model_daily_mae_t} t`} sub={`baseline ${card.backtest.naive_daily_mae_t} t`} />
            <Kpi label="Driver fit R² (within mine)" value={card.driver_fit.r2_within_mine} sub={card.driver_fit.window} />
            <Kpi label="P10–P90 band coverage" value={pct(card.intervals?.coverage_calibrated ?? card.driver_fit.p10_p90_coverage)} sub={card.intervals ? `nominal 80% · conformal (raw heads ${pct(card.intervals.coverage_raw)})` : 'nominal 80%'} />
          </div>
          {card.leaderboard && <Leaderboard task="shortfall" rows={card.leaderboard} />}
          <p className="muted small">Backtest uses rolling origins ({card.backtest.origins.join(', ')}) with drivers frozen at the origin and rainfall from climatology: what the system actually knows on the day.
            Every forecaster gets the same driver scenario, so each one can answer “what if we fix the excavator?”. The lowest 30-day error serves the dashboard and the optimiser.
            Top drivers (GBM): {card.importance.slice(0, 4).map((x) => `${x.feature} (${pct(x.value)})`).join(', ')}.</p>
        </Card>
      )}
    </>
  )
}
