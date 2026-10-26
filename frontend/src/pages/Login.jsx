import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'

const ROLE_ICON = {
  admin: 'M12 2 4 5v6c0 5 3.4 9.7 8 11 4.6-1.3 8-6 8-11V5l-8-3z',
  mgmt: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z',
  planner: 'M15 5.1 9 3 3 5v16l6-2.1 6 2.1 6-2V3l-6 2.1zM15 19l-6-2.1V5l6 2.1V19z',
  ops: 'M3 3v18h18v-2H5V3H3zm4 12h2v3H7v-3zm4-5h2v8h-2v-8zm4-3h2v11h-2V7z',
}

export default function Login({ theme, toggleTheme }) {
  const { login, register } = useAuth()
  const [mode, setMode] = useState('login')
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [show, setShow] = useState(false)
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [demo, setDemo] = useState([])
  const [hl, setHl] = useState(null)
  const userRef = useRef(null)

  useEffect(() => {
    userRef.current?.focus()
    api.demoAccounts().then(setDemo).catch(() => setDemo([]))
    api.health().then((h) => setHl(h.headline ?? null)).catch(() => {})
  }, [])

  const submit = async (e, u = username, p = password) => {
    e?.preventDefault()
    if (!u || !p) { setErr('Enter your username and password.'); return }
    setBusy(true); setErr(null)
    try {
      const user = await login(u, p, remember)
      window.location.hash = user.home
    } catch (ex) {
      setErr(ex.status === 429 ? ex.message : ex.status === 401 ? 'Invalid username or password.' : `Couldn't reach the server (${ex.message}).`)
    } finally { setBusy(false) }
  }

  const signUp = async (e) => {
    e.preventDefault()
    if (!name.trim() || !username || !password) { setErr('Complete all fields.'); return }
    setBusy(true); setErr(null)
    try {
      const user = await register(name, username, password, remember)
      window.location.hash = user.home
    } catch (ex) {
      setErr(ex.status === 409 ? 'That username is already taken.' : ex.message || 'Account creation failed.')
    } finally { setBusy(false) }
  }

  return (
    <div className="login">
      <div className="liquid-bg" aria-hidden="true"><i /><i /><i /></div>
      <button className="icon-btn login-theme" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
        {theme === 'dark' ? '☀' : '☾'}
      </button>

      <section className="login-hero">
        <div className="brand big">
          <img src="/favicon.svg" alt="" width="44" height="44" />
          <div>
            <div className="brand-n">KhanijDrishti</div>
            <div className="brand-s">Manganese intelligence platform</div>
          </div>
        </div>
        <h1>From satellite pixel to<br /><span className="grad-text">month-end target.</span></h1>
        <p className="lead">AI/ML and space technology to find the next manganese reserve and see production shortfalls before they happen, with corrective actions ready.</p>
        <ul className="hero-pills">
          <li className="liquid"><b>{hl ? hl.zones : '—'}</b><span>probable reserve zones{hl ? ` (${hl.new_zones} greenfield)` : ''}</span></li>
          <li className="liquid"><b>{hl?.occ_auc ?? '—'}</b><span>AUC on held-out USGS occurrences</span></li>
          <li className="liquid"><b>{hl ? `${hl.real_feeds}/${hl.feeds}` : '—'}</b><span>data feeds from real sources</span></li>
        </ul>
        <div className="hero-layers">
          {['Sentinel-1/2', 'Copernicus DEM', 'MODIS LST', 'EMAG2 magnetics', 'Macrostrat geology', 'USGS MRDS', 'ERA5 weather', 'Mine telemetry API'].map((x) => <span key={x}>{x}</span>)}
        </div>
      </section>

      <section className="login-card liquid" aria-labelledby="login-title">
        <div className="auth-tabs" role="tablist" aria-label="Account access">
          <button type="button" role="tab" aria-selected={mode === 'login'} onClick={() => { setMode('login'); setErr(null) }}>Sign in</button>
          <button type="button" role="tab" aria-selected={mode === 'register'} onClick={() => { setMode('register'); setErr(null) }}>Create account</button>
        </div>
        <h2 id="login-title">{mode === 'login' ? 'Sign in' : 'Create account'}</h2>
        <p className="muted small">{mode === 'login' ? "Access is role-based. You'll see the modules your role is cleared for." : 'New accounts start with safe, read-only Viewer access.'}</p>
        <form onSubmit={mode === 'login' ? submit : signUp} noValidate>
          {mode === 'register' && <label className="field"><span>Full name</span><input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></label>}
          <label className="field">
            <span>Username</span>
            <input ref={userRef} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" spellCheck="false" />
          </label>
          <label className="field">
            <span>Password</span>
            <div className="pw">
              <input type={show ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
              <button type="button" className="pw-t" onClick={() => setShow(!show)} aria-label={show ? 'Hide password' : 'Show password'}>{show ? 'Hide' : 'Show'}</button>
            </div>
          </label>
          <label className="chk"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Keep me signed in on this device</label>
          {err && <div className="login-err" role="alert">{err}</div>}
          {mode === 'register' && <div className="password-hint">At least 10 characters with a letter and number.</div>}
          <button className="btn primary block" disabled={busy}>{busy ? (mode === 'login' ? 'Signing in…' : 'Creating…') : (mode === 'login' ? 'Sign in' : 'Create viewer account')}</button>
        </form>

        {demo.length > 0 && (
          <div className="demo">
            <div className="demo-h"><span>Demo accounts</span><em>hackathon build</em></div>
            <div className="demo-grid">
              {demo.map((d) => (
                <button key={d.username} type="button" className="demo-acc liquid" disabled={busy}
                        onClick={() => { setUsername(d.username); setPassword(d.password); submit(null, d.username, d.password) }}>
                  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d={ROLE_ICON[d.role]} fill="currentColor" /></svg>
                  <span><b>{d.role_label}</b><small>{d.focus}</small></span>
                </button>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  )
}
