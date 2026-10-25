import { useRef, useState } from 'react'
import { api, download, useApi } from '../api'
import { useAuth } from '../auth'
import GlassSelect from './GlassSelect'
import { Card, ErrorBox, Loading } from './ui'

const ACCEPT = { boreholes: '.csv,.xlsx,.xls', geochem: '.csv,.xlsx,.xls', operations: '.csv,.xlsx,.xls', aeromag: '.tif,.tiff', geology: '.zip,.geojson,.json' }
const HELP = {
  operations: 'MIS export rows (production, equipment, blasting, manpower or plan, one record per row). Ingested immediately through the same validation as device data.',
  boreholes: 'Drill-hole intercepts. Replaces the simulated boreholes, so grade, thickness and tonnage become real.',
  geochem: 'Stream-sediment or soil samples (e.g. GSI NGCM). Becomes a real model input.',
  aeromag: 'Single-band magnetic anomaly grid (e.g. GSI NGPM). Replaces EMAG2 with higher-resolution survey data.',
  geology: 'Lithology polygons (e.g. GSI Bhukosh 1:50k). Replaces the coarse Macrostrat units.',
}

/** Manual uploads: validated, versioned, and applied on the next pipeline run. */
export default function Uploads({ onRerun }) {
  const { can } = useAuth()
  const u = useApi(api.uploads, [])
  const [kind, setKind] = useState('boreholes')
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [drag, setDrag] = useState(false)
  const input = useRef(null)

  if (u.error) return <ErrorBox error={u.error} retry={u.reload} />
  if (!u.data) return <Loading />
  const spec = u.data.kinds[kind]

  const send = () => {
    if (!file) return
    setBusy(true); setResult(null)
    api.uploadFile(kind, file)
      .then((r) => { setResult(r); setFile(null); u.reload() })
      .catch((e) => setResult({ status: 'rejected', message: e.message }))
      .finally(() => setBusy(false))
  }

  return (
    <Card title="Data uploads" sub="Manual uploads (drilling, maps): validated, versioned in the data lake, and applied on the next pipeline run">
      <div className="upl">
        <div className="upl-form">
          <label className="lp-l">What are you uploading?</label>
          <GlassSelect ariaLabel="Upload kind" value={kind} onChange={(k) => { setKind(k); setFile(null); setResult(null) }}
                       options={Object.entries(u.data.kinds).map(([k, v]) => ({ value: k, label: v.label, sub: v.formats }))} />
          <p className="muted small">{HELP[kind]}</p>
          {spec.required.length > 0 && (
            <p className="small">Columns: <span className="mono">{spec.required.join(', ')}</span>
              {spec.optional.length > 0 && <span className="muted"> · optional <span className="mono">{spec.optional.join(', ')}</span></span>}
              {' '}<button className="linkish" onClick={() => download(`/api/uploads/template/${kind}.csv`, `${kind}_template.csv`)}>Download template</button>
            </p>
          )}
          <div className={`dropzone ${drag ? 'drag' : ''} ${file ? 'has' : ''}`} role="button" tabIndex={0}
               onClick={() => input.current?.click()} onKeyDown={(e) => e.key === 'Enter' && input.current?.click()}
               onDragOver={(e) => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)}
               onDrop={(e) => { e.preventDefault(); setDrag(false); setFile(e.dataTransfer.files[0] || null); setResult(null) }}>
            <input ref={input} type="file" accept={ACCEPT[kind]} hidden onChange={(e) => { setFile(e.target.files[0] || null); setResult(null) }} />
            {file ? <><b>{file.name}</b><span className="muted small">{(file.size / 1024).toFixed(0)} KB · click to change</span></>
                  : <><b>Drop a file here</b><span className="muted small">or click to choose · {spec.formats}</span></>}
          </div>
          <button className="btn primary block" disabled={!file || busy} onClick={send}>{busy ? 'Validating…' : 'Upload & validate'}</button>
          {result && (
            <div className={`upl-result ${result.status}`}>
              <b>{result.status === 'accepted' ? 'Accepted' : 'Rejected'}</b> {result.message}
              {result.status === 'accepted' && (
                <div className="small">{result.rows} records stored. They take effect on the next pipeline run.
                  {can('pipeline_run') && onRerun && <> <button className="linkish" onClick={onRerun}>Re-run now</button></>}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="table-wrap glass-table upl-hist">
          <table className="tbl compact">
            <thead><tr><th>When</th><th>Kind</th><th>File</th><th>By</th><th className="r">Rows</th><th>Status</th><th /></tr></thead>
            <tbody>
              {u.data.history.length === 0 && <tr><td colSpan="7" className="muted">No uploads yet. Open data and simulated layers are in use.</td></tr>}
              {u.data.history.map((h) => (
                <tr key={h.upload_id}>
                  <td className="mono small nowrap">{h.ts.replace('T', ' ')}</td>
                  <td className="nowrap">{u.data.kinds[h.kind]?.label ?? h.kind}</td>
                  <td className="small">{h.filename}<div className="muted small">{h.message}</div></td>
                  <td className="small">{h.user}</td>
                  <td className="r mono">{h.rows}</td>
                  <td><span className={`badge ${h.status === 'accepted' ? (h.active ? 'risk-low' : 'pri-brownfield') : 'sev-high'}`}>
                    {h.status === 'accepted' ? (h.active ? 'active' : 'superseded') : 'rejected'}</span></td>
                  <td>{h.status === 'accepted' && h.active ? <button className="btn sm" onClick={() => api.deactivateUpload(h.upload_id).then(u.reload)}>Deactivate</button> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Card>
  )
}
