const RAMPS = {
  magma: ['#000004', '#2c115f', '#721f81', '#b73779', '#f1605d', '#feb078', '#fcfdbf'],
  viridis: ['#440154', '#414487', '#2a788e', '#22a884', '#7ad151', '#fde725'],
  ndvi: ['#8c510a', '#d8b365', '#f6e8c3', '#c7e9c0', '#41ab5d', '#00441b'],
  thermal: ['#313695', '#74add1', '#e0f3f8', '#fee090', '#f46d43', '#a50026'],
  terrain: ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#d8b365', '#8c510a'],
  blues: ['#f7fbff', '#c6dbef', '#6baed6', '#2171b5', '#08306b'],
  oranges: ['#fff5eb', '#fdd0a2', '#fd8d3c', '#d94801', '#7f2704'],
  greys: ['#111111', '#555555', '#aaaaaa', '#f0f0f0'],
  mn: ['#f7f4f9', '#d4b9da', '#c994c7', '#df65b0', '#ce1256', '#67001f'],
}

export const GEOLOGY_COLORS = ['#c2185b', '#9e9e9e', '#7e57c2', '#37474f', '#e6c27a']

// How each grid layer is drawn. `fade` makes low values transparent so the basemap shows through.
export const LAYER_STYLE = {
  prospectivity: { ramp: 'magma', min: 0, max: 1, fade: 0.12 },
  confidence: { ramp: 'viridis', min: 0, max: 1 },
  est_grade: { ramp: 'oranges', fade: 0 },
  est_thickness: { ramp: 'blues' },
  est_tonnes: { ramp: 'magma', min: 0, fade: 0.02 },
  mn_index: { ramp: 'mn' },
  ndvi: { ramp: 'ndvi' },
  lst: { ramp: 'thermal' },
  sar_vv: { ramp: 'greys' },
  elevation: { ramp: 'terrain' },
  slope: { ramp: 'oranges' },
  hillshade: { ramp: 'greys', min: 0, max: 1 },
  rain_annual: { ramp: 'blues' },
  soil_moisture: { ramp: 'blues' },
  geology_unit: { categorical: GEOLOGY_COLORS },
  geochem_mn_ppm: { ramp: 'mn', log: true },
  mag_nT: { ramp: 'thermal' },
  lineament_km: { ramp: 'viridis' },
}

function hex(h) {
  const n = parseInt(h.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

export function rampColor(name, x) {
  const stops = RAMPS[name].map(hex)
  const v = Math.min(1, Math.max(0, x)) * (stops.length - 1)
  const i = Math.min(stops.length - 2, Math.floor(v))
  const f = v - i
  return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f))
}

export function rampCss(name, steps = 8) {
  return `linear-gradient(90deg, ${Array.from({ length: steps }, (_, i) => {
    const [r, g, b] = rampColor(name, i / (steps - 1))
    return `rgb(${r},${g},${b})`
  }).join(',')})`
}

/** Render a grid layer to a data URL (one pixel per cell) for a Leaflet image overlay. */
export function renderLayer(values, meta, layerMeta, name) {
  const { rows, cols } = meta
  const style = LAYER_STYLE[name] || { ramp: 'viridis' }
  const canvas = document.createElement('canvas')
  canvas.width = cols
  canvas.height = rows
  const ctx = canvas.getContext('2d')
  const img = ctx.createImageData(cols, rows)
  const tf = style.log ? (v) => Math.log10(Math.max(v, 1e-6)) : (v) => v
  const lo = tf(style.min ?? layerMeta.min)
  const hi = tf(style.max ?? layerMeta.max)
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    const o = i * 4
    if (v == null) { img.data[o + 3] = 0; continue }
    let rgb, a = 255
    if (style.categorical) {
      rgb = hex(style.categorical[Math.round(v)] || '#000000')
    } else {
      const x = (tf(v) - lo) / (hi - lo || 1)
      rgb = rampColor(style.ramp, x)
      if (style.fade != null && x < style.fade) a = 0
      else if (style.fade != null) a = Math.round(120 + 135 * Math.min(1, x))
    }
    img.data[o] = rgb[0]; img.data[o + 1] = rgb[1]; img.data[o + 2] = rgb[2]; img.data[o + 3] = a
  }
  ctx.putImageData(img, 0, 0)
  return canvas.toDataURL()
}
