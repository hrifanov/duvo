"""Redis client factory + queue primitives.

Queue is a Redis list at `jobs:pending`. Producers LPUSH JSON-encoded jobs,
consumers BRPOP with a timeout so they block without polling.

Connection config comes from REDIS_HOST / REDIS_PORT env vars (default
localhost:6379). `decode_responses=True` so callers get `str`, not `bytes`.
"""

import json
import os

import redis

QUEUE_KEY = "jobs:pending"


def get_client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )


def enqueue(client: redis.Redis, job: dict) -> None:
    client.lpush(QUEUE_KEY, json.dumps(job))


def dequeue(client: redis.Redis, timeout: int = 5) -> dict | None:
    result = client.brpop(QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    _, payload = result
    return json.loads(payload)
