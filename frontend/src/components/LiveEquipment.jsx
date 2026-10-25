import { useEffect, useState } from 'react'
import { Area, Bar, CartesianGrid, ComposedChart, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api'
import { useAuth } from '../auth'
import { Card } from './ui'
import { ChartDefs, GlassLegend, GlassTooltip, axisTick, glassDot, useChartId } from './ChartKit'

const STATUS = ['working', 'idle', 'maintenance', 'breakdown']
const CODE = { w: 'working', i: 'idle', m: 'maintenance', b: 'breakdown', '-': 'nodata' }
const KIND = { excavator: 'Excavators', dumper: 'Dumpers', drill: 'Drills', loader: 'Loaders', hoist: 'Hoists' }
const ago = (min) => (min < 1 ? 'just now' : min < 60 ? `${Math.round(min)} min ago` : min < 1440 ? `${Math.round(min / 60)} h ago` : `${Math.round(min / 1440)} d ago`)
const hhmm = (s) => s?.slice(11, 16)

/** Equipment status: live fleet from device heartbeats, 24 h status timelines, 30-day availability log. */
export default function EquipmentStatus({ mineId }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const { can } = useAuth()
  const cid = useChartId()

  const load = () => api.equipmentStatus(mineId).then((x) => { setD(x); setErr(null) }).catch(setErr)
  useEffect(() => {
    setD(null)
    load()
    const id = setInterval(load, 20000)
    return () => clearInterval(id)
  }, [mineId]) // eslint-disable-line react-hooks/exhaustive-deps

  const feed = (action) => { setBusy(true); api.equipmentFeed(action).then(() => setTimeout(load, 1500)).finally(() => setBusy(false)) }
  const s = d?.summary
  const live = s && s.online > 0
  const groups = {}
  d?.machines.forEach((m) => { (groups[m.kind] ??= []).push(m) })
  const daily = (d?.daily || []).map((r) => ({ date: r.date, avail: r.excavator_availability_pct / 100, down: r.downtime_hrs,
    fleet: r.dumpers_total ? r.dumpers_available / r.dumpers_total : null }))

  return (
    <Card title="Equipment status" sub="Fleet telematics heartbeats through the ingestion API · status timelines · daily availability log"
          right={<div className="row gap6">
            {live ? <span className="live-dot">live · {ago((Date.now() - new Date(s.last_seen).getTime()) / 60000)}</span>
              : <span className="badge sev-medium">{s?.last_seen ? `offline since ${s.last_seen.replace('T', ' ').slice(0, 16)}` : 'no telemetry'}</span>}
            {can('pipeline_run') && d && (d.feed.running
              ? <button className="btn sm" disabled={busy} onClick={() => feed('stop')}>Stop virtual feed</button>
              : <button className="btn sm primary" disabled={busy} onClick={() => feed('start')}>Start virtual feed</button>)}
          </div>}>
      {err && <p className="muted small">Equipment status unavailable: {err.message}</p>}
      {d && !d.machines.length && <p className="muted small">No heartbeats in the last 24 h. Connect telematics to <span className="mono">POST /api/ingest/telemetry</span> with a device key{can('pipeline_run') ? ', or start the virtual feed.' : '.'}</p>}
      {d && d.machines.length > 0 && (
        <>
          <div className="eq-sum">
            {STATUS.map((st) => <div key={st} className={`eq-tile st-${st}`}><b>{live ? s[st] : '—'}</b><span>{st}</span></div>)}
            <div className="eq-tile"><b>{live && s.availability_now != null ? `${Math.round(s.availability_now * 100)}%` : '—'}</b><span>available now · {s.online}/{s.total} online</span></div>
          </div>
          <div className="eq-tl">
            <div className="eq-tl-axis small muted"><span>{hhmm(d.timeline_start)}</span><span>24 h status timeline ({d.bucket_min}-min buckets)</span><span>now</span></div>
            {Object.entries(groups).map(([kind, ms]) => (
              <div key={kind} className="eq-tl-group">
                <div className="eq-tl-kind small">{KIND[kind] ?? kind}</div>
                {ms.map((m) => (
                  <div key={m.equipment_id} className="eq-tl-row" title={`${m.equipment_id}: ${m.online ? m.status : 'offline'} · last seen ${ago(m.age_min)}${m.availability_24h != null ? ` · 24 h availability ${Math.round(m.availability_24h * 100)}%` : ''}${m.breakdowns_24h ? ` · ${m.breakdowns_24h} breakdown(s)` : ''}${m.fuel_pct != null ? ` · fuel ${m.fuel_pct}%` : ''}`}>
                    <span className={`eq-dot ${m.online ? `st-${m.status}` : 'off'}`} />
                    <span className="mono small eq-tl-id">{m.equipment_id.split('-').slice(1).join('-')}</span>
                    <span className="eq-tl-bar">{[...m.timeline].map((c, i) => <i key={i} className={`tl-${CODE[c]}`} />)}</span>
                    <span className="mono small eq-tl-v">{m.availability_24h != null ? `${Math.round(m.availability_24h * 100)}%` : '—'}</span>
                  </div>
                ))}
              </div>
            ))}
            <div className="eq-legend small muted">{STATUS.map((st) => <span key={st}><i className={`tl-${st}`} /> {st}</span>)}<span><i className="tl-nodata" /> no signal</span></div>
          </div>
        </>
      )}
      {daily.length > 0 && (
        <>
          <h4 className="mini-h">Last 30 days: daily equipment log</h4>
          <div className="chart-glass">
            <ResponsiveContainer width="100%" height={190}>
              <ComposedChart data={daily} margin={{ left: -8, right: 4, top: 8 }}>
                <ChartDefs id={cid} colors={{ violet: 'var(--c-violet)', red: 'var(--c-red)' }} />
                <CartesianGrid stroke="var(--grid)" strokeDasharray="3 6" vertical={false} />
                <XAxis dataKey="date" tickFormatter={(v) => v.slice(5)} minTickGap={24} tick={axisTick} axisLine={false} tickLine={false} />
                <YAxis yAxisId="a" domain={[0.3, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} tick={axisTick} width={44} axisLine={false} tickLine={false} />
                <YAxis yAxisId="h" orientation="right" tick={axisTick} width={30} axisLine={false} tickLine={false} />
                <Tooltip content={<GlassTooltip formatter={(v, n) => [n.startsWith('Downtime') ? `${v} h` : `${Math.round(v * 100)}%`, n]} />} />
                <Legend content={<GlassLegend />} />
                <Bar yAxisId="h" dataKey="down" name="Downtime (h)" barSize={6} radius={[99, 99, 0, 0]} fill={`url(#${cid}-b-red)`} isAnimationActive={false} />
                <Area yAxisId="a" dataKey="avail" name="Excavator availability" stroke="var(--c-violet)" strokeWidth={2} fill={`url(#${cid}-v-violet)`}
                      baseValue={0.3} className="gline glow-violet" activeDot={glassDot('var(--c-violet)')} isAnimationActive={false} />
                <Area yAxisId="a" dataKey="fleet" name="Dumpers available" stroke="var(--c-blue)" strokeWidth={1.5} fill="none" strokeDasharray="4 3"
                      activeDot={glassDot('var(--c-blue)')} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </Card>
  )
}
