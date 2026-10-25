import { useState } from 'react'
import { api, useApi } from '../api'
import { useAuth } from '../auth'
import GlassSelect from './GlassSelect'
import { Card, ErrorBox, Loading, ProvBadge } from './ui'

const MINES = ['BLG', 'UKW', 'TRD', 'DGB', 'CHK', 'KDR']

/** Operational ingestion: devices, channels, batches, dead letters, and the MOIL calibration. */
export default function IngestionPanel() {
  const { can } = useAuth()
  const s = useApi(api.ingestStatus, [])
  const [form, setForm] = useState({ device_id: '', mine_id: 'BLG', label: '' })
  const [issued, setIssued] = useState(null)
  const [err, setErr] = useState(null)
  if (s.error) return <ErrorBox error={s.error} retry={s.reload} />
  if (!s.data) return <Loading />
  const d = s.data
  const issue = () => {
    setErr(null)
    api.createDevice(form).then((r) => { setIssued(r); setForm({ ...form, device_id: '', label: '' }); s.reload() }).catch(setErr)
  }
  return (
    <>
      <Card title="Operational ingestion" sub="Mine operations enter only through the ingestion API: devices, MIS uploads and backfill share one validation path"
            right={<button className="btn sm" onClick={s.reload}>Refresh</button>}>
        <div className="kpis small-kpis">
          {[['production_shift', 'shift production'], ['equipment_daily', 'equipment days'], ['blasting', 'blasting days'],
            ['manpower', 'manpower days'], ['heartbeat', 'heartbeats'], ['dead_letter', 'dead-lettered']].map(([k, l]) => (
            <div key={k} className={`kpi ${k === 'dead_letter' && d.counts[k] ? 'tone-amber' : ''}`}><div className="kpi-l">{l}</div><div className="kpi-v">{d.counts[k].toLocaleString('en-IN')}</div></div>
          ))}
        </div>
        <p className="small">Data up to <b className="mono">{d.latest_date ?? '—'}</b> · channels: {d.by_channel.map((c) => `${c.channel} ${Number(c.ok).toLocaleString('en-IN')} ok / ${c.bad} rejected`).join(' · ')}</p>
        <div className="grid-2">
          <div className="table-wrap glass-table" style={{ maxHeight: 300 }}>
            <table className="tbl compact">
              <thead><tr><th>Device</th><th>Mine</th><th>Last seen</th><th /></tr></thead>
              <tbody>{d.devices.map((v) => (
                <tr key={v.device_id} className={v.active ? '' : 'acked'}>
                  <td><span className="mono small">{v.device_id}</span><div className="muted small">{v.label}</div></td>
                  <td>{v.mine_id}</td>
                  <td className="mono small nowrap">{v.last_seen?.replace('T', ' ') ?? 'never'}</td>
                  <td>{can('users') && v.active ? <button className="btn sm" onClick={() => api.revokeDevice(v.device_id).then(s.reload)}>Revoke</button> : !v.active ? <span className="muted small">revoked</span> : null}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="table-wrap glass-table" style={{ maxHeight: 300 }}>
            <table className="tbl compact">
              <thead><tr><th>Dead letter</th><th>Reason</th></tr></thead>
              <tbody>{d.dead_letter.map((x, i) => (
                <tr key={i}><td className="mono small nowrap">{x.ts.replace('T', ' ')}<div className="muted">{x.channel} · {x.source}</div></td>
                  <td className="small">{x.error}<div className="muted mono" style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{x.record}</div></td></tr>
              ))}</tbody>
            </table>
          </div>
        </div>
        {can('users') && (
          <div className="dev-form">
            <span className="lp-l">Issue a device key</span>
            <input className="field-in" placeholder="device id, e.g. blg-fms-01" value={form.device_id} onChange={(e) => setForm({ ...form, device_id: e.target.value })} />
            <GlassSelect ariaLabel="Mine" value={form.mine_id} onChange={(v) => setForm({ ...form, mine_id: v })} options={MINES.map((m) => ({ value: m, label: m }))} />
            <input className="field-in" placeholder="label (optional)" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
            <button className="btn primary" disabled={!form.device_id} onClick={issue}>Issue key</button>
            {err && <span className="small neg">{err.message}</span>}
            {issued && <div className="key-once"><b>{issued.device_id}</b> → <code>{issued.key}</code><div className="muted small">{issued.note} Send it as the <span className="mono">X-Device-Key</span> header to <span className="mono">POST /api/ingest/telemetry</span>.</div></div>}
          </div>
        )}
      </Card>
      <Card title="Calibration to MOIL's published production" sub={d.calibration.note} pad={false}>
        <div className="table-wrap glass-table">
          <table className="tbl compact">
            <thead><tr><th>Period</th><th className="r">MOIL output (lakh t)</th><th>How obtained</th><th>Source</th></tr></thead>
            <tbody>{d.calibration.periods.map((p) => (
              <tr key={p.start}><td className="mono small nowrap">{p.start} → {p.end}</td>
                <td className="r mono">{p.lakh_t ?? '—'}</td>
                <td className="small">{p.lakh_t == null ? <><ProvBadge real={false} /> {p.derivation}</> : <><ProvBadge real /> {p.derivation}</>}</td>
                <td className="small muted">{p.sources.join('; ')}</td></tr>
            ))}</tbody>
          </table>
        </div>
        <p className="small" style={{ padding: '10px 14px' }}><ProvBadge real={false} /> Assumptions: our six mines = <b>{d.calibration.share_of_company * 100}%</b> of MOIL output;
          split {Object.entries(d.calibration.mine_split).map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(', ')}; monthly plan = same month last year × {d.calibration.plan_growth}.</p>
      </Card>
    </>
  )
}
