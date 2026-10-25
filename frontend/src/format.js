const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })
const nf1 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 })

export const t = (v) => (v == null ? '—' : `${nf0.format(v)} t`)
export const kt = (v) => (v == null ? '—' : `${nf1.format(v / 1000)} kt`)
export const n0 = (v) => (v == null ? '—' : nf0.format(v))
export const n1 = (v) => (v == null ? '—' : nf1.format(v))
export const pct = (v, d = 0) => (v == null ? '—' : d === 0 && v >= 0.995 ? '>99%' : `${(v * 100).toFixed(d)}%`)
export const signed = (v, f = n0) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${f(Math.abs(v))}`)
export const shortDate = (s) => new Date(s + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })

export function riskLevel(p) {
  if (p >= 0.8) return { key: 'high', label: 'High' }
  if (p >= 0.4) return { key: 'medium', label: 'Medium' }
  return { key: 'low', label: 'Low' }
}
