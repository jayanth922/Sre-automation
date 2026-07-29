"use client"

import type { ReactNode } from "react"
import { sparkPoints } from "@/lib/console"

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
