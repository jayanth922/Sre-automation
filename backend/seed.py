import asyncio
import os
import sys

# Add parent directory to path to import backend modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import AsyncSessionLocal
from backend.crud import create_user, get_user_by_email, create_cluster, get_clusters_for_org
from backend.schemas import UserCreate, ClusterCreate

async def seed_default_user():
    async with AsyncSessionLocal() as db:
        email = os.getenv("SEED_ADMIN_EMAIL", "admin@example.com")
        password = os.getenv("SEED_ADMIN_PASSWORD", "admin")
        org_name = os.getenv("SEED_ADMIN_ORG", "SRE Admin Org")

        user = await get_user_by_email(db, email)
        if not user:
            print(f"Creating default user: {email}")
            new_user = UserCreate(email=email, password=password, role="admin", org_name=org_name)
            user = await create_user(db, new_user)
            print("User created successfully.")
        else:
            print(f"User {email} already exists.")

        # Seed Default Cluster if requested
        seed_cluster_token = os.getenv("SEED_CLUSTER_TOKEN")
        seed_cluster_name = os.getenv("SEED_CLUSTER_NAME", "SRE Demo Cluster")
        seed_cluster_status = os.getenv("SEED_CLUSTER_STATUS", "offline")
        
        if seed_cluster_token:
            clusters = await get_clusters_for_org(db, user.org_id)
            existing_cluster = next((c for c in clusters if c.token == seed_cluster_token), None)
            
            if not existing_cluster:
                print(f"Seeding cluster: {seed_cluster_name}")
                from backend.models import Cluster
                from datetime import datetime, timezone
                db_cluster = Cluster(
                    name=seed_cluster_name,
                    org_id=user.org_id,
                    token=seed_cluster_token,
                    status=seed_cluster_status,
                    last_heartbeat=datetime.now(timezone.utc) if seed_cluster_status == "online" else None
                )
                db.add(db_cluster)
                await db.commit()
                print(f"Cluster {seed_cluster_name} seeded successfully as {seed_cluster_status}.")
            else:
                print(f"Cluster with token {seed_cluster_token[:8]}... already exists. Updating status to {seed_cluster_status}.")
                existing_cluster.status = seed_cluster_status
                if seed_cluster_status == "online":
                    from datetime import datetime, timezone
                    existing_cluster.last_heartbeat = datetime.now(timezone.utc)
                await db.commit()

if __name__ == "__main__":
    asyncio.run(seed_default_user())
