import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/**
 * Accessible custom select with a liquid-glass menu.
 * options: [{ value, label, sub?, group? }]. The menu is portalled to <body> so it is never
 * clipped by a card's overflow and its backdrop blur sees the real page.
 */
export default function GlassSelect({ value, onChange, options, ariaLabel, className = '', align = 'left', minMenuWidth = 0 }) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [pos, setPos] = useState(null)
  const btn = useRef(null)
  const menu = useRef(null)
  const typed = useRef({ s: '', t: 0 })
  const id = useId()
  const current = options.find((o) => o.value === value)

  const place = () => {
    const r = btn.current?.getBoundingClientRect()
    if (!r) return
    const width = Math.max(r.width, minMenuWidth)
    const left = align === 'right' ? Math.max(8, r.right - width) : Math.min(r.left, window.innerWidth - width - 8)
    const below = window.innerHeight - r.bottom
    const up = below < 240 && r.top > below
    setPos({ left, width, top: up ? undefined : r.bottom + 6, bottom: up ? window.innerHeight - r.top + 6 : undefined, maxHeight: Math.min(360, (up ? r.top : below) - 16) })
  }

  const openMenu = () => {
    place()
    setActive(Math.max(0, options.findIndex((o) => o.value === value)))
    setOpen(true)
  }
  const close = (focus = true) => { setOpen(false); if (focus) btn.current?.focus() }
  const choose = (o) => { onChange(o.value); close() }

  useLayoutEffect(() => { if (open) place() }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (!menu.current?.contains(e.target) && !btn.current?.contains(e.target)) close(false) }
    const onMove = (e) => { if (!menu.current?.contains(e.target)) place() }
    document.addEventListener('pointerdown', onDoc)
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    return () => {
      document.removeEventListener('pointerdown', onDoc)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onMove, true)
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (open) menu.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  const onKey = (e) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); openMenu() }
      return
    }
    if (e.key === 'Escape') { e.preventDefault(); close() }
    else if (e.key === 'Tab') close(false)
    else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(options.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Home') { e.preventDefault(); setActive(0) }
    else if (e.key === 'End') { e.preventDefault(); setActive(options.length - 1) }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(options[active]) }
    else if (e.key.length === 1) {
      const now = Date.now()
      typed.current = { s: (now - typed.current.t < 600 ? typed.current.s : '') + e.key.toLowerCase(), t: now }
      const i = options.findIndex((o) => String(o.label).toLowerCase().startsWith(typed.current.s))
      if (i >= 0) setActive(i)
    }
  }

  let lastGroup
  return (
    <>
      <button
        ref={btn} type="button" className={`gsel ${open ? 'open' : ''} ${className}`}
        aria-haspopup="listbox" aria-expanded={open} aria-controls={`${id}-list`} aria-label={ariaLabel}
        aria-activedescendant={open ? `${id}-o${active}` : undefined}
        onClick={() => (open ? close() : openMenu())} onKeyDown={onKey}
      >
        <span className="gsel-v">{current?.label ?? '—'}</span>
        <svg className="gsel-c" viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M5 7.5l5 5 5-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </button>
      {open && pos && createPortal(
        <ul ref={menu} id={`${id}-list`} role="listbox" aria-label={ariaLabel} className="gsel-menu"
            style={{ left: pos.left, top: pos.top, bottom: pos.bottom, minWidth: pos.width, maxHeight: pos.maxHeight }}>
          {options.map((o, i) => {
            const head = o.group && o.group !== lastGroup
            lastGroup = o.group
            return [
              head && <li key={`g-${o.group}`} className="gsel-g" role="presentation">{o.group}</li>,
              <li key={o.value} id={`${id}-o${i}`} data-i={i} role="option" aria-selected={o.value === value}
                  className={`gsel-o ${i === active ? 'act' : ''} ${o.value === value ? 'sel' : ''}`}
                  onPointerEnter={() => setActive(i)} onClick={() => choose(o)}>
                <span className="gsel-ol">{o.label}{o.sub && <small>{o.sub}</small>}</span>
                {o.value === value && <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M4.5 10.5l3.5 3.5 7.5-8" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>}
              </li>,
            ]
          })}
        </ul>,
        document.body,
      )}
    </>
  )
}
