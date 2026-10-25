import { useEffect, useState } from 'react'
import { api, useApi } from '../api'
import { Card, ErrorBox, Loading } from './ui'
import GlassSelect from './GlassSelect'

const SEV = [{ value: 'high', label: 'High only' }, { value: 'medium', label: 'Medium and high' }, { value: 'info', label: 'Everything' }]

/** Admin: SMTP relay, Twilio-compatible SMS gateway, recipients, test send. */
export default function DeliverySettings({ onChange }) {
  const d = useApi(api.notifySettings, [])
  const [f, setF] = useState(null)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [rcp, setRcp] = useState({ name: '', channel: 'email', address: '', audiences: ['management'], min_severity: 'high' })
  const [test, setTest] = useState({ channel: 'email', address: '' })

  useEffect(() => { if (d.data) setF({ ...d.data.settings, smtp_password: '', sms_auth_token: '' }) }, [d.data])
  if (d.error) return <ErrorBox error={d.error} retry={d.reload} />
  if (!d.data || !f) return <Loading />
  const s = d.data.settings
  const set = (k) => (e) => setF((o) => ({ ...o, [k]: e?.target ? (e.target.type === 'checkbox' ? (e.target.checked ? '1' : '0') : e.target.value) : e }))
  const run = (p, label) => {
    setBusy(true); setMsg(null)
    return p.then((r) => { setMsg(typeof r === 'string' ? r : `${label}: done`); d.reload(); onChange?.(); return r })
      .catch((e) => { setMsg(`${label} failed: ${e.message}`) }).finally(() => setBusy(false))
  }
  const save = () => {
    const body = Object.fromEntries(Object.entries(f).filter(([k]) => !k.endsWith('_set') && !k.endsWith('_ready') && !k.endsWith('_from_env')))
    run(api.saveNotifySettings(body), 'Settings saved')
  }
  const sendTest = () => run(api.notifyTest(test).then((r) => `Test ${test.channel} → ${r.message.status}${r.message.last_error ? `: ${r.message.last_error}` : ''}`), 'Test')

  return (
    <Card title="Delivery settings" sub="Real delivery: SMTP for email, any Twilio-compatible REST gateway for SMS. Unconfigured channels hold messages in the outbox.">
      {msg && <p className="small reg-msg">{msg}</p>}
      <div className="grid-2 tight">
        <section className="gpane">
          <div className="row between"><h4>Email (SMTP)</h4><span className={`badge ${s.email_ready ? 'risk-low' : 'sev-medium'}`}>{s.email_ready ? 'ready' : 'not configured'}</span></div>
          <label className="check small"><input type="checkbox" checked={f.email_enabled === '1'} onChange={set('email_enabled')} /> Enable email delivery</label>
          <div className="form-grid">
            <label>SMTP host<input className="input" value={f.smtp_host} onChange={set('smtp_host')} placeholder="smtp.office365.com" /></label>
            <label>Port<input className="input" value={f.smtp_port} onChange={set('smtp_port')} /></label>
            <label>Security<GlassSelect ariaLabel="SMTP security" value={f.smtp_security} onChange={set('smtp_security')}
              options={[{ value: 'starttls', label: 'STARTTLS' }, { value: 'ssl', label: 'SSL/TLS' }, { value: 'none', label: 'None (local relay)' }]} /></label>
            <label>Username<input className="input" value={f.smtp_user} onChange={set('smtp_user')} autoComplete="off" /></label>
            <label>Password<input className="input" type="password" value={f.smtp_password} onChange={set('smtp_password')} autoComplete="new-password"
              placeholder={s.smtp_password_from_env ? 'set by KD_SMTP_PASSWORD' : s.smtp_password_set ? '•••••• (stored; leave blank to keep)' : ''} /></label>
            <label>From<input className="input" value={f.smtp_from} onChange={set('smtp_from')} /></label>
          </div>
        </section>
        <section className="gpane">
          <div className="row between"><h4>SMS (Twilio-compatible)</h4><span className={`badge ${s.sms_ready ? 'risk-low' : 'sev-medium'}`}>{s.sms_ready ? 'ready' : 'not configured'}</span></div>
          <label className="check small"><input type="checkbox" checked={f.sms_enabled === '1'} onChange={set('sms_enabled')} /> Enable SMS delivery</label>
          <div className="form-grid">
            <label className="span2">Gateway base URL<input className="input" value={f.sms_base_url} onChange={set('sms_base_url')} /></label>
            <label>Account SID<input className="input" value={f.sms_account_sid} onChange={set('sms_account_sid')} autoComplete="off" /></label>
            <label>Auth token<input className="input" type="password" value={f.sms_auth_token} onChange={set('sms_auth_token')} autoComplete="new-password"
              placeholder={s.sms_auth_token_from_env ? 'set by KD_SMS_TOKEN' : s.sms_auth_token_set ? '•••••• (stored; leave blank to keep)' : ''} /></label>
            <label>From number<input className="input" value={f.sms_from} onChange={set('sms_from')} placeholder="+1…" /></label>
          </div>
          <p className="muted small">Posts to <span className="mono">{'{base}'}/2010-04-01/Accounts/{'{SID}'}/Messages.json</span> with basic auth (Twilio API shape).</p>
        </section>
      </div>
      <div className="row gap6 wrap" style={{ margin: '10px 0 14px' }}>
        <button className="btn sm primary" disabled={busy} onClick={save}>Save settings</button>
        <span className="muted small" style={{ marginLeft: 8 }}>Test:</span>
        <GlassSelect ariaLabel="Test channel" value={test.channel} onChange={(v) => setTest((t) => ({ ...t, channel: v }))} options={[{ value: 'email', label: 'Email' }, { value: 'sms', label: 'SMS' }]} />
        <input className="input" style={{ maxWidth: 240 }} value={test.address} onChange={(e) => setTest((t) => ({ ...t, address: e.target.value }))}
               placeholder={test.channel === 'email' ? 'you@example.org' : '+9198…'} />
        <button className="btn sm" disabled={busy || !test.address} onClick={sendTest}>Send test</button>
      </div>

      <h4 className="mini-h">Recipients</h4>
      <div className="table-wrap">
        <table className="tbl compact">
          <thead><tr><th>Name</th><th>Channel</th><th>Address</th><th>Receives</th><th>Alerts</th><th /></tr></thead>
          <tbody>
            {d.data.recipients.length === 0 && <tr><td colSpan={6} className="muted small">No recipients yet: alerts and reports are archived but not sent to anyone.</td></tr>}
            {d.data.recipients.map((r) => (
              <tr key={r.id} className={r.active ? '' : 'is-baseline'}>
                <td><b>{r.name}</b></td><td className="small">{r.channel === 'sms' ? 'SMS' : 'Email'}</td>
                <td className="mono small">{r.address}</td><td className="small">{r.audiences.replaceAll(',', ', ')}</td>
                <td className="small">{SEV.find((x) => x.value === r.min_severity)?.label}</td>
                <td className="r nowrap">
                  <button className="btn sm" disabled={busy} onClick={() => run(api.setRecipientActive(r.id, !r.active), r.active ? 'Paused' : 'Resumed')}>{r.active ? 'Pause' : 'Resume'}</button>{' '}
                  <button className="btn sm danger" disabled={busy} onClick={() => run(api.deleteRecipient(r.id), 'Removed')}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="sched-form gpane">
        <div className="form-grid">
          <label>Name<input className="input" value={rcp.name} onChange={(e) => setRcp({ ...rcp, name: e.target.value })} placeholder="Mine manager, Dongri" /></label>
          <label>Channel<GlassSelect ariaLabel="Channel" value={rcp.channel} onChange={(v) => setRcp({ ...rcp, channel: v })} options={[{ value: 'email', label: 'Email' }, { value: 'sms', label: 'SMS' }]} /></label>
          <label>{rcp.channel === 'sms' ? 'Mobile (E.164)' : 'Email'}<input className="input" value={rcp.address} onChange={(e) => setRcp({ ...rcp, address: e.target.value })} placeholder={rcp.channel === 'sms' ? '+919800000000' : 'name@moil.nic.in'} /></label>
          <label>Alerts<GlassSelect ariaLabel="Minimum severity" value={rcp.min_severity} onChange={(v) => setRcp({ ...rcp, min_severity: v })} options={SEV} /></label>
        </div>
        <div className="row gap12 wrap small"><span className="muted">Receives alerts & reports for</span>
          {d.data.audiences.map((a) => <label key={a} className="check"><input type="checkbox" checked={rcp.audiences.includes(a)}
            onChange={() => setRcp({ ...rcp, audiences: rcp.audiences.includes(a) ? rcp.audiences.filter((x) => x !== a) : [...rcp.audiences, a] })} /> {a}</label>)}
        </div>
        <div><button className="btn sm primary" disabled={busy || !rcp.name || !rcp.address || !rcp.audiences.length}
          onClick={() => run(api.addRecipient(rcp), 'Recipient added').then((r) => r && setRcp({ ...rcp, name: '', address: '' }))}>Add recipient</button></div>
      </div>
    </Card>
  )
}
