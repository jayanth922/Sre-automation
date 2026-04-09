"use client"

import { useEffect } from "react"
import { useParams, useRouter } from "next/navigation"

export default function ClusterRedirectPage() {
    const router = useRouter()
    const params = useParams()
    const clusterId = params.id as string

    useEffect(() => {
        router.replace(`/clusters/${clusterId}/incidents`)
    }, [clusterId, router])

    return null
}
