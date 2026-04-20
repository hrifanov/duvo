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
