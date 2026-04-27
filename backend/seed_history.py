"""
Seed 90 days of realistic historical incidents for the demo cluster.
Run once after the platform is up:
    uv run python backend/seed_history.py
"""
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import AsyncSessionLocal
from backend.models import Incident, IncidentSeverity, IncidentStatus, Cluster

ALERT_TEMPLATES = [
    ("[api-gateway] HighErrorRate",        IncidentSeverity.CRITICAL, "HTTP 5xx error rate exceeded 5% on api-gateway"),
    ("[checkout-service] HighLatency",     IncidentSeverity.HIGH,     "p99 latency > 2s on checkout-service"),
    ("[inventory-service] PodCrashLoop",   IncidentSeverity.CRITICAL, "inventory-service pod restarting repeatedly"),
    ("[api-gateway] HighRequestVolume",    IncidentSeverity.MEDIUM,   "Request rate spike detected on api-gateway"),
    ("[checkout-service] MemoryPressure",  IncidentSeverity.HIGH,     "Memory usage > 90% on checkout-service"),
    ("[inventory-service] HighErrorRate",  IncidentSeverity.HIGH,     "Error rate spike on inventory-service"),
    ("[api-gateway] SlowQueries",          IncidentSeverity.MEDIUM,   "Database query latency elevated"),
    ("[checkout-service] PodCrashLoop",    IncidentSeverity.CRITICAL, "checkout-service OOMKilled, restarting"),
    ("[inventory-service] CPUThrottling",  IncidentSeverity.LOW,      "CPU throttling detected on inventory pods"),
    ("[api-gateway] CertExpiringSoon",     IncidentSeverity.LOW,      "TLS certificate expires in 7 days"),
]

RESOLUTIONS = [
    "Root cause identified as a bad deploy. Rolled back to previous version. Error rate returned to baseline.",
    "Memory leak in connection pool. Restarted pods and applied patch. Monitoring stable.",
    "Traffic spike from load test. Rate limiting applied. Service recovered.",
    "Database connection pool exhausted. Pool size increased. Latency normalized.",
    "Misconfigured liveness probe caused unnecessary restarts. Probe thresholds adjusted.",
    "OOMKill due to uncapped memory limits. Resource limits increased and HPA configured.",
    "Network policy blocking internal traffic. Policy updated. Connectivity restored.",
    "Disk I/O saturation on node. Workloads redistributed to healthy nodes.",
]


async def seed_history():
    async with AsyncSessionLocal() as db:
        # Find the demo cluster
        from sqlalchemy import select
        result = await db.execute(select(Cluster).limit(1))
        cluster = result.scalar_one_or_none()
        if not cluster:
            print("No cluster found. Run the platform first and seed the default user.")
            return

        print(f"Seeding historical incidents for cluster: {cluster.name} ({cluster.id})")

        now = datetime.now(timezone.utc)
        added = 0

        for days_ago in range(90, 0, -1):
            # Vary incident frequency: more on weekdays, occasional bursts
            base_date = now - timedelta(days=days_ago)
            weekday = base_date.weekday()
            # 0-4 = Mon-Fri (higher chance), 5-6 = weekend (lower chance)
            incident_chance = 0.65 if weekday < 5 else 0.25
            # Occasional burst days (simulate a bad deploy week)
            if days_ago in (7, 14, 30, 45, 60):
                incident_chance = 0.95

            if random.random() > incident_chance:
                continue

            # Pick 1-3 incidents on active days
            n = random.randint(1, 3) if incident_chance > 0.8 else 1
            for _ in range(n):
                title, severity, description = random.choice(ALERT_TEMPLATES)

                # Random hour in business-ish hours (more incidents during peak)
                hour = random.choices(
                    range(24),
                    weights=[1,1,1,1,1,1,2,3,4,5,5,5,5,5,5,5,4,4,3,3,2,2,1,1],
                    k=1
                )[0]
                created_at = base_date.replace(hour=hour, minute=random.randint(0, 59), second=0, microsecond=0)

                # MTTR: 5 min to 4 hours
                resolve_minutes = random.randint(5, 240)
                resolved_at = created_at + timedelta(minutes=resolve_minutes)
                summary = random.choice(RESOLUTIONS)

                incident = Incident(
                    cluster_id=cluster.id,
                    title=title,
                    description=description,
                    severity=severity,
                    status=IncidentStatus.RESOLVED,
                    summary=summary,
                    created_at=created_at,
                    resolved_at=resolved_at,
                )
                db.add(incident)
                added += 1

        await db.commit()
        print(f"Done. Inserted {added} historical incidents over 90 days.")


if __name__ == "__main__":
    asyncio.run(seed_history())
