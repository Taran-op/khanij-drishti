import { useEffect, useRef, useState } from 'react'
import { HashRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import Overview from './pages/Overview'
import Reserves from './pages/Reserves'
import Production from './pages/Production'
import Optimize from './pages/Optimize'
import Alerts from './pages/Alerts'
import System from './pages/System'
import Login from './pages/Login'
import { api, getDataMode, setDataMode, useApi } from './api'
import { AuthProvider, useAuth } from './auth'
import { Card, Loading } from './components/ui'

// Theme: an explicit choice is remembered; until then we follow the OS setting live.
const darkQuery = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: dark)') : null
function useTheme() {
  const [choice, setChoice] = useState(() => { try { return localStorage.getItem('kd-theme') } catch { return null } })
  const [systemDark, setSystemDark] = useState(() => !!darkQuery?.matches)
  useEffect(() => {
    if (!darkQuery) return
    const on = (e) => setSystemDark(e.matches)
    darkQuery.addEventListener('change', on)
    return () => darkQuery.removeEventListener('change', on)
  }, [])
  const theme = choice || (systemDark ? 'dark' : 'light')
  useEffect(() => { document.documentElement.dataset.theme = theme }, [theme])
  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    setChoice(next)
    try { localStorage.setItem('kd-theme', next) } catch { /* private mode */ }
  }
  return [theme, toggle]
}

const SUN = 'M12 4V2m0 20v-2m8-8h2M2 12h2m13.66-5.66 1.41-1.41M4.93 19.07l1.41-1.41m0-11.32L4.93 4.93m14.14 14.14-1.41-1.41M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z'
const MOON = 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'

const I = {
  overview: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z',
  map: 'M15 5.1 9 3 3 5v16l6-2.1 6 2.1 6-2V3l-6 2.1zM15 19l-6-2.1V5l6 2.1V19z',
  chart: 'M3 3v18h18v-2H5V3H3zm4 12h2v3H7v-3zm4-5h2v8h-2v-8zm4-3h2v11h-2V7z',
  bulb: 'M9 21h6v-1H9v1zm3-19a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z',
  bell: 'M12 22a2 2 0 0 0 2-2h-4a2 2 0 0 0 2 2zm6-6V11a6 6 0 0 0-5-5.9V4a1 1 0 0 0-2 0v1.1A6 6 0 0 0 6 11v5l-2 2v1h16v-1l-2-2z',
  stack: 'M12 2 2 7l10 5 10-5-10-5zm-10 10 10 5 10-5M2 17l10 5 10-5',
}
const Icon = ({ d, stroke }) => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
    {stroke ? <path d={d} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" /> : <path d={d} fill="currentColor" />}
  </svg>
)

// Each page names the permission it needs; nav and routes are filtered by the signed-in role.
const PAGES = [
  { to: '/', label: 'Command Center', short: 'Overview', icon: I.overview, end: true, perm: 'overview', el: <Overview /> },
  { to: '/reserves', label: 'Reserve Explorer', short: 'Reserves', icon: I.map, perm: 'reserves', el: <Reserves /> },
  { to: '/production', label: 'Production & Shortfall', short: 'Production', icon: I.chart, perm: 'production', el: <Production /> },
  { to: '/optimize', label: 'Optimization', short: 'Optimize', icon: I.bulb, perm: 'optimize', el: <Optimize /> },
  { to: '/alerts', label: 'Alerts & Reports', short: 'Alerts', icon: I.bell, perm: 'alerts', el: <Alerts /> },
  { to: '/system', label: 'Data & Models', short: 'Data & Models', icon: I.stack, stroke: true, perm: 'system', el: <System /> },
]

const initials = (name) => name.replace(/\(.*\)/, '').split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('')

function UserMenu() {
  const { user, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const off = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const esc = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('pointerdown', off)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('pointerdown', off); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div className="user" ref={ref}>
      <button className="user-btn" onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open}>
        <span className={`avatar role-${user.role}`}>{initials(user.name)}</span>
        <span className="user-n hide-sm"><b>{user.name.replace(/\s*\(.*\)/, '')}</b><small>{user.role_label}</small></span>
      </button>
      {open && (
        <div className="user-menu" role="menu">
          <div className="um-h">
            <span className={`avatar lg role-${user.role}`}>{initials(user.name)}</span>
            <div><b>{user.name}</b><div className="muted small">@{user.username}</div></div>
          </div>
          <div className="um-role"><span className="badge sev-info">{user.role_label}</span><div className="muted small">{user.focus}</div></div>
          <div className="um-perms">
            {user.perms.map((p) => <span key={p} className="chip">{p.replace('_', ' ')}</span>)}
          </div>
          {user.last_login && <div className="muted small">Previous sign-in: {user.last_login.replace('T', ' ')}</div>}
          <button className="btn block" role="menuitem" onClick={logout}>Sign out</button>
        </div>
      )}
    </div>
  )
}

