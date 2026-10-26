import { useCallback, useEffect, useRef, useState } from 'react'

// Session token (set by the auth context). Every request carries it; a 401 signs the user out.
let token = null
export const setToken = (t) => { token = t }
let dataMode = (() => { try { return sessionStorage.getItem('kd-data-mode') || 'live' } catch { return 'live' } })()
export const setDataMode = (mode) => {
  dataMode = mode === 'simulation' ? 'simulation' : 'live'
  try { sessionStorage.setItem('kd-data-mode', dataMode) } catch { /* private mode */ }
}
export const getDataMode = () => dataMode

// Empty in local/same-origin deployments. Vercel sets VITE_API_BASE to the public HTTPS URL of
// the container API (for example https://api.khanij.example.gov.in) at build time.
const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')
const endpoint = (url) => API_BASE && url.startsWith('/api/') ? `${API_BASE}${url}` : url

async function request(url, opts = {}) {
  const r = await fetch(endpoint(url), { ...opts, headers: { ...(opts.headers || {}), 'X-KD-Data-Mode': dataMode, ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  if (r.status === 401 && token) window.dispatchEvent(new Event('kd-unauthorized'))
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail || detail } catch { /* not json */ }
    const e = new Error(detail)
    e.status = r.status
    throw e
  }
  return r
}
const j = async (url, opts) => (await request(url, opts)).json()
const post = (body) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) })

/** Authenticated file download (an <a href> can't send the Authorization header). */
export async function download(url, filename) {
  const blob = await (await request(url)).blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}

const layerCache = new Map()

export const api = {
  login: (username, password) => j('/api/auth/login', post({ username, password })),
  register: (name, username, password) => j('/api/auth/register', post({ name, username, password })),
  me: () => j('/api/auth/me'),
  health: () => j('/api/health'),
  logout: () => j('/api/auth/logout', post()),
  demoAccounts: () => j('/api/auth/demo'),
  users: () => j('/api/auth/users'),
  uploads: () => j('/api/uploads'),
  uploadFile: (kind, file) => {
    const fd = new FormData()
    fd.append('kind', kind)
    fd.append('file', file)
    return j('/api/uploads', { method: 'POST', body: fd })
  },
  deactivateUpload: (id) => j(`/api/uploads/${id}/deactivate`, post()),
  ingestStatus: () => j('/api/ingest/status'),
  equipmentLive: () => j('/api/equipment/live'),
  equipmentStatus: (mine) => j(`/api/equipment/status?mine_id=${mine}`),
  equipmentFeed: (action) => j('/api/equipment/feed', post({ action })),
  createDevice: (body) => j('/api/ingest/devices', post(body)),
  revokeDevice: (id) => j(`/api/ingest/devices/${id}/revoke`, post()),
  overview: () => j('/api/overview'),
  gridMeta: () => j('/api/grid/meta'),
  layer: (name) => {
    if (!layerCache.has(name)) layerCache.set(name, j(`/api/grid/layer/${name}`).catch((e) => { layerCache.delete(name); throw e }))
    return layerCache.get(name)
  },
  cell: (lat, lon) => j(`/api/grid/cell?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`),
  vector: (name) => j(`/api/vector/${name}`),
  reserves: () => j('/api/reserves'),
  production: (id) => j(`/api/production/${id}`),
  forecast: () => j('/api/forecast'),
  whatif: (body) => j('/api/whatif', post(body)),
  optimize: (body) => j('/api/optimize', post(body)),
  actions: () => j('/api/actions'),
  alerts: () => j('/api/alerts'),
  ack: (id) => j(`/api/alerts/${id}/ack`, post()),
  system: () => j('/api/system'),
  operations: () => j('/api/ops'),
  runBackup: (includeLake = false) => j('/api/ops/backup', post({ include_lake: includeLake })),
  runRestoreDrill: (name = null) => j('/api/ops/drill', post({ name })),
  runPipeline: () => j('/api/pipeline/run', post()),
  models: () => j('/api/models'),
  terrainMeta: () => j('/api/terrain/meta'),
  terrainElevation: () => request('/api/terrain/elevation.bin').then((r) => r.arrayBuffer()),
  terrainTexture: (kind) => request(`/api/terrain/texture/${kind}.png`).then((r) => r.blob()).then((b) => URL.createObjectURL(b)),
  reportCatalog: () => j('/api/reports/catalog'),
  schedules: () => j('/api/schedules'),
  addSchedule: (body) => j('/api/schedules', post(body)),
  updateSchedule: (id, body) => j(`/api/schedules/${id}`, { ...post(body), method: 'PATCH' }),
  deleteSchedule: (id) => j(`/api/schedules/${id}`, { method: 'DELETE' }),
  runSchedule: (id) => j(`/api/schedules/${id}/run`, post()),
  notifySettings: () => j('/api/notify/settings'),
  saveNotifySettings: (body) => j('/api/notify/settings', { ...post(body), method: 'PUT' }),
  addRecipient: (body) => j('/api/notify/recipients', post(body)),
  setRecipientActive: (id, active) => j(`/api/notify/recipients/${id}/active`, post({ active })),
  deleteRecipient: (id) => j(`/api/notify/recipients/${id}`, { method: 'DELETE' }),
  notifyTest: (body) => j('/api/notify/test', post(body)),
  outbox: () => j('/api/notify/outbox'),
  promoteModel: (task, version) => j(`/api/models/${task}/${encodeURIComponent(version)}/promote`, post()),
  rollbackModel: (task) => j(`/api/models/${task}/rollback`, post()),
  unpinModel: (task) => j(`/api/models/${task}/unpin`, post()),
  clearCache: () => layerCache.clear(),
}

/** Fetch on mount / when deps change. Returns { data, error, loading, reload }. */
export function useApi(fn, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const seq = useRef(0)
  const load = useCallback(() => {
    const id = ++seq.current
    setState((s) => ({ ...s, loading: true, error: null }))
    fn()
      .then((data) => id === seq.current && setState({ data, error: null, loading: false }))
      .catch((error) => id === seq.current && setState({ data: null, error, loading: false }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(load, [load])
  return { ...state, reload: load }
}
