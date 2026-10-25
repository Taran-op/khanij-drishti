import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { Link, useNavigate } from 'react-router-dom'
import { Bar as RBar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useApi } from '../api'
import { useAuth } from '../auth'
import GisMap from '../components/GisMap'
import { ChartDefs, GlassTooltip, axisTick, useChartId } from '../components/ChartKit'
import { ErrorBox, Kpi, Loading, RiskBadge, Sev } from '../components/ui'
import { kt, n0, n1, pct, t } from '../format'

const ICON = {
  mines: 'M3 21h18M5 21V9l7-5 7 5v12M9 21v-6h6v6',
  map: 'M15 5.1 9 3 3 5v16l6-2.1 6 2.1 6-2V3l-6 2.1zM9 3v15.9M15 5.1V21',
  gap: 'M4 20V10M10 20V4M16 20v-8M22 20H2',
  alerts: 'M12 22a2 2 0 0 0 2-2h-4a2 2 0 0 0 2 2zm6-6V11a6 6 0 0 0-12 0v5l-2 2v1h16v-1l-2-2z',
  fy: 'M3 3v18h18M7 15l4-4 3 3 6-6',
}

// The five Command Center sections. Each renders as a liquid blob until opened.
const SECTIONS = [
  { key: 'mines', title: 'Mine status', sub: 'this month', tone: 'blue' },
  { key: 'map', title: 'Reserve & risk map', sub: 'prospectivity + risk', tone: 'violet' },
  { key: 'gap', title: 'Gap to target', sub: 'expected shortfall', tone: 'red' },
  { key: 'alerts', title: 'Priority alerts', sub: 'for your role', tone: 'amber' },
  { key: 'fy', title: 'Year to date', sub: 'FY by mine', tone: 'green' },
]

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

const TONE = Object.fromEntries(SECTIONS.map((s) => [s.key, s.tone]))
const clearBlobNames = () => document.querySelectorAll('[data-blob]').forEach((e) => { e.style.viewTransitionName = '' })

/**
 * Blob ⇄ panel morph via the View Transitions API. Only ONE element pair is ever named
 * ('vt-panel'): the clicked blob and the panel. Everything else crossfades with the page,
 * so no per-element snapshots (which render backdrop-filter as hard squares) are taken.
 * kind: 'open' | 'close' | 'switch' — drives the corner-radius animation in blobs.css.
 */
function morph(update, kind, key, prep) {
  if (!document.startViewTransition || reducedMotion()) { update(); return }
  const root = document.documentElement
  clearBlobNames()
  prep?.()
  root.style.setProperty('--vt-tone', `var(--c-${TONE[key]})`)
  root.classList.add('vt-run', `vt-${kind}`)
  const tr = document.startViewTransition(() => flushSync(update))
  // A skipped transition (hidden tab, rapid double-click) rejects `ready`; the update still applies.
  tr.ready.catch(() => {})
  tr.finished.catch(() => {}).finally(() => {
    root.classList.remove('vt-run', 'vt-open', 'vt-close', 'vt-switch')
    clearBlobNames()
  })
}

