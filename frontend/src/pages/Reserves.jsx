import { useEffect, useMemo, useState } from 'react'
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useApi } from '../api'
import GisMap, { BASEMAPS } from '../components/GisMap'
import Terrain3D from '../components/Terrain3D'
import { Card, ErrorBox, LayerLegend, Loading, ProvBadge, Segmented } from '../components/ui'
import GlassSelect from '../components/GlassSelect'
import { n1, pct } from '../format'
import { Leaderboard } from '../components/ModelRegistry'

const OVERLAYS = [
  ['zones', 'Probable reserve zones'], ['mines', 'Producing mines'], ['deposits', 'Recorded Mn sites (USGS)'],
  ['boreholes', 'Boreholes (simulated)'], ['lineaments', 'Rivers (OSM)'],
]

export default function Reserves() {
  const meta = useApi(api.gridMeta, [])
  const res = useApi(api.reserves, [])
  const [layer, setLayer] = useState('prospectivity')
  const [opacity, setOpacity] = useState(0.85)
  const [basemap, setBasemap] = useState('satellite')
  const [overlays, setOverlays] = useState({ zones: true, mines: true, deposits: false, boreholes: false, lineaments: false })
  const [tab, setTab] = useState('zones')
  const [cell, setCell] = useState(null)
  const [cellLoading, setCellLoading] = useState(false)
  const [focus, setFocus] = useState(null)
  const [panelOpen, setPanelOpen] = useState(true)
  const [view, setView] = useState('2d')
  const [drape, setDrape] = useState('rgb')
  const [exag, setExag] = useState(8)

  const groups = useMemo(() => {
    const g = {}
    meta.data?.layers.filter((l) => l.name !== 'label').forEach((l) => { (g[l.group] ??= []).push(l) })
    return g
  }, [meta.data])

  const clickCell = (ll) => {
    setTab('cell'); setCellLoading(true)
    api.cell(ll.lat, ll.lng).then(setCell).catch(() => setCell(null)).finally(() => setCellLoading(false))
  }

  if (meta.error) return <ErrorBox error={meta.error} retry={meta.reload} />
  const lm = meta.data?.layers.find((l) => l.name === layer)

  return (
    <div className="page gis-page">
      <div className="page-h">
        <div>
          <h1>Reserve Explorer</h1>
          <p className="muted">Satellite + geology + geophysics → ML prospectivity → probable reserve zones with confidence. Click anywhere to drill down.</p>
        </div>
        <Segmented value={view} onChange={setView} options={[{ value: '2d', label: '2D map' }, { value: '3d', label: '3D terrain' }]} />
      </div>
      <div className="gis-layout">
        <div className="gis-main">
          {view === '2d'
            ? <GisMap height="100%" layer={layer} opacity={opacity} basemap={basemap} overlays={overlays}
                      onCellClick={clickCell} selected={cell && { lat: cell.lat, lon: cell.lon }} focus={focus} />
            : meta.data && <Terrain3D gridMeta={meta.data} layer={layer} opacity={opacity} drape={drape} overlays={overlays} exaggeration={exag}
                                      onPick={clickCell} selected={cell && { lat: cell.lat, lon: cell.lon }} focus={focus} />}
          <div className={`layer-panel ${panelOpen ? '' : 'closed'}`}>
            <button className="lp-toggle" onClick={() => setPanelOpen(!panelOpen)}>{panelOpen ? '‹ Layers' : 'Layers ›'}</button>
            {panelOpen && meta.data && (
              <div className="lp-body">
                {view === '2d' ? <>
                  <label className="lp-l">Basemap</label>
                  <GlassSelect ariaLabel="Basemap" value={basemap} onChange={setBasemap}
                               options={Object.entries(BASEMAPS).map(([k, b]) => ({ value: k, label: b.label }))} />
                </> : <>
                  <label className="lp-l">Terrain drape</label>
                  <GlassSelect ariaLabel="Terrain drape" value={drape} onChange={setDrape} minMenuWidth={230}
                               options={[{ value: 'rgb', label: 'Sentinel-2 true colour', sub: 'REAL · B4/B3/B2 dry-season median' },
                                 { value: 'swir', label: 'SWIR geology composite', sub: 'REAL · B12/B8A/B4: oxides & clays' },
                                 { value: 'ndvi', label: 'Vegetation (NDVI)', sub: 'REAL · Sentinel-2' },
                                 { value: 'plain', label: 'Plain relief', sub: 'shaded DEM only' }]} />
                  <label className="lp-l">Vertical exaggeration ×{exag}</label>
                  <input type="range" min="1" max="20" step="1" value={exag} onChange={(e) => setExag(+e.target.value)} />
                </>}
                <label className="lp-l">Data layer</label>
                <GlassSelect ariaLabel="Data layer" value={layer ?? ''} onChange={(v) => setLayer(v || null)} minMenuWidth={250}
                             options={[{ value: '', label: 'None' },
                               ...Object.entries(groups).flatMap(([g, ls]) => ls.map((l) => ({
                                 value: l.name, label: l.desc, group: g,
                                 sub: l.real == null ? 'model output' : `${l.real ? 'REAL' : 'SIMULATED'} · ${l.source}`,
                               })))]} />
                <label className="lp-l">Opacity {Math.round(opacity * 100)}%</label>
                <input type="range" min="0.1" max="1" step="0.05" value={opacity} onChange={(e) => setOpacity(+e.target.value)} />
                <label className="lp-l">Overlays</label>
                {OVERLAYS.map(([k, label]) => (
                  <label key={k} className="chk">
                    <input type="checkbox" checked={overlays[k]} onChange={(e) => setOverlays({ ...overlays, [k]: e.target.checked })} /> {label}
                  </label>
                ))}
                <LayerLegend name={layer} meta={lm} geologyLegend={meta.data.geology_legend} />
              </div>
            )}
          </div>
        </div>

        <aside className="gis-side card">
          <div className="tabs">
            <Segmented value={tab} onChange={setTab} options={[{ value: 'zones', label: 'Zones' }, { value: 'cell', label: 'Drill-down' }, { value: 'model', label: 'Model' }]} />
          </div>
          <div className="gis-side-b">
            {tab === 'zones' && <Zones res={res} onPick={(z) => { setFocus({ lat: z.centroid_lat, lon: z.centroid_lon, zoom: 11.5 }); clickCell({ lat: z.centroid_lat, lng: z.centroid_lon }) }} />}
            {tab === 'cell' && (cellLoading ? <Loading label="Explaining cell" /> : cell ? <CellPanel cell={cell} meta={meta.data} /> :
              <p className="muted">Click a location on the map to see the model's estimate, the features driving it, a cross-section and the nearest boreholes.</p>)}
            {tab === 'model' && (res.data ? <ModelCard m={res.data.model} /> : <Loading />)}
          </div>
        </aside>
      </div>
    </div>
  )
}

function Zones({ res, onPick }) {
  if (res.error) return <ErrorBox error={res.error} />
  if (!res.data) return <Loading />
  const zones = res.data.zones
  const count = (s) => zones.filter((z) => z.status === s).length
  return (
    <>
      <p className="muted small"><b>{count('new')}</b> new probable zones (no recorded manganese site within 3 km),{' '}
        <b>{count('occurrence')}</b> independently confirming a recorded USGS occurrence, and <b>{count('mine')}</b> brownfield
        extensions around mines. Grade and tonnage use simulated boreholes, so they're indicative only.</p>
      <ul className="zlist">
        {zones.map((z) => (
          <li key={z.zone_id} onClick={() => onPick(z)} className={z.status} data-pri={z.priority.toLowerCase()}
              tabIndex={0} role="button" aria-label={`Show ${z.zone_id} on the map`}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onPick(z))}>
            <div className="z-h">
              <b>{z.zone_id}</b>
              <span className={`badge pri-${z.priority.toLowerCase()}`}>{z.priority}</span>
            </div>
            <div className="muted small">near {z.nearest_known} · {z.area_km2} km² · {z.unfc}</div>
            <div className="z-stats">
              <span><em>p̄</em>{z.mean_prob.toFixed(2)}</span>
              <span><em>conf</em>{z.confidence.toFixed(2)}</span>
              <span><em>grade</em>{z.est_grade_pct}%</span>
              <span><em>~Mt</em>{z.est_tonnes_mt}</span>
            </div>
          </li>
        ))}
      </ul>
    </>
  )
}

