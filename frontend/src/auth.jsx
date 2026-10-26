import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, setToken } from './api'

const AuthCtx = createContext(null)
export const useAuth = () => useContext(AuthCtx)

const KEY = 'kd-session'
function readStored() {
  try {
    const raw = localStorage.getItem(KEY) || sessionStorage.getItem(KEY)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

export function AuthProvider({ children }) {
  const [session, setSession] = useState(() => {
    const s = readStored()
    if (s && s.expires > Date.now()) { setToken(s.token); return s }
    return null
  })
  const [checking, setChecking] = useState(!!session)

  const clear = useCallback(() => {
    setToken(null)
    try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY) } catch { /* private mode */ }
    setSession(null)
  }, [])

  // Re-validate a stored session with the server (role may have changed, token may be revoked).
  useEffect(() => {
    if (!session) return
    api.me().then((user) => setSession((s) => s && { ...s, user })).catch(clear).finally(() => setChecking(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onUnauth = () => clear()
    window.addEventListener('kd-unauthorized', onUnauth)
    return () => window.removeEventListener('kd-unauthorized', onUnauth)
  }, [clear])

  const login = async (username, password, remember) => {
    const r = await api.login(username, password)
    const s = { token: r.token, user: r.user, expires: Date.now() + r.expires_in * 1000 }
    setToken(r.token)
    try { (remember ? localStorage : sessionStorage).setItem(KEY, JSON.stringify(s)) } catch { /* private mode */ }
    setSession(s)
    return r.user
  }
  const register = async (name, username, password, remember) => {
    const r = await api.register(name, username, password)
    const s = { token: r.token, user: r.user, expires: Date.now() + r.expires_in * 1000 }
    setToken(r.token)
    try { (remember ? localStorage : sessionStorage).setItem(KEY, JSON.stringify(s)) } catch { /* private mode */ }
    setSession(s)
    return r.user
  }
  const logout = async () => {
    try { await api.logout() } catch { /* already invalid */ }
    clear()
    // full reload drops every in-memory cache (map layers, vectors) from the previous session
    window.location.hash = '#/'
    window.location.reload()
  }
  const can = (perm) => !!session?.user.perms.includes(perm)

  return <AuthCtx.Provider value={{ user: session?.user, checking, login, register, logout, can }}>{children}</AuthCtx.Provider>
}
