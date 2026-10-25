import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js'
import { api } from '../api'
import { renderLayer } from '../colormaps'

/*
 * 3D GIS viewer. Real Copernicus DEM (0.002°) extruded with vertical exaggeration; drape = real
 * Sentinel-2 composite (true colour / SWIR geology / NDVI) with the selected model or data layer
 * blended on top, zone outlines and rivers painted into the drape; mines and recorded sites as 3D
 * pins. Click the surface to drill down exactly as on the 2D map.
 */
const DRAPE_W = 1500
const ZONE_STYLE = { new: '#ffd54f', occurrence: '#4dd0e1' }

const loadImage = (url) => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url })
const cache = {}
const once = (k, f) => (cache[k] ??= f().catch((e) => { delete cache[k]; throw e }))

export default function Terrain3D({ gridMeta, layer, opacity, drape, overlays, exaggeration, onPick, selected, focus }) {
  const host = useRef(null)
  const S = useRef({}) // three.js objects live outside React state
  const [status, setStatus] = useState('Loading terrain…')
  const [info, setInfo] = useState(null)
  const [hover, setHover] = useState(null)
  const [autoRotate, setAutoRotate] = useState(false)
  const cb = useRef({})
  cb.current = { onPick }

  // ---- scene setup (once) ----
  useEffect(() => {
    let disposed = false
    const el = host.current
    const s = S.current
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: false })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    el.appendChild(renderer.domElement)
    const labels = new CSS2DRenderer()
    labels.domElement.className = 't3d-labels'
    el.appendChild(labels.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(42, 1, 0.5, 2000)
    camera.up.set(0, 0, 1)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.maxPolarAngle = Math.PI * 0.47
    controls.minDistance = 8
    controls.maxDistance = 420
    controls.autoRotateSpeed = 0.6
    scene.add(new THREE.HemisphereLight(0xdfe9ff, 0x2a2016, 0.9))
    const sun = new THREE.DirectionalLight(0xffffff, 2.3)
    sun.position.set(-120, 140, 160) // NW sun: the usual cartographic hillshade direction
    scene.add(sun)
    Object.assign(s, { renderer, labels, scene, camera, controls, pins: new THREE.Group(), pick: null })
    scene.add(s.pins)

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight
      renderer.setSize(w, h)
      labels.setSize(w, h)
      camera.aspect = w / Math.max(h, 1)
      camera.updateProjectionMatrix()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(el)
    resize()

    let raf
    const loop = () => { controls.update(); renderer.render(scene, camera); labels.render(scene, camera); raf = requestAnimationFrame(loop) }
    loop()

    // picking: a click (not a drag) on the surface → lat/lon
    const ray = new THREE.Raycaster()
    const ndc = (e) => {
      const r = renderer.domElement.getBoundingClientRect()
      return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1)
    }
    const hit = (e) => {
      if (!s.mesh) return null
      ray.setFromCamera(ndc(e), camera)
      const h = ray.intersectObject(s.mesh)[0]
      if (!h?.uv) return null
      const b = s.meta.bounds
      return { lat: b.lat_min + h.uv.y * (b.lat_max - b.lat_min), lng: b.lon_min + h.uv.x * (b.lon_max - b.lon_min), elev: s.elevAt(h.uv.x, h.uv.y) }
    }
    let down = null
    const onDown = (e) => { down = [e.clientX, e.clientY] }
    const onUp = (e) => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5) {
        const p = hit(e)
        if (p) cb.current.onPick?.(p)
      }
      down = null
    }
    let last = 0
    const onMove = (e) => {
      const now = performance.now()
      if (now - last < 60) return
      last = now
      const p = hit(e)
      setHover(p)
    }
    renderer.domElement.addEventListener('pointerdown', onDown)
    renderer.domElement.addEventListener('pointerup', onUp)
    renderer.domElement.addEventListener('pointermove', onMove)
    renderer.domElement.addEventListener('pointerleave', () => setHover(null))

    // terrain mesh from the real DEM
    Promise.all([once('tmeta', () => api.terrainMeta()), once('telev', () => api.terrainElevation())]).then(([meta, buf]) => {
      if (disposed) return
      const elev = new Int16Array(buf)
      const W = meta.width, H = meta.height
      const geo = new THREE.PlaneGeometry(meta.km.x, meta.km.y, W - 1, H - 1)
      const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.92, metalness: 0, side: THREE.DoubleSide })
      const mesh = new THREE.Mesh(geo, mat)
      scene.add(mesh)
      // skirt/base plate for depth cue
      const base = new THREE.Mesh(new THREE.PlaneGeometry(meta.km.x * 1.04, meta.km.y * 1.06),
        new THREE.MeshBasicMaterial({ color: 0x0b1424, transparent: true, opacity: 0.55 }))
      base.position.z = -0.4
      scene.add(base)
      s.elevAt = (u, v) => {
        const c = Math.min(W - 1, Math.max(0, Math.round(u * (W - 1))))
        const r = Math.min(H - 1, Math.max(0, Math.round((1 - v) * (H - 1))))
        return elev[r * W + c]
      }
      Object.assign(s, { mesh, geo, mat, elev, meta, W, H })
      setInfo(meta)
      s.applyExag?.()
      s.redraw?.()
      s.placePins?.()
      // initial camera: looking north-east over the belt
      camera.position.set(-40, -125, 85)
      controls.target.set(0, 0, 0)
      controls.update()
      setStatus(null)
    }).catch((e) => setStatus(`Terrain unavailable: ${e.message}`))

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      s.geo?.dispose(); s.mat?.dispose(); s.tex?.dispose()
      renderer.dispose()
      el.innerHTML = ''
      S.current = {}
    }
  }, [])

  // ---- vertical exaggeration ----
  useEffect(() => {
    const s = S.current
    s.exag = exaggeration
    s.applyExag = () => {
      if (!s.geo) return
      const pos = s.geo.attributes.position
      const lo = s.meta.elev_min
      for (let i = 0; i < pos.count; i++) pos.setZ(i, ((s.elev[i] - lo) / 1000) * s.exag)
      pos.needsUpdate = true
      s.geo.computeVertexNormals()
      s.placePins?.()
    }
    s.applyExag()
  }, [exaggeration])

  // ---- drape texture: base imagery + data layer + vector outlines ----
  useEffect(() => {
    const s = S.current
    let cancelled = false
    s.redraw = async () => {
      if (!s.meta || !gridMeta) return
      const b = s.meta.bounds
      const CW = DRAPE_W, CH = Math.round(DRAPE_W * s.meta.full[1] / s.meta.full[0])
      const cv = document.createElement('canvas')
      cv.width = CW; cv.height = CH
      const ctx = cv.getContext('2d')
      const X = (lon) => ((lon - b.lon_min) / (b.lon_max - b.lon_min)) * CW
      const Y = (lat) => ((b.lat_max - lat) / (b.lat_max - b.lat_min)) * CH
      try {
        if (drape === 'plain') {
          ctx.fillStyle = '#b9b4a6'; ctx.fillRect(0, 0, CW, CH)
        } else {
          const url = await once(`tex-${drape}`, () => api.terrainTexture(drape))
          const img = await loadImage(url)
          ctx.drawImage(img, 0, 0, CW, CH)
        }
        if (layer) {
          const lm = gridMeta.layers.find((l) => l.name === layer)
          const vals = await api.layer(layer)
          const img = await loadImage(renderLayer(vals.values, gridMeta, lm, layer))
          ctx.globalAlpha = opacity
          ctx.imageSmoothingEnabled = true
          ctx.drawImage(img, 0, 0, CW, CH)
          ctx.globalAlpha = 1
        }
        const poly = (coords) => { coords.forEach(([lon, lat], i) => (i ? ctx.lineTo(X(lon), Y(lat)) : ctx.moveTo(X(lon), Y(lat)))) }
        if (overlays.lineaments) {
          const fc = await once('v-lineaments', () => api.vector('lineaments'))
          ctx.lineWidth = 2.2; ctx.strokeStyle = 'rgba(79,195,247,.85)'
          fc.features.forEach((f) => {
            const g = f.geometry
            const lines = g.type === 'LineString' ? [g.coordinates] : g.type === 'MultiLineString' ? g.coordinates : []
            lines.forEach((ln) => { ctx.beginPath(); poly(ln); ctx.stroke() })
          })
        }
        if (overlays.zones) {
          const fc = await once('v-zones', () => api.vector('reserve_zones'))
          fc.features.forEach((f) => {
            const c = ZONE_STYLE[f.properties.status] || '#ffffff'
            ctx.beginPath(); poly(f.geometry.coordinates[0]); ctx.closePath()
            ctx.fillStyle = c + '22'; ctx.fill()
            ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.stroke()
            ctx.lineWidth = 2; ctx.strokeStyle = c; ctx.stroke()
          })
        }
      } catch (e) {
        setStatus(`Drape failed: ${e.message}`)
        return
      }
      if (cancelled || !s.mat) return
      const tex = new THREE.CanvasTexture(cv)
      tex.colorSpace = THREE.SRGBColorSpace
      tex.anisotropy = s.renderer.capabilities.getMaxAnisotropy()
      s.tex?.dispose()
      s.tex = tex
      s.mat.map = tex
      s.mat.needsUpdate = true
    }
    s.redraw()
    return () => { cancelled = true }
  }, [drape, layer, opacity, overlays.zones, overlays.lineaments, gridMeta])

  // ---- pins: mines, recorded sites, zone labels ----
  useEffect(() => {
    const s = S.current
    let cancelled = false
    s.placePins = async () => {
      if (!s.meta) return
      const g = s.pins
      g.children.slice().forEach((c) => { g.remove(c); c.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); if (o.element) o.element.remove() }) })
      const b = s.meta.bounds
      const toXY = (lon, lat) => [((lon - b.lon_min) / (b.lon_max - b.lon_min) - 0.5) * s.meta.km.x, ((lat - b.lat_min) / (b.lat_max - b.lat_min) - 0.5) * s.meta.km.y]
      const zAt = (lon, lat) => ((s.elevAt((lon - b.lon_min) / (b.lon_max - b.lon_min), (lat - b.lat_min) / (b.lat_max - b.lat_min)) - s.meta.elev_min) / 1000) * s.exag
      const pin = (lon, lat, color, height, text, cls) => {
        const [x, y] = toXY(lon, lat)
        const z = zAt(lon, lat)
        const grp = new THREE.Group()
        const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.12, height, 6), new THREE.MeshBasicMaterial({ color }))
        stem.rotation.x = Math.PI / 2
        stem.position.set(x, y, z + height / 2)
        const head = new THREE.Mesh(new THREE.SphereGeometry(0.75, 14, 10), new THREE.MeshBasicMaterial({ color }))
        head.position.set(x, y, z + height)
        grp.add(stem, head)
        if (text) {
          const d = document.createElement('div')
          d.className = `t3d-label ${cls || ''}`
          d.textContent = text
          const lab = new CSS2DObject(d)
          lab.position.set(x, y, z + height + 1.4)
          grp.add(lab)
        }
        g.add(grp)
      }
      try {
        if (overlays.mines) {
          const fc = await once('v-mines', () => api.vector('mines'))
          if (cancelled) return
          fc.features.forEach((f) => { const [lon, lat] = f.geometry.coordinates; pin(lon, lat, 0xa3e635, 6, f.properties.name.split(' (')[0], 'mine') })
        }
        if (overlays.deposits) {
          const fc = await once('v-deposits', () => api.vector('known_deposits'))
          if (cancelled) return
          fc.features.forEach((f) => { const [lon, lat] = f.geometry.coordinates; pin(lon, lat, f.properties.kind === 'occurrence' ? 0x4dd0e1 : 0xff8a65, 3, null) })
        }
        if (overlays.zones) {
          const fc = await once('v-zones', () => api.vector('reserve_zones'))
          if (cancelled) return
          fc.features.forEach((f) => {
            const p = f.properties
            const [x, y] = toXY(p.centroid_lon, p.centroid_lat)
            const d = document.createElement('div')
            d.className = 't3d-label zone'
            d.textContent = p.zone_id
            const lab = new CSS2DObject(d)
            lab.position.set(x, y, zAt(p.centroid_lon, p.centroid_lat) + 2.2)
            g.add(lab)
          })
        }
        if (selected) pin(selected.lon, selected.lat, 0x00e5ff, 4.5, null)
      } catch (e) {
        setStatus(`Overlay failed: ${e.message}`)
      }
    }
    s.placePins()
    return () => { cancelled = true }
  }, [overlays.mines, overlays.deposits, overlays.zones, selected?.lat, selected?.lon])

  // ---- fly to a focus point (zone picked in the side panel) ----
  useEffect(() => {
    const s = S.current
    if (!focus || !s.meta) return
    const b = s.meta.bounds
    const x = ((focus.lon - b.lon_min) / (b.lon_max - b.lon_min) - 0.5) * s.meta.km.x
    const y = ((focus.lat - b.lat_min) / (b.lat_max - b.lat_min) - 0.5) * s.meta.km.y
    const z = ((s.elevAt((focus.lon - b.lon_min) / (b.lon_max - b.lon_min), (focus.lat - b.lat_min) / (b.lat_max - b.lat_min)) - s.meta.elev_min) / 1000) * s.exag
    const from = s.controls.target.clone(), to = new THREE.Vector3(x, y, z)
    const camFrom = s.camera.position.clone(), camTo = new THREE.Vector3(x - 14, y - 34, z + 26)
    let t = 0
    const step = () => {
      t = Math.min(1, t + 0.04)
      const k = t * t * (3 - 2 * t)
      s.controls.target.lerpVectors(from, to, k)
      s.camera.position.lerpVectors(camFrom, camTo, k)
      if (t < 1) requestAnimationFrame(step)
    }
    step()
  }, [focus?.lat, focus?.lon])

  useEffect(() => { if (S.current.controls) S.current.controls.autoRotate = autoRotate }, [autoRotate])

  const reset = () => {
    const s = S.current
    s.camera.position.set(-40, -125, 85)
    s.controls.target.set(0, 0, 0)
  }

  return (
    <div className="t3d">
      <div ref={host} className="t3d-host" />
      {status && <div className="t3d-status">{status}</div>}
      <div className="t3d-tools">
        <button className="btn sm" onClick={reset}>Reset view</button>
        <button className={`btn sm ${autoRotate ? 'primary' : ''}`} onClick={() => setAutoRotate(!autoRotate)}>{autoRotate ? 'Stop orbit' : 'Orbit'}</button>
      </div>
      <div className="t3d-foot small">
        {hover ? <span className="mono">{hover.lat.toFixed(4)}°N {hover.lng.toFixed(4)}°E · {hover.elev} m</span> : <span className="muted">Drag to orbit · right-drag to pan · scroll to zoom · click to drill down</span>}
        {info && <span className="muted"> · <b className="prov-real">REAL</b> {info.source.dem} + Sentinel-2 L2A{info.source.scenes ? ` (${info.source.scenes} scenes` : ''}{info.source.dates ? `, ${info.source.dates})` : info.source.scenes ? ')' : ''} · relief {Math.round(info.elev_min)}–{Math.round(info.elev_max)} m × {exaggeration}</span>}
      </div>
    </div>
  )
}