function CellPanel({ cell, meta }) {
  const v = cell.values
  const top = cell.explanation.contributions.slice(0, 8)
  const maxAbs = Math.max(...top.map((c) => Math.abs(c.contribution)), 0.01)
  const [section, setSection] = useState(null)
  useEffect(() => {
    Promise.all([api.layer('elevation'), api.layer('prospectivity')]).then(([e, p]) => {
      const row = Math.floor((meta.bounds.lat_max - cell.lat) / meta.res)
      const out = []
      for (let c = 0; c < meta.cols; c++) {
        const i = row * meta.cols + c
        out.push({ lon: +(meta.bounds.lon_min + (c + 0.5) * meta.res).toFixed(3), elev: e.values[i], p: p.values[i] })
      }
      setSection(out)
    })
  }, [cell, meta])
  return (
    <div className="cell gstack">
      <div className="gloc mono">
        <span className="gloc-dot" />{cell.lat.toFixed(3)}°N, {cell.lon.toFixed(3)}°E<span className="muted"> · cell #{cell.cell_id}</span>
      </div>
      <div className="cell-kpis">
        <div><span>Prospectivity</span><b>{pct(v.prospectivity)}</b></div>
        <div><span>Confidence</span><b>{pct(v.confidence)}</b></div>
        <div><span>Est. grade</span><b>{n1(v.est_grade)}% Mn</b></div>
        <div><span>Est. thickness</span><b>{n1(v.est_thickness)} m</b></div>
      </div>
      <section className="gpane">
      <h4>Why the model thinks so</h4>
      <p className="muted small">Change in probability if each feature were at the area median. Positive values push this cell toward "ore".</p>
      <div className="contrib">
        {top.map((c) => (
          <div key={c.key} className="contrib-r">
            <span className="contrib-n">{c.feature}</span>
            <span className="contrib-bar">
              <span className={c.contribution >= 0 ? 'pos' : 'neg'} style={{ width: `${(Math.abs(c.contribution) / maxAbs) * 50}%`, [c.contribution >= 0 ? 'left' : 'right']: '50%' }} />
            </span>
            <span className="mono small">{c.contribution >= 0 ? '+' : ''}{c.contribution.toFixed(2)}</span>
          </div>
        ))}
      </div>
      </section>
      <section className="gpane">
      <h4>E–W cross-section through this cell</h4>
      {section ? (
        <ResponsiveContainer width="100%" height={150}>
          <ComposedChart data={section} margin={{ left: -10, right: 0, top: 4 }}>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="lon" tick={{ fontSize: 10, fill: 'var(--muted)' }} minTickGap={40} />
            <YAxis yAxisId="e" tick={{ fontSize: 10, fill: 'var(--muted)' }} width={40} domain={['auto', 'auto']} tickFormatter={(x) => Math.round(x)} />
            <YAxis yAxisId="p" orientation="right" domain={[0, 1]} hide />
            <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8 }} formatter={(x, n) => [n === 'Elevation' ? `${x.toFixed(0)} m` : x.toFixed(2), n]} />
            <Area yAxisId="p" dataKey="p" name="Prospectivity" fill="#b73779" fillOpacity={0.35} stroke="#b73779" isAnimationActive={false} />
            <Line yAxisId="e" dataKey="elev" name="Elevation" stroke="var(--ink)" dot={false} strokeWidth={1.4} isAnimationActive={false} />
            <ReferenceLine yAxisId="e" x={+cell.lon.toFixed(3)} stroke="#00acc1" />
          </ComposedChart>
        </ResponsiveContainer>
      ) : <Loading />}
      </section>
      <section className="gpane flush">
      <h4>Layer values</h4>
      <table className="tbl compact kv">
        <tbody>
          {[['mn_index', 'Mn index', 3], ['ndvi', 'NDVI', 2], ['lst', 'LST °C', 1], ['elevation', 'Elevation m', 0], ['slope', 'Slope °', 1],
            ['geochem_mn_ppm', 'Stream Mn ppm', 0], ['mag_nT', 'Magnetics nT', 1], ['lineament_km', 'To fault km', 1], ['rain_annual', 'Rain mm/yr', 0]].map(([k, l, d]) => (
            <tr key={k}><td>{l}</td><td className="r mono">{v[k]?.toFixed(d)}</td></tr>
          ))}
          <tr><td>Geology</td><td className="r">{meta.geology_legend[Math.round(v.geology_unit)]}</td></tr>
        </tbody>
      </table>
      </section>
      <section className="gpane flush">
      <h4>Nearest boreholes</h4>
      <table className="tbl compact">
        <thead><tr><th>Hole</th><th className="r">km</th><th className="r">Thick.</th><th className="r">Grade</th></tr></thead>
        <tbody>
          {cell.nearest_boreholes.map((h) => (
            <tr key={h.hole_id}><td className="mono">{h.hole_id}</td><td className="r">{h.d_km.toFixed(1)}</td>
              <td className="r">{h.thickness_m > 0 ? `${h.thickness_m} m` : 'barren'}</td><td className="r">{h.grade_mn_pct}%</td></tr>
          ))}
        </tbody>
      </table>
      </section>
    </div>
  )
}

