// Pass-through. Cluster-scoped routes provide their own console shell
// (rail + header) via app/(dashboard)/clusters/[id]/layout.tsx. The cluster
// picker / connect screen at "/" renders full-bleed.
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}
