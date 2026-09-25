#!/usr/bin/env python3
"""
Redis State Store for SRE Agent

Provides persistent state storage using Redis with TTL support.
Ensures state survives server restarts and supports distributed deployments.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

try:
    import redis
    from redis.exceptions import ConnectionError, RedisError
except ImportError:
    redis = None
    RedisError = Exception
    ConnectionError = Exception

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class RedisStateStore:
    """Redis-based state storage with TTL support."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        default_ttl: int = 3600,  # 1 hour default TTL
    ):
        """
        Initialize Redis state store.

        Args:
            redis_url: Redis connection URL (defaults to REDIS_URL env var or localhost)
            default_ttl: Default TTL in seconds for stored keys
        """
        self.default_ttl = default_ttl
        self.redis_client = None

        if redis is None:
            logger.warning("⚠️ redis package not installed. State will not persist.")
            logger.warning("⚠️ Install with: pip install redis")
            return

        # Get Redis URL from env or parameter
        if not redis_url:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

        try:
            # Parse Redis URL
            self.redis_client = redis.from_url(
                redis_url,
                decode_responses=True,  # Automatically decode responses to strings
                socket_connect_timeout=5,
                socket_timeout=5,
            )

            # Test connection
            self.redis_client.ping()
            logger.info(f"✅ Connected to Redis at {redis_url}")

        except ConnectionError as e:
            logger.error(f"❌ Failed to connect to Redis: {e}")
            logger.error("⚠️ State storage will not work. Check REDIS_URL environment variable.")
            self.redis_client = None
        except Exception as e:
            logger.error(f"❌ Redis initialization error: {e}")
            self.redis_client = None

    def is_available(self) -> bool:
        """Check if Redis is available."""
        if self.redis_client is None:
            return False
        try:
            self.redis_client.ping()
            return True
        except Exception:
            return False

    def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl: Optional[int] = None,
    ) -> bool:
        """
        Store a value in Redis with optional TTL.

        Args:
            key: Storage key (typically session_id)
            value: Dictionary to store
            ttl: Time to live in seconds (uses default_ttl if not provided)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            logger.warning(f"⚠️ Redis not available, cannot store key: {key}")
            return False

        try:
            # Serialize value to JSON
            serialized = json.dumps(value, default=str)

            # Use provided TTL or default
            ttl_seconds = ttl if ttl is not None else self.default_ttl

            # Store with TTL
            self.redis_client.setex(
                f"sre_agent:approval:{key}",
                ttl_seconds,
                serialized,
            )

            logger.info(f"✅ Stored state for key: {key} (TTL: {ttl_seconds}s)")
            return True

        except RedisError as e:
            logger.error(f"❌ Redis error storing key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Error storing key {key}: {e}")
            return False

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a value from Redis.

        Args:
            key: Storage key (typically session_id)

        Returns:
            Stored dictionary or None if not found
        """
        if not self.is_available():
            logger.warning(f"⚠️ Redis not available, cannot retrieve key: {key}")
            return None

        try:
            serialized = self.redis_client.get(f"sre_agent:approval:{key}")

            if serialized is None:
                logger.info(f"ℹ️ Key not found: {key}")
                return None

            # Deserialize from JSON
            value = json.loads(serialized)
            logger.info(f"✅ Retrieved state for key: {key}")
            return value

        except RedisError as e:
            logger.error(f"❌ Redis error retrieving key {key}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON decode error for key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Error retrieving key {key}: {e}")
            return None

    def delete(self, key: str) -> bool:
        """
        Delete a value from Redis.

        Args:
            key: Storage key (typically session_id)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            logger.warning(f"⚠️ Redis not available, cannot delete key: {key}")
            return False

        try:
            deleted = self.redis_client.delete(f"sre_agent:approval:{key}")
            if deleted > 0:
                logger.info(f"✅ Deleted state for key: {key}")
                return True
            else:
                logger.info(f"ℹ️ Key not found for deletion: {key}")
                return False

        except RedisError as e:
            logger.error(f"❌ Redis error deleting key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Error deleting key {key}: {e}")
            return False

    def exists(self, key: str) -> bool:
        """
        Check if a key exists in Redis.

        Args:
            key: Storage key (typically session_id)

        Returns:
            True if key exists, False otherwise
        """
        if not self.is_available():
            return False

        try:
            return self.redis_client.exists(f"sre_agent:approval:{key}") > 0
        except Exception:
            return False

    def get_ttl(self, key: str) -> Optional[int]:
        """
        Get remaining TTL for a key.

        Args:
            key: Storage key (typically session_id)

        Returns:
            Remaining TTL in seconds, or None if key doesn't exist or has no TTL
        """
        if not self.is_available():
            return None

        try:
            ttl = self.redis_client.ttl(f"sre_agent:approval:{key}")
            return ttl if ttl >= 0 else None
        except Exception:
            return None

    def append_log(self, key: str, message: str) -> bool:
        """
        Append a log message to the session execution log (Atomic RPUSH).
        """
        if not self.is_available():
            return False
            
        try:
            redis_key = f"sre_agent:logs:{key}"
            self.redis_client.rpush(redis_key, message)
            # Set TTL if new key (prevents eternal logs)
            if self.redis_client.llen(redis_key) == 1:
                self.redis_client.expire(redis_key, self.default_ttl)
            return True
        except Exception as e:
            logger.error(f"❌ Error appending log for {key}: {e}")
            return False

    def get_logs(self, key: str) -> list[str]:
        """
        Get all logs for a session.
        """
        if not self.is_available():
            return []
            
        try:
            redis_key = f"sre_agent:logs:{key}"
            return self.redis_client.lrange(redis_key, 0, -1)
        except Exception as e:
            logger.error(f"❌ Error getting logs for {key}: {e}")
            return []

    # ----------------------------------------------------------------------
    # Cluster Lock (Break Glass)
    # ----------------------------------------------------------------------

    def set_cluster_lock(self, cluster_id: str, locked: bool) -> bool:
        """Set or unset the emergency lock for a cluster."""
        if not self.is_available():
            return False
        try:
            key = f"CLUSTER_LOCK:{cluster_id}"
            if locked:
                self.redis_client.set(key, "LOCKED")
            else:
                self.redis_client.delete(key)
            return True
        except Exception as e:
            logger.error(f"❌ Error setting lock for {cluster_id}: {e}")
            return False

    def is_cluster_locked(self, cluster_id: str) -> bool:
        """Check if cluster is in emergency lock mode."""
        if not self.is_available():
            return False
        try:
            key = f"CLUSTER_LOCK:{cluster_id}"
            return self.redis_client.exists(key) > 0
        except Exception as e:
            logger.error(f"❌ Error checking lock for {cluster_id}: {e}")
            return False

    def set_topology_cache(self, cluster_id: str, adjacency: Dict[str, Any], ttl: int) -> bool:
        """Cache a cluster's inferred service-adjacency map for ``ttl`` seconds."""
        if not cluster_id or ttl <= 0 or not self.is_available():
            return False
        try:
            self.redis_client.setex(
                f"sre_agent:topology:{cluster_id}",
                ttl,
                json.dumps(adjacency, default=str),
            )
            return True
        except Exception as e:
            logger.error(f"❌ Error caching topology for cluster {cluster_id}: {e}")
            return False

    def get_topology_cache(self, cluster_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cluster's cached service-adjacency map, if present."""
        if not cluster_id or not self.is_available():
            return None
        try:
            serialized = self.redis_client.get(f"sre_agent:topology:{cluster_id}")
            if serialized is None:
                return None
            return json.loads(serialized)
        except Exception as e:
            logger.error(f"❌ Error reading cached topology for cluster {cluster_id}: {e}")
            return None

    def set_idempotency(self, key: str, ttl: int) -> bool:
        """Atomically claim a mutation key for ``ttl`` seconds.

        Redis ``SET ... NX EX`` is deliberately used as one operation: separate
        existence and write calls would leave a race in the live-mutation path.
        ``False`` means either the key was already claimed or Redis was not
        available; callers that authorize writes must fail closed in both cases.
        """
        if not key or ttl <= 0 or not self.is_available():
            return False
        try:
            claimed = self.redis_client.set(
                f"sre_agent:idempotency:{key}",
                "CLAIMED",
                nx=True,
                ex=ttl,
            )
            return bool(claimed)
        except Exception as e:
            logger.error(f"❌ Error claiming idempotency key {key}: {e}")
            return False


# Global instance (initialized on import)
_state_store: Optional[RedisStateStore] = None


def get_state_store() -> RedisStateStore:
    """Get or create the global Redis state store instance."""
    global _state_store
    if _state_store is None:
        _state_store = RedisStateStore()
    return _state_store
