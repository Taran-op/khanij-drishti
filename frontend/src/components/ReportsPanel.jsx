import { useState } from 'react'
import { api, download, useApi } from '../api'
import { useAuth } from '../auth'
import { Card, ErrorBox, Loading } from './ui'
import GlassSelect from './GlassSelect'

const CSV = [
  ['forecast', '30-day production forecast', 'production'],
  ['production', 'Production log (90 days)', 'production'],
  ['reserve_zones', 'Probable reserve zones', 'reserves'],
  ['alerts', 'Alert register', 'alerts'],
  ['boreholes', 'Borehole database', 'reserves'],
]
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const AUDS = ['operations', 'planners', 'geology', 'management']
const kb = (b) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`)
const when = (s) => (s ? s.replace('T', ' ').slice(0, 16) : '—')

export function describe(s) {
  const at = `at ${s.at}`
  if (s.every === 'daily') return `Daily ${at}`
  if (s.every === 'weekly') return `Weekly, ${WEEKDAYS[s.weekday ?? 0]} ${at}`
  return `Monthly, day ${s.monthday ?? 1} ${at}`
}

/** On-demand PDF / Excel generation + raw CSV exports. */
export function ReportDownloads({ catalog, onGenerated }) {
  const { can } = useAuth()
  const [busy, setBusy] = useState(null)
  const [err, setErr] = useState(null)
  const get = (url, name, key) => {
    setBusy(key); setErr(null)
    download(url, name).then(onGenerated).catch((e) => setErr(e.message)).finally(() => setBusy(null))
  }
  return (
    <Card title="Reports" sub="Generated from the live models and stores, archived, and delivered on schedule">
      {err && <p className="small sev-text-high">{err}</p>}
      <ul className="reports">
        {catalog.reports.map((r) => (
          <li key={r.name}>
            <div><b>{r.title}</b><div className="muted small">{r.desc}</div></div>
            <div className="row gap6">
              <button className="btn sm" disabled={!!busy} onClick={() => get(`/api/reports/${r.name}.pdf`, `${r.name}.pdf`, `${r.name}.pdf`)}>
                {busy === `${r.name}.pdf` ? 'Building…' : 'PDF'}</button>
              <button className="btn sm" disabled={!!busy} onClick={() => get(`/api/reports/${r.name}.xlsx`, `${r.name}.xlsx`, `${r.name}.xlsx`)}>
                {busy === `${r.name}.xlsx` ? 'Building…' : 'Excel'}</button>
            </div>
          </li>
        ))}
      </ul>
      <h4 className="mini-h">Raw data (CSV)</h4>
      <div className="chips">
        {CSV.filter((r) => can(r[2])).map(([k, title]) => (
          <button key={k} className="chip-btn" disabled={busy === k} onClick={() => get(`/api/reports/${k}.csv`, `${k}.csv`, k)}>
            {busy === k ? '…' : title}
          </button>
        ))}
      </div>
    </Card>
  )
}

export function ReportArchive({ archive }) {
  const [busy, setBusy] = useState(null)
  return (
    <Card title="Report archive" sub="Every generated report, on demand or scheduled" pad={false}>
      <div className="table-wrap" style={{ maxHeight: 360 }}>
        <table className="tbl compact">
          <thead><tr><th>Report</th><th>Created</th><th>By</th><th className="r">Size</th><th /></tr></thead>
          <tbody>
            {archive.length === 0 && <tr><td colSpan={5} className="muted small">No reports generated yet.</td></tr>}
            {archive.map((e) => (
              <tr key={e.id}>
                <td><b>{e.title}</b><div className="muted small">{e.subtitle}</div></td>
                <td className="mono small nowrap">{when(e.created)}</td>
                <td className="small">{e.by}</td>
                <td className="r mono small">{kb(e.size)}</td>
                <td className="r"><button className="btn sm" disabled={busy === e.id}
                  onClick={() => { setBusy(e.id); download(`/api/reports/archive/${e.id}`, e.id.split('/')[1]).finally(() => setBusy(null)) }}>
                  {e.format.toUpperCase()}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

export function Schedules({ catalog, onChange }) {
  const s = useApi(api.schedules, [])
  const { can } = useAuth()
  const admin = can('notify_admin')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(null)
  const [form, setForm] = useState(null)
  if (s.error) return <ErrorBox error={s.error} retry={s.reload} />
  if (!s.data) return <Loading />
  const act = (id, p, label) => {
    setBusy(id); setMsg(null)
    return p.then((r) => { setMsg(`${label}: ${r.detail ?? 'done'}`); s.reload(); onChange?.(); return true })
      .catch((e) => { setMsg(`${label} failed: ${e.message}`); return false }).finally(() => setBusy(null))
  }
  const names = Object.fromEntries(catalog.reports.map((r) => [r.name, r.title]))

  return (
    <Card title="Scheduled jobs" right={<span className={`badge ${s.data.alive ? 'risk-low' : 'sev-high'}`}>{s.data.alive ? 'scheduler running' : 'scheduler stopped'}</span>}
          sub="Reports are generated, archived and emailed to their audiences; the data refresh re-runs the full pipeline" pad={false}>
      {msg && <p className="small reg-msg" style={{ margin: '8px 14px' }}>{msg}</p>}
      <div className="table-wrap">
        <table className="tbl compact">
          <thead><tr><th>Job</th><th>When</th><th>Next run</th><th>Last run</th>{admin && <th />}</tr></thead>
          <tbody>
            {s.data.schedules.map((x) => (
              <tr key={x.id} className={x.active ? '' : 'is-baseline'}>
                <td><b>{x.name}</b>
                  <div className="muted small">{x.kind === 'report' ? `${names[x.report] ?? x.report} · ${x.formats.toUpperCase().replace(',', ' + ')} → ${x.audiences.replaceAll(',', ', ')}` : 'Full data & ML pipeline'}</div></td>
                <td className="small nowrap">{describe(x)}</td>
                <td className="mono small nowrap">{x.active ? when(x.next_run) : 'paused'}</td>
                <td className="small">{x.last_run ? <><span className={`badge ${x.last_status === 'ok' ? 'risk-low' : 'sev-high'}`}>{x.last_status}</span> <span className="mono">{when(x.last_run)}</span>
                  <div className="muted small clamp2" title={x.last_detail}>{x.last_detail}</div></> : <span className="muted">never</span>}</td>
                {admin && <td className="r nowrap">
                  <button className="btn sm" disabled={!!busy} onClick={() => act(x.id, api.runSchedule(x.id), `Run "${x.name}"`)}>{busy === x.id ? 'Running…' : 'Run now'}</button>{' '}
                  <button className="btn sm" disabled={!!busy} onClick={() => act(x.id, api.updateSchedule(x.id, { active: !x.active }), x.active ? 'Paused' : 'Resumed')}>{x.active ? 'Pause' : 'Resume'}</button>{' '}
                  <button className="btn sm" disabled={!!busy} onClick={() => setForm({ ...x })}>Edit</button>
                </td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {admin && !form && <div className="pad-row"><button className="btn sm" onClick={() => setForm({ name: '', kind: 'report', report: catalog.reports[0]?.name, formats: 'pdf', every: 'daily', at: '07:00', weekday: 0, monthday: 1, audiences: 'management' })}>+ New schedule</button></div>}
      {admin && form && <ScheduleForm form={form} setForm={setForm} catalog={catalog}
        onSave={(f) => act(f.id ?? 'new', f.id ? api.updateSchedule(f.id, f) : api.addSchedule(f), f.id ? 'Saved' : 'Added').then((ok) => ok && setForm(null))}
        onDelete={form.id ? () => act(form.id, api.deleteSchedule(form.id), 'Deleted') : null} />}
      {s.data.runs.length > 0 && (
        <details className="pad-row">
          <summary className="small linkish">Run history ({s.data.runs.length})</summary>
          <table className="tbl compact"><tbody>{s.data.runs.map((r) => (
            <tr key={r.id}><td className="mono small nowrap">{when(r.started)}</td><td className="small">{r.name}</td><td className="small">{r.trigger}</td>
              <td><span className={`badge ${r.status === 'ok' ? 'risk-low' : 'sev-high'}`}>{r.status}</span></td><td className="small muted">{r.detail}</td></tr>
          ))}</tbody></table>
        </details>
      )}
    </Card>
  )
}

function ScheduleForm({ form, setForm, catalog, onSave, onDelete }) {
  const f = form
  const set = (k) => (v) => setForm((o) => ({ ...o, [k]: v?.target ? v.target.value : v }))
  const auds = new Set((f.audiences || '').split(',').filter(Boolean))
  const toggleAud = (a) => { auds.has(a) ? auds.delete(a) : auds.add(a); set('audiences')([...auds].join(',')) }
  const fmts = new Set((f.formats || 'pdf').split(','))
  const toggleFmt = (x) => { fmts.has(x) ? fmts.delete(x) : fmts.add(x); if (fmts.size) set('formats')([...fmts].join(',')) }
  return (
    <div className="sched-form gpane">
      <div className="form-grid">
        <label>Name<input className="input" value={f.name} onChange={set('name')} placeholder="e.g. Shift-start brief" /></label>
        {!f.id && <label>Job<GlassSelect ariaLabel="Job kind" value={f.kind} onChange={set('kind')} options={[{ value: 'report', label: 'Report' }, { value: 'pipeline', label: 'Data & model refresh' }]} /></label>}
        {f.kind === 'report' && <label>Report<GlassSelect ariaLabel="Report" value={f.report} onChange={set('report')} options={catalog.reports.map((r) => ({ value: r.name, label: r.title }))} /></label>}
        <label>Repeat<GlassSelect ariaLabel="Repeat" value={f.every} onChange={set('every')} options={[{ value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' }, { value: 'monthly', label: 'Monthly' }]} /></label>
        {f.every === 'weekly' && <label>On<GlassSelect ariaLabel="Weekday" value={String(f.weekday ?? 0)} onChange={(v) => set('weekday')(Number(v))} options={WEEKDAYS.map((d, i) => ({ value: String(i), label: d }))} /></label>}
        {f.every === 'monthly' && <label>Day of month<input className="input" type="number" min={1} max={28} value={f.monthday ?? 1} onChange={(e) => set('monthday')(Number(e.target.value))} /></label>}
        <label>At<input className="input" type="time" value={f.at} onChange={set('at')} /></label>
      </div>
      {f.kind === 'report' && <div className="row gap12 wrap small">
        <span className="muted">Formats</span>{['pdf', 'xlsx'].map((x) => <label key={x} className="check"><input type="checkbox" checked={fmts.has(x)} onChange={() => toggleFmt(x)} /> {x.toUpperCase()}</label>)}
        <span className="muted" style={{ marginLeft: 12 }}>Send to</span>{AUDS.map((a) => <label key={a} className="check"><input type="checkbox" checked={auds.has(a)} onChange={() => toggleAud(a)} /> {a}</label>)}
      </div>}
      <div className="row gap6">
        <button className="btn sm primary" disabled={!f.name} onClick={() => onSave(f)}>{f.id ? 'Save' : 'Add schedule'}</button>
        <button className="btn sm" onClick={() => setForm(null)}>Cancel</button>
        {onDelete && <button className="btn sm danger" onClick={() => { onDelete(); setForm(null) }}>Delete</button>}
      </div>
    </div>
  )
}

export function Outbox({ messages, counts }) {
  return (
    <Card title="Delivery outbox" sub={`Email & SMS: ${Object.entries(counts || {}).map(([k, v]) => `${v} ${k}`).join(' · ') || 'nothing queued yet'}`} pad={false}>
      <div className="table-wrap" style={{ maxHeight: 340 }}>
        <table className="tbl compact">
          <thead><tr><th>When</th><th>Ch.</th><th>To</th><th>Subject</th><th>Status</th></tr></thead>
          <tbody>
            {messages.length === 0 && <tr><td colSpan={5} className="muted small">No messages yet. Add recipients under Delivery settings.</td></tr>}
            {messages.map((n) => (
              <tr key={n.id}>
                <td className="mono small nowrap">{when(n.ts)}</td>
                <td className="small">{n.channel === 'sms' ? 'SMS' : 'Email'}</td>
                <td className="small"><b>{n.recipient}</b><div className="mono muted">{n.address}</div></td>
                <td className="small">{n.subject}{n.attachment && <div className="muted mono">📎 {n.attachment}</div>}</td>
                <td><span className={`badge ${n.status === 'sent' ? 'risk-low' : n.status === 'failed' ? 'sev-high' : 'sev-medium'}`} title={n.last_error || ''}>{n.status}</span>
                  {n.last_error && <div className="muted small clamp2" title={n.last_error}>{n.last_error}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
