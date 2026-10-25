import { useEffect, useState } from 'react'
import { api, useApi } from '../api'
import { useAuth } from '../auth'
import { Card, ErrorBox, Loading } from './ui'

const TASKS = {
  reserve: { title: 'Reserve prospectivity', metric: 'gen_score', note: 'generalisation score = mean of spatial-block CV AUC and AUC on held-out USGS occurrences >6 km from training mines (higher is better)' },
  shortfall: { title: 'Production shortfall forecaster', metric: 'mape_30d', note: '30-day total MAPE over 6 rolling backtest origins, drivers frozen at the origin (lower is better)' },
}
const NAMES = { random_forest: 'Random forest', xgboost: 'XGBoost', unet: 'U-Net (CNN)', ensemble: 'Ensemble', gbm: 'Gradient boosting', lstm: 'LSTM', prophet: 'Prophet', seasonal_naive: 'Seasonal naive' }
const COLS = {
  reserve: [['gen_score', 'Gen. score'], ['cv_auc', 'CV AUC'], ['pr_auc', 'PR AUC'], ['occ_auc_far', 'Occ. AUC >6 km'], ['hit_rate', 'Occ. in zone', 'pct']],
  shortfall: [['mape_30d', '30-day MAPE', '%'], ['daily_mae_t', 'Daily MAE (t)'], ['rel_rmse_30d', 'Rel. RMSE']],
}
const fmt = (v, kind) => (v == null ? '—' : kind === 'pct' ? `${(v * 100).toFixed(0)}%` : kind === '%' ? `${v}%` : v)

/** Leaderboard of the latest training run (read-only; used inside model cards too). */
export function Leaderboard({ task, rows, onPromote, busy }) {
  const cols = COLS[task]
  const key = TASKS[task].metric
  const vals = rows.filter((r) => r.version).map((r) => r[key])
  const best = task === 'reserve' ? Math.max(...vals) : Math.min(...vals)
  return (
    <div className="table-wrap">
      <table className="tbl compact leaderboard">
        <thead><tr><th>Model</th>{cols.map(([k, l]) => <th key={k} className="r">{l}</th>)}{onPromote && <th />}</tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.name} className={r.champion ? 'is-champ' : r.version ? '' : 'is-baseline'}>
            <td>
              <b>{NAMES[r.name] ?? r.name}</b>{r.champion && <span className="badge champ">serving</span>}
              {r.beats_naive === false && <span className="badge sev-medium">trails baseline (30-day)</span>}
              <div className="muted small">{r.algorithm}</div>
            </td>
            {cols.map(([k, , kind]) => <td key={k} className={`r mono ${k === key && r[k] === best ? 'pos' : ''}`}>{fmt(r[k], kind)}</td>)}
            {onPromote && <td className="r">{r.version && !r.champion &&
              <button className="btn sm" disabled={busy} onClick={() => onPromote(r.version)}>Promote</button>}</td>}
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

export default function ModelRegistry({ onChange }) {
  const m = useApi(api.models, [])
  const { can } = useAuth()
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [open, setOpen] = useState({})
  const admin = can('models')

  // A reserve promotion re-maps the zones in the background: poll until it finishes.
  useEffect(() => {
    if (!m.data?.job?.running) return
    const id = setInterval(() => api.models().then((d) => {
      if (!d.job.running) { api.clearCache(); m.reload(); onChange?.(); setBusy(false); setMsg(d.job.error ? `Re-map failed: ${d.job.error}` : 'Reserve zones re-mapped with the promoted model.') }
    }), 2500)
    return () => clearInterval(id)
  }, [m.data?.job?.running]) // eslint-disable-line react-hooks/exhaustive-deps

  if (m.error) return <ErrorBox error={m.error} retry={m.reload} />
  if (!m.data) return <Loading />
  const { registry, leaderboards } = m.data

  const act = (p, label) => {
    setBusy(true); setMsg(null)
    p.then((r) => {
      setMsg(r.job ? `${label}: re-mapping reserve zones…` : `${label}: done.`)
      if (!r.job) { setBusy(false); onChange?.() }
      api.clearCache(); m.reload()
    }).catch((e) => { setMsg(`${label} failed: ${e.message}`); setBusy(false) })
  }

  return (
    <Card title="Model registry" sub="Every trained candidate is versioned with its metrics and data fingerprint. The champion serves the dashboard; administrators can promote any version or roll back.">
      {msg && <p className="small reg-msg">{msg}</p>}
      <div className="gstack">
        {Object.entries(TASKS).map(([task, t]) => {
          const r = registry[task]
          if (!r) return null
          const champ = r.versions.find((v) => v.version === r.champion)
          return (
            <section key={task} className="gpane">
              <div className="reg-h">
                <div>
                  <h4>{t.title}</h4>
                  <p className="muted small">Serving <b>{NAMES[champ?.algo] ?? champ?.algo}</b> <span className="mono">{r.champion}</span>.
                    {r.pinned ? <> Pinned by an administrator: retraining keeps this family.</> : <> Auto: each training run promotes the leaderboard winner.</>}</p>
                </div>
                {admin && <div className="reg-actions">
                  {r.pinned && <button className="btn sm" disabled={busy} onClick={() => act(api.unpinModel(task), 'Unpin')}>Unpin</button>}
                  {r.previous && <button className="btn sm" disabled={busy} onClick={() => act(api.rollbackModel(task), 'Rollback')}>Roll back</button>}
                </div>}
              </div>
              <Leaderboard task={task} rows={leaderboards[task] || []} busy={busy}
                onPromote={admin ? (v) => act(api.promoteModel(task, v), `Promote ${v}`) : null} />
              <p className="muted small">{t.note}</p>
              <button className="linkish small" onClick={() => setOpen((o) => ({ ...o, [task]: !o[task] }))}>
                {open[task] ? 'Hide' : 'Show'} version history ({r.versions.length})
              </button>
              {open[task] && (
                <div className="table-wrap" style={{ maxHeight: 260 }}>
                  <table className="tbl compact">
                    <thead><tr><th>Version</th><th>Created</th><th>Data</th><th className="r">{t.metric}</th><th>Status</th>{admin && <th />}</tr></thead>
                    <tbody>{r.versions.map((v) => (
                      <tr key={v.version}>
                        <td className="mono small">{v.version}</td>
                        <td className="mono small nowrap">{v.created.replace('T', ' ')}</td>
                        <td className="mono small muted">{v.data_version}</td>
                        <td className="r mono">{v.metrics?.[t.metric] ?? '—'}</td>
                        <td><span className={`badge ${v.status === 'champion' ? 'champ' : ''}`}>{v.status}</span></td>
                        {admin && <td className="r">{v.status !== 'champion' &&
                          <button className="btn sm" disabled={busy} onClick={() => act(api.promoteModel(task, v.version), `Promote ${v.version}`)}>Promote</button>}</td>}
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
            </section>
          )
        })}
      </div>
    </Card>
  )
}
