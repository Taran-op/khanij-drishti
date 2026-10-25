import { useEffect, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { api } from '../api'
import { renderLayer, LAYER_STYLE } from '../colormaps'
import { pct } from '../format'

export const BASEMAPS = {
  satellite: {
    label: 'Satellite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
  },
  topo: {
    label: 'Topographic',
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    attribution: '© OpenStreetMap contributors, SRTM | © OpenTopoMap',
    maxZoom: 16,
  },
  streets: {
    label: 'Streets',
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '© OpenStreetMap contributors',
  },
  none: { label: 'None (offline)' },
}

const vectorCache = {}
const getVector = (name) => (vectorCache[name] ??= api.vector(name).catch((e) => { delete vectorCache[name]; throw e }))
let metaPromise
const getMeta = () => (metaPromise ??= api.gridMeta().catch((e) => { metaPromise = null; throw e }))

const RISK_COLOR = (p) => (p == null ? '#3b82f6' : p >= 0.8 ? '#dc2626' : p >= 0.4 ? '#f59e0b' : '#16a34a')

export default function GisMap({
  layer = 'prospectivity', opacity = 0.85, basemap = 'satellite',
  overlays = { zones: true, mines: true }, mineRisk, onCellClick, onZoneClick, selected, focus,
  height = 520, interactive = true,
}) {
  const el = useRef(null)
  const map = useRef(null)
  const refs = useRef({ groups: {} })
  const [meta, setMeta] = useState(null)
  const [err, setErr] = useState(null)
  const cb = useRef({})
  cb.current = { onCellClick, onZoneClick }

  // create map once
  useEffect(() => {
    const m = L.map(el.current, {
      zoomControl: interactive, attributionControl: true, scrollWheelZoom: interactive,
      dragging: interactive, doubleClickZoom: interactive, preferCanvas: true, zoomSnap: 0.25,
    })
    map.current = m
    L.control.scale({ imperial: false }).addTo(m)
    m.on('click', (e) => cb.current.onCellClick?.(e.latlng))
    // Keep the AOI framed while the container settles, until the user takes over.
    let userMoved = false, aoi = null
    const fit = () => aoi && !userMoved && m.getSize().y > 0 && m.fitBounds(aoi, { padding: [8, 8], animate: false })
    el.current.addEventListener('pointerdown', () => { userMoved = true })
    el.current.addEventListener('wheel', () => { userMoved = true }, { passive: true })
    getMeta().then((md) => {
      if (map.current !== m) return
      setMeta(md)
      const b = md.bounds
      aoi = [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]]
      fit()
      L.rectangle([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], {
        color: '#ffffff', weight: 1, dashArray: '4 4', fill: false, interactive: false,
      }).addTo(m)
    }).catch(setErr)
    const ro = new ResizeObserver(() => { m.invalidateSize(); fit() })
    ro.observe(el.current)
    return () => {
      ro.disconnect(); m.remove()
      map.current = null
      refs.current = { groups: {} } // layers belonged to the removed map
    }
  }, [interactive])

  // basemap
  useEffect(() => {
    const m = map.current
    if (!m) return
    refs.current.base?.remove()
    const b = BASEMAPS[basemap]
    if (b?.url) {
      refs.current.base = L.tileLayer(b.url, { attribution: b.attribution, maxZoom: b.maxZoom || 18 }).addTo(m)
      refs.current.base.bringToBack()
    } else refs.current.base = null
    el.current.style.background = basemap === 'none' ? '#1b2433' : '#0d1420'
  }, [basemap])

  // raster layer
  useEffect(() => {
    if (!meta) return
    let cancelled = false
    const m = map.current
    if (!layer) { refs.current.raster?.remove(); refs.current.raster = null; return }
    api.layer(layer).then(({ values }) => {
      if (cancelled || map.current !== m) return
      const lm = meta.layers.find((x) => x.name === layer) || {}
      const url = renderLayer(values, meta, lm, layer)
      const b = meta.bounds
      refs.current.raster?.remove()
      const pixel = LAYER_STYLE[layer]?.categorical || layer === 'label'
      refs.current.raster = L.imageOverlay(url, [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], {
        opacity, className: `kd-raster${pixel ? ' pixel' : ''}`, interactive: false,
      }).addTo(m)
    }).catch(setErr)
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layer, meta])

  useEffect(() => { refs.current.raster?.setOpacity(opacity) }, [opacity])

  // vector overlays
  useEffect(() => {
    const m = map.current
    if (!m) return
    const g = refs.current.groups
    const want = {
      lineaments: () => getVector('lineaments').then((fc) => L.geoJSON(fc, {
        style: (f) => f.properties.kind === 'river'
          ? { color: '#4fc3f7', weight: 2, opacity: 0.8 }
          : { color: '#ff7043', weight: 1.5, dashArray: '6 4', opacity: 0.9 },
        onEachFeature: (f, l) => l.bindTooltip(f.properties.kind === 'river' ? `${f.properties.id} river` : `Fault ${f.properties.id}`, { sticky: true }),
      })),
      zones: () => getVector('reserve_zones').then((fc) => L.geoJSON(fc, {
        style: (f) => ({
          new: { color: '#ffd54f', weight: 2.5, dashArray: '5 4', fillColor: '#ffd54f', fillOpacity: 0.08 },
          occurrence: { color: '#4dd0e1', weight: 2.2, dashArray: '2 4', fillColor: '#4dd0e1', fillOpacity: 0.06 },
        }[f.properties.status] || { color: '#ffffff', weight: 1.2, fillOpacity: 0.03, opacity: 0.85 }),
        onEachFeature: (f, l) => {
          const p = f.properties
          const what = { new: 'New probable zone', occurrence: 'Confirms a recorded occurrence' }[p.status] || 'Around a known mine'
          l.bindTooltip(`<b>${p.zone_id}</b> · ${what}<br>near ${p.nearest_known}<br>
            p̄ ${p.mean_prob.toFixed(2)} · conf ${p.confidence.toFixed(2)}`, { sticky: true })
          l.on('click', (e) => { L.DomEvent.stopPropagation(e); cb.current.onZoneClick?.(p) ; cb.current.onCellClick?.(e.latlng) })
        },
      })),
      deposits: () => getVector('known_deposits').then((fc) => L.geoJSON(fc, {
        // filled = mine (training label), hollow cyan = recorded occurrence (held out of training)
        pointToLayer: (f, ll) => (f.properties.kind === 'occurrence'
          ? L.circleMarker(ll, { radius: 4, color: '#4dd0e1', weight: 2, fillOpacity: 0 })
          : L.circleMarker(ll, { radius: 4.5, color: '#fff', weight: 1.5, fillColor: '#ff8a65', fillOpacity: 0.95 }))
          .bindTooltip(`<b>${f.properties.name}</b><br>${f.properties.source || 'known deposit'}`),
      })),
      boreholes: () => getVector('boreholes').then((fc) => L.geoJSON(fc, {
        pointToLayer: (f, ll) => {
          const hit = f.properties.thickness_m > 0
          return L.circleMarker(ll, { radius: 2.6, color: hit ? '#00e676' : '#b0bec5', weight: 1, fillOpacity: 0.9 })
            .bindTooltip(`${f.properties.hole_id}: ${hit ? `${f.properties.thickness_m} m @ ${f.properties.grade_mn_pct}% Mn` : 'barren'}`)
        },
      })),
      mines: () => getVector('mines').then((fc) => L.geoJSON(fc, {
        pointToLayer: (f, ll) => {
          const p = mineRisk?.[f.properties.id]
          return L.marker(ll, {
            icon: L.divIcon({
              className: 'kd-mine',
              html: `<span class="pin" style="background:${RISK_COLOR(p)}"></span><span class="lbl">${f.properties.name}</span>`,
              iconSize: [12, 12], iconAnchor: [6, 6],
            }),
          }).bindTooltip(`<b>${f.properties.name}</b> (${f.properties.method})<br>${f.properties.district}, ${f.properties.state}` +
            (p != null ? `<br>Month-target miss risk: <b>${pct(p)}</b>` : ''))
        },
      })),
    }
    // mine pins are coloured by risk, so rebuild them when the risk map changes
    if (refs.current.riskUsed !== mineRisk && g.mines) { g.mines.layer?.remove(); delete g.mines }
    refs.current.riskUsed = mineRisk
    Object.entries(want).forEach(([k, make]) => {
      const on = !!overlays[k]
      if (on && !g[k]) {
        const slot = (g[k] = {})
        make().then((layerObj) => {
          if (refs.current.groups[k] === slot && map.current === m) slot.layer = layerObj.addTo(m)
        }).catch(setErr)
      } else if (!on && g[k]) {
        g[k].layer?.remove()
        delete g[k]
      }
    })
  }, [overlays, mineRisk])

  // selected cell outline
  useEffect(() => {
    refs.current.sel?.remove()
    if (!selected || !meta || !map.current) return
    const h = meta.res / 2
    refs.current.sel = L.rectangle([[selected.lat - h, selected.lon - h], [selected.lat + h, selected.lon + h]], {
      color: '#00e5ff', weight: 2, fill: false, interactive: false,
    }).addTo(map.current)
  }, [selected, meta])

  useEffect(() => {
    if (focus && map.current) map.current.flyTo([focus.lat, focus.lon], focus.zoom || 12, { duration: 0.8 })
  }, [focus])

  return (
    <div className="gis-wrap" style={{ height }}>
      <div ref={el} className="gis-map" />
      {err && <div className="gis-err">Map data unavailable: {String(err.message || err)}</div>}
    </div>
  )
}