const Svg = ({ d, size = 22 }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
    <path d={d} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

export default function Overview() {
  const { user, can } = useAuth()
  const ov = useApi(api.overview, [])
  const al = useApi(api.alerts, [])
  const [open, setOpen] = useState(null)
  const headRef = useRef(null)
  const lastBlob = useRef(null)

  const openSection = (key) => {
    const switching = !!open
    lastBlob.current = key
    morph(() => setOpen(key), switching ? 'switch' : 'open', key, switching ? undefined : () => {
      const el = document.querySelector(`.blobs [data-blob="${key}"]`)
      if (el) el.style.viewTransitionName = 'vt-panel'
    })
  }
  const close = () => {
    lastBlob.current = open
    morph(() => setOpen(null), 'close', open)
  }

  // On close, the blob we're returning to must carry the shared name in the NEW snapshot.
  // Layout effects run inside flushSync, i.e. before the browser captures that snapshot.
  useLayoutEffect(() => {
    if (open || !lastBlob.current || !document.documentElement.classList.contains('vt-close')) return
    const el = document.querySelector(`.blobs [data-blob="${lastBlob.current}"]`)
    if (el) el.style.viewTransitionName = 'vt-panel'
  }, [open])

  // Focus follows the morph: into the panel on open, back to the blob on close. Esc collapses.
  useEffect(() => {
    if (open) headRef.current?.focus()
    else if (lastBlob.current) document.querySelector(`[data-blob="${lastBlob.current}"]`)?.focus()
  }, [open])
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && close()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  if (ov.error) return <ErrorBox error={ov.error} retry={ov.reload} />
  if (!ov.data) return <Loading label="Running forecasts" />
  const { kpis: k, fy } = ov.data
  const fyPct = k.fy_projected_t / k.fy_target_t
  const highAlerts = (al.data?.alerts || []).filter((a) => a.severity === 'high' && a.status === 'open').length

  const teaser = {
    mines: { stat: `${k.mines_high_risk}/${k.mines}`, label: 'mines at high risk' },
    map: { stat: k.new_zones, label: 'new reserve zones' },
    gap: { stat: kt(k.month_gap_t), label: `short in ${ov.data.month}` },
    alerts: { stat: al.data ? highAlerts : '…', label: 'high-severity alerts' },
    fy: { stat: pct(fyPct, 1), label: `${fy.fy} projected` },
  }
  const current = SECTIONS.find((s) => s.key === open)

  return (
    <div className="page">
      <div className="page-h">
        <div>
          <h1>Command Center</h1>
          <p className="muted">Welcome, {user.name.replace(/\s*\(.*\)/, '')} · {user.role_label}: {user.focus}</p>
        </div>
        <div className="row gap">
          {can('reserves') && <Link className="btn" to="/reserves">Open GIS viewer</Link>}
          {can('optimize_run') && <Link className="btn primary" to="/optimize">Get recommendations</Link>}
        </div>
      </div>

      <div className="kpis">
        <Kpi label={`${ov.data.month} projected vs target`} value={kt(k.month_projected_t)}
             sub={<>target {kt(k.month_target_t)} · gap <b className="neg">{kt(k.month_gap_t)}</b></>} tone={k.month_gap_t > 0 ? 'red' : 'green'} />
        <Kpi label="Mines at high risk this month" value={`${k.mines_high_risk} / ${k.mines}`} sub={<>≥80% chance of missing target · {k.mines_medium_risk} more at medium</>} tone={k.mines_high_risk ? 'red' : k.mines_medium_risk ? 'amber' : 'green'} />
        <Kpi label={`${fy.fy} projected attainment`} value={pct(fyPct, 1)} sub={<>{kt(k.fy_projected_t)} of {kt(k.fy_target_t)} plan</>} tone={fyPct < 0.95 ? 'amber' : 'green'} />
        <Kpi label="New probable reserve zones" value={k.new_zones} sub={<>{k.new_zones_high} high priority · ~{n1(k.new_zone_tonnes_mt)} Mt indicative</>} tone="violet" />
        <Kpi label="Open alerts" value={k.open_alerts} sub={<>{k.high_alerts} high severity</>} tone={k.high_alerts ? 'red' : 'blue'} />
      </div>

      {!open ? (
        <section className="blobs" aria-label="Command Center sections">
          <p className="blobs-hint muted small">Tap a blob to dive in</p>
          {SECTIONS.map((s, i) => (
            <div key={s.key} className="blob-wrap" style={{ '--i': i }}>
              <button className={`blob tone-${s.tone}`} data-blob={s.key}
                      onClick={() => openSection(s.key)} aria-expanded="false" aria-controls="blob-panel">
                <span className="blob-ic"><Svg d={ICON[s.key]} /></span>
                <span className="blob-stat">{teaser[s.key].stat}</span>
                <span className="blob-lbl">{teaser[s.key].label}</span>
                <span className="blob-title">{s.title}</span>
              </button>
            </div>
          ))}
        </section>
      ) : (
        <>
          <nav className="blob-dock" aria-label="Switch section">
            <button className="dock-back" onClick={close} aria-label="Back to all sections">
              <Svg d="M15 18l-6-6 6-6" size={18} /> All
            </button>
            {SECTIONS.filter((s) => s.key !== open).map((s) => (
              <button key={s.key} className={`blob mini tone-${s.tone}`} data-blob={s.key}
                      onClick={() => openSection(s.key)} title={s.title}>
                <span className="blob-ic"><Svg d={ICON[s.key]} size={18} /></span>
                <span className="mini-t">{s.title}</span>
              </button>
            ))}
          </nav>
          <section id="blob-panel" className={`blob-panel tone-${current.tone}`} style={{ viewTransitionName: 'vt-panel' }}
                   aria-labelledby="blob-panel-title">
            <header className="bp-h">
              <span className="blob-ic lg"><Svg d={ICON[current.key]} size={22} /></span>
              <div className="grow">
                <h2 id="blob-panel-title" ref={headRef} tabIndex={-1}>{current.title}</h2>
                <p className="muted small">{SUBTITLE[current.key](ov.data)}</p>
              </div>
              {current.key === 'alerts' && <Link to="/alerts" className="btn sm">All alerts →</Link>}
              {current.key === 'map' && can('reserves') && <Link to="/reserves" className="btn sm">Open GIS viewer →</Link>}
              <button className="icon-btn" onClick={close} aria-label="Close section" title="Close (Esc)">✕</button>
            </header>
            <div className="bp-b">
              {open === 'mines' && <MineStatus mines={ov.data.mines} />}
              {open === 'map' && <RiskMap mines={ov.data.mines} />}
              {open === 'gap' && <GapChart mines={ov.data.mines} />}
              {open === 'alerts' && <PriorityAlerts al={al} />}
              {open === 'fy' && <FyTable fy={fy} />}
            </div>
          </section>
        </>
      )}
    </div>
  )
}

const SUBTITLE = {
  mines: () => 'Month-to-date actuals + model forecast for the remaining days. Click a mine for its full forecast.',
  map: () => 'Prospectivity heat-map · new zones dashed amber · pins coloured by shortfall risk',
  gap: (d) => `Projected shortfall against the ${d.month} target, by mine. Click a bar to open that mine.`,
  alerts: () => 'Early warnings addressed to your role, highest severity first',
  fy: (d) => `${d.fy.fy}: projection = YTD actual + 30-day forecast + remaining plan × YTD attainment`,
}

function MineStatus({ mines }) {
  const nav = useNavigate()
  return (
    <div className="table-wrap glass-table">
      <table className="tbl">
        <thead><tr><th>Mine</th><th>Method</th><th className="r">MTD</th><th className="r">Projected</th><th className="r">Target</th><th>Risk of miss</th></tr></thead>
        <tbody>
          {mines.map((m) => (
            <tr key={m.id} className="click" onClick={() => nav(`/production/${m.id}`)}>
              <td className="nowrap"><b>{m.name}</b><div className="muted small">{m.district}, {m.state}</div></td>
              <td className="cap">{m.method}</td>
              <td className="r mono">{n0(m.mtd_actual_t)}</td>
              <td className="r mono">{n0(m.projected_t)}<div className="muted small">{n0(m.p10_t)}–{n0(m.p90_t)}</div></td>
              <td className="r mono">{n0(m.target_t)}</td>
              <td><RiskBadge p={m.prob_miss} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RiskMap({ mines }) {
  const { can } = useAuth()
  const nav = useNavigate()
  const risk = Object.fromEntries(mines.map((m) => [m.id, m.prob_miss]))
  return (
    <div className="bp-map">
      <GisMap height="100%" layer="prospectivity" opacity={0.8} overlays={{ zones: true, mines: true }} mineRisk={risk}
              onZoneClick={() => can('reserves') && nav('/reserves')} />
    </div>
  )
}

function GapChart({ mines }) {
  const nav = useNavigate()
  const cid = useChartId()
  const chart = mines.map((m) => {
    const tone = m.prob_miss >= 0.8 ? 'red' : m.prob_miss >= 0.4 ? 'amber' : 'green'
    return { name: m.name, gap: Math.max(0, m.target_t - m.projected_t), p: m.prob_miss, id: m.id, tone, swatch: `var(--c-${tone})` }
  })
  return (
    <div className="chart-glass">
      <ResponsiveContainer width="100%" height={360}>
        <BarChart data={chart} margin={{ left: 0, right: 8, top: 24, bottom: 0 }} barCategoryGap="30%">
          <ChartDefs id={cid} colors={{ red: 'var(--c-red)', amber: 'var(--c-amber)', green: 'var(--c-green)' }} />
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
          <XAxis dataKey="name" tick={{ ...axisTick, fill: 'var(--ink-2)', fontWeight: 600 }} interval={0} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={n0} width={52} tick={axisTick} axisLine={false} tickLine={false} />
          <Tooltip cursor={{ fill: 'var(--hover)', radius: 12 }}
                   content={<GlassTooltip formatter={(v, _n, p) => [`${t(v)} · risk ${pct(p.payload.p)}`, 'Gap to target']} />} />
          <RBar dataKey="gap" name="Gap" radius={[99, 99, 6, 6]} className="gbar" cursor="pointer" isAnimationActive={false} maxBarSize={72}
                background={{ fill: 'var(--grid)', radius: 99 }} onClick={(d) => nav(`/production/${d.id}`)}
                label={{ position: 'top', formatter: (v) => (v > 0 ? `${(v / 1000).toFixed(1)}k` : ''), fill: 'var(--ink-2)', fontSize: 11, fontWeight: 700 }}>
            {chart.map((c) => (
              <Cell key={c.id} fill={`url(#${cid}-b-${c.tone})`}
                    style={{ filter: `drop-shadow(0 4px 10px color-mix(in srgb, var(--c-${c.tone}) 45%, transparent))` }} />
            ))}
          </RBar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function PriorityAlerts({ al }) {
  if (al.error) return <ErrorBox error={al.error} retry={al.reload} />
  if (!al.data) return <Loading />
  return (
    <ul className="alist big flat">
      {al.data.alerts.slice(0, 10).map((a) => (
        <li key={a.alert_id} className={a.status === 'acknowledged' ? 'acked' : ''}>
          <Sev s={a.severity} />
          <div className="grow"><b>{a.title}</b><div className="small">{a.message}</div><div className="muted small">{a.recommended_action}</div></div>
        </li>
      ))}
    </ul>
  )
}

function FyTable({ fy }) {
  return (
    <div className="table-wrap glass-table">
      <table className="tbl">
        <thead><tr><th>Mine</th><th className="r">YTD actual</th><th className="r">YTD target</th><th>YTD attainment</th><th className="r">FY projected</th><th className="r">FY plan</th></tr></thead>
        <tbody>
          {fy.mines.map((m) => (
            <tr key={m.mine_id}>
              <td><b>{m.name}</b></td>
              <td className="r mono">{n0(m.ytd_actual_t)}</td>
              <td className="r mono">{n0(m.ytd_target_t)}</td>
              <td><div className="inline-bar"><span style={{ width: `${Math.min(100, m.attainment_ytd * 100)}%` }} className={m.attainment_ytd < 0.9 ? 'tone-bg-red' : m.attainment_ytd < 0.97 ? 'tone-bg-amber' : 'tone-bg-green'} /></div><span className="small mono">{pct(m.attainment_ytd, 1)}</span></td>
              <td className="r mono">{n0(m.fy_projected_t)}</td>
              <td className="r mono">{n0(m.fy_target_t)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
