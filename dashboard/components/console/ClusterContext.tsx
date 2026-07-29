"use client"

import { createContext, useContext } from "react"
import type { Cluster } from "@/lib/console"

export const ClusterContext = createContext<Cluster | null>(null)

export function useCluster(): Cluster | null {
  return useContext(ClusterContext)
}
