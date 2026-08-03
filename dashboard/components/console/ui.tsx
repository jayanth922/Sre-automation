"use client"

import { useEffect, useState, type ReactNode } from "react"
import { sparkPoints } from "@/lib/console"

/**
 * Live "updated Ns ago" label that ticks every second, so the header always
 * shows how fresh the data is. Pass the ms timestamp of the last successful load.
 */
export function useFreshness(updatedAtMs: number): string {
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const secs = Math.max(0, Math.floor((Date.now() - updatedAtMs) / 1000))
  if (secs < 2) return "updated just now"
  if (secs < 60) return `updated ${secs}s ago`
  const m = Math.floor(secs / 60)
  return `updated ${m}m ago`
}

const TONE: Record<string, string> = {
  ok: "#3f6b46",
  warn: "#946611",
  crit: "#8c2f2c",
  sel: "#294a78",
  neutral: "#9d9a8e",
}

export function Sparkline({ series, tone = "neutral", width = 96, height = 22 }: { series: number[]; tone?: string; width?: number; height?: number }) {
  return (
    <svg className="sx-sp" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ width, height }}>
      <polyline fill="none" stroke={TONE[tone] ?? TONE.neutral} strokeWidth="1.4" points={sparkPoints(series, width, height)} />
    </svg>
  )
}

export function SectionTitle({ title, meta, action }: { title: string; meta?: ReactNode; action?: ReactNode; }) {
  return (
    <div className="sx-secttl">
      <h2>{title}</h2>
      {meta !== undefined && <span className="meta">{meta}</span>}
      <div className="rule" />
      {action}
    </div>
  )
}

export function Spinner() {
  return <div className="sx-spinner" role="status" aria-label="Loading" />
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="sx-empty">{children}</div>
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="sx-empty" style={{ borderColor: "var(--crit-t)", color: "var(--crit)" }}>
      {children}
    </div>
  )
}