export function ModelCard({ m }) {
  const max = m.importance[0].value
  const ta = m.tonnage_assumptions
  return (
    <div className="modelcard gstack">
      <section className="gpane">
        <h4>{m.name}</h4>
        <p className="muted small">{m.algorithm}. {m.validation}.</p>
        {m.inputs && (
          <div className="mini-stats" style={{ marginTop: 6 }}>
            <span><em>inputs</em>{m.inputs}</span>
            {m.sites && <span><em>training mines</em>{m.sites.mines}</span>}
            {m.sites && <span><em>held-out occurrences</em>{m.sites.occurrences}</span>}
          </div>
        )}
        {m.label_source && <p className="muted small">Labels: {m.label_source}.</p>}
      </section>
      {m.leaderboard?.length > 0 && (
        <section className="gpane">
          <h4>Candidate models</h4>
          <p className="muted small">Trained on the same real inputs and labels, scored on the same two held-out tests. The best generalisation score serves the map; feature importance and cell explanations come from the {m.explanation_model?.split(' (')[0] ?? 'random forest'}.</p>
          <Leaderboard task="reserve" rows={m.leaderboard} />
        </section>
      )}
      <div className="cell-kpis">
        <div><span>ROC-AUC (spatial CV)</span><b>{m.roc_auc}</b></div>
        <div><span>PR-AUC</span><b>{m.pr_auc}</b></div>
        <div><span>Labelled +/−</span><b>{m.labels.positive}/{m.labels.negative}</b></div>
        <div><span>Unlabelled cells</span><b>{m.labels.unlabelled.toLocaleString('en-IN')}</b></div>
      </div>
      <section className="gpane">
        <h4>Feature importance</h4>
        {m.real_share != null && (
          <div className="real-share">
            <div className="rs-bar"><span style={{ width: `${m.real_share * 100}%` }} /></div>
            <p className="small"><b>{(m.real_share * 100).toFixed(0)}%</b> of the model's decision weight comes from <b>real</b> data
              (satellite, radar, terrain, magnetics, mapped geology).
              {m.real_share < 0.999 && ' The rest is simulated geology, geochemistry and geophysics until real sources replace them.'}</p>
          </div>
        )}
        {m.importance.slice(0, 12).map((f) => (
          <div key={f.key} className={`imp ${f.real ? 'is-real' : f.real === false ? 'is-sim' : ''}`}>
            <span>{f.feature}</span>
            <span className="imp-b"><span style={{ width: `${(f.value / max) * 100}%` }} /></span>
            <span className="mono small imp-v">{(f.value * 100).toFixed(1)}%</span>
          </div>
        ))}
        {m.real_share != null && <div className="legend-src"><span className="imp-key real" /> real input <span className="imp-key sim" /> simulated input</div>}
      </section>
      <section className="gpane">
        <h4>Sub-surface estimation</h4>
        <div className="mini-stats">
          <span><em>grade R²</em>{m.subsurface.grade_mn_pct.r2}</span>
          <span><em>MAE</em>{m.subsurface.grade_mn_pct.mae}% Mn</span>
          <span><em>thick. R²</em>{m.subsurface.thickness_m.r2}</span>
          <span><em>MAE</em>{m.subsurface.thickness_m.mae} m</span>
          <span><em>n</em>{m.subsurface.grade_mn_pct.n}</span>
        </div>
        <p className="muted small">{m.subsurface_algorithm}. Modest by design: surface data only weakly constrains depth, which is why new zones need drilling.</p>
        {m.subsurface_data && <p className="small"><ProvBadge real={false} /> {m.subsurface_data}. Grade, thickness and tonnage are indicative only.</p>}
      </section>
      {m.occurrence_check?.auc != null && (
        <section className="gpane">
          <h4>Held-out occurrence test</h4>
          <div className="mini-stats">
            <span><em>AUC</em>{m.occurrence_check.auc}</span>
            <span><em>occurrences in a zone</em>{(m.occurrence_check.hit_rate * 100).toFixed(0)}%</span>
            <span><em>background flagged</em>{(m.occurrence_check.background_flag_rate * 100).toFixed(1)}%</span>
          </div>
          <p className="muted small">{m.occurrence_check.n_occurrences} real USGS MRDS manganese occurrences were never shown to the model. How well it scores them, by distance from the nearest training mine:</p>
          <table className="tbl compact kv">
            <thead><tr><th>Distance</th><th className="r">n</th><th className="r">AUC</th><th className="r">In a zone</th></tr></thead>
            <tbody>{m.occurrence_check.by_distance?.map((b) => (
              <tr key={b.band}><td>{b.band}</td><td className="r mono">{b.n}</td><td className="r mono">{b.auc}</td><td className="r mono">{(b.hit_rate * 100).toFixed(0)}%</td></tr>
            ))}</tbody>
          </table>
          <p className="muted small">Far from training mines the ranking still holds (high AUC) but scores fall under the zone threshold, so treat distant zones as leads to verify, not discoveries.</p>
        </section>
      )}
      {m.hidden_horizon_check?.auc_far_from_known && (
        <section className="gpane">
          <h4>Unexplored-horizon check</h4>
          <div className="mini-stats"><span><em>AUC</em>{m.hidden_horizon_check.auc_far_from_known.toFixed(3)}</span></div>
          <p className="muted small">On cells &gt;6 km from any known deposit, scored against the simulator's planted ore horizons, so the model generalises beyond the mines it was trained on.</p>
        </section>
      )}
      <section className="gpane">
        <h4>Tonnage assumptions</h4>
        <div className="mini-stats">
          <span><em>density</em>{ta.density_t_m3} t/m³</span>
          <span><em>down-dip</em>{ta.depth_extent_m} m</span>
          <span><em>continuity</em>{ta.continuity}</span>
          <span><em>strike</em>{ta.cell_strike_m} m/cell</span>
          <span><em>threshold</em>p ≥ {m.threshold}</span>
        </div>
      </section>
    </div>
  )
}
