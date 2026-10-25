import { useEffect, useState } from 'react'
import { api, useApi } from '../api'
import { Card, ErrorBox, Loading, ProvBadge } from '../components/ui'
import { ModelCard } from './Reserves'
import { useAuth } from '../auth'
import Uploads from '../components/Uploads'
import IngestionPanel from '../components/IngestionPanel'
import ModelRegistry from '../components/ModelRegistry'

const kb = (b) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${(b / 1e3).toFixed(0)} KB`)

export default function System() {
  const s = useApi(api.system, [])
  const { can } = useAuth()
  const [job, setJob] = useState(null)

  useEffect(() => {
    if (!job?.running) return
    const id = setInterval(() => api.system().then((d) => {
      setJob(d.job)
      if (!d.job.running) { api.clearCache(); s.reload() }
    }), 2000)
    return () => clearInterval(id)
  }, [job?.running]) // eslint-disable-line react-hooks/exhaustive-deps

  if (s.error) return <ErrorBox error={s.error} retry={s.reload} />
  if (!s.data) return <Loading />
  const { inventory: inv, runs, ingestion, models, backtest, provenance = [] } = s.data
  const byLayer = (l) => runs.filter((r) => r.layer === l)
  const realFiles = ingestion.filter((r) => r.real).length

  const LAYERS = [
    { n: 1, t: 'Data Sources', c: 'l1', body: <><b>Real:</b> Sentinel-2 L2A, Sentinel-1 RTC, Copernicus DEM, MODIS LST (Planetary Computer) · ERA5 rain & soil moisture, 16-day NWP forecast (Open-Meteo) · OSM-verified mine locations. <b>Simulated (being replaced):</b> geology, geochem, geophysics, boreholes, mine operations<div className="muted small">{inv.lake.files} raw files ({realFiles} real) · {kb(inv.lake.bytes)} · sources: {inv.lake.sources.join(', ')}</div></> },
    { n: 2, t: 'Ingestion & Processing', c: 'l2', body: <>{byLayer(2).map((r) => <span key={r.stage} className="chip">{r.stage}</span>)}
      <div className="muted small">{Object.entries(byLayer(2).find((r) => r.stage === 'tabular cleaning')?.info || {}).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`).join(' · ')}</div></> },
    { n: 3, t: 'Data Storage', c: 'l3', body: <>Data lake · Warehouse ({Object.keys(inv.warehouse.tables).length} tables, {kb(inv.warehouse.bytes)}) · Geospatial DB ({inv.geodb.tables.grid_cells?.toLocaleString('en-IN')} cells, {kb(inv.geodb.bytes)}) · Feature store ({inv.feature_store.length} versioned sets)</> },
    { n: 4, t: 'AI/ML & Analytics', c: 'l4', body: <>Reserves: {(models.reserve.leaderboard || []).map((b) => b.name.replace('_', ' ')).join(', ')} (serving <b>{models.reserve.champion?.replace('_', ' ')}</b>) · Shortfall: {(models.shortfall.leaderboard || []).filter((b) => b.version).map((b) => b.name.toUpperCase()).join(', ')} (serving <b>{models.shortfall.champion?.toUpperCase()}</b>, 30-day MAPE {models.shortfall.backtest.model_mape_30d}%) · Model registry · Greedy prescriptive optimiser</> },
    { n: 5, t: 'Application', c: 'l5', body: <>Web dashboard · Interactive GIS viewer · Reports & alerts (Email/SMS outbox) · CSV/PDF export · REST API (/api/*)</> },
    { n: 6, t: 'Users & Actions', c: 'l6', body: <>Mine planners & geologists · Operations team · Management / board · Administrator (the signed-in role controls every module and API call)</> },
  ]

  const rerun = () => api.runPipeline().then(setJob)

  return (
    <div className="page">
      <div className="page-h">
        <div>
          <h1>Data & Models</h1>
          <p className="muted">The architecture, live: what each layer holds, what the pipeline did, and how well the models validate.</p>
        </div>
        {can('pipeline_run') && <button className="btn primary" disabled={job?.running} onClick={rerun}>{job?.running ? 'Pipeline running…' : 'Re-run full pipeline'}</button>}
      </div>
      {job && (job.running || job.log?.length > 0) && (
        <Card title="Pipeline run">
          <pre className="log">{job.log.join('\n') || 'starting…'}{job.error && `\nERROR ${job.error}`}</pre>
        </Card>
      )}
      {can('users') && <UsersAccess />}
      {can('data_upload') && <Uploads onRerun={rerun} />}
      <IngestionPanel />
      <OperationsPanel canRun={can('pipeline_run')} />
      <div className="arch">
        {LAYERS.map((l) => (
          <div key={l.n} className={`arch-l ${l.c}`}>
            <div className="arch-n">{l.n}</div>
            <div><b>{l.t}</b><div className="small">{l.body}</div></div>
          </div>
        ))}
        <div className="arch-l l7"><div className="arch-n">⚙</div><div><b>Security, infrastructure & deployment</b>
          <div className="small">Role-based access enforced server-side · PBKDF2 password hashing · signed, expiring sessions · login lockout · audit log · PostgreSQL/PostGIS · independent API, worker and scheduler services · Prometheus/Grafana monitoring · versioned migrations · verified backups · TLS ingress · SQLite local fallback</div></div></div>
      </div>

      <Card title="Data provenance" sub="Every real feed in the data lake: where it came from, which scenes, which dates" pad={false}>
        <div className="table-wrap glass-table">
          <table className="tbl compact">
            <thead><tr><th>Dataset</th><th>Source</th><th>Dates</th><th className="r">Scenes</th><th>Method</th><th>Licence</th><th>Fetched</th></tr></thead>
            <tbody>{provenance.map((p) => (
              <tr key={p.name}>
                <td className="nowrap"><ProvBadge real={!!p.real} /> <span className="mono small">{p.name}</span></td>
                <td className="small">{p.source}{p.collection ? <div className="muted mono">{p.collection}</div> : null}</td>
                <td className="mono small nowrap">{p.date_range ?? '—'}</td>
                <td className="r mono">{p.scenes || '—'}</td>
                <td className="small muted">{p.method ?? '—'}</td>
                <td className="small muted">{p.licence ?? '—'}</td>
                <td className="mono small nowrap">{p.fetched_at?.replace('T', ' ') ?? '—'}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </Card>

      <ModelRegistry onChange={s.reload} />

      <div className="grid-2">
        <Card title="Reserve model card"><ModelCard m={models.reserve} /></Card>
        <BacktestCard card={models.shortfall} backtest={backtest} />
      </div>

      <div className="grid-2">
        <Card title="Ingestion log" sub="Every raw file in the lake and the channel it would arrive through" pad={false}>
          <div className="table-wrap" style={{ maxHeight: 360 }}>
            <table className="tbl compact">
              <thead><tr><th>Source</th><th>File</th><th>Origin</th><th className="r">Records</th><th>Channel</th></tr></thead>
              <tbody>{ingestion.map((r) => <tr key={r.file}><td>{r.source}</td><td className="mono small">{r.file}</td><td className="nowrap"><ProvBadge real={!!r.real} /></td><td className="r mono">{r.records.toLocaleString('en-IN')}</td><td className="small muted">{r.channel}</td></tr>)}</tbody>
            </table>
          </div>
        </Card>
        <Card title="Pipeline runs & feature store" pad={false}>
          <div className="table-wrap" style={{ maxHeight: 200 }}>
            <table className="tbl compact">
              <thead><tr><th>Layer</th><th>Stage</th><th>Details</th></tr></thead>
              <tbody>{runs.map((r, i) => <tr key={i}><td>L{r.layer}</td><td>{r.stage}</td><td className="small muted mono">{JSON.stringify(r.info).slice(0, 90)}</td></tr>)}</tbody>
            </table>
          </div>
          <div className="table-wrap">
            <table className="tbl compact">
              <thead><tr><th>Feature set</th><th>Version</th><th className="r">Rows</th><th className="r">Cols</th></tr></thead>
              <tbody>{inv.feature_store.map((f) => <tr key={f.name}><td>{f.name}<div className="muted small">{f.description}</div></td><td className="mono small">{f.version}</td><td className="r mono">{f.rows.toLocaleString('en-IN')}</td><td className="r mono">{f.columns.length}</td></tr>)}</tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}

function OperationsPanel({ canRun }) {
  const o = useApi(api.operations, [])
  const [busy, setBusy] = useState('')
  const act = (kind) => {
    setBusy(kind)
    const p = kind === 'backup' ? api.runBackup(false) : api.runRestoreDrill()
    p.then(() => { setTimeout(o.reload, 800); setBusy('') }).catch(() => setBusy(''))
  }
  if (o.error) return <ErrorBox error={o.error} retry={o.reload} />
  if (!o.data) return <Loading />
  const d = o.data
  const checks = d.readiness?.checks || d.ready?.checks || []
  const drift = d.drift?.features || d.feature_drift?.features || []
  const jobs = d.jobs?.recent || []
  return (
    <Card title="Platform operations" sub="Live readiness, data/model drift, background work and disaster recovery">
      <div className="metric-row">
        <div className="metric"><span>Readiness</span><b className={d.readiness?.ready ? 'pos' : 'neg'}>{d.readiness?.ready ? 'READY' : 'DEGRADED'}</b></div>
        <div className="metric"><span>Database</span><b>{d.database?.backend || d.backend || '—'}</b></div>
        <div className="metric"><span>Backups</span><b>{d.backups?.items?.length || 0}</b></div>
        <div className="metric"><span>Migrations pending</span><b>{d.migrations?.pending?.length || 0}</b></div>
      </div>
      <div className="grid-2">
        <div>
          <h3>Health checks</h3>
          {(Array.isArray(checks) ? checks : Object.entries(checks).map(([name, v]) => ({ name, ...(typeof v === 'object' ? v : { ok: !!v }) }))).map((v) => <div className="row-between small" key={v.name}><span>{v.name.replaceAll('_', ' ')} <span className="muted">{v.detail}</span></span><span className={`badge ${v.ok === false ? (v.required === false ? 'risk-med' : 'sev-high') : 'risk-low'}`}>{v.ok === false ? (v.required === false ? 'WARN' : 'FAIL') : 'OK'}</span></div>)}
          {drift.length > 0 && <><h3>Feature drift</h3>{drift.map((x) => <div className="row-between small" key={x.feature}><span>{x.feature}</span><span className="mono">PSI {Number(x.psi).toFixed(3)} · {x.level}</span></div>)}</>}
        </div>
        <div>
          <div className="row-between"><h3>Recovery</h3>{canRun && <span><button className="btn" disabled={!!busy} onClick={() => act('backup')}>Create backup</button> <button className="btn" disabled={!!busy || !d.backups?.items?.length} onClick={() => act('drill')}>Restore drill</button></span>}</div>
          <div className="table-wrap" style={{ maxHeight: 220 }}><table className="tbl compact"><thead><tr><th>Job</th><th>Status</th><th>Started</th></tr></thead><tbody>{jobs.slice(0, 8).map((x) => <tr key={x.id}><td>{x.kind}</td><td>{x.status}</td><td className="mono small">{x.started?.replace('T', ' ') || x.created?.replace('T', ' ')}</td></tr>)}</tbody></table></div>
          <div className="muted small">Latest drill: {d.backups?.drills?.[0]?.status || 'not run'} · Latest backup: {d.backups?.items?.[0]?.name || 'none'}</div>
        </div>
      </div>
    </Card>
  )
}

function BacktestCard({ card, backtest }) {
  const fams = (card.leaderboard || []).filter((b) => b.version).map((b) => b.name)
  const cols = fams.length ? fams : ['model']
  return (
    <Card title="Shortfall backtest" sub={`30-day error per origin and mine; every forecaster on the same rolling origins. Serving: ${card.algorithm}. Baseline: ${card.baseline}`} pad={false}>
      <div className="table-wrap" style={{ maxHeight: 640 }}>
        <table className="tbl compact">
          <thead><tr><th>Origin</th><th>Mine</th><th className="r">Actual 30d</th>{cols.map((f) => <th key={f} className="r">{f.toUpperCase()}{f === card.champion ? ' ★' : ''}</th>)}<th className="r">Naive</th></tr></thead>
          <tbody>
            {backtest.map((b, i) => {
              const errs = [...cols.map((f) => b[`${f}_ape`]), b.naive_ape]
              const lo = Math.min(...errs)
              return (
                <tr key={i}><td className="mono small">{b.origin}</td><td>{b.mine_id}</td>
                  <td className="r mono">{Math.round(b.actual_30d).toLocaleString('en-IN')}</td>
                  {errs.map((e, k) => <td key={k} className={`r mono ${e === lo ? 'pos' : ''}`}>{(e * 100).toFixed(1)}%</td>)}</tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function UsersAccess() {
  const u = useApi(api.users, [])
  if (u.error) return <ErrorBox error={u.error} />
  if (!u.data) return <Loading />
  const { users, audit, roles, permissions } = u.data
  const perms = Object.keys(permissions)
  return (
    <div className="grid-2">
      <Card title="Users & access" sub="Role → permission matrix, enforced by the API on every request" pad={false}>
        <div className="table-wrap">
          <table className="tbl compact">
            <thead><tr><th>User</th><th>Role</th><th>Last sign-in</th></tr></thead>
            <tbody>{users.map((x) => <tr key={x.username}><td><b>{x.name}</b><div className="muted small mono">@{x.username}</div></td><td>{x.role_label}</td><td className="small mono">{x.last_login?.replace('T', ' ') ?? 'never'}</td></tr>)}</tbody>
          </table>
          <table className="tbl compact matrix">
            <thead><tr><th>Permission</th>{Object.keys(roles).map((r) => <th key={r} className="c">{r}</th>)}</tr></thead>
            <tbody>{perms.map((p) => (
              <tr key={p}><td>{p.replace('_', ' ')}<div className="muted small">{permissions[p]}</div></td>
                {Object.entries(roles).map(([r, v]) => <td key={r} className="c">{v.perms.includes(p) ? <span className="yes">●</span> : <span className="no">–</span>}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
      </Card>
      <Card title="Audit log" sub="Sign-ins, failures, lockouts, sign-outs and model promotions" pad={false}>
        <div className="table-wrap" style={{ maxHeight: 560 }}>
          <table className="tbl compact">
            <thead><tr><th>Time</th><th>User</th><th>Event</th><th>IP</th></tr></thead>
            <tbody>{audit.map((a, i) => (
              <tr key={i}><td className="mono small">{a.ts.replace('T', ' ')}</td><td>{a.username}</td>
                <td><span className={`badge ${a.ok ? 'risk-low' : 'sev-high'}`}>{a.event.replace('_', ' ')}</span></td><td className="mono small">{a.ip}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
