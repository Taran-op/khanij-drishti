import { riskLevel, pct } from '../format'
import { rampCss, LAYER_STYLE, GEOLOGY_COLORS } from '../colormaps'

export function Card({ title, sub, right, children, className = '', pad = true }) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="card-h">
          <div>
            {title && <h3>{title}</h3>}
            {sub && <p className="muted small">{sub}</p>}
          </div>
          {right}
        </header>
      )}
      <div className={pad ? 'card-b' : ''}>{children}</div>
    </section>
  )
}

export function Kpi({ label, value, sub, tone, icon }) {
  return (
    <div className={`kpi ${tone ? `tone-${tone}` : ''}`}>
      <div className="kpi-l">{icon && <span className="kpi-i">{icon}</span>}{label}</div>
      <div className="kpi-v">{value}</div>
      {sub && <div className="kpi-s">{sub}</div>}
    </div>
  )
}

export function RiskBadge({ p, showPct = true }) {
  const r = riskLevel(p)
  return <span className={`badge risk-${r.key}`}>{r.label}{showPct && ` · ${pct(p)}`}</span>
}

export function Sev({ s }) {
  return <span className={`badge sev-${s}`}>{s}</span>
}

export function Loading({ label = 'Loading' }) {
  return <div className="loading"><span className="spinner" />{label}…</div>
}

export function ErrorBox({ error, retry }) {
  if (!error) return null
  return (
    <div className="errorbox">
      <b>Couldn't reach the API.</b> {String(error.message || error)}
      <div className="muted small">Is the backend running on port 8000? (<code>uvicorn kd.api:app --port 8000</code>)</div>
      {retry && <button className="btn sm" onClick={retry}>Retry</button>}
    </div>
  )
}

export function Bar({ value, max = 1, tone = 'blue', label }) {
  return (
    <div className="bar" title={label}>
      <span className={`bar-f tone-bg-${tone}`} style={{ width: `${Math.max(0, Math.min(100, (value / max) * 100))}%` }} />
    </div>
  )
}

/** REAL / SIMULATED / model-output tag for a layer or model input. */
export function ProvBadge({ real }) {
  if (real == null) return <span className="badge prov prov-model">model</span>
  return <span className={`badge prov ${real ? 'prov-real' : 'prov-sim'}`}>{real ? 'real' : 'simulated'}</span>
}

function LegendSource({ meta }) {
  return (
    <div className="legend-src">
      <ProvBadge real={meta.real} />
      <span className="muted">{meta.real == null ? 'Derived by the ML pipeline' : meta.source}{meta.date_range ? ` · ${meta.date_range}` : ''}</span>
    </div>
  )
}

export function LayerLegend({ name, meta, geologyLegend }) {
  if (!name || !meta) return null
  const style = LAYER_STYLE[name] || { ramp: 'viridis' }
  if (style.categorical) {
    return (
      <div className="legend">
        <div className="legend-t">{meta.desc}</div>
        {Object.entries(geologyLegend || {}).map(([k, v]) => (
          <div key={k} className="legend-row"><span className="sw" style={{ background: GEOLOGY_COLORS[k] }} />{v}</div>
        ))}
        <LegendSource meta={meta} />
      </div>
    )
  }
  const lo = style.min ?? meta.min
  const hi = style.max ?? meta.max
  const fmt = (v) => (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2))
  return (
    <div className="legend">
      <div className="legend-t">{meta.desc} {meta.unit && <span className="muted">({meta.unit})</span>}</div>
      <div className="ramp" style={{ background: rampCss(style.ramp) }} />
      <div className="ramp-l"><span>{fmt(lo)}</span><span>{fmt(hi)}</span></div>
      <LegendSource meta={meta} />
    </div>
  )
}

export function Segmented({ value, options, onChange }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.value} className={value === o.value ? 'on' : ''} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  )
}
