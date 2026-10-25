import { useState } from 'react'
import { api, useApi } from '../api'
import { useAuth } from '../auth'
import { Card, ErrorBox, Loading, Sev } from '../components/ui'
import GlassSelect from '../components/GlassSelect'
import { Outbox, ReportArchive, ReportDownloads, Schedules } from '../components/ReportsPanel'
import DeliverySettings from '../components/DeliverySettings'

export default function Alerts() {
  const a = useApi(api.alerts, [])
  const { can, user } = useAuth()
  const cat = useApi(api.reportCatalog, [])
  const out = useApi(api.outbox, [])
  const [filter, setFilter] = useState('all')
  if (a.error) return <ErrorBox error={a.error} retry={a.reload} />
  if (!a.data) return <Loading />
  const list = a.data.alerts.filter((x) => filter === 'all' || x.severity === filter || x.category === filter)
  const cats = [...new Set(a.data.alerts.map((x) => x.category))]
  const ack = (id) => api.ack(id).then(a.reload)

  return (
    <div className="page">
      <div className="page-h">
        <div>
          <h1>Alerts & Reports</h1>
          <p className="muted">Early warnings addressed to <b>{user.role_label}</b>, Email/SMS delivery, scheduled PDF & Excel reports.</p>
        </div>
        <button className="btn" onClick={() => window.print()}>Print / Save as PDF</button>
      </div>
      <div className="grid-2 wide-left">
        <Card title={`Alerts (${list.length})`} right={
          <GlassSelect ariaLabel="Filter alerts" value={filter} onChange={setFilter} align="right" minMenuWidth={200}
                       options={[{ value: 'all', label: 'All alerts' },
                         ...['high', 'medium', 'info'].map((v) => ({ value: v, label: v[0].toUpperCase() + v.slice(1), group: 'Severity' })),
                         ...cats.map((c) => ({ value: c, label: c, group: 'Category' }))]} />} pad={false}>
          <ul className="alist big">
            {list.map((x) => (
              <li key={x.alert_id} className={x.status === 'acknowledged' ? 'acked' : ''}>
                <Sev s={x.severity} />
                <div className="grow">
                  <div className="row between"><b>{x.title}</b><span className="muted small mono">{x.alert_id}</span></div>
                  <div className="small">{x.message}</div>
                  <div className="small"><span className="muted">Recommended:</span> {x.recommended_action}</div>
                  <div className="muted small">{x.category} · to {x.audience.replaceAll(',', ', ')}</div>
                </div>
                {x.status === 'open' ? (can('alerts_ack') && <button className="btn sm" onClick={() => ack(x.alert_id)}>Acknowledge</button>) : <span className="muted small">ack'd</span>}
              </li>
            ))}
          </ul>
        </Card>
        <div className="stack">
          {can('reports') && cat.data && <ReportDownloads catalog={cat.data} onGenerated={cat.reload} />}
          {out.data && <Outbox messages={out.data.messages} counts={out.data.counts} />}
        </div>
      </div>
      {can('reports') && cat.data && (
        <>
          <Schedules catalog={cat.data} onChange={() => { cat.reload(); out.reload() }} />
          <ReportArchive archive={cat.data.archive} />
        </>
      )}
      {can('notify_admin') && <DeliverySettings onChange={out.reload} />}
    </div>
  )
}
