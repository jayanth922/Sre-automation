"use client"

import type { ReactNode } from "react"
import { useCluster } from "@/components/console/ClusterContext"

interface ConsolePageProps {
  crumb?: ReactNode
  title: ReactNode
  live?: boolean
  updated?: string | null
  children: ReactNode
}

export function ConsolePage({ crumb, title, live, updated, children }: ConsolePageProps) {
  const cluster = useCluster()
  const resolvedCrumb = crumb !== undefined ? crumb : cluster?.name
  return (
    <>
      <div className="sx-header">
        <div className="sx-topline">
          <div>
            {resolvedCrumb !== undefined && resolvedCrumb !== null && <div className="sx-crumb">{resolvedCrumb}</div>}
            <h1>{title}</h1>
          </div>
          <div className="sx-grow" />
          {live !== undefined && (
            <div className="sx-stamp">
              <span className={`sx-live${live ? "" : " off"}`}>
                <span className="b" />
                {live ? "LIVE · streaming" : "offline"}
              </span>
              {updated && (
                <>
                  <br />
                  {updated}
                </>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="sx-body sx-view">{children}</div>
    </>
  )
}