function Shell({ children, theme, toggleTheme }) {
  const { can } = useAuth()
  const health = useApi(api.health, [])
  const alerts = useApi(() => (can('alerts') ? api.alerts() : Promise.resolve(null)), [])
  const high = alerts.data?.alerts.filter((a) => a.severity === 'high' && a.status === 'open').length
  const [open, setOpen] = useState(false)
  const [mode] = useState(getDataMode)
  const toggleMode = () => {
    setDataMode(mode === 'simulation' ? 'live' : 'simulation')
    window.location.reload()
  }
  return (
    <div className="shell">
      <div className="liquid-bg" aria-hidden="true"><i /><i /><i /></div>
      <header className="topnav glass">
        <div className="brand">
          <img src="/favicon.svg" alt="" width="30" height="30" />
          <div>
            <div className="brand-n">KhanijSetu</div>
            <div className="brand-s">Manganese intelligence</div>
          </div>
        </div>
        <nav className={open ? 'open' : ''}>
          {PAGES.filter((p) => can(p.perm)).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} onClick={() => setOpen(false)} title={n.label}>
              <Icon d={n.icon} stroke={n.stroke} />
              <span>{n.short}</span>
              {n.to === '/alerts' && high > 0 && <em className="pill">{high}</em>}
            </NavLink>
          ))}
        </nav>
        <div className="top-r">
          <button className={`mode-switch ${mode === 'simulation' ? 'demo' : 'live'}`} onClick={toggleMode}
                  aria-label={`Data mode: ${mode === 'simulation' ? 'simulation demo' : 'verified live'}. Click to switch.`}
                  title={mode === 'simulation' ? 'Demo data is synthetic and not MOIL live data. Click for verified live mode.' : 'Verified data only. Click for the screening-round simulation.'}>
            <span className="mode-label">Data</span><span className="mode-value"><span className="dot" />{mode === 'simulation' ? 'Demo' : 'Live'}</span>
          </button>
          <span className="asof" title="Balaghat · Nagpur · Bhandara belt · 6 producing mines · 12,000 grid cells">
            <span className="dot" data-ok={health.data?.ok ? '1' : '0'} />
            <span className="hide-sm">Data as of </span><b>{health.data?.as_of ?? '…'}</b>
          </span>
          <button className="icon-btn" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
                  title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path d={theme === 'dark' ? SUN : MOON} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <UserMenu />
          <button className="hamb" onClick={() => setOpen(!open)} aria-label="Menu" aria-expanded={open}>☰</button>
        </div>
      </header>
      <main className={`content ${mode === 'simulation' ? 'simulation-mode' : ''}`}>{children}</main>
    </div>
  )
}

function Restricted({ page }) {
  const { user } = useAuth()
  return (
    <div className="page">
      <Card title="Access restricted">
        <p>The <b>{page.label}</b> module isn't available to the <b>{user.role_label}</b> role.</p>
        <p className="muted small">Ask an administrator if you need access. Your role can use: {PAGES.filter((p) => user.perms.includes(p.perm)).map((p) => p.label).join(', ')}.</p>
        <a className="btn primary" href={`#${user.home}`}>Go to my home page</a>
      </Card>
    </div>
  )
}

function Gate({ theme, toggleTheme }) {
  const { user, checking, can } = useAuth()
  if (checking) return <div className="boot"><div className="liquid-bg" aria-hidden="true"><i /><i /><i /></div><Loading label="Restoring your session" /></div>
  if (!user) return <Login theme={theme} toggleTheme={toggleTheme} />
  const guard = (p, el = p.el) => (can(p.perm) ? el : <Restricted page={p} />)
  return (
    // key: remount everything when the user changes so no data from the previous session lingers
    <HashRouter key={user.username}>
      <Shell theme={theme} toggleTheme={toggleTheme}>
        <Routes>
          {PAGES.map((p) => <Route key={p.to} path={p.to} element={p.to === '/' && !can('overview') ? <Navigate to={user.home} /> : guard(p)} />)}
          <Route path="/production/:mineId" element={guard(PAGES[2])} />
          <Route path="*" element={<Navigate to={user.home} />} />
        </Routes>
      </Shell>
    </HashRouter>
  )
}

export default function App() {
  const [theme, toggleTheme] = useTheme()
  return (
    <AuthProvider>
      <Gate theme={theme} toggleTheme={toggleTheme} />
    </AuthProvider>
  )
}
